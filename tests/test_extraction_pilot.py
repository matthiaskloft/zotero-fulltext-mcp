"""E2 pilot tooling: manifest selection, licence gate and draft proposals. Synthetic PDFs only.

No PDF, page image or copyrighted text is committed; PDFs are built in tmp_path with PyMuPDF.
The one test that reads the real corpus cache is opt-in (EXTRACTION_PILOT_REAL_CACHE=1).
"""

import hashlib
import json
import os
import random
import urllib.request
from pathlib import Path

import pymupdf
import pytest

from extraction import RecordError, drafts, load_record, load_reviewed_expectation, pilot

# --- synthetic sources -----------------------------------------------------------------------

FIELDS = ("physics", "chemistry", "statistics", "clinical-medicine", "law", "economics", "psychology", "education")


def _entry(i, **over):
    entry = {
        "id": f"syn-{i:03d}", "title": f"Synthetic {i}", "url": "https://example.com/x.pdf",
        "rights_source": "https://example.com/r", "license": "CC BY 4.0", "sha256": f"{i:064x}",
        "pages": 12 + i % 20, "styles": ["native text"], "field": FIELDS[i % len(FIELDS)], "language": "en",
    }
    entry.update(over)
    return entry


def _sources():
    sources = [_entry(i) for i in range(60)]
    sources[3].update(styles=["two columns"], field=None, language=None)
    sources[4].update(styles=["scanned pages"], field="extra")
    sources[5].update(styles=["scanned pages", "noisy existing OCR"], field="extra", pages=300)
    for i, lang in zip(range(6, 12), ("de", "fr", "es", "de", "fr", "es")):
        sources[i].update(language=lang, field="extra")
    return sources


# --- licence gate ----------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "CC BY 4.0", "CC-BY-4.0 (notice in PDF)", "CC BY 2.0", "CC BY (version not stated)", "CC0 1.0",
    "Creative Commons Attribution (version not stated in inspected notice)",
    "Public domain in the United States; see rights source for jurisdiction details",
    "CC BY 4.0 (article page; the PDF notice says CC BY 3.0)",
])
def test_licence_gate_accepts(text):
    assert pilot.license_gate(text) == (True, "ok")


@pytest.mark.parametrize("text", [
    "CC BY-NC 4.0", "CC BY-ND 4.0", "CC BY-SA 4.0", "CC BY-NC-ND 3.0", "Creative Commons Attribution-NonCommercial",
    "Publisher licence, all rights reserved", "", None, "see rights source", "Open access",
    "CC BY 4.0 (article; the described dataset is CC BY-NC 4.0 and is not part of this corpus)",
])
def test_licence_gate_rejects(text):
    ok, reason = pilot.license_gate(text)
    assert not ok and reason


def test_every_real_source_licence_is_decided_explicitly():
    # Only the one entry whose note mentions an NC dataset may fail; anything new must be reviewed.
    rejected = {s["id"] for s in pilot.load_sources() if not pilot.license_gate(s["license"])[0]}
    assert rejected == {"johd-conlit-dataset"}


# --- manifest selection ----------------------------------------------------------------------


def test_manifest_is_deterministic_and_order_independent():
    sources = _sources()
    first = pilot.render_manifest(pilot.build_manifest(sources))
    shuffled = sources[:]
    random.Random(1).shuffle(shuffled)
    assert pilot.render_manifest(pilot.build_manifest(shuffled)) == first
    assert pilot.render_manifest(pilot.build_manifest(sources, seed="another-seed")) != first


def test_pilot_and_held_out_are_disjoint_and_meet_quotas():
    manifest = pilot.build_manifest(_sources())
    pilot_ids = {d["id"] for d in manifest["pilot"]}
    held_ids = {d["id"] for d in manifest["held_out"]}
    assert pilot_ids and held_ids and not pilot_ids & held_ids
    assert pilot.validate_manifest(manifest, _sources()) == []
    assert sum(d.get("page_quota", 0) for d in manifest["pilot"]) == 20
    assert sum(d["role"] == "whole_paper" for d in manifest["pilot"]) == pilot.PILOT_WHOLE_PAPERS
    assert manifest["unfilled"] == []


