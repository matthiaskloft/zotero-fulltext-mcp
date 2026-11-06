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

from extraction import RecordError, drafts, load_record, load_reviewed_expectation, pilot, pilot_records

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
    "cc by-nc 4.0", "cc-by-sa-4.0", "CC BY-nd", "CC BY Non Commercial 4.0",
    "Creative Commons Attribution Share Alike 4.0", "CC BY 4.0, copyright Elsevier",
    "CC BY 4.0 except third-party material", "Public domain?",
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
    assert {"set": "held_out", "stratum": "scans", "unit": "documents", "wanted": 1, "found": 0} in manifest["unfilled"]


class _FakeChecks(pilot.PageChecks):
    """Page checks driven by a table, standing in for cached PDFs."""

    def __init__(self, supported, no_references=()):
        self.supported, self.no_references = supported, set(no_references)

    def pages(self, entry, stratum, wanted):
        pages = self.supported.get((entry["id"], stratum), [])
        return pages[:wanted], len(pages)

    def reference_list_found(self, entry):
        return entry["id"] not in self.no_references


def test_page_checks_replace_failed_documents_by_rank_and_fill_pages():
    sources = _sources()
    everywhere = {(s["id"], st): [2, 4, 6] for s in sources for st in pilot.STRATA}
    first = pilot.build_manifest(sources, checks=_FakeChecks(everywhere))
    assert sum(len(d["candidate_pages"]) for d in first["pilot"] if d["stratum"]) == 20 and first["unfilled"] == []
    failing = first["pilot"][0]["id"]
    broken = {k: v for k, v in everywhere.items() if k[0] != failing}
    second = pilot.build_manifest(sources, checks=_FakeChecks(broken))
    assert failing not in {d["id"] for d in second["pilot"] if d["stratum"]}
    assert any(r["id"] == failing and r["set"] == "pilot" for r in second["page_check_rejected"])
    assert sum(len(d["candidate_pages"]) for d in second["pilot"] if d["stratum"]) == 20


def test_shortfall_in_pages_is_reported_exactly():
    sources = _sources()
    table_ids = [s["id"] for s in sources if "tables" in pilot.candidate_strata(s)]
    only_two = {(i, "tables"): [3] for i in table_ids[:2]}
    manifest = pilot.build_manifest(sources, checks=_FakeChecks(only_two))
    short = {(u["set"], u["stratum"]): (u["wanted"], u["found"], u["unit"]) for u in manifest["unfilled"]}
    assert short[("pilot", "tables")] == (4, 2, "pages")


def test_whole_papers_need_a_found_reference_list():
    sources = _sources()
    everywhere = {(s["id"], st): [2] for s in sources for st in pilot.STRATA}
    checks = _FakeChecks(everywhere)
    ids = [d["id"] for d in pilot.build_manifest(sources, checks=checks)["pilot"] if d["role"] == "whole_paper"]
    assert ids
    manifest = pilot.build_manifest(sources, checks=_FakeChecks(everywhere, no_references=ids))
    assert not set(ids) & {d["id"] for d in manifest["pilot"] if d["role"] == "whole_paper"}
    assert all(d["reference_list_found"] for d in manifest["pilot"] if d["role"] == "whole_paper")
    assert {r["id"] for r in manifest["page_check_rejected"]} >= set(ids)


def test_replay_regenerates_a_checked_manifest_without_pdfs():
    sources = _sources()
    everywhere = {(s["id"], st): [2, 4] for s in sources for st in pilot.STRATA}
    manifest = pilot.build_manifest(sources, checks=_FakeChecks(everywhere))
    replayed = pilot.build_manifest(sources, checks=pilot.ReplayChecks(manifest))
    assert pilot.render_manifest(replayed) == pilot.render_manifest(manifest)


def test_styles_equations_and_tables_are_metadata_basis():
    entry = _entry(0, styles=["native text", "equations", "tables"])
    assert pilot.candidate_strata(entry)["equations"] == "metadata"
    assert pilot.candidate_strata(entry)["tables"] == "metadata"


