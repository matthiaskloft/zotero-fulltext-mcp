"""Chunking strategies and separate experimental indexes for the S1a sweeps (issue #107).

Everything here is benchmark-owned. An *experimental index* is a scratch SQLite file with its own
small schema (documents, chunks, an FTS5 table with production's title/creators/text/citation_key
columns, image markup blanked from the body column like production, and an ``experiment_meta`` marker). It is never a
generation of the production index: it carries no ``current.json``, is refused inside any directory
tree that holds one, and an existing file is only replaced when it carries this module's marker.
The production schema and storage code in ``src/`` are not touched, so a chunk-size or boundary
sweep can run without replacing the user's current generation.

Chunk specs are ``strategy:target[:overlap]``:

* ``chars:6000:500``      the current production slicing (target and overlap in characters);
* ``sentence:384:0``      sentence-respecting packing (target and overlap in tokens);
* ``structural:384:48``   heading-bounded paragraph/table/equation packing (tokens).

Every chunk is a contiguous slice of the original text, so offsets and hashes stay authoritative:
``sha256(text[start:end])`` is the chunk hash. Nothing is dropped or silently truncated; an atom
larger than the target is split further (sentences, then whitespace pieces, then character runs).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from zotero_pdf_text.zotero_db import read_only_uri
from zotero_pdf_text.fts import _chunk_text, chunk_sha256, fts_body_text, image_markup_spans

STRATEGIES = ("chars", "sentence", "structural")
SWEEP_TARGETS = (128, 256, 384, 512, 768)
BOUNDED_OVERLAP_FRACTION = 0.125  # inside the proposal's 10-15% bound
EXPERIMENT_SUFFIX = ".experiment.sqlite"
MARKER_TABLE = "experiment_meta"
SCHEMA_VERSION = 1
DEFAULT_TOKENIZER = "regex"


# --- tokenizers ---------------------------------------------------------------------------------


class Tokenizer:
    """Counts tokens; ``name`` and ``version`` are recorded with every result."""

    name = "abstract"
    version = "0"

    def count(self, text: str) -> int:
        raise NotImplementedError


class RegexTokenizer(Tokenizer):
    """Dependency-free approximation: one token per word run or punctuation mark.

    It is deterministic and pinned by ``version``, not a model tokenizer. Budgets stated in these
    tokens are comparable across runs of this harness; use a model tokenizer (``tiktoken:<name>``)
    when the budget must match a specific embedding model.
    """

    name = "regex"
    version = "1"
    _pattern = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    def count(self, text: str) -> int:
        return len(self._pattern.findall(text))


class TiktokenTokenizer(Tokenizer):
    def __init__(self, encoding: str) -> None:
        try:
            import tiktoken  # type: ignore[import-not-found]
        except ImportError:
            raise SystemExit("--tokenizer tiktoken:<encoding> needs the optional tiktoken package.") from None
        try:
            self._encoding = tiktoken.get_encoding(encoding)
        except Exception:
            raise SystemExit(f"Unknown tiktoken encoding: {encoding}.") from None
        self.name = f"tiktoken:{encoding}"
        self.version = str(getattr(tiktoken, "__version__", "unknown"))

    def count(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))


def get_tokenizer(spec: str = DEFAULT_TOKENIZER) -> Tokenizer:
    if spec == "regex":
        return RegexTokenizer()
    if spec.startswith("tiktoken:") and len(spec) > len("tiktoken:"):
        return TiktokenTokenizer(spec.split(":", 1)[1])
    raise SystemExit("--tokenizer must be 'regex' or 'tiktoken:<encoding>'.")


# --- specs --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkSpec:
    strategy: str
    target: int
    overlap: int = 0

    @property
    def unit(self) -> str:
        return "chars" if self.strategy == "chars" else "tokens"

    @property
    def label(self) -> str:
        return f"{self.strategy}:{self.target}:{self.overlap}"

    def to_dict(self) -> dict[str, object]:
        return {"strategy": self.strategy, "target": self.target, "overlap": self.overlap, "unit": self.unit}


def parse_spec(text: str) -> ChunkSpec:
    parts = text.split(":")
    if len(parts) not in (2, 3) or parts[0] not in STRATEGIES:
        raise SystemExit(f"--chunking must be strategy:target[:overlap] with strategy in {list(STRATEGIES)}.")
    try:
        target = int(parts[1])
        overlap = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        raise SystemExit("--chunking target and overlap must be integers.") from None
    if target < 1 or overlap < 0 or overlap >= target:
        raise SystemExit("--chunking needs target >= 1 and 0 <= overlap < target.")
    return ChunkSpec(parts[0], target, overlap)


def sweep_specs(strategy: str, targets: Sequence[int] = SWEEP_TARGETS) -> list[ChunkSpec]:
    """Each target with zero overlap and with bounded overlap (12.5% of the target, in tokens)."""
    if strategy not in ("sentence", "structural"):
        raise SystemExit("--sweep supports the token-budgeted strategies: sentence, structural.")
    specs = []
    for target in targets:
        specs.append(ChunkSpec(strategy, target, 0))
        specs.append(ChunkSpec(strategy, target, max(1, round(target * BOUNDED_OVERLAP_FRACTION))))
    return specs


# --- boundaries ---------------------------------------------------------------------------------

_ABBREVIATIONS = frozenset({"al", "e.g", "i.e", "fig", "figs", "eq", "eqs", "vs", "cf", "etc", "no", "sec", "ref"})
_SENTENCE_END = re.compile(r"[.!?][)\]\"'”’]*\s+")
_NEXT_STARTS = re.compile(r"[A-Z0-9(\[\"“$\\]")
_HEADING = re.compile(r"[ \t]{0,3}#{1,6}[ \t]+\S")
_TABLE_ROW = re.compile(r"[ \t]*\|")


@dataclass(frozen=True)
class Atom:
    start: int
    end: int
    tokens: int
    section_start: bool = False


def sentence_spans(text: str, lo: int = 0, hi: int | None = None) -> list[tuple[int, int]]:
    """Offset-preserving sentence spans of ``text[lo:hi]``, whitespace excluded."""
    hi = len(text) if hi is None else hi
    spans: list[tuple[int, int]] = []
    pos = lo
    for match in _SENTENCE_END.finditer(text, lo, hi):
        nxt = match.end()
        if nxt >= hi or not _NEXT_STARTS.match(text[nxt]):
            continue
        word = re.search(r"([\w.]+)$", text[max(lo, match.start() - 12) : match.start()])
        if word and word.group(1).lower().rstrip(".") in _ABBREVIATIONS:
            continue
        spans.append((pos, match.start() + len(match.group(0).rstrip())))
        pos = nxt
    if pos < hi:
        spans.append((pos, hi))
    return [(a, b) for a, b in ((a, _rstrip(text, a, b)) for a, b in spans) if b > a]


def _rstrip(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1].isspace():
        end -= 1
    return end


def _split_oversized(text: str, start: int, end: int, target: int, tokenizer: Tokenizer) -> list[tuple[int, int]]:
    """Split one span larger than ``target`` tokens without dropping text.

    Prefers whitespace boundaries. A single unbroken run above the target is sliced by characters
    in proportion to its token density, so the result always makes progress and covers the span.
    """
    pieces: list[tuple[int, int]] = []
    piece_start: int | None = None
    piece_end = start
    piece_tokens = 0

    def flush() -> None:
        nonlocal piece_start, piece_tokens
        if piece_start is not None:
            pieces.append((piece_start, piece_end))
        piece_start, piece_tokens = None, 0

    for match in re.finditer(r"\S+", text[start:end]):
        a, b = start + match.start(), start + match.end()
        tokens = tokenizer.count(text[a:b])
        if tokens > target:
            flush()
            step = max(1, int((b - a) * target / tokens))
            pieces.extend((s, min(b, s + step)) for s in range(a, b, step))
            continue
        if piece_start is not None and piece_tokens + tokens > target:
            flush()
        if piece_start is None:
            piece_start = a
        piece_tokens += tokens
        piece_end = b
    flush()
    return pieces


def _sentence_atoms(text: str, tokenizer: Tokenizer, target: int) -> list[Atom]:
    atoms = []
    for a, b in sentence_spans(text):
        atoms.extend(_sized_atoms(text, a, b, target, tokenizer))
    return atoms


def _sized_atoms(text: str, a: int, b: int, target: int, tokenizer: Tokenizer, flag: bool = False) -> list[Atom]:
    tokens = tokenizer.count(text[a:b])
    if tokens <= target:
        return [Atom(a, b, tokens, flag)]
    return [
        Atom(s, e, tokenizer.count(text[s:e]), flag and i == 0)
        for i, (s, e) in enumerate(_split_oversized(text, a, b, target, tokenizer))
    ]


def _block_spans(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield ``(kind, start, end)`` for headings, tables, display math and paragraphs."""
    lines = [(m.start(), m.end()) for m in re.finditer(r"[^\n]*(?:\n|$)", text) if m.end() > m.start()]
    i = 0
    while i < len(lines):
        a, b = lines[i]
        line = text[a:b]
        if not line.strip():
            i += 1
            continue
        if _HEADING.match(line):
            yield "heading", a, _rstrip(text, a, b)
            i += 1
        elif _TABLE_ROW.match(line):
            j = i
            while j + 1 < len(lines) and _TABLE_ROW.match(text[lines[j + 1][0] : lines[j + 1][1]]):
                j += 1
            yield "table", a, _rstrip(text, a, lines[j][1])
            i = j + 1
        elif line.strip().startswith("$$"):
            j = i
            closed = line.strip().count("$$") >= 2
            while not closed and j + 1 < len(lines):
                j += 1
                closed = "$$" in text[lines[j][0] : lines[j][1]]
            yield "math", a, _rstrip(text, a, lines[j][1])
            i = j + 1
        else:
            j = i
            while j + 1 < len(lines):
                nxt = text[lines[j + 1][0] : lines[j + 1][1]]
                if not nxt.strip() or _HEADING.match(nxt) or _TABLE_ROW.match(nxt) or nxt.strip().startswith("$$"):
                    break
                j += 1
            yield "paragraph", a, _rstrip(text, a, lines[j][1])
            i = j + 1


