"""Committed reviewed pilot expectations (E2): every record must load through the pilot loader.

The records were reviewed by an AI agent, not a human (see docs/extraction-pilot-review.md). The
test checks structure and manifest consistency; it cannot check transcription fidelity. With the
public PDF cache present (EXTRACTION_PILOT_PDF_CACHE, or the default corpus cache) the pinned PDF
hashes, page geometry and render hashes are also verified when the installed PyMuPDF matches the
recording version.
"""

import json
import os
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
