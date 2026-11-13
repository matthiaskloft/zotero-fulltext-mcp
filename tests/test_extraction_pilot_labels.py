"""Committed reviewed pilot expectations (E2): every record must load through the pilot loader.

The records were reviewed by an AI agent, not a human (see docs/extraction-pilot-review.md). The
test checks structure and manifest consistency; it cannot check transcription fidelity. With the
public PDF cache present (EXTRACTION_PILOT_PDF_CACHE, or the default corpus cache) the pinned PDF
hashes, page geometry and render hashes are also verified when the installed PyMuPDF matches the
recording version.
"""

import json
import os
import re
from pathlib import Path

import pymupdf
import pytest

from extraction import pilot, pilot_records
from extraction.records import load_reviewed_expectation

LABEL_DIR = pilot.REPO_ROOT / "benchmarks" / "extraction" / "expectations" / "pilot"
RECORDS = sorted(LABEL_DIR.glob("*.expectation.json"))
MANIFEST = json.loads(pilot.MANIFEST_PATH.read_bytes())


def _cache():
    cache = Path(os.environ.get("EXTRACTION_PILOT_PDF_CACHE", pilot.REPO_ROOT / "benchmarks" / "public_pdfs" / ".cache"))
    return cache if cache.is_dir() else None


def test_reviewed_pilot_records_are_committed():
    assert RECORDS, "no reviewed pilot expectations found"


@pytest.mark.parametrize("path", RECORDS, ids=lambda p: p.name)
def test_record_loads_and_belongs_to_the_pilot(path):
    record = load_reviewed_expectation(path)
    review = record["provenance"]["review"]
    assert review["status"] == "reviewed" and review["basis"] == "page_image_inspection"
    pilot_ids = {d["id"] for d in MANIFEST["pilot"]}
    held_out_ids = {d["id"] for d in MANIFEST["held_out"]}
    corpus_id = record["document"]["corpus_id"]
    assert corpus_id in pilot_ids and corpus_id not in held_out_ids
    assert pilot_records.check_pilot_expectation(record, MANIFEST) == []


@pytest.mark.parametrize("path", RECORDS, ids=lambda p: p.name)
def test_record_matches_pdf_cache_when_available(path):
    cache = _cache()
    record = load_reviewed_expectation(path)
    if cache is None or not (cache / f"{record['document']['corpus_id']}.pdf").is_file():
        pytest.skip("public PDF cache not available")
    versions = {p["render"]["renderer_version"] for p in record["pages"]}
    if versions != {pymupdf.VersionBind}:
        pytest.skip(f"records were rendered with PyMuPDF {sorted(versions)}, installed {pymupdf.VersionBind}")
    assert pilot_records.check_pilot_expectation(record, MANIFEST, cache) == []


# A hyphen that exists only because a word was split at a line break is layout, not transcription, so
# the labels join it (`critical`, not `criti- cal`) and keep a real hyphen without a space
# (`high-quality`). The only `letter- ` sequences left are listed here: suspended hyphens in a
# coordination, where the space is real, and the Stam table cells, where the page itself prints a
# space after the hyphen inside a single-line cell (checked on the page image; not a line break).
SUSPENDED_HYPHENS = {
    ("pilot-natcomm-metascape-whole", "p2-b12", "single- and"),
    ("pilot-natcomm-metascape-whole", "p9-b28", "Meta- and"),
    ("pilot-natcomm-metascape-whole", "ref9", "Meta- and"),
    ("pilot-plosmed-research-findings-false", "p5-b3", "pre- and"),
}
PRINTED_SPACE_CELLS = {
    ("pilot-stam-iron-pnictides", "p6-b6", f"{prefix}- Ca10(Ir4As8)(Fe2As2)5")
    for prefix in ("Ge", "P", "Re", "K", "Sr", "Ba")
}
_LINE_HYPHEN = re.compile(r"\w+- (?=\S)\S*")


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _label_strings(record):
    for block in record["blocks"]:
        if isinstance(block.get("text"), str):
            yield block["id"], block["text"]
        for cell in (block.get("table") or {}).get("cells", []):
            yield block["id"], cell["text"]
    for entry in (record.get("references") or {}).get("entries", []):
        yield entry["id"], entry["raw"]
        for text in _walk_strings({"fields": entry.get("fields"), "doi": entry.get("doi")}):
            yield entry["id"], text


def test_labels_carry_no_line_break_hyphens():
    found = set()
    for path in RECORDS:
        record = json.loads(path.read_bytes())
        for where, text in _label_strings(record):
            for match in _LINE_HYPHEN.finditer(text):
                token = match.group(0)
                if (record["record_id"], where, token) not in PRINTED_SPACE_CELLS:
                    token = " ".join(token.split(" ")[:2])
                found.add((record["record_id"], where, token))
    allowed = SUSPENDED_HYPHENS | PRINTED_SPACE_CELLS
    assert found == allowed, (sorted(found - allowed), sorted(allowed - found))
