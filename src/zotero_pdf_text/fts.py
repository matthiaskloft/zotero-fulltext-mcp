from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal

from ._atomic import replace_with_retry


DEFAULT_CHUNK_CHARS = 6000
DEFAULT_OVERLAP_CHARS = 500
MAX_QUERY_CHARS = 1_000
MAX_QUERY_TERMS = 20
MAX_QUERY_TERM_CHARS = 64
MAX_SEARCH_RESULTS = 100
SEARCH_CANDIDATE_MULTIPLIER = 5
MIN_SEARCH_CANDIDATES = 50
MAX_SEARCH_CANDIDATES = 500
SearchMode = Literal["all_terms", "any_terms", "phrase"]
SEARCH_MODES = frozenset({"all_terms", "any_terms", "phrase"})
# Starting guess for how many characters consecutive stored chunks advance by. An index is not
# required to have been built with the default chunk_chars/overlap_chars, so this is only an
# initial estimate for the first fetch in _fetch_covering_chunks below, never assumed correct.
_CHUNK_ADVANCE_CHARS_ESTIMATE = max(DEFAULT_CHUNK_CHARS - DEFAULT_OVERLAP_CHARS, 1)
_MAX_CHUNK_FETCH_ITERATIONS = 6
DEFAULT_CONTEXT_RECORD_LIMIT = 50


@dataclass(frozen=True)
class FtsBuildSummary:
    database: str
    source_jsonl: str
    records: int
    chunks: int
    total_chars: int
    total_words: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    zotero_parent_key: str
    zotero_attachment_key: str
    title: str
    creators: str
    year: str
    doi: str
    citation_key: str
    snippet: str
    score: float
    chunk_index: int
    start_char: int
    end_char: int
    markdown_sha256: str
    chunk_sha256: str
    matched_fields: list[str]
    source_path: str
    markdown_path: str
    extraction_tool: str
    classification: str
    identity_status: str
    identity_rule: str
    has_math: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FullTextResult:
    zotero_parent_key: str
    zotero_attachment_key: str
    title: str
    creators: str
    year: str
    doi: str
    citation_key: str
    chunk_index: int | None
    start_char: int
    end_char: int
    total_chars: int
    markdown_sha256: str
    chunk_sha256: str | None
    chunk_count: int
    previous_chunk_index: int | None
    next_chunk_index: int | None
    has_more: bool | None
    stored_chunk_char_start: int | None
    stored_chunk_char_end: int | None
    truncated: bool
    text: str
    source_path: str
    markdown_path: str
    extraction_tool: str
    classification: str
    identity_status: str
    identity_rule: str
    has_math: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ChunkNotFoundError(LookupError):
    """Raised when an attachment exists but an exact chunk index does not."""


class StaleLocatorError(LookupError):
    """Raised when a caller's source locator names content the index no longer holds.

    A locator carries the ``markdown_sha256`` its character offsets were measured against. If the
    attachment has since been reconverted -- by a math-OCR pass, an image-OCR enrichment, or a
    plain reconversion -- those offsets now address different text, and the chunk at that index is
    no longer the passage the caller cited. The attachment still exists, so this is neither
    ``KeyError`` nor ``ChunkNotFoundError``: it is a specific, recoverable staleness that the
    caller repairs by searching again.
    """

    def __init__(self, message: str, *, expected: str, actual: str) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class DuplicateAttachmentKeyError(ValueError):
    """Raised when an index build encounters the same zotero_attachment_key twice.

    Duplicate keys would make get_fulltext return an arbitrary row for that attachment, so the
    build refuses instead of publishing an ambiguous index.
    """


