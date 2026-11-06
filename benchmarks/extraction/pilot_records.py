"""Load path for reviewed pilot expectations: ``load_reviewed_expectation`` plus pilot-specific checks.

A draft can be turned into a ``reviewed`` record by editing a few fields, so this loader adds what
the generic schema cannot know:

- the draft wording and text-layer proposal notes must be gone (the reviewer cleaned them up);
- a record with no blocks, or only ``other`` blocks, is refused. The one exception is an explicit
  empty-page attestation, which is accepted only for a scan-stratum record with no blocks at all
  and, because it is checked against the PDF, only when a cache is supplied and the scoped pages
  have no text layer; it is refused for every other stratum and for whole papers;
- the record must belong to the committed manifest: document hash, page count and scope (the
  manifest's pinned candidate pages, or the complete document for a whole paper);
- given the PDF cache: the pinned PDF hash, page geometry and render hashes must reproduce. A render
  that cannot be reproduced (different renderer or version) is reported, never silently skipped.

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
_GEOMETRY_TOLERANCE = 0.01


def check_pilot_expectation(record: dict[str, Any], manifest: dict[str, Any], cache: Path | None = None) -> list[str]:
    """Findings (empty = acceptable) for a schema-valid reviewed expectation, against the manifest."""
    errors: list[str] = []
    provenance = record["provenance"]
    note = provenance.get("uncertainty", {}).get("note", "")
    if DRAFT_NOTE in note or "becomes ground truth only through human review" in note:
        errors.append("provenance still carries the draft note; remove it when review is complete")
    blocks = record["blocks"]
    if any(PROPOSAL_BLOCK_NOTE in block.get("uncertainty", {}).get("note", "") for block in blocks):
        errors.append("a block still carries the unreviewed text-layer proposal note")
    attested = note.startswith(EMPTY_PAGES_ATTESTATION)

    corpus_id = record["document"].get("corpus_id")
    entries = [d for name in ("pilot", "held_out") for d in manifest[name] if d["id"] == corpus_id]
    if not entries:
        return [*errors, f"document corpus_id {corpus_id!r} is not in the manifest"]
    entry = entries[0]

    if attested:
        errors.extend(_attestation_findings(record, entry, cache))
    elif not blocks or all(b["type"] == "other" for b in blocks):
        errors.append(
            "reviewed record has no typed blocks (none, or only 'other'); label the page content "
            "(an empty-page attestation exists only for confirmed empty scan pages, see docs/extraction-pilot-review.md)")

    if record["document"]["pdf_sha256"] != entry["pdf_sha256"]:
        errors.append("document.pdf_sha256 differs from the manifest pin")
    if record["document"]["page_count"] != entry["pdf_pages"]:
        errors.append("document.page_count differs from the manifest")
    scope = record["scope"]
    if entry["role"] == "whole_paper":
        if scope["kind"] != "complete_document":
            errors.append("a whole paper must be recorded as complete_document")
    elif scope["kind"] != "pages" or (
        entry.get("candidate_pages") is not None and not set(scope["physical_pages"]) <= set(entry["candidate_pages"])
    ):
        errors.append(f"scope {scope['physical_pages']} is outside the manifest's pinned candidate pages {entry.get('candidate_pages')}")
    if cache is not None:
        errors.extend(_check_against_cache(record, entry, cache))
    return errors


def _attestation_findings(record: dict[str, Any], entry: dict[str, Any], cache: Path | None) -> list[str]:
    if record["blocks"]:
        return ["the empty-page attestation requires a record with no blocks at all"]
    if entry["role"] != "stratum_pages" or entry["stratum"] != "scans":
        return ["the empty-page attestation is refused for this record: it is accepted only for scan-stratum pages"]
    if cache is None:
        return ["the empty-page attestation can only be accepted with the PDF cache, to confirm there is no text layer"]
    import pymupdf

    path = cache / f"{entry['id']}.pdf"
    if not path.is_file():
        return [f"{entry['id']}: PDF not in cache; cannot confirm the pages have no text layer"]
    findings = []
    with pymupdf.open(path) as doc:
        for number in record["scope"]["physical_pages"]:
            if doc[number - 1].get_text().strip():
                findings.append(f"page {number} has a text layer, so it cannot be attested as empty")
    return findings


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
            number = page["physical_page"]
            work = doc[number - 1]
            rotation = work.rotation
            work.remove_rotation()
            geometry = page["geometry"]
            if geometry["rotation"] != rotation:
                errors.append(f"page {number}: recorded rotation {geometry['rotation']} differs from the PDF's {rotation}")
            if geometry["frame"] == "displayed" and geometry["unit"] == "pt" and (
                abs(geometry["width"] - work.rect.width) > _GEOMETRY_TOLERANCE
                or abs(geometry["height"] - work.rect.height) > _GEOMETRY_TOLERANCE
            ):
                errors.append(f"page {number}: recorded size differs from the PDF's displayed page size")
            render = page["render"]
            if render["renderer"] != "pymupdf" or render["renderer_version"] != pymupdf.VersionBind:
                errors.append(
                    f"page {number}: render hash cannot be reproduced here (recorded {render['renderer']} "
                    f"{render['renderer_version']}, available pymupdf {pymupdf.VersionBind}); verify it with the recording version")
                continue
            png = work.get_pixmap(dpi=render["dpi"], alpha=False).tobytes("png")
            if hashlib.sha256(png).hexdigest() != render["image_sha256"]:
                errors.append(f"page {number}: render hash does not match a fresh render of the pinned PDF")
    return errors


def load_pilot_expectation(path: str | Path, manifest: dict[str, Any], cache: Path | None = None) -> dict[str, Any]:
    """Load a reviewed pilot expectation or raise ``RecordError`` listing every finding."""
    record = load_reviewed_expectation(path)
    errors = check_pilot_expectation(record, manifest, cache)
    if errors:
        raise RecordError(errors)
    return record
