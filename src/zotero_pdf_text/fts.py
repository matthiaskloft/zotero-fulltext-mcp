from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_CHUNK_CHARS = 6000
DEFAULT_OVERLAP_CHARS = 500
# Consecutive stored chunks advance by (chunk size - overlap) characters. Fetching this many
# chunks is always enough to cover any max_chars request without reading a whole large
# document's chunk rows just to truncate them afterward.
_CHUNK_ADVANCE_CHARS = max(DEFAULT_CHUNK_CHARS - DEFAULT_OVERLAP_CHARS, 1)
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
    if output.exists():
        output.unlink()

    con = sqlite3.connect(output)
    try:
        _create_schema(con)
        records = chunks = total_chars = total_words = 0
        with index_jsonl.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
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

    return FtsBuildSummary(
        database=str(output),
        source_jsonl=str(index_jsonl),
        records=records,
        chunks=chunks,
        total_chars=total_chars,
        total_words=total_words,
    )


def search_fts(db_path: Path, query: str, *, limit: int = 10) -> list[SearchResult]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    match_query = _match_query(query)
    con = connect_readonly(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
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
                bm25(chunks_fts) AS score,
                c.chunk_index,
                c.start_char,
                c.end_char,
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
            ORDER BY score ASC
            LIMIT ?
            """,
            (match_query, max(limit * 5, 50)),
        ).fetchall()
    finally:
        con.close()
    deduped: list[SearchResult] = []
    seen_records: set[int] = set()
    for row in rows:
        row_dict = dict(row)
        record_id = int(row_dict.pop("record_id"))
        if record_id in seen_records:
            continue
        seen_records.add(record_id)
        row_dict["has_math"] = bool(row_dict["has_math"])
        deduped.append(SearchResult(**row_dict))
        if len(deduped) >= limit:
            break
    return deduped


def get_fulltext(
    db_path: Path,
    *,
    attachment_key: str,
    max_chars: int = 12000,
    chunk_index: int | None = None,
) -> FullTextResult:
    if not attachment_key:
        raise ValueError("attachment_key is required")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")

    con = connect_readonly(db_path)
    con.row_factory = sqlite3.Row
    try:
        metadata = con.execute(
            "SELECT * FROM metadata WHERE zotero_attachment_key = ?",
            (attachment_key,),
        ).fetchone()
        if metadata is None:
            raise KeyError(f"No record found for attachment key {attachment_key}")
        if chunk_index is None:
            chunk_limit = math.ceil(max_chars / _CHUNK_ADVANCE_CHARS) + 1
            chunk_rows = con.execute(
                """
                SELECT chunk_index, start_char, end_char, text
                FROM chunks
                WHERE record_id = ?
                ORDER BY chunk_index
                LIMIT ?
                """,
                (metadata["record_id"], chunk_limit),
            ).fetchall()
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

    if not chunk_rows:
        text = ""
        start_char = end_char = 0
    elif chunk_index is None:
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
    else:
        row = chunk_rows[0]
        start_char = int(row["start_char"])
        text = row["text"][:max_chars]
        end_char = start_char + len(text)

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
        text=text,
        source_path=metadata["source_path"],
        markdown_path=metadata["markdown_path"],
        extraction_tool=metadata["extraction_tool"],
        classification=metadata["classification"],
        identity_status=metadata["identity_status"],
        identity_rule=metadata["identity_rule"],
        has_math=bool(metadata["has_math"]),
    )


def get_item_context(
    db_path: Path,
    *,
    parent_key: str | None = None,
    attachment_key: str | None = None,
    limit: int = DEFAULT_CONTEXT_RECORD_LIMIT,
) -> dict[str, object]:
    if not parent_key and not attachment_key:
        raise ValueError("parent_key or attachment_key is required")
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
        chunk = text[start:end].strip()
        if chunk:
            yield chunk_index, start, end, chunk
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


def _match_query(query: str) -> str:
    terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not terms:
        raise ValueError("query must contain at least one searchable term")
    return " AND ".join(f'"{term}"' for term in terms)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


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