def test_layout_strata_come_first_and_metadata_beats_assumption():
    manifest = pilot.build_manifest(_sources())
    by_stratum = {s: [d for d in manifest["pilot"] if d["stratum"] == s] for s in pilot.STRATA}
    assert all(by_stratum[s] for s in pilot.STRATA)
    assert by_stratum["two-column"][0]["id"] == "syn-003" and by_stratum["two-column"][0]["basis"] == "metadata"
    assert {d["language"] for d in by_stratum["non-english"]} == {"de", "fr", "es"}
    assert all(d["basis"] == "metadata" for d in by_stratum["scans"] + by_stratum["non-english"])


def test_ineligible_entries_are_excluded_and_reported_with_reason():
    sources = _sources()
    sources[3]["license"] = "CC BY-NC 4.0"  # the only metadata-verified two-column entry
    sources[8]["license"] = "Publisher licence"
    sources[9]["license"] = ""
    manifest = pilot.build_manifest(sources)
    excluded = {e["id"]: e["reason"] for e in manifest["excluded"]}
    assert set(excluded) == {"syn-003", "syn-008", "syn-009"} and all(excluded.values())
    selected = {d["id"] for d in manifest["pilot"] + manifest["held_out"]}
    assert not selected & set(excluded)


def test_shortfall_is_reported_not_hidden():
    sources = [s for s in _sources() if s["id"] != "syn-005"]  # leaves one scan for two sets
    manifest = pilot.build_manifest(sources)
    assert {"set": "held_out", "stratum": "scans", "wanted": 1, "found": 0} in manifest["unfilled"]


def test_selection_inputs_hash_ignores_labels_but_not_licences():
    sources = _sources()
    base = pilot.selection_inputs_sha256(sources)
    sources[0]["expected_quality"] = "good"
    assert pilot.selection_inputs_sha256(sources) == base
    sources[0]["license"] = "CC BY-NC 4.0"
    assert pilot.selection_inputs_sha256(sources) != base


def test_validate_manifest_flags_overlap_and_ineligible():
    sources = _sources()
    manifest = pilot.build_manifest(sources)
    overlapping = json.loads(json.dumps(manifest))
    overlapping["held_out"].append(dict(overlapping["pilot"][0]))
    assert any("both the pilot and the held-out" in p for p in pilot.validate_manifest(overlapping, sources))
    target = manifest["pilot"][0]["id"]
    next(s for s in sources if s["id"] == target)["license"] = "CC BY-NC 4.0"
    assert any("fails the licence gate" in p for p in pilot.validate_manifest(manifest, sources))


# --- the committed manifest --------------------------------------------------------------------


def test_committed_manifest_is_pinned_and_regenerates_identically():
    assert pilot.check_manifest() == []
    manifest = json.loads(pilot.MANIFEST_PATH.read_bytes())
    assert [e["id"] for e in manifest["excluded"]] == ["johd-conlit-dataset"]
    assert manifest["unfilled"] == []
    assert not {d["id"] for d in manifest["pilot"]} & {d["id"] for d in manifest["held_out"]}


def test_manifest_drift_is_detected(tmp_path):
    manifest_copy, pin_copy = tmp_path / "m.json", tmp_path / "m.sha256"
    data = pilot.MANIFEST_PATH.read_bytes()
    manifest_copy.write_bytes(data)
    pin_copy.write_bytes(pilot.PIN_PATH.read_bytes())
    assert pilot.check_manifest(manifest_copy, pin_copy) == []
    manifest_copy.write_bytes(data.replace(b'"seed": "e2-pilot-2026-11"', b'"seed": "e2-pilot-2026-12"'))
    problems = pilot.check_manifest(manifest_copy, pin_copy)
    assert any("pin" in p for p in problems) and any("regeneration" in p for p in problems)