def _structural_atoms(text: str, tokenizer: Tokenizer, target: int) -> list[Atom]:
    atoms: list[Atom] = []
    section_pending = True  # the document start opens a section
    for kind, a, b in _block_spans(text):
        if kind == "heading":
            section_pending = True
        tokens = tokenizer.count(text[a:b])
        if tokens <= target:
            block = [Atom(a, b, tokens)]
        elif kind == "table":
            block = []
            for m in re.finditer(r"[^\n]+", text[a:b]):  # split an oversized table by rows
                block.extend(_sized_atoms(text, a + m.start(), a + m.end(), target, tokenizer))
        elif kind == "paragraph":
            block = _sentence_atoms_range(text, a, b, tokenizer, target)
        else:
            block = _sized_atoms(text, a, b, target, tokenizer)
        if block and section_pending:
            block[0] = Atom(block[0].start, block[0].end, block[0].tokens, True)
            section_pending = False
        atoms.extend(block)
    return atoms


def _sentence_atoms_range(text: str, lo: int, hi: int, tokenizer: Tokenizer, target: int) -> list[Atom]:
    atoms: list[Atom] = []
    for a, b in sentence_spans(text, lo, hi):
        atoms.extend(_sized_atoms(text, a, b, target, tokenizer))
    return atoms