def test_same_work_under_another_id_never_crosses_sets():
    sources = _sources()
    for i, dup in ((20, 21), (22, 23), (24, 25)):  # same DOI, same title, same PDF hash
        key = ("doi", "10.1/x") if i == 20 else ("title", "Same Title") if i == 22 else ("sha256", "f" * 64)
        sources[i][key[0]] = sources[dup][key[0]] = key[1]
    manifest = pilot.build_manifest(sources)
    assert pilot.validate_manifest(manifest, sources) == []
    pilot_keys = set().union(*(pilot.identity_keys(s) for s in sources if s["id"] in {d["id"] for d in manifest["pilot"]}))
    held_keys = set().union(*(pilot.identity_keys(s) for s in sources if s["id"] in {d["id"] for d in manifest["held_out"]}))
    assert not pilot_keys & held_keys
    forced = json.loads(json.dumps(manifest))
    forced["held_out"].append(dict(forced["pilot"][0], id="syn-021"))
    sources[21]["sha256"] = forced["pilot"][0]["pdf_sha256"]
    assert any("same work" in p for p in pilot.validate_manifest(forced, sources))


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
    assert any("pin" in p for p in problems) and any("regeneration" in p or "selection changed" in p for p in problems)


def test_crlf_checkout_still_verifies(tmp_path):
    crlf = tmp_path / "m.json"
    crlf.write_bytes(pilot.MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert pilot.check_manifest(crlf, pilot.PIN_PATH) == []




# --- draft proposals (host step, synthetic PDFs) ---------------------------------------------

LOREM = "Synthetic filler sentence for layout checks only. "
YEARS = "\n".join(f"Author {i}. Title of work {i}. Journal, {2000 + i}." for i in range(6))


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


def _make_pdf(path: Path, *, rotate=0, kind="two-column", pages=3, references=False, crop=False):
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=612, height=792)
        if number == 0:
            (_two_column_page if kind == "two-column" else _table_page)(page)
        else:
            page.insert_text((72, 100), f"Plain page {number}", fontsize=11)
    if references:
        doc.new_page(width=612, height=792).insert_text((72, 80), "References\n" + YEARS, fontsize=10)
    if crop:
        doc[0].set_cropbox(pymupdf.Rect(20, 20, 500, 700))
    if rotate:
        doc[0].set_rotation(rotate)
    doc.save(path)
    doc.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_for(doc_id, digest, *, role="stratum_pages", stratum="two-column", pages=(1,), pdf_pages=3):
    doc = {"id": doc_id, "role": role, "stratum": stratum if role == "stratum_pages" else None,
           "basis": "assumed" if role == "stratum_pages" else None, "pdf_sha256": digest, "pdf_pages": pdf_pages}
    if role == "stratum_pages":
        doc["candidate_pages"] = list(pages)
    return {"seed": "test-seed", "pilot": [doc], "held_out": []}