def test_crlf_checkout_still_verifies(tmp_path):
    crlf = tmp_path / "m.json"
    crlf.write_bytes(pilot.MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert pilot.check_manifest(crlf, pilot.PIN_PATH) == []


# --- draft proposals (host step, synthetic PDFs) ---------------------------------------------

LOREM = "Synthetic filler sentence for layout checks only. "


def _two_column_page(page):
    for x in (40, 320):
        for row in range(4):
            page.insert_textbox(pymupdf.Rect(x, 60 + row * 150, x + 240, 190 + row * 150), LOREM * 6, fontsize=9)


def _table_page(page):
    x0, y0, cell_w, cell_h = 60, 100, 100, 30
    for r in range(5):
        for c in range(4):
            rect = pymupdf.Rect(x0 + c * cell_w, y0 + r * cell_h, x0 + (c + 1) * cell_w, y0 + (r + 1) * cell_h)
            page.draw_rect(rect, color=(0, 0, 0), width=1)
            page.insert_textbox(rect + (3, 3, -3, -3), f"v{r}{c}", fontsize=9)


def _make_pdf(path: Path, *, rotate=0, kind="two-column", pages=3):
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=612, height=792)
        if number == 0:
            (_two_column_page if kind == "two-column" else _table_page)(page)
        else:
            page.insert_text((72, 100), f"Plain page {number}", fontsize=11)
    if rotate:
        doc[0].set_rotation(rotate)
    doc.save(path)
    doc.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_for(doc_id, digest, *, role="stratum_pages", stratum="two-column", quota=1):
    doc = {"id": doc_id, "role": role, "stratum": stratum if role == "stratum_pages" else None,
           "basis": "assumed" if role == "stratum_pages" else None, "pdf_sha256": digest}
    if role == "stratum_pages":
        doc["page_quota"] = quota
    return {"seed": "test-seed", "pilot": [doc], "held_out": []}


@pytest.fixture
def dirs(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache, tmp_path / "out"


def test_draft_is_a_valid_unreviewed_expectation_with_render_identity(dirs):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-a.pdf")
    report = drafts.run(_manifest_for("syn-a", digest), cache, out, titles={"syn-a": "Synthetic A"})
    assert report["problems"] == []
    path = out / "drafts" / "pilot-syn-a.expectation.draft.json"
    record = load_record(path, kind="expectation")
    assert record["provenance"]["review"] == {"status": "draft"}
    assert record["provenance"]["proposed_by"]["tool"] == drafts.TOOL_NAME
    assert record["document"]["pdf_sha256"] == digest and record["document"]["title"] == "Synthetic A"
    assert record["scope"] == {"kind": "pages", "physical_pages": [1]}
    page = record["pages"][0]
    image = out / "images" / "syn-a-p0001.png"
    assert page["render"]["dpi"] == drafts.DEFAULT_DPI
    assert page["render"]["image_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert page["geometry"]["frame"] == "displayed" and page["geometry"]["rotation"] == 0
    assert record["blocks"] and all(b["type"] == "other" and b["uncertainty"]["level"] == "high" for b in record["blocks"])
    with pytest.raises(RecordError, match="not reviewed"):
        load_reviewed_expectation(path)


def test_flipping_status_alone_does_not_make_a_draft_reviewed(dirs, tmp_path):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-a.pdf")
    drafts.run(_manifest_for("syn-a", digest), cache, out)
    record = json.loads((out / "drafts" / "pilot-syn-a.expectation.draft.json").read_text(encoding="utf-8"))
    record["provenance"]["review"]["status"] = "reviewed"
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(RecordError):
        load_reviewed_expectation(forged)  # no reviewer, basis or hashes: the schema refuses it


@pytest.mark.parametrize("rotation", [90, 270])
def test_rotated_page_geometry_is_read_separately_and_declared_displayed(dirs, rotation):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-r.pdf", rotate=rotation)
    drafts.run(_manifest_for("syn-r", digest, role="whole_paper"), cache, out)
    record = load_record(out / "drafts" / "pilot-syn-r-whole.expectation.draft.json", kind="expectation")
    geometry = record["pages"][0]["geometry"]
    assert geometry["rotation"] == rotation and geometry["frame"] == "displayed"
    assert (geometry["width"], geometry["height"]) == (792, 612)  # displayed dimensions, rotation applied


def test_missing_and_hash_mismatched_pdfs_are_reported_not_skipped(dirs, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("the draft tool must never download"))
    cache, out = dirs
    digest = _make_pdf(cache / "syn-bad.pdf")
    manifest = _manifest_for("syn-bad", "0" * 64)
    manifest["pilot"].append({"id": "syn-gone", "role": "stratum_pages", "stratum": "tables", "basis": "assumed", "pdf_sha256": digest, "page_quota": 1})
    report = drafts.run(manifest, cache, out)
    statuses = {d["id"]: d["status"] for d in report["documents"]}
    assert statuses == {"syn-bad": "hash_mismatch", "syn-gone": "missing"}
    assert len(report["problems"]) == 2
    assert not list((out / "drafts").glob("*.json"))
    assert json.loads((out / "selection_report.json").read_text(encoding="utf-8"))["problems"]


def test_unsupported_stratum_is_reported_instead_of_picking_an_arbitrary_page(dirs):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-a.pdf", kind="two-column")
    report = drafts.run(_manifest_for("syn-a", digest, stratum="tables"), cache, out)
    assert report["documents"][0]["status"] == "stratum_unsupported"
    assert report["problems"] and not list((out / "drafts").glob("*.json"))


def test_layout_checks_distinguish_synthetic_pages(tmp_path):
    cols, tables = tmp_path / "c.pdf", tmp_path / "t.pdf"
    _make_pdf(cols, kind="two-column")
    _make_pdf(tables, kind="tables")
    with pymupdf.open(cols) as c, pymupdf.open(tables) as t:
        assert drafts.page_support("two-column", c[0]) and not drafts.page_support("two-column", c[1])
        assert drafts.page_support("tables", t[0]) and not drafts.page_support("tables", c[0])
        assert not drafts.page_support("scans", c[0])
        assert not drafts.page_support("references", c[0])


def test_reference_pages_start_at_the_reference_heading(tmp_path):
    path = tmp_path / "refs.pdf"
    doc = pymupdf.open()
    years = "\n".join(f"Author {i}. Title of work {i}. Journal, {2000 + i}." for i in range(6))
    for text in (years, years, "References\n" + years, years):
        doc.new_page(width=612, height=792).insert_text((72, 80), text, fontsize=10)
    doc.new_page(width=612, height=792).insert_text((72, 80), "Appendix", fontsize=10)
    doc.save(path)
    nohead = tmp_path / "nohead.pdf"
    other = pymupdf.open()
    other.new_page(width=612, height=792).insert_text((72, 80), years, fontsize=10)
    other.save(nohead)
    with pymupdf.open(path) as saved, pymupdf.open(nohead) as bare:
        assert drafts.choose_pages(saved, "references", 5, "s", "x") == ([3, 4], 2)
        assert drafts.choose_pages(bare, "references", 1, "s", "x") == ([], 0)


def test_scan_stratum_detects_full_page_image(tmp_path):
    path = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 60, 80), False)
    pix.clear_with(200)
    page.insert_image(page.rect, pixmap=pix)
    doc.new_page(width=300, height=400)
    doc.save(path)
    with pymupdf.open(path) as saved:
        assert drafts.choose_pages(saved, "scans", 2, "s", "x") == ([1], 1)


