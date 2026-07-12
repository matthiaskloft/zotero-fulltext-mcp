from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class TextIndexRecord:
    zotero_parent_key: str
    zotero_attachment_key: str
    title: str
    creators: str
    year: str
    doi: str
    citation_key: str
    source_path: str
    markdown_path: str
    markdown_sha256: str
    extraction_tool: str
    char_count: int
    word_count: int
    page_count: str
    classification: str
    identity_status: str
    identity_rule: str
    has_math: bool
    text: str


def _atomic_write_text(path: Path, content: str) -> None:
    """Write content to path via a same-directory temp file plus atomic replace.

    A crash or interruption mid-write leaves the temp file orphaned (cleaned up on the next
    successful call, or manually) rather than leaving `path` itself truncated or half-written.
    """
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8", newline="\n")
        _replace_with_retry(tmp_path, path)
    finally:
        # Suppress cleanup failures so they never shadow a real exception from the write/replace
        # above (missing_ok=True already handles the common case where the temp file no longer
        # exists because the replace succeeded).
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 5, initial_delay: float = 0.05) -> None:
    """os.replace with short retries against a transient Windows PermissionError.

    See the identical helper in fts.py for the rationale -- Windows can raise PermissionError if
    another process has `dst` open at the exact instant of rename; POSIX doesn't have this issue.
    """
    delay = initial_delay
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def load_indexed_keys(jsonl_path: Path) -> set[str]:
    """Return the set of zotero_attachment_key values already present in a JSONL index."""
    if not jsonl_path.exists():
        return set()
    keys: set[str] = set()
    with jsonl_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                record = json.loads(line)
                key = record.get("zotero_attachment_key", "")
                if key:
                    keys.add(key)
    return keys


def append_text_index(new_manifest: Path, existing_jsonl: Path, output: Path) -> Path:
    """Append records from new_manifest to existing_jsonl, skipping already-indexed attachment keys."""
    output.parent.mkdir(parents=True, exist_ok=True)
    existing_keys = load_indexed_keys(existing_jsonl)
    new_rows = [
        row
        for row in _converted_rows(new_manifest)
        if row.get("zotero_attachment_key", "") not in existing_keys
    ]
    if not new_rows:
        if output.resolve() != existing_jsonl.resolve() and existing_jsonl.exists():
            import shutil
            shutil.copy2(existing_jsonl, output)
        return output
    new_records = [_record_from_manifest_row(row) for row in new_rows]
    existing_text = existing_jsonl.read_text(encoding="utf-8") if existing_jsonl.exists() else ""
    parts: list[str] = []
    if existing_text:
        parts.append(existing_text)
        if not existing_text.endswith("\n"):
            parts.append("\n")
    for record in new_records:
        parts.append(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    _atomic_write_text(output, "".join(parts))
    return output


def replace_text_index_record(jsonl_path: Path, attachment_key: str, new_record: TextIndexRecord) -> Path:
    """Replace the JSONL line for attachment_key with new_record, preserving all other lines and order."""
    if not jsonl_path.exists():
        raise FileNotFoundError(jsonl_path)
    # Split on the literal "\n" the writer uses, not str.splitlines() -- splitlines() also
    # breaks on Unicode line/paragraph separators (U+2028/U+2029), which can legitimately
    # appear inside a record's extracted text and would otherwise fragment that JSON line.
    content = jsonl_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        if not line.strip():
            new_lines.append(line)
            continue
        record = json.loads(line)
        if record.get("zotero_attachment_key") == attachment_key:
            new_lines.append(json.dumps(asdict(new_record), ensure_ascii=False))
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        raise KeyError(f"No JSONL record found for attachment key {attachment_key}")
    _atomic_write_text(jsonl_path, "\n".join(new_lines) + "\n")
    return jsonl_path


def build_text_index(manifest: Path, output: Path) -> Path:
    if not manifest.exists():
        raise FileNotFoundError(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)

    records = [_record_from_manifest_row(row) for row in _converted_rows(manifest)]
    content = "".join(json.dumps(asdict(record), ensure_ascii=False) + "\n" for record in records)
    _atomic_write_text(output, content)
    _write_summary(output.with_suffix(".summary.md"), manifest, output, records)
    return output


def _converted_rows(manifest: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("status") in {"converted", "skipped_existing"} and row.get("output_path"):
                rows.append(row)
    return rows


def _record_from_manifest_row(row: dict[str, str]) -> TextIndexRecord:
    markdown_path = Path(row["output_path"])
    markdown = markdown_path.read_text(encoding="utf-8")
    text = _strip_front_matter(markdown)
    return TextIndexRecord(
        zotero_parent_key=row.get("zotero_parent_key", ""),
        zotero_attachment_key=row.get("zotero_attachment_key", ""),
        title=row.get("title", ""),
        creators=row.get("creators", ""),
        year=row.get("year", ""),
        doi=row.get("doi", ""),
        citation_key=row.get("citation_key", ""),
        source_path=row.get("source_path", ""),
        markdown_path=str(markdown_path),
        markdown_sha256=_sha256(markdown_path),
        extraction_tool=row.get("extraction_tool", ""),
        char_count=len(text),
        word_count=len(text.split()),
        page_count=row.get("page_count", ""),
        classification=row.get("classification", ""),
        identity_status=row.get("identity_status", ""),
        identity_rule=row.get("identity_rule", ""),
        has_math=row.get("has_math", "false").strip().lower() == "true",
        text=text,
    )


def _strip_front_matter(markdown: str) -> str:
    if not markdown.startswith("---\n"):
        return markdown.strip()
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return markdown.strip()
    return markdown[end + len("\n---\n") :].strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_summary(path: Path, manifest: Path, output: Path, records: list[TextIndexRecord]) -> None:
    total_chars = sum(record.char_count for record in records)
    total_words = sum(record.word_count for record in records)
    lines = [
        "# Zotero Text Index Summary",
        "",
        f"- Created: {datetime.now().isoformat(timespec='seconds')}",
        f"- Manifest: `{manifest}`",
        f"- Index: `{output}`",
        f"- Records: {len(records)}",
        f"- Total characters: {total_chars}",
        f"- Total words: {total_words}",
        "",
        "## Schema",
        "",
        "- Zotero keys, citation keys, and bibliographic metadata",
        "- Source PDF and Markdown paths",
        "- Markdown SHA-256 and text size statistics",
        "- Full Markdown-derived text in `text`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
