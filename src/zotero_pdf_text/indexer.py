from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .identity import strip_front_matter


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
    # Provenance of the source PDF this text was extracted from, and when this record was built.
    # Both default to "" so records constructed by older callers stay valid; an empty
    # `source_sha256` means "not known" and an audit reports it as unverifiable rather than
    # unchanged. See `converter.ConversionResult.source_sha256` for why it can legitimately be
    # absent even on a successful pipeline run.
    source_sha256: str = ""
    indexed_at: str = ""


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
                if not isinstance(record, dict):
                    continue
                key = record.get("zotero_attachment_key", "")
                if key:
                    keys.add(key)
    return keys


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
    text = strip_front_matter(markdown)
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
        # Carried through from the conversion manifest, never recomputed here. Re-hashing the PDF
        # at index time would record today's file against text extracted from an older one --
        # confident-looking provenance that is wrong in exactly the case `source_changed` exists
        # to catch. A manifest predating this field yields "", i.e. not known.
        source_sha256=row.get("source_sha256", ""),
        indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