def _pack(text: str, atoms: list[Atom], target: int, overlap: int, tokenizer: Tokenizer, hard_sections: bool) -> list[tuple[int, int]]:
    """Greedily pack consecutive atoms; each chunk is the contiguous slice covering its atoms.

    The token cost of a chunk is measured on that slice (including inter-atom whitespace), so the
    reported size is the real size. A heading (``hard_sections``) always starts a new chunk.
    Overlap re-includes trailing whole atoms of the previous chunk up to ``overlap`` tokens, and
    only after a size-forced break, never across a section boundary.
    """
    chunks: list[tuple[int, int]] = []
    i, n = 0, len(atoms)
    while i < n:
        j = i
        while j < n:
            if j > i and hard_sections and atoms[j].section_start:
                break
            tentative = tokenizer.count(text[atoms[i].start : atoms[j].end])
            if j > i and tentative > target:
                break
            j += 1
        chunks.append((atoms[i].start, atoms[j - 1].end))
        if j >= n:
            break
        next_start = j
        if overlap and not (hard_sections and atoms[j].section_start):
            back, carried = j, 0
            while back - 1 > i and carried + atoms[back - 1].tokens <= overlap:
                back -= 1
                carried += atoms[back].tokens
            next_start = back
        i = max(next_start, i + 1)
    return chunks