def chunk_sha256(text: str) -> str:
    """Hash one stored chunk's text, deriving what a document-level hash cannot express.

    Not stored as a column. `chunks.text` is already on disk, so this is computable by any reader
    from an index built by any version -- adding a column would instead force every existing index
    to be rebuilt before a caller could use chunk-level verification at all. Search computes it
    only for the rows it actually returns, inside the query that already reads their stored text.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IndexSchemaUnsupportedError(RuntimeError):
    """Raised when a SQLite file is not (or is no longer) a supported full-text index."""


_REQUIRED_TABLES = frozenset({"metadata", "chunks", "chunks_fts"})
_REQUIRED_METADATA_COLUMNS = frozenset(
    {
        "record_id",
        "zotero_parent_key",
        "zotero_attachment_key",
        "title",
        "creators",
        "year",
        "doi",
        "citation_key",
        "source_path",
        "markdown_path",
        "markdown_sha256",
        "extraction_tool",
        "char_count",
        "word_count",
        "page_count",
        "classification",
        "identity_status",
        "identity_rule",
        "has_math",
    }
)


def build_fts_index(
    index_jsonl: Path,
    output: Path,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> FtsBuildSummary:
    if not index_jsonl.exists():
        raise FileNotFoundError(index_jsonl)
    _validate_chunking(chunk_chars, overlap_chars)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Build into a same-directory temp file and only replace `output` via an atomic os.replace
    # once the build is complete and passes an integrity check. A crash or interruption mid-build
    # then leaves the previous `output` (if any) untouched and still queryable, rather than
    # destroyed by the old unlink-then-build-in-place approach.
    tmp_path = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    tmp_path.unlink(missing_ok=True)
    try:
        con = sqlite3.connect(tmp_path)
        try:
            _create_schema(con)
            records = chunks = total_chars = total_words = 0
            seen_attachment_keys: set[str] = set()
            with index_jsonl.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    attachment_key = str(record.get("zotero_attachment_key") or "")
                    if attachment_key:
                        if attachment_key in seen_attachment_keys:
                            raise DuplicateAttachmentKeyError(
                                f"Attachment key {attachment_key} appears more than once in "
                                f"{index_jsonl}; a duplicate key would make full-text retrieval "
                                "return an arbitrary row. Run 'zotero-pdf-text "
                                "find-duplicate-attachments' to locate the duplicates, then "
                                "remove the redundant JSONL record before rebuilding."
                            )
                        seen_attachment_keys.add(attachment_key)
                    records += 1
                    total_chars += int(record.get("char_count") or 0)
                    total_words += int(record.get("word_count") or 0)
                    record_id = _insert_metadata(con, record)
                    for chunk in _chunk_text(record.get("text", ""), chunk_chars, overlap_chars):
                        chunks += 1
                        _insert_chunk(con, record_id, record, chunk)
            con.commit()
        finally:
            con.close()

        _check_integrity(tmp_path)
        replace_with_retry(tmp_path, output)
    finally:
        # Suppress cleanup failures here so they never shadow a real exception from the build or
        # integrity check above (missing_ok=True already handles the common case where the
        # temp file no longer exists because the replace above succeeded).
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)

    return FtsBuildSummary(
        database=str(output),
        source_jsonl=str(index_jsonl),
        records=records,
        chunks=chunks,
        total_chars=total_chars,
        total_words=total_words,
    )


def _check_integrity(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("PRAGMA integrity_check").fetchall()
    finally:
        con.close()
    if not rows or rows[0][0] != "ok":
        details = "; ".join(row[0] for row in rows) if rows else "no result"
        raise RuntimeError(f"FTS build failed integrity check: {details}")


def search_fts(
    db_path: Path,
    query: str,
    *,
    limit: int = 10,
    search_mode: SearchMode = "all_terms",
) -> list[SearchResult]:
    terms = _validate_search_request(query, limit, search_mode)
    match_query = _match_query(terms, search_mode)
    candidate_limit = min(MAX_SEARCH_CANDIDATES, max(limit * SEARCH_CANDIDATE_MULTIPLIER, MIN_SEARCH_CANDIDATES))
    con = connect_readonly(db_path)
    con.row_factory = sqlite3.Row
    try:
        # record_rank dedup requires ranking the full matched-row set before LIMIT applies (a
        # window function can't use SQLite's top-N/ORDER BY LIMIT shortcut), so a common query
        # term can force a full scan of matching chunk rows. Acceptable for this tool's
        # single-user/personal-index scale; revisit if the index grows much larger.
        #
        # This ranking pass only needs a bounded, cheap signal for "did the text field match"
        # (the char(2)/char(3) marker technique below, scoped to a 32-token snippet), not the
        # full stored chunk text. The full highlight()-vs-original comparison used to detect
        # matched_fields precisely is deferred to a second query scoped to just the rows that
        # survive ranking and LIMIT, so it never runs against the full candidate set.
        rows = con.execute(
            """
            WITH matches AS (
                SELECT
                    m.record_id,
                    m.zotero_parent_key,
                    m.zotero_attachment_key,
                    m.title,
                    m.creators,
                    m.year,
                    m.doi,
                    m.citation_key,
                    snippet(chunks_fts, 2, '[', ']', ' ... ', 32) AS snippet,
                    -- Body-match detection only, using control characters that cannot appear in
                    -- indexed document text (unlike '[' or ']', which are common in citations and
                    -- math notation and would otherwise falsely signal a body-text match).
                    snippet(chunks_fts, 2, char(2), char(3), '', 32) AS body_match_marker,
                    bm25(chunks_fts, 8.0, 1.0, 1.0, 6.0) AS score,
                    c.chunk_id,
                    c.chunk_index,
                    c.start_char,
                    c.end_char,
                    m.markdown_sha256,
                    m.source_path,
                    m.markdown_path,
                    m.extraction_tool,
                    m.classification,
                    m.identity_status,
                    m.identity_rule,
                    m.has_math
                FROM chunks_fts f
                JOIN chunks c ON c.chunk_id = f.chunk_id
                JOIN metadata m ON m.record_id = f.record_id
                WHERE chunks_fts MATCH ?
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY record_id
                    ORDER BY CASE WHEN instr(body_match_marker, char(2)) > 0 THEN 0 ELSE 1 END, score ASC, chunk_index ASC
                ) AS record_rank
                FROM matches
            )
            SELECT * FROM ranked
            WHERE record_rank = 1
            ORDER BY score ASC, zotero_attachment_key ASC, chunk_index ASC
            LIMIT ?
            """,
            (match_query, candidate_limit),
        ).fetchall()
        selected_rows = rows[:limit]
        chunk_facts_by_chunk_id = _chunk_facts_for_chunks(
            con, match_query, [row["chunk_id"] for row in selected_rows]
        )
    finally:
        con.close()
    results: list[SearchResult] = []
    for row in selected_rows:
        row_dict = dict(row)
        row_dict.pop("record_id")
        row_dict.pop("record_rank")
        row_dict.pop("body_match_marker")
        facts = chunk_facts_by_chunk_id[row_dict.pop("chunk_id")]
        row_dict["matched_fields"] = facts.matched_fields
        row_dict["chunk_sha256"] = facts.text_sha256
        row_dict["has_math"] = bool(row_dict["has_math"])
        results.append(SearchResult(**row_dict))
    return results


@dataclass(frozen=True)
class _ChunkFacts:
    matched_fields: list[str]
    text_sha256: str


def _chunk_facts_for_chunks(
    con: sqlite3.Connection, match_query: str, chunk_ids: list[int]
) -> dict[int, _ChunkFacts]:
    """Compute precise matched_fields and a chunk content hash for already-selected chunks.

    Comparing each highlighted value with its source detects FTS-inserted markers without
    mistaking marker-like control characters already present in scholarly text for a match. This
    requires the full stored text of each field, so it is scoped to the caller's final result
    rows (bounded by the search `limit`) rather than the full ranking candidate set.

    The chunk hash is computed here rather than in a query of its own precisely because this one
    already reads `c.text` for exactly these rows: hashing rides along for free, and the ranking
    pass upstream keeps avoiding stored text entirely.
    """
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = con.execute(
        f"""
        SELECT
            c.chunk_id,
            highlight(chunks_fts, 0, char(2), char(3)) AS title_highlighted,
            highlight(chunks_fts, 1, char(2), char(3)) AS creators_highlighted,
            highlight(chunks_fts, 2, char(2), char(3)) AS text_highlighted,
            highlight(chunks_fts, 3, char(2), char(3)) AS citation_key_highlighted,
            m.title,
            m.creators,
            c.text AS stored_text,
            m.citation_key
        FROM chunks_fts f
        JOIN chunks c ON c.chunk_id = f.chunk_id
        JOIN metadata m ON m.record_id = f.record_id
        WHERE chunks_fts MATCH ? AND c.chunk_id IN ({placeholders})
        """,
        (match_query, *chunk_ids),
    ).fetchall()
    result: dict[int, _ChunkFacts] = {}
    for row in rows:
        row_dict = dict(row)
        result[row_dict["chunk_id"]] = _ChunkFacts(
            matched_fields=[
                field
                for field, original_field in (
                    ("title", "title"),
                    ("creators", "creators"),
                    ("text", "stored_text"),
                    ("citation_key", "citation_key"),
                )
                if row_dict[f"{field}_highlighted"] != row_dict[original_field]
            ],
            text_sha256=chunk_sha256(row_dict["stored_text"]),
        )
    return result


def check_locator_freshness(
    expected_content_sha256: str | None,
    stored_markdown_sha256: str,
    *,
    attachment_key: str,
) -> None:
    """Decide whether a caller's locator may still be used to retrieve a passage.

    ``expected_content_sha256`` is what the caller's locator recorded; ``stored_markdown_sha256``
    is what the index holds for that attachment right now. ``get_fulltext`` calls this only when
    the caller supplied a hash, so ``None`` should not normally arrive; an unverified request stays
    legal, because omitting the locator is how a caller reads a document it never searched for.

    Returns normally to allow retrieval; raises ``StaleLocatorError`` to refuse it, which reaches
    the MCP client as the ``stale_locator`` error code.

    The policy is to refuse, because the two failure modes are not symmetric. A refusal is loud and
    recoverable. Serving the chunk at the same index out of replaced content is silent and
    *undetectable downstream*: the response is shaped exactly like a correct one, so a caller that
    quotes it attributes wording to a source that no longer says it, and nothing later in the chain
    can notice. For a server whose purpose is supplying evidence for claims, that is the failure
    worth paying friction to exclude.

    Strictness here is cheap because it is opt-in. Supplying a hash *is* the request to be held to
    one version; a caller that does not care omits it and is unaffected. It is also the reversible
    direction -- loosening later, if staleness proves to fire mostly on passages that did not
    change, breaks no one, whereas shipping lenient and tightening afterwards breaks every caller
    that came to rely on soft behavior.

    This is the document-level check, which necessarily over-refuses: ``markdown_sha256`` covers
    the whole document, so a pass that rewrites one region -- image OCR splicing a recovered
    equation, say -- invalidates locators into every chunk of that document, including chunks whose
    text is byte-identical. ``check_chunk_freshness`` is the precise counterpart; a caller that
    supplies a chunk hash is verified by that instead.
    """
    if expected_content_sha256 is None:
        return
    if expected_content_sha256 == stored_markdown_sha256:
        return
    raise StaleLocatorError(
        f"The converted text for attachment {attachment_key} has been replaced since this locator "
        "was issued; its character offsets no longer address the cited passage.",
        expected=expected_content_sha256,
        actual=stored_markdown_sha256,
    )


def check_chunk_freshness(
    expected_chunk_sha256: str | None,
    stored_chunk_text: str,
    *,
    attachment_key: str,
    chunk_index: int,
) -> None:
    """Verify one chunk's stored text against the hash a caller's locator recorded.

    Preferred over ``check_locator_freshness`` whenever the caller has a chunk hash, because it
    refuses exactly what changed. A reconversion that rewrites one equation leaves every other
    chunk byte-identical, and those citations stay valid under this check while the document hash
    would have rejected all of them.

    What it guarantees is textual, not positional: the passage returned is the passage cited. The
    surrounding document may still have shifted, so the response's character offsets are the
    current ones rather than the locator's -- which is why they are read back from the index on
    every call instead of being echoed from the request.
    """
    if expected_chunk_sha256 is None:
        return
    actual = chunk_sha256(stored_chunk_text)
    if expected_chunk_sha256 == actual:
        return
    raise StaleLocatorError(
        f"Chunk {chunk_index} of attachment {attachment_key} no longer holds the text this "
        "locator cited; it has been replaced since the locator was issued.",
        expected=expected_chunk_sha256,
        actual=actual,
    )


def get_fulltext(
    db_path: Path,
    *,
    attachment_key: str,
    max_chars: int = 12000,
    chunk_index: int | None = None,
    expected_content_sha256: str | None = None,
    expected_chunk_sha256: str | None = None,
) -> FullTextResult:
    if not attachment_key:
        raise ValueError("attachment_key is required")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    if expected_chunk_sha256 is not None and chunk_index is None:
        # A preview spans several chunks, so there is no single chunk whose hash could be checked.
        raise ValueError("expected_chunk_sha256 requires an exact chunk_index")

    con = connect_readonly(db_path)
    con.row_factory = sqlite3.Row
    try:
        metadata = con.execute(
            """
            SELECT metadata.*, (
                SELECT COUNT(*) FROM chunks WHERE chunks.record_id = metadata.record_id
            ) AS chunk_count
            FROM metadata
            WHERE zotero_attachment_key = ?
            """,
            (attachment_key,),
        ).fetchone()
        if metadata is None:
            raise KeyError(f"No record found for attachment key {attachment_key}")
        if expected_content_sha256 is not None and expected_chunk_sha256 is None:
            # Checked before any chunk is read: a stale locator's offsets address text this index
            # no longer holds, so there is nothing worth fetching for it. Skipped when a chunk hash
            # was supplied -- that check is strictly more precise, and enforcing both would refuse
            # a chunk whose own text is intact merely because some other part of the document was
            # rewritten, which is the over-refusal the chunk hash exists to remove.
            check_locator_freshness(
                expected_content_sha256,
                metadata["markdown_sha256"] or "",
                attachment_key=attachment_key,
            )
        if chunk_index is None:
            chunk_rows = _fetch_covering_chunks(con, metadata["record_id"], max_chars)
        else:
            chunk_rows = con.execute(
                """
                SELECT chunk_index, start_char, end_char, text
                FROM chunks
                WHERE record_id = ? AND chunk_index = ?
                ORDER BY chunk_index
                """,
                (metadata["record_id"], chunk_index),
            ).fetchall()
    finally:
        con.close()

    chunk_count = int(metadata["chunk_count"])
    if chunk_index is not None and not chunk_rows:
        raise ChunkNotFoundError(f"Chunk {chunk_index} does not exist for attachment {attachment_key}")

    if not chunk_rows:
        result_chunk_sha256 = None
        text = ""
        start_char = end_char = 0
        stored_chunk_char_start = stored_chunk_char_end = None
        truncated = end_char < int(metadata["char_count"] or 0)
        previous_chunk_index = next_chunk_index = has_more = None
    elif chunk_index is None:
        # A preview is assembled from several stored chunks, so no single chunk hash describes it.
        result_chunk_sha256 = None
        parts: list[str] = []
        start_char = int(chunk_rows[0]["start_char"])
        end_char = int(chunk_rows[0]["end_char"])
        for row in chunk_rows:
            next_part = row["text"]
            candidate = "\n\n".join(parts + [next_part]) if parts else next_part
            if len(candidate) > max_chars:
                remaining = max_chars - (len("\n\n".join(parts)) if parts else 0)
                if remaining > 0:
                    parts.append(next_part[:remaining])
                    end_char = int(row["start_char"]) + remaining
                break
            parts.append(next_part)
            end_char = int(row["end_char"])
        text = "\n\n".join(parts)[:max_chars]
        stored_chunk_char_start = stored_chunk_char_end = None
        truncated = end_char < int(metadata["char_count"] or 0)
        previous_chunk_index = next_chunk_index = has_more = None
    else:
        row = chunk_rows[0]
        # Verified against the full stored chunk, never the max_chars-truncated slice below: the
        # hash identifies the stored passage, so a smaller window must not change the answer.
        check_chunk_freshness(
            expected_chunk_sha256,
            row["text"],
            attachment_key=attachment_key,
            chunk_index=chunk_index,
        )
        result_chunk_sha256 = chunk_sha256(row["text"])
        stored_chunk_char_start = start_char = int(row["start_char"])
        stored_chunk_char_end = int(row["end_char"])
        text = row["text"][:max_chars]
        end_char = start_char + len(text)
        truncated = end_char < stored_chunk_char_end
        previous_chunk_index = chunk_index - 1 if chunk_index > 0 else None
        next_chunk_index = chunk_index + 1 if chunk_index + 1 < chunk_count else None
        has_more = next_chunk_index is not None

    return FullTextResult(
        zotero_parent_key=metadata["zotero_parent_key"],
        zotero_attachment_key=metadata["zotero_attachment_key"],
        title=metadata["title"],
        creators=metadata["creators"],
        year=metadata["year"],
        doi=metadata["doi"],
        citation_key=metadata["citation_key"],
        chunk_index=chunk_index,
        start_char=start_char,
        end_char=end_char,
        total_chars=int(metadata["char_count"] or 0),
        markdown_sha256=metadata["markdown_sha256"],
        chunk_sha256=result_chunk_sha256,
        chunk_count=chunk_count,
        previous_chunk_index=previous_chunk_index,
        next_chunk_index=next_chunk_index,
        has_more=has_more,
        stored_chunk_char_start=stored_chunk_char_start,
        stored_chunk_char_end=stored_chunk_char_end,
        truncated=truncated,
        text=text,
        source_path=metadata["source_path"],
        markdown_path=metadata["markdown_path"],
        extraction_tool=metadata["extraction_tool"],
        classification=metadata["classification"],
        identity_status=metadata["identity_status"],
        identity_rule=metadata["identity_rule"],
        has_math=bool(metadata["has_math"]),
    )


def _fetch_covering_chunks(con: sqlite3.Connection, record_id: int, max_chars: int) -> list[sqlite3.Row]:
    """Fetch just enough leading chunks (in order) to cover max_chars characters of text.

    An index is not required to have been built with DEFAULT_CHUNK_CHARS/DEFAULT_OVERLAP_CHARS,
    so a single LIMIT computed from those defaults can under-fetch for a smaller chunk size. This
    starts from that default as an estimate, then grows the LIMIT and re-queries until the
    actually-fetched rows span at least max_chars characters or no more rows exist -- bounded by
    _MAX_CHUNK_FETCH_ITERATIONS so a small window request still never degrades into reading an
    entire large document's chunks.
    """
    limit = math.ceil(max_chars / _CHUNK_ADVANCE_CHARS_ESTIMATE) + 1
    rows: list[sqlite3.Row] = []
    for _ in range(_MAX_CHUNK_FETCH_ITERATIONS):
        rows = con.execute(
            """
            SELECT chunk_index, start_char, end_char, text
            FROM chunks
            WHERE record_id = ?
            ORDER BY chunk_index
            LIMIT ?
            """,
            (record_id, limit),
        ).fetchall()
        if not rows or len(rows) < limit:
            return rows
        span = int(rows[-1]["end_char"]) - int(rows[0]["start_char"])
        if span >= max_chars:
            return rows
        limit *= 2
    return rows


def get_item_context(
    db_path: Path,
    *,
    parent_key: str | None = None,
    attachment_key: str | None = None,
    limit: int = DEFAULT_CONTEXT_RECORD_LIMIT,
) -> dict[str, object]:
    if (parent_key is None) == (attachment_key is None):
        raise ValueError("exactly one of parent_key and attachment_key is required")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    con = connect_readonly(db_path)
    con.row_factory = sqlite3.Row
    try:
        if attachment_key:
            rows = con.execute(
                "SELECT * FROM metadata WHERE zotero_attachment_key = ? ORDER BY title LIMIT ?",
                (attachment_key, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM metadata WHERE zotero_parent_key = ? ORDER BY title LIMIT ?",
                (parent_key, limit),
            ).fetchall()
    finally:
        con.close()
    return {"records": [_metadata_dict(row) for row in rows]}


def coverage_report(db_path: Path) -> dict[str, object]:
    con = connect_readonly(db_path)
    con.row_factory = sqlite3.Row
    try:
        metadata_rows = con.execute("SELECT * FROM metadata").fetchall()
        chunks = con.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    finally:
        con.close()

    return {
        "records": len(metadata_rows),
        "chunks": int(chunks),
        "total_chars": sum(int(row["char_count"] or 0) for row in metadata_rows),
        "total_words": sum(int(row["word_count"] or 0) for row in metadata_rows),
        "by_classification": dict(Counter(row["classification"] or "" for row in metadata_rows)),
        "by_identity_status": dict(Counter(row["identity_status"] or "" for row in metadata_rows)),
        "by_extraction_tool": dict(Counter(row["extraction_tool"] or "" for row in metadata_rows)),
        "by_has_math": dict(Counter(bool(row["has_math"]) for row in metadata_rows)),
    }


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE metadata (
            record_id INTEGER PRIMARY KEY,
            zotero_parent_key TEXT NOT NULL,
            zotero_attachment_key TEXT NOT NULL,
            title TEXT NOT NULL,
            creators TEXT NOT NULL,
            year TEXT NOT NULL,
            doi TEXT NOT NULL,
            citation_key TEXT NOT NULL,
            source_path TEXT NOT NULL,
            markdown_path TEXT NOT NULL,
            markdown_sha256 TEXT NOT NULL,
            extraction_tool TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            word_count INTEGER NOT NULL,
            page_count TEXT NOT NULL,
            classification TEXT NOT NULL,
            identity_status TEXT NOT NULL,
            identity_rule TEXT NOT NULL,
            has_math INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX metadata_attachment_key_idx ON metadata(zotero_attachment_key);
        CREATE INDEX metadata_parent_key_idx ON metadata(zotero_parent_key);
        CREATE TABLE chunks (
            chunk_id INTEGER PRIMARY KEY,
            record_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            start_char INTEGER NOT NULL,
            end_char INTEGER NOT NULL,
            text TEXT NOT NULL,
            FOREIGN KEY(record_id) REFERENCES metadata(record_id)
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            title,
            creators,
            text,
            citation_key,
            record_id UNINDEXED,
            chunk_id UNINDEXED,
            tokenize='unicode61'
        );
        """
    )