@pytest.fixture
def dirs(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache, tmp_path / "out"


def _draft(cache, out, name="syn-a", **options):
    digest = _make_pdf(cache / f"{name}.pdf")
    manifest = _manifest_for(name, digest)
    report = drafts.run(manifest, cache, out, titles={name: "Synthetic A"}, **options)
    return manifest, report, out / "drafts" / f"pilot-{name}.expectation.draft.json"


def test_draft_is_a_valid_unreviewed_expectation_with_render_identity(dirs):
    cache, out = dirs
    manifest, report, path = _draft(cache, out)
    assert report["problems"] == []
    record = load_record(path, kind="expectation")
    assert record["provenance"]["review"] == {"status": "draft"}
    assert record["provenance"]["proposed_by"]["tool"] == drafts.TOOL_NAME
    assert record["document"]["pdf_sha256"] == manifest["pilot"][0]["pdf_sha256"] and record["document"]["title"] == "Synthetic A"
    assert record["scope"] == {"kind": "pages", "physical_pages": [1]}
    page = record["pages"][0]
    image = out / "images" / "syn-a-p0001.png"
    assert page["render"]["dpi"] == drafts.DEFAULT_DPI
    assert page["render"]["image_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert page["geometry"]["frame"] == "displayed" and page["geometry"]["rotation"] == 0
    assert record["blocks"] == []  # no proposals by default: nothing to anchor the reviewer
    with pytest.raises(RecordError, match="not reviewed"):
        load_reviewed_expectation(path)


def test_text_proposals_are_opt_in_and_marked(dirs):
    cache, out = dirs
    _, _, path = _draft(cache, out, text_proposals=True)
    record = load_record(path, kind="expectation")
    assert record["blocks"] and all(b["type"] == "other" and b["uncertainty"]["level"] == "high" for b in record["blocks"])


def _reviewed(path, tmp_path, *, blocks=None, note=None, mutate=None):
    record = json.loads(path.read_text(encoding="utf-8"))
    record["provenance"]["review"] = {"status": "reviewed", "reviewer": "rev1", "reviewed_at": "2026-11-06T10:00:00Z", "basis": "page_image_inspection"}
    if note is None:
        record["provenance"].pop("uncertainty")
    else:
        record["provenance"]["uncertainty"] = {"level": "low", "note": note}
    if blocks is None:
        text = "Reviewed paragraph."
        blocks = [{"id": "b1", "type": "paragraph", "page": 1, "reading_order": 1, "text": text,
                   "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "bbox": {"x0": 10, "y0": 10, "x1": 200, "y1": 40}}]
    record["blocks"] = blocks
    if mutate:
        mutate(record)
    target = tmp_path / "reviewed.json"
    target.write_text(json.dumps(record), encoding="utf-8")
    return target


def test_reviewed_record_from_a_draft_passes_only_when_properly_finished(dirs, tmp_path):
    cache, out = dirs
    manifest, _, path = _draft(cache, out)
    good = _reviewed(path, tmp_path)
    assert pilot_records.load_pilot_expectation(good, manifest, cache)["blocks"]
    with pytest.raises(RecordError, match="not reviewed"):
        pilot_records.load_pilot_expectation(path, manifest)  # still a draft
    left_note = _reviewed(path, tmp_path, note=pilot.DRAFT_NOTE)
    with pytest.raises(RecordError, match="draft note"):
        pilot_records.load_pilot_expectation(left_note, manifest)


def test_reviewed_record_without_typed_blocks_needs_an_explicit_attestation(dirs, tmp_path):
    cache, out = dirs
    manifest, _, path = _draft(cache, out)
    empty = _reviewed(path, tmp_path, blocks=[])
    with pytest.raises(RecordError, match="no typed blocks"):
        pilot_records.load_pilot_expectation(empty, manifest)
    other = [{"id": "b1", "type": "other", "page": 1, "reading_order": 1, "text": None, "bbox": {"x0": 1, "y0": 1, "x1": 5, "y1": 5}}]
    with pytest.raises(RecordError, match="no typed blocks"):
        pilot_records.load_pilot_expectation(_reviewed(path, tmp_path, blocks=other), manifest)
    attested = _reviewed(path, tmp_path, blocks=[], note=pilot_records.EMPTY_PAGES_ATTESTATION + " (blank separator page)")
    assert pilot_records.load_pilot_expectation(attested, manifest)["blocks"] == []


def test_reviewed_record_must_match_the_manifest_pdf_and_render_hashes(dirs, tmp_path):
    cache, out = dirs
    manifest, _, path = _draft(cache, out)
    doc = manifest["pilot"][0]
    wrong_scope = _reviewed(path, tmp_path, mutate=lambda r: r["scope"].update(physical_pages=[2]))
    wrong_scope_record = json.loads(wrong_scope.read_text(encoding="utf-8"))
    wrong_scope_record["pages"][0]["physical_page"] = 2
    wrong_scope_record["blocks"][0]["page"] = 2
    wrong_scope.write_text(json.dumps(wrong_scope_record), encoding="utf-8")
    with pytest.raises(RecordError, match="candidate pages"):
        pilot_records.load_pilot_expectation(wrong_scope, manifest)
    other = {"pilot": [dict(doc, pdf_sha256="1" * 64)], "held_out": []}
    with pytest.raises(RecordError, match="manifest pin"):
        pilot_records.load_pilot_expectation(_reviewed(path, tmp_path), other)
    stale = _reviewed(path, tmp_path, mutate=lambda r: r["pages"][0]["render"].update(image_sha256="2" * 64))
    with pytest.raises(RecordError, match="render hash"):
        pilot_records.load_pilot_expectation(stale, manifest, cache)
    (cache / "syn-a.pdf").write_bytes((cache / "syn-a.pdf").read_bytes() + b"\n%x")
    with pytest.raises(RecordError, match="pinned hash"):
        pilot_records.load_pilot_expectation(_reviewed(path, tmp_path), manifest, cache)
    unknown = {"pilot": [dict(doc, id="other")], "held_out": []}
    with pytest.raises(RecordError, match="not in the manifest"):
        pilot_records.load_pilot_expectation(_reviewed(path, tmp_path), unknown)


def test_flipping_status_alone_does_not_make_a_draft_reviewed(dirs, tmp_path):
    cache, out = dirs
    _, _, path = _draft(cache, out)
    record = json.loads(path.read_text(encoding="utf-8"))
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


def test_page_checks_run_on_the_displayed_page(tmp_path):
    path = tmp_path / "rot.pdf"
    _make_pdf(path, rotate=90)
    with pymupdf.open(path) as doc:
        page = doc[0]
        assert page.rotation == 90
        drafts.page_support("scans", page)
        assert page.rotation == 0 and (page.rect.width, page.rect.height) == (792, 612)  # judged as displayed


def test_rotated_and_cropped_pages_are_refused_not_guessed(dirs):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-c.pdf", rotate=90, crop=True)
    report = drafts.run(_manifest_for("syn-c", digest, role="whole_paper"), cache, out)
    assert report["documents"][0]["status"] == "refused" and "CropBox" in report["problems"][-1]
    assert not list((out / "drafts").glob("*.json"))
    plain_crop = _make_pdf(cache / "syn-d.pdf", crop=True)  # cropped but not rotated is fine
    report = drafts.run(_manifest_for("syn-d", plain_crop, role="whole_paper"), cache, out)
    assert report["documents"][0].get("draft")


def test_missing_and_hash_mismatched_pdfs_are_reported_not_skipped(dirs, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("the draft tool must never download"))
    cache, out = dirs
    digest = _make_pdf(cache / "syn-bad.pdf")
    manifest = _manifest_for("syn-bad", "0" * 64)
    manifest["pilot"].append({"id": "syn-gone", "role": "stratum_pages", "stratum": "tables", "basis": "assumed",
                              "pdf_sha256": digest, "pdf_pages": 3, "candidate_pages": [1]})
    report = drafts.run(manifest, cache, out)
    statuses = {d["id"]: d["status"] for d in report["documents"]}
    assert statuses == {"syn-bad": "hash_mismatch", "syn-gone": "missing"}
    assert len(report["problems"]) == 2
    assert not list((out / "drafts").glob("*.json"))
    assert json.loads((out / report["report_file"]).read_text(encoding="utf-8"))["problems"]
    with pytest.raises(drafts.PdfProblem):
        drafts.CachedPageChecks(cache, "s").pages({"id": "syn-gone", "sha256": digest}, "tables", 1)


def test_candidate_page_that_fails_the_check_is_flagged_not_trusted(dirs):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-a.pdf", kind="two-column")
    report = drafts.run(_manifest_for("syn-a", digest, stratum="tables"), cache, out)
    assert report["documents"][0]["check_failed_pages"] == [1]
    assert any("check failed" in p for p in report["problems"])


def test_manifest_without_candidate_pages_is_reported(dirs):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-a.pdf")
    manifest = _manifest_for("syn-a", digest)
    del manifest["pilot"][0]["candidate_pages"]
    report = drafts.run(manifest, cache, out)
    assert report["documents"][0]["status"] == "no_candidate_pages" and report["problems"]


def test_layout_checks_distinguish_synthetic_pages(tmp_path):
    cols, tables = tmp_path / "c.pdf", tmp_path / "t.pdf"
    _make_pdf(cols, kind="two-column")
    _make_pdf(tables, kind="tables")
    with pymupdf.open(cols) as c, pymupdf.open(tables) as t:
        assert drafts.page_support("two-column", c[0]) and not drafts.page_support("two-column", c[1])
        assert drafts.page_support("tables", t[0]) and not drafts.page_support("tables", c[0])
        assert not drafts.page_support("scans", c[0])
        assert not drafts.page_support("references", c[0])


def test_table_check_rejects_grids_that_are_too_small_empty_or_images(tmp_path):
    path = tmp_path / "grids.pdf"
    doc = pymupdf.open()
    small = doc.new_page(width=612, height=792)  # 2 x 2 grid with text
    for r in range(2):
        for c in range(2):
            rect = pymupdf.Rect(60 + c * 100, 100 + r * 30, 160 + c * 100, 130 + r * 30)
            small.draw_rect(rect, color=(0, 0, 0))
            small.insert_textbox(rect, "x", fontsize=9)
    empty = doc.new_page(width=612, height=792)  # 4 x 4 grid, no text
    for r in range(4):
        for c in range(4):
            empty.draw_rect(pymupdf.Rect(60 + c * 80, 100 + r * 30, 140 + c * 80, 130 + r * 30), color=(0, 0, 0))
    figure = doc.new_page(width=612, height=792)  # grid drawn over a full-page image
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 60, 80), False)
    pix.clear_with(180)
    figure.insert_image(figure.rect, pixmap=pix)
    for r in range(4):
        for c in range(4):
            rect = pymupdf.Rect(60 + c * 80, 100 + r * 30, 140 + c * 80, 130 + r * 30)
            figure.draw_rect(rect, color=(0, 0, 0))
            figure.insert_textbox(rect, "x", fontsize=9)
    doc.save(path)
    with pymupdf.open(path) as saved:
        assert [drafts.page_support("tables", saved[i]) for i in range(3)] == [False, False, False]


def test_reference_pages_start_at_the_reference_heading_and_numbered_lists_count(tmp_path):
    path = tmp_path / "refs.pdf"
    doc = pymupdf.open()
    for text in (YEARS, YEARS, "References\n" + YEARS, YEARS):
        doc.new_page(width=612, height=792).insert_text((72, 80), text, fontsize=10)
    doc.new_page(width=612, height=792).insert_text((72, 80), "Appendix", fontsize=10)
    doc.save(path)
    numbered = tmp_path / "numbered.pdf"
    other = pymupdf.open()
    other.new_page(width=612, height=792).insert_text((72, 80), "7. REFERENCES\n" + "\n".join(f"{i}. Some cited work without a year" for i in range(1, 8)), fontsize=10)
    other.save(numbered)
    nohead = tmp_path / "nohead.pdf"
    bare = pymupdf.open()
    bare.new_page(width=612, height=792).insert_text((72, 80), YEARS, fontsize=10)
    bare.save(nohead)
    with pymupdf.open(path) as a, pymupdf.open(numbered) as b, pymupdf.open(nohead) as c:
        assert drafts.choose_pages(a, "references", 5, "s", "x") == ([3, 4], 2)
        assert drafts.choose_pages(b, "references", 1, "s", "x") == ([1], 1)
        assert drafts.choose_pages(c, "references", 1, "s", "x") == ([], 0)


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


def test_whole_paper_draft_covers_every_page_and_reports_the_reference_list(dirs):
    cache, out = dirs
    digest = _make_pdf(cache / "syn-w.pdf", pages=3)
    report = drafts.run(_manifest_for("syn-w", digest, role="whole_paper"), cache, out)
    record = load_record(out / "drafts" / "pilot-syn-w-whole.expectation.draft.json", kind="expectation")
    assert record["scope"] == {"kind": "complete_document", "physical_pages": [1, 2, 3]}
    assert report["documents"][0]["reference_list_found"] is False
    assert any("no reference list" in p for p in report["problems"])
    with_refs = _make_pdf(cache / "syn-x.pdf", pages=2, references=True)
    report = drafts.run(_manifest_for("syn-x", with_refs, role="whole_paper"), cache, out)
    assert report["documents"][0]["reference_list_found"] is True and report["problems"] == []


def test_unknown_id_fails_and_reports_and_drafts_are_not_overwritten(dirs):
    cache, out = dirs
    manifest, first, path = _draft(cache, out)
    with pytest.raises(ValueError, match="unknown pilot document"):
        drafts.run(manifest, cache, out, only=["nope"])
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")  # stand-in for review edits
    edited = path.read_bytes()
    second = drafts.run(manifest, cache, out)
    assert path.read_bytes() == edited and second["documents"][0]["status"] == "exists"
    assert first["report_file"] != second["report_file"] and len(list(out.glob("selection_report-*.json"))) == 2
    drafts.run(manifest, cache, out, overwrite=True)
    assert path.read_bytes() != edited


def test_output_inside_any_git_checkout_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        drafts.main(["--out", str(pilot.REPO_ROOT / "benchmarks" / "oops")])
    other = tmp_path / "other-checkout"
    (other / ".git").mkdir(parents=True)
    assert drafts._inside_git_checkout(other / "deep" / "out")
    with pytest.raises(SystemExit):
        drafts.main(["--out", str(other / "out")])
    assert not drafts._inside_git_checkout(tmp_path / "plain")


def test_default_output_is_outside_every_checkout():
    assert not drafts._inside_git_checkout(drafts.DEFAULT_OUT)


def test_changed_manifest_is_refused_by_the_host_tool(tmp_path):
    changed = tmp_path / "m.json"
    changed.write_bytes(pilot.MANIFEST_PATH.read_bytes() + b" ")
    with pytest.raises(SystemExit):
        drafts.main(["--manifest", str(changed), "--out", str(tmp_path / "out")])


def test_unknown_id_on_the_command_line_fails(tmp_path):
    with pytest.raises(SystemExit):
        drafts.main(["--id", "no-such-doc", "--out", str(tmp_path / "out")])


@pytest.mark.skipif(os.environ.get("EXTRACTION_PILOT_REAL_CACHE") != "1", reason="opt-in: needs the local corpus cache")
def test_real_cache_status_is_reported_for_every_pilot_document():
    manifest = json.loads(pilot.MANIFEST_PATH.read_bytes())
    for doc in manifest["pilot"]:
        status, _, detail = drafts.verify_cached(doc, drafts.DEFAULT_CACHE)
        assert status in {"ok", "missing", "hash_mismatch"}, detail