def chunk_document(text: str, spec: ChunkSpec, tokenizer: Tokenizer) -> list[tuple[int, int]]:
    """Chunk ``text`` into ``(start, end)`` slices according to ``spec``."""
    if spec.strategy == "chars":
        return [(start, end) for _, start, end, _ in _chunk_text(text, spec.target, spec.overlap)]
    if spec.strategy == "sentence":
        return _pack(text, _sentence_atoms(text, tokenizer, spec.target), spec.target, spec.overlap, tokenizer, False)
    return _pack(text, _structural_atoms(text, tokenizer, spec.target), spec.target, spec.overlap, tokenizer, True)


# --- corpus -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusDoc:
    attachment_key: str
    citation_key: str
    text: str
    title: str = ""
    creators: str = ""


def load_corpus(path: Path) -> dict[str, CorpusDoc]:
    """Load documents from a ``*.jsonl`` index export or a JSON list, keyed by attachment key."""
    try:
        raw = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            records = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read the corpus file: {type(exc).__name__}.") from None
    if not isinstance(records, list):
        raise SystemExit("The corpus must be a JSON list or a JSONL file of records.")
    docs: dict[str, CorpusDoc] = {}
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict) or not isinstance(record.get("text"), str):
            raise SystemExit(f"Corpus record #{position} needs a string \"text\".")
        key = str(record.get("zotero_attachment_key") or "")
        if not key or key in docs:
            raise SystemExit(f"Corpus record #{position} needs a unique zotero_attachment_key.")
        docs[key] = CorpusDoc(
            key, str(record.get("citation_key") or ""), record["text"],
            str(record.get("title") or ""), str(record.get("creators") or ""),
        )
    return docs


def corpus_sha256(docs: dict[str, CorpusDoc]) -> str:
    digest = hashlib.sha256()
    for key in sorted(docs):
        digest.update(f"{key}\0{docs[key].citation_key}\0".encode())
        digest.update(hashlib.sha256(docs[key].text.encode("utf-8")).digest())
    return digest.hexdigest()


# --- experimental index -------------------------------------------------------------------------


@dataclass
class ExperimentSummary:
    path: Path
    spec: ChunkSpec
    tokenizer: str
    tokenizer_version: str
    corpus_sha256: str
    documents: int
    chunks: int
    build_seconds: float
    index_bytes: int
    size_tokens: dict[str, float] = field(default_factory=dict)
    size_chars: dict[str, float] = field(default_factory=dict)
    over_target: int = 0

    def to_dict(self) -> dict[str, object]:
        """Reproducibility facts; never the path (it may name the user's directories)."""
        return {
            "chunking": self.spec.to_dict(),
            "tokenizer": {"name": self.tokenizer, "version": self.tokenizer_version},
            "corpus_sha256": self.corpus_sha256,
            "documents": self.documents,
            "chunks": self.chunks,
            "build_seconds": round(self.build_seconds, 3),
            "index_bytes": self.index_bytes,
            "size_tokens": self.size_tokens,
            "size_chars": self.size_chars,
            "chunks_over_target": self.over_target,
        }


def _distribution(values: list[int]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p50": percentile(ordered, 50),
        "p95": percentile(ordered, 95),
        "max": ordered[-1],
        "mean": round(statistics.fmean(ordered), 1),
    }


