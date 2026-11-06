"""Load path for reviewed pilot expectations: ``load_reviewed_expectation`` plus pilot-specific checks.

A draft can be turned into a ``reviewed`` record by editing a few fields, so this loader adds what
the generic schema cannot know:

- the draft wording and text-layer proposal notes must be gone (the reviewer cleaned them up);
- a record with no blocks, or only ``other`` blocks, is refused unless it carries an explicit
  attestation that the scoped pages hold no content;
- the record must belong to the committed manifest: document hash, scope (the manifest's candidate
  pages, or the complete document for a whole paper) and, when the PDF cache is supplied, the
  pinned PDF hash and the page render hashes.

Nothing here proves a human looked at the pages; it removes the ways a mechanical promotion goes
unnoticed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .pilot import DRAFT_NOTE, PROPOSAL_BLOCK_NOTE
from .records import RecordError, load_reviewed_expectation

# The expectation schema has no field for this, so it is a fixed prefix of provenance.uncertainty.note.
EMPTY_PAGES_ATTESTATION = "reviewed: scoped pages contain no content blocks"


def check_pilot_expectation(record: dict[str, Any], manifest: dict[str, Any], cache: Path | None = None) -> list[str]:
    """Findings (empty = acceptable) for a schema-valid reviewed expectation, against the manifest."""
    errors: list[str] = []
    provenance = record["provenance"]
    note = provenance.get("uncertainty", {}).get("note", "")
    if DRAFT_NOTE in note or "becomes ground truth only through human review" in note:
        errors.append("provenance still carries the draft note; remove it when review is complete")
    if any(PROPOSAL_BLOCK_NOTE in block.get("uncertainty", {}).get("note", "") for block in record["blocks"]):
        errors.append("a block still carries the unreviewed text-layer proposal note")
    blocks = record["blocks"]
    attested = note.startswith(EMPTY_PAGES_ATTESTATION)
    if not attested and (not blocks or all(b["type"] == "other" for b in blocks)):
        errors.append(
            "reviewed record has no typed blocks (none, or only 'other'); label the page content or, if the pages "
            f"are truly empty, start provenance.uncertainty.note with {EMPTY_PAGES_ATTESTATION!r}")

    corpus_id = record["document"].get("corpus_id")
    entries = [d for name in ("pilot", "held_out") for d in manifest[name] if d["id"] == corpus_id]
    if not entries:
        return [*errors, f"document corpus_id {corpus_id!r} is not in the manifest"]
    entry = entries[0]
    if record["document"]["pdf_sha256"] != entry["pdf_sha256"]:
        errors.append("document.pdf_sha256 differs from the manifest pin")
    scope = record["scope"]
    if entry["role"] == "whole_paper":
        if scope["kind"] != "complete_document":
            errors.append("a whole paper must be recorded as complete_document")
    elif entry.get("candidate_pages") is not None and not set(scope["physical_pages"]) <= set(entry["candidate_pages"]):
        errors.append(f"scope pages {scope['physical_pages']} are outside the manifest's candidate pages {entry['candidate_pages']}")
    if record["document"]["page_count"] != entry["pdf_pages"]:
        errors.append("document.page_count differs from the manifest")
    if cache is not None:
        errors.extend(_check_against_cache(record, entry, cache))
    return errors


def _check_against_cache(record: dict[str, Any], entry: dict[str, Any], cache: Path) -> list[str]:
    import pymupdf

    path = cache / f"{entry['id']}.pdf"
    if not path.is_file():
        return [f"{entry['id']}: PDF not in cache; cannot verify hashes"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["pdf_sha256"]:
        return [f"{entry['id']}: cached PDF does not match the pinned hash"]
    errors: list[str] = []
    with pymupdf.open(path) as doc:
        for page in record["pages"]:
            render = page["render"]
            if render["renderer"] != "pymupdf" or render["renderer_version"] != pymupdf.VersionBind:
                continue  # another renderer or version cannot reproduce the bytes; the hash stays the reviewer's record
            work = doc[page["physical_page"] - 1]
            work.remove_rotation()
            png = work.get_pixmap(dpi=render["dpi"], alpha=False).tobytes("png")
            if hashlib.sha256(png).hexdigest() != render["image_sha256"]:
                errors.append(f"page {page['physical_page']}: render hash does not match a fresh render of the pinned PDF")
    return errors


def load_pilot_expectation(path: str | Path, manifest: dict[str, Any], cache: Path | None = None) -> dict[str, Any]:
    """Load a reviewed pilot expectation or raise ``RecordError`` listing every finding."""
    record = load_reviewed_expectation(path)
    errors = check_pilot_expectation(record, manifest, cache)
    if errors:
        raise RecordError(errors)
    return record