def test_page_choice_is_deterministic_and_seeded(tmp_path):
    path = tmp_path / "many.pdf"
    doc = pymupdf.open()
    for _ in range(12):
        _two_column_page(doc.new_page(width=612, height=792))
    doc.save(path)
    with pymupdf.open(path) as saved:
        first = drafts.choose_pages(saved, "two-column", 3, "seed-1", "x")
        assert first == drafts.choose_pages(saved, "two-column", 3, "seed-1", "x")
        assert first[1] == 12
        assert any(drafts.choose_pages(saved, "two-column", 3, f"seed-{i}", "x")[0] != first[0] for i in range(2, 8))


def test_whole_paper_draft_covers_every_page(dirs):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-w.pdf", pages=3)
    drafts.run(_manifest_for("syn-w", digest, role="whole_paper"), cache, out)
    record = load_record(out / "drafts" / "pilot-syn-w-whole.expectation.draft.json", kind="expectation")
    assert record["scope"] == {"kind": "complete_document", "physical_pages": [1, 2, 3]}


def test_output_inside_the_repository_is_refused():
    with pytest.raises(SystemExit):
        drafts.main(["--out", str(pilot.REPO_ROOT / "benchmarks" / "oops")])


def test_default_output_is_outside_the_repository():
    assert not drafts._is_inside(drafts.DEFAULT_OUT, pilot.REPO_ROOT)


def test_changed_manifest_is_refused_by_the_host_tool(tmp_path):
    changed = tmp_path / "m.json"
    changed.write_bytes(pilot.MANIFEST_PATH.read_bytes() + b" ")
    with pytest.raises(SystemExit):
        drafts.main(["--manifest", str(changed), "--out", str(tmp_path / "out")])


@pytest.mark.skipif(os.environ.get("EXTRACTION_PILOT_REAL_CACHE") != "1", reason="opt-in: needs the local corpus cache")
def test_real_cache_status_is_reported_for_every_pilot_document():
    manifest = json.loads(pilot.MANIFEST_PATH.read_bytes())
    for doc in manifest["pilot"]:
        status, _, detail = drafts.verify_cached(doc, drafts.DEFAULT_CACHE)
        assert status in {"ok", "missing", "hash_mismatch"}, detail