def percentile(ordered: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending sequence."""
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def assert_safe_experiment_path(path: Path) -> None:
    """Refuse any location that could be, or sit inside, a production index."""
    resolved = path.resolve()
    if not resolved.name.endswith(EXPERIMENT_SUFFIX):
        raise SystemExit(f"Experimental index files must be named *{EXPERIMENT_SUFFIX}.")
    for directory in (resolved.parent, *resolved.parent.parents):
        if (directory / "current.json").exists() or directory.name == "generations":
            raise SystemExit("Refusing to build an experimental index inside a production output root.")
    if resolved.exists():
        try:
            con = sqlite3.connect(read_only_uri(resolved, immutable=False), uri=True)
            try:
                con.execute(f"SELECT 1 FROM {MARKER_TABLE} LIMIT 1")
            finally:
                con.close()
        except sqlite3.Error:
            raise SystemExit("Refusing to replace a file that is not an experimental index.") from None


def build_experiment_index(
    docs: dict[str, CorpusDoc], spec: ChunkSpec, tokenizer: Tokenizer, path: Path
) -> ExperimentSummary:
    """Build ``path`` from ``docs`` with ``spec``. Replaces only a previous experimental index."""
    assert_safe_experiment_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    started = time.perf_counter()
    token_sizes: list[int] = []
    char_sizes: list[int] = []
    try:
        con = sqlite3.connect(tmp)
        try:
            con.executescript(
                f"""
                CREATE TABLE {MARKER_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE docs (
                    doc_id INTEGER PRIMARY KEY, attachment_key TEXT NOT NULL, citation_key TEXT NOT NULL,
                    text_chars INTEGER NOT NULL, text_sha256 TEXT NOT NULL);
                CREATE TABLE chunks (
                    chunk_id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
                    start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, tokens INTEGER NOT NULL,
                    chunk_sha256 TEXT NOT NULL, text TEXT NOT NULL);
                CREATE INDEX chunks_doc_idx ON chunks(doc_id, chunk_index);
                CREATE VIRTUAL TABLE chunks_fts USING fts5(title, creators, text, citation_key, tokenize='unicode61');
                """
            )
            over = 0
            for doc_id, key in enumerate(sorted(docs), start=1):
                doc = docs[key]
                con.execute(
                    "INSERT INTO docs VALUES (?, ?, ?, ?, ?)",
                    (doc_id, doc.attachment_key, doc.citation_key, len(doc.text), chunk_sha256(doc.text)),
                )
                image_spans = image_markup_spans(doc.text)
                for index, (start, end) in enumerate(chunk_document(doc.text, spec, tokenizer)):
                    text = doc.text[start:end]
                    tokens = tokenizer.count(text)
                    over += spec.unit == "tokens" and tokens > spec.target
                    token_sizes.append(tokens)
                    char_sizes.append(len(text))
                    cursor = con.execute(
                        "INSERT INTO chunks (doc_id, chunk_index, start_char, end_char, tokens, chunk_sha256, text)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (doc_id, index, start, end, tokens, chunk_sha256(text), text),
                    )
                    con.execute(
                        "INSERT INTO chunks_fts (rowid, title, creators, text, citation_key) VALUES (?, ?, ?, ?, ?)",
                        (cursor.lastrowid, doc.title, doc.creators, fts_body_text(text, start, image_spans),
                         doc.citation_key),
                    )
            meta = {
                "schema_version": SCHEMA_VERSION,
                "chunking": json.dumps(spec.to_dict()),
                "tokenizer": tokenizer.name,
                "tokenizer_version": tokenizer.version,
                "corpus_sha256": corpus_sha256(docs),
            }
            con.executemany(f"INSERT INTO {MARKER_TABLE} VALUES (?, ?)", [(k, str(v)) for k, v in meta.items()])
            con.commit()
        finally:
            con.close()
        build_seconds = time.perf_counter() - started
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    return ExperimentSummary(
        path=path,
        spec=spec,
        tokenizer=tokenizer.name,
        tokenizer_version=tokenizer.version,
        corpus_sha256=corpus_sha256(docs),
        documents=len(docs),
        chunks=len(token_sizes),
        build_seconds=build_seconds,
        index_bytes=path.stat().st_size,
        size_tokens=_distribution(token_sizes),
        size_chars=_distribution(char_sizes),
        over_target=over,
    )


def open_experiment(path: Path) -> sqlite3.Connection:
    """Read-only connection to an experimental index (never a production one)."""
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(read_only_uri(path.resolve(), immutable=False), uri=True)
    con.row_factory = sqlite3.Row
    return con


def iter_chunks(con: sqlite3.Connection, attachment_key: str) -> Iterable[sqlite3.Row]:
    return con.execute(
        "SELECT c.* FROM chunks c JOIN docs d ON d.doc_id = c.doc_id WHERE d.attachment_key = ?"
        " ORDER BY c.chunk_index",
        (attachment_key,),
    ).fetchall()