def _insert_metadata(con: sqlite3.Connection, record: dict[str, object]) -> int:
    columns = [
        "zotero_parent_key",
        "zotero_attachment_key",
        "title",
        "creators",
        "year",
        "doi",
        "citation_key",
        "source_path",
        "markdown_path",
        "markdown_sha256",
        "extraction_tool",
        "char_count",
        "word_count",
        "page_count",
        "classification",
        "identity_status",
        "identity_rule",
        "has_math",
    ]
    values = [_string(record.get(column)) for column in columns]
    values[11] = int(record.get("char_count") or 0)
    values[12] = int(record.get("word_count") or 0)
    values[17] = int(bool(record.get("has_math", False)))
    cursor = con.execute(
        f"INSERT INTO metadata ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        values,
    )
    return int(cursor.lastrowid)


def _insert_chunk(
    con: sqlite3.Connection,
    record_id: int,
    record: dict[str, object],
    chunk: tuple[int, int, int, str],
) -> None:
    chunk_index, start_char, end_char, text = chunk
    cursor = con.execute(
        """
        INSERT INTO chunks (record_id, chunk_index, start_char, end_char, text)
        VALUES (?, ?, ?, ?, ?)
        """,
        (record_id, chunk_index, start_char, end_char, text),
    )
    chunk_id = int(cursor.lastrowid)
    con.execute(
        """
        INSERT INTO chunks_fts (rowid, title, creators, text, citation_key, record_id, chunk_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk_id,
            _string(record.get("title")),
            _string(record.get("creators")),
            text,
            _string(record.get("citation_key")),
            record_id,
            chunk_id,
        ),
    )


def _chunk_text(text: str, chunk_chars: int, overlap_chars: int) -> Iterable[tuple[int, int, int, str]]:
    _validate_chunking(chunk_chars, overlap_chars)
    if not text:
        return
    start = 0
    chunk_index = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_chars, text_len)
        raw_chunk = text[start:end]
        chunk = raw_chunk.strip()
        if chunk:
            leading_whitespace = len(raw_chunk) - len(raw_chunk.lstrip())
            yield chunk_index, start + leading_whitespace, start + leading_whitespace + len(chunk), chunk
            chunk_index += 1
        if end >= text_len:
            break
        start = max(start + 1, end - overlap_chars)


def _validate_chunking(chunk_chars: int, overlap_chars: int) -> None:
    if chunk_chars < 1:
        raise ValueError("chunk_chars must be at least 1")
    if overlap_chars < 0:
        raise ValueError("overlap_chars must be non-negative")
    if overlap_chars >= chunk_chars:
        raise ValueError("overlap_chars must be smaller than chunk_chars")


def _validate_search_request(query: str, limit: int, search_mode: str) -> list[str]:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must contain 1 to {MAX_QUERY_CHARS} characters")
    terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not terms:
        raise ValueError("query must contain at least one searchable term")
    if len(terms) > MAX_QUERY_TERMS:
        raise ValueError(f"query may contain at most {MAX_QUERY_TERMS} searchable terms")
    if any(len(term) > MAX_QUERY_TERM_CHARS for term in terms):
        raise ValueError(f"query terms may contain at most {MAX_QUERY_TERM_CHARS} characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
    if search_mode not in SEARCH_MODES:
        raise ValueError(f"search_mode must be one of: {', '.join(sorted(SEARCH_MODES))}")
    return terms


def _match_query(terms: list[str], search_mode: str) -> str:
    if search_mode == "phrase":
        return '"' + " ".join(terms) + '"'
    operator = " AND " if search_mode == "all_terms" else " OR "
    return operator.join(f'"{term}"' for term in terms)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        _assert_supported_schema(con, db_path)
    except BaseException:
        con.close()
        raise
    return con


def _assert_supported_schema(con: sqlite3.Connection, db_path: Path) -> None:
    """Fail with a recovery instruction when db_path is not a supported full-text index.

    Without this, pointing a reader at an empty, foreign, or legacy-schema SQLite file surfaces
    as a low-level 'no such table'/'no such column' OperationalError from whichever query runs
    first, which names neither the problem nor the fix.
    """
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        missing_tables = _REQUIRED_TABLES - tables
        missing_columns: frozenset[str] = frozenset()
        if not missing_tables:
            columns = {row[1] for row in con.execute("PRAGMA table_info(metadata)").fetchall()}
            missing_columns = _REQUIRED_METADATA_COLUMNS - columns
    except sqlite3.DatabaseError as exc:
        raise IndexSchemaUnsupportedError(
            f"{db_path} is not a readable SQLite database ({exc}). Rebuild the index with "
            "'zotero-pdf-text rebuild-index'."
        ) from exc
    if missing_tables or missing_columns:
        missing = ", ".join(sorted(missing_tables) + sorted(missing_columns))
        raise IndexSchemaUnsupportedError(
            f"{db_path} is not a supported full-text index (missing: {missing}). It may predate "
            "the current index schema or be a different SQLite file entirely. Rebuild it with "
            "'zotero-pdf-text rebuild-index'."
        )


def _metadata_dict(row: sqlite3.Row) -> dict[str, object]:
    keys = [
        "zotero_parent_key",
        "zotero_attachment_key",
        "title",
        "creators",
        "year",
        "doi",
        "citation_key",
        "source_path",
        "markdown_path",
        "markdown_sha256",
        "extraction_tool",
        "char_count",
        "word_count",
        "page_count",
        "classification",
        "identity_status",
        "identity_rule",
        "has_math",
    ]
    result = {key: row[key] for key in keys}
    result["has_math"] = bool(result["has_math"])
    return result


def _string(value: object) -> str:
    return "" if value is None else str(value)
