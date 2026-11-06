"""Extraction scorer and defect injectors: purely synthetic records, no PDFs or real text."""

import copy
import json

import pytest

from extraction import defects, options_sha256, sha256_bytes, sha256_text, validate_record
from extraction.scorer import (
    METRIC_VERSIONS,
    ScoringError,
    failure_result,
    score_files,
    score_records,
    score_to_result,
)

PDF_HASH = "a" * 64


def _page(number):
    return {
        "physical_page": number, "printed_label": None,
        "geometry": {"unit": "pt", "origin": "top-left", "frame": "unrotated", "box": "cropbox", "rotation": 0, "width": 600, "height": 800},
        "render": {"renderer": "synthetic-renderer", "renderer_version": "1.0", "dpi": 144,
                   "image_sha256": "b" * 64},
    }


def _block(block_id, kind, page, order, text=None, **extra):
    block = {"id": block_id, "type": kind, "page": page, "reading_order": order,
             "bbox": {"x0": 10, "y0": 10 + 50 * order, "x1": 500, "y1": 40 + 50 * order}}
    if text is not None:
        block["text"] = text
        block["text_sha256"] = sha256_text(text)
    block.update(extra)
    return block


def _cell(row, col, text, header=False):
    cell = {"row": row, "col": col, "text": text}
    if header:
        cell["is_header"] = True
    return cell


RAW1 = "[1] A. Author. Synthetic paper. 2020."
RAW2 = "[2] B. Writer. Another one. 2021. doi:10.0000/Synthetic"


def _expectation():
    table = {"n_rows": 3, "n_cols": 3, "cells": [
        _cell(0, 0, "Group", True), _cell(0, 1, "Mean", True), _cell(0, 2, "Delta", True),
        _cell(1, 0, "Control"), _cell(1, 1, "4.25"), _cell(1, 2, "-0.50"),
        _cell(2, 0, "Treated"), _cell(2, 1, "5.75"), _cell(2, 2, "+1.25")]}
    return {
        "schema_version": 1, "record_kind": "expectation", "record_id": "exp-1",
        "document": {"pdf_sha256": PDF_HASH, "title": "Synthetic Document", "page_count": 2},
        "scope": {"kind": "complete_document", "physical_pages": [1, 2]},
        "pages": [_page(1), _page(2)],
        "blocks": [
            _block("h1", "heading", 1, 0, "Alpha Heading"),
            _block("p1", "paragraph", 1, 1, "The effect is -0.5 at p < .05 for Group A."),
            _block("p2", "paragraph", 1, 2, "Second paragraph with Mixed Case and x = 3.14."),
            _block("t1", "table", 1, 3, None, table=table),
            _block("c1", "caption", 1, 4, "Table 1. Synthetic group means.", caption_for="t1"),
            _block("e1", "equation", 2, 5, None, equation={"latex": "y=-x+1", "number": "(1)", "display": True}),
            _block("r1b", "reference_entry", 2, 6, RAW1),
            _block("r2b", "reference_entry", 2, 7, RAW2),
        ],
        "references": {
            "entries": [
                {"id": "r1", "ordinal": 1, "page": 2, "block_id": "r1b", "raw": RAW1,
                 "raw_sha256": sha256_text(RAW1),
                 "fields": {"title": "Synthetic paper", "year": 2020, "authors": [{"family": "Author", "given": "A."}]},
                 "doi": {"status": "absent_in_source"}},
                {"id": "r2", "ordinal": 2, "page": 2, "block_id": "r2b", "raw": RAW2,
                 "raw_sha256": sha256_text(RAW2),
                 "fields": {"title": "Another one", "year": 2021},
                 "doi": {"status": "present", "value": "10.0000/Synthetic"}},
            ],
            "markers": [],
        },
        "provenance": {"review": {"status": "reviewed", "reviewer": "reviewer-a",
                                  "reviewed_at": "2026-01-01T00:00:00Z", "basis": "page_image_inspection"}},
    }


def _observation_of(expectation):
    obs = copy.deepcopy(expectation)
    obs["record_kind"] = "observation"
    obs["record_id"] = "obs-1"
    obs["provenance"] = {"produced_by": {"tool": "synthetic-extractor", "version": "0.1"},
                         "created_at": "2026-01-01T00:00:00Z"}
    for entry in obs["references"]["entries"]:
        if entry["doi"]["status"] != "present":
            entry["doi"] = {"status": "not_found"}
    return obs


@pytest.fixture
def exp():
    return _expectation()


@pytest.fixture
def obs(exp):
    return _observation_of(exp)


def _failed(score, mode="strict"):
    return {(c["name"], c["subject"]) for c in score["checks"] if c["mode"] == mode and not c["passed"]}


def _rescore(exp, obs):
    validate_record(obs)  # every injected defect must still be a valid record
    return score_records(exp, obs)


def test_clean_observation_passes_strict_and_tolerant(exp, obs):
    score = score_records(exp, obs)
    assert score["strict_passed"] and score["strict_failures"] == 0
    assert score["tolerant_only_matches"] == 0
    assert not _failed(score) and not _failed(score, "tolerant")
    names = {m["name"] for m in score["metrics"]}
    assert names >= {"text_presence", "text_span_hash", "reading_order", "table_cell_match", "table_exact",
                     "equation_match", "caption_match", "reference_segmentation_recall",
                     "reference_field_accuracy", "reference_doi_recovery", "reference_doi_absent_correct"}
    assert all(m["value"] == 1.0 and m["version"] == METRIC_VERSIONS[m["name"]] for m in score["metrics"])


def test_missing_sign_in_text_fails_strict(exp, obs):
    score = _rescore(exp, defects.drop_sign(obs, "p1"))
    assert not score["strict_passed"]
    assert ("text_presence", "p1") in _failed(score)
    assert ("text_presence", "p1") in _failed(score, "tolerant")  # normalization cannot restore a sign


def test_missing_sign_in_table_cell_fails_strict(exp, obs):
    score = _rescore(exp, defects.drop_sign(obs, "t1", cell=(1, 2)))
    assert {("table_cell_match", "t1"), ("table_exact", "t1")} <= _failed(score)
    cell_check = next(c for c in score["checks"] if c["name"] == "table_cell_match" and c["mode"] == "strict")
    assert (cell_check["matched"], cell_check["total"]) == (8, 9)


def test_missing_sign_in_equation_fails_strict(exp, obs):
    assert ("equation_match", "e1") in _failed(_rescore(exp, defects.drop_sign(obs, "e1")))


def test_swapped_table_values_fail_strict(exp, obs):
    score = _rescore(exp, defects.swap_table_values(obs, "t1", (1, 1), (2, 1)))
    assert not score["strict_passed"]
    assert {("table_cell_match", "t1"), ("table_exact", "t1")} <= _failed(score)
    assert ("table_exact", "t1") in _failed(score, "tolerant")  # tolerant cannot repair positions


def test_reordered_columns_fail_strict(exp, obs):
    score = _rescore(exp, defects.reorder_columns(obs, "t1", [0, 2, 1]))
    assert not score["strict_passed"]
    assert {("table_cell_match", "t1"), ("table_exact", "t1")} <= _failed(score)


def test_omitted_text_fails_strict(exp, obs):
    whole = _rescore(exp, defects.omit_text(obs, "p2"))
    assert ("text_presence", "p2") in _failed(whole)
    part = _rescore(exp, defects.omit_text(obs, "p2", "Mixed Case and "))
    assert ("text_presence", "p2") in _failed(part)
    assert ("text_span_hash", "p2") in _failed(part)
    assert not part["strict_passed"]


def test_omitted_caption_fails_strict(exp, obs):
    assert ("caption_match", "c1") in _failed(_rescore(exp, defects.omit_text(obs, "c1")))


def test_swapped_reading_order_fails_strict(exp, obs):
    score = _rescore(exp, defects.swap_reading_order(obs, "p1", "p2"))
    assert not score["strict_passed"]
    assert {("reading_order", "h1->p1"), ("reading_order", "p1->p2")} & _failed(score)


def test_caption_linked_to_wrong_target_fails(exp, obs):
    obs["blocks"].append(_block("f1", "figure", 1, 20, None, graphic={"kind": "chart"},
                                bbox={"x0": 10, "y0": 600, "x1": 500, "y1": 700}))
    next(b for b in obs["blocks"] if b["id"] == "c1")["caption_for"] = "f1"
    assert ("caption_match", "c1") in _failed(_rescore(exp, obs))


def test_injectors_leave_input_untouched_and_reject_noops(obs):
    before = copy.deepcopy(obs)
    defects.drop_sign(obs, "p1")
    defects.reorder_columns(obs, "t1", [2, 1, 0])
    assert obs == before
    with pytest.raises(ValueError):
        defects.drop_sign(obs, "h1")
    with pytest.raises(ValueError):
        defects.swap_table_values(obs, "t1", (0, 0), (0, 0))
    with pytest.raises(ValueError):
        defects.reorder_columns(obs, "t1", [0, 1, 2])
    with pytest.raises(ValueError):
        defects.omit_text(obs, "nope")


def test_tolerant_only_match_does_not_pass_strict(exp, obs):
    block = next(b for b in obs["blocks"] if b["id"] == "p2")
    block["text"] = "second  paragraph with mixed case and x = 3.14."  # case and spacing only
    block["text_sha256"] = sha256_text(block["text"])
    score = _rescore(exp, obs)
    assert not score["strict_passed"]
    assert ("text_presence", "p2") in _failed(score)
    assert ("text_span_hash", "p2") in _failed(score)  # a normalized match is not fidelity
    tolerant = next(c for c in score["checks"] if c["mode"] == "tolerant" and c["name"] == "text_presence"
                    and c["subject"] == "p2")
    assert tolerant["passed"] and tolerant["tolerant_only"]
    assert score["tolerant_only_matches"] >= 1
    metrics = {(m["name"], m["mode"]): m["value"] for m in score["metrics"]}
    assert metrics[("text_presence", "strict")] < metrics[("text_presence", "tolerant")] == 1.0


def test_text_on_wrong_page_fails(exp, obs):
    next(b for b in obs["blocks"] if b["id"] == "p2")["page"] = 2
    score = _rescore(exp, obs)
    assert ("text_presence", "p2") in _failed(score)
    assert "page(s) [2]" in next(c for c in score["checks"] if c["subject"] == "p2")["detail"]


def test_refuses_mismatched_pdf_hash_before_scoring(exp, obs):
    obs["document"]["pdf_sha256"] = "b" * 64
    with pytest.raises(ScoringError, match="pdf_sha256 mismatch"):
        score_records(exp, obs)


def test_only_reviewed_expectations_and_right_kinds(exp, obs):
    draft = copy.deepcopy(exp)
    draft["provenance"]["review"] = {"status": "draft"}
    with pytest.raises(ScoringError, match="not reviewed"):
        score_records(draft, obs)
    with pytest.raises(ScoringError, match="expectation, observation"):
        score_records(obs, obs)
    with pytest.raises(ScoringError, match="invalid record"):
        score_records({"record_kind": "expectation"}, obs)


def test_score_files_round_trip(exp, obs, tmp_path):
    (tmp_path / "e.json").write_text(json.dumps(exp), encoding="utf-8")
    (tmp_path / "o.json").write_text(json.dumps(obs), encoding="utf-8")
    assert score_files(tmp_path / "e.json", tmp_path / "o.json")["strict_passed"]
    exp["provenance"]["review"] = {"status": "draft"}
    (tmp_path / "d.json").write_text(json.dumps(exp), encoding="utf-8")
    with pytest.raises(ScoringError):
        score_files(tmp_path / "d.json", tmp_path / "o.json")


def test_high_uncertainty_items_are_listed_not_scored(exp, obs):
    next(b for b in exp["blocks"] if b["id"] == "p2")["uncertainty"] = {"level": "high"}
    score = _rescore(exp, defects.omit_text(obs, "p2"))
    assert score["strict_passed"]
    assert {"metric": "text_presence", "subject": "p2",
            "reason": "expected block marked high uncertainty"} in score["not_scored"]


# --- references ----------------------------------------------------------------------------------


def _entry(obs, entry_id):
    return next(e for e in obs["references"]["entries"] if e["id"] == entry_id)


def test_absent_doi_is_not_a_parsing_failure(exp, obs):
    score = score_records(exp, obs)  # r1 prints no DOI and the observation reports none
    assert score["strict_passed"]
    recovery = [c for c in score["checks"] if c["name"] == "reference_doi_recovery"]
    assert {c["subject"] for c in recovery} == {"r2"}  # the absent-DOI entry is not in the denominator


def test_invented_doi_fails_strict(exp, obs):
    _entry(obs, "r1")["doi"] = {"status": "present", "value": "10.1111/invented"}
    assert ("reference_doi_absent_correct", "r1") in _failed(_rescore(exp, obs))


def test_missed_or_failed_doi_recovery_fails_strict(exp, obs):
    _entry(obs, "r2")["doi"] = {"status": "not_found"}
    assert ("reference_doi_recovery", "r2") in _failed(_rescore(exp, obs))
    _entry(obs, "r2")["doi"] = {"status": "parse_failed"}
    assert ("reference_doi_recovery", "r2") in _failed(_rescore(exp, obs))


def test_doi_case_difference_is_tolerant_only(exp, obs):
    _entry(obs, "r2")["doi"] = {"status": "present", "value": "10.0000/synthetic"}
    score = _rescore(exp, obs)
    assert ("reference_doi_recovery", "r2") in _failed(score)
    assert ("reference_doi_recovery", "r2") not in _failed(score, "tolerant")
    assert not score["strict_passed"] and score["tolerant_only_matches"] == 1


def test_segmentation_scored_separately_from_parsing(exp, obs):
    _entry(obs, "r2")["raw"] = RAW2 + " extra"  # garbled entry boundary
    _entry(obs, "r2")["raw_sha256"] = sha256_text(_entry(obs, "r2")["raw"])
    score = _rescore(exp, obs)
    failed = _failed(score)
    assert ("reference_segmentation_recall", "r2") in failed
    # r2 is unsegmented, so it is not additionally charged as a parsing or identifier failure.
    assert not {n for n, _ in failed} & {"reference_field_accuracy", "reference_doi_recovery"}
    precision = next(m for m in score["metrics"]
                     if m["name"] == "reference_segmentation_precision" and m["mode"] == "strict")
    assert precision["value"] == 0.5 and precision["n"] == 2


def test_field_parsing_errors_are_reported(exp, obs):
    _entry(obs, "r1")["fields"]["year"] = 2021
    _entry(obs, "r1")["fields"]["authors"] = [{"family": "Author"}]
    score = _rescore(exp, obs)
    check = next(c for c in score["checks"] if c["name"] == "reference_field_accuracy" and c["subject"] == "r1"
                 and c["mode"] == "strict")
    assert (check["matched"], check["total"]) == (1, 3)
    assert "year" in check["detail"] and "authors" in check["detail"]


def test_observation_without_references_is_not_scored_as_zero(exp, obs):
    del obs["references"]
    score = _rescore(exp, obs)
    assert not [c for c in score["checks"] if c["name"].startswith("reference_")]
    assert score["not_scored"][0]["metric"] == "reference_*"


# --- run output ----------------------------------------------------------------------------------


def test_metrics_fit_run_schema_and_failures_stay_visible(exp, obs):
    good = score_records(exp, defects.drop_sign(obs, "p1"))
    options = {"dpi": 144}
    run = {
        "schema_version": 1, "record_kind": "run", "record_id": "run-1", "started_at": "2026-01-01T00:00:00Z",
        "metric_versions": METRIC_VERSIONS,
        "configurations": [
            {"id": "c1", "tool": "synthetic-extractor", "version": "0.1", "options": options,
             "options_sha256": options_sha256(options)},
            {"id": "c2", "tool": "slow-tool", "version": "1", "options_sha256": "c" * 64},
            {"id": "c3", "tool": "absent-tool", "version": "0", "options_sha256": "d" * 64},
        ],
        "inputs": [{"id": "i1", "pdf_sha256": PDF_HASH, "expectation_sha256": sha256_bytes(b"exp")}],
        "results": [
            score_to_result("c1", "i1", good, "e" * 64, latency_seconds=1.0),
            failure_result("c2", "i1", "timeout", "Timeout", "exceeded 60 s", latency_seconds=60),
            failure_result("c3", "i1", "missing_backend", "BackendNotInstalled", "not installed"),
        ],
    }
    validate_record(run)
    by_status = {r["status"]: r for r in run["results"]}
    assert set(by_status) == {"ok", "timeout", "missing_backend"}
    assert "metrics" not in by_status["timeout"] and "metrics" not in by_status["missing_backend"]
    assert {m["mode"] for m in by_status["ok"]["metrics"]} == {"strict", "tolerant"}
    with pytest.raises(ValueError):
        failure_result("c1", "i1", "ok", "x", "y")


# --- cross-page chains, Markdown-only observations, output spans ------------------------------------

PA = "Results continue across the page with a delta of 0.5 and"
PB = "the second half keeps a sign of -1.5 intact."
RA = "[3] C. Chain. A long title that wraps"
RB = "across the page break. 2022."


def _chain_expectation():
    def tbl(rows):
        cells = []
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                cells.append(_cell(r, c, text, header=row is rows[0]))
        return {"n_rows": len(rows), "n_cols": 2, "cells": cells}

    header = ["Group", "Delta"]
    record = _expectation()
    record["record_id"] = "exp-chain"
    record["document"]["page_count"] = 3
    record["scope"] = {"kind": "complete_document", "physical_pages": [1, 2, 3]}
    record["pages"] = [_page(1), _page(2), _page(3)]
    record["blocks"] = [
        _block("h1", "heading", 1, 0, "Chain Heading"),
        _block("pa", "paragraph", 1, 1, PA),
        _block("ta", "table", 1, 2, None, table=tbl([header, ["A", "-0.50"]])),
        _block("pb", "paragraph", 2, 3, PB, continues_from="pa"),
        _block("tb", "table", 2, 4, None, table=tbl([header, ["B", "+1.25"], ["C", "2.00"]]), continues_from="ta"),
        _block("ra", "reference_entry", 2, 5, RA),
        _block("rb", "reference_entry", 3, 6, RB, continues_from="ra"),
    ]
    raw = RA + " " + RB
    record["references"] = {"entries": [
        {"id": "r3", "ordinal": 1, "page": 2, "end_page": 3, "block_id": "ra", "raw": raw,
         "raw_sha256": sha256_text(raw), "doi": {"status": "absent_in_source"}}], "markers": []}
    return record


def _merged(obs):
    """What a correct extractor emits when it merges chains: one block each, on the first page."""
    out = copy.deepcopy(obs)
    del out["references"]["entries"][0]["end_page"]  # one merged block: no continuation, so no end_page
    blocks = {b["id"]: b for b in out["blocks"]}
    merged_text = PA + "\n" + PB
    blocks["pa"]["text"], blocks["pa"]["text_sha256"] = merged_text, sha256_text(merged_text)
    ta = blocks["ta"]["table"]
    ta["n_rows"] = 4
    ta["cells"] += [_cell(2, 0, "B"), _cell(2, 1, "+1.25"), _cell(3, 0, "C"), _cell(3, 1, "2.00")]
    raw = RA + " " + RB
    for block_id in ("pb", "tb", "rb"):
        out["blocks"].remove(blocks[block_id])
    blocks["ra"]["text"], blocks["ra"]["text_sha256"] = raw, sha256_text(raw)
    return out


@pytest.fixture
def chain_exp():
    return _chain_expectation()


@pytest.fixture
def chain_obs(chain_exp):
    return _observation_of(chain_exp)


def test_chain_fixture_is_valid_and_split_observation_passes(chain_exp, chain_obs):
    validate_record(chain_exp)
    assert score_records(chain_exp, chain_obs)["strict_passed"]


def test_correct_merged_chains_pass_strict(chain_exp, chain_obs):
    merged = _merged(chain_obs)
    validate_record(merged)
    score = score_records(chain_exp, merged)
    assert score["strict_passed"], _failed(score)
    names = {(c["name"], c["subject"]) for c in score["checks"] if c["mode"] == "strict"}
    assert {("table_exact", "ta+tb"), ("text_span_hash", "pa+pb"), ("reference_segmentation_recall", "r3")} <= names
    cells = next(c for c in score["checks"] if c["name"] == "table_cell_match" and c["mode"] == "strict")
    assert (cells["matched"], cells["total"]) == (8, 8)  # repeated header on the continuation is not a data row


def test_merged_chain_with_a_space_joiner_also_passes_hash(chain_exp, chain_obs):
    merged = _merged(chain_obs)
    block = next(b for b in merged["blocks"] if b["id"] == "pa")
    block["text"] = PA + " " + PB
    block["text_sha256"] = sha256_text(block["text"])
    assert score_records(chain_exp, merged)["strict_passed"]


def test_merged_chain_defects_still_fail_strict(chain_exp, chain_obs):
    merged = _merged(chain_obs)
    sign = score_records(chain_exp, defects.drop_sign(merged, "pa"))  # the only sign is in the second fragment
    assert ("text_presence", "pb") in _failed(sign) and not sign["strict_passed"]
    swapped = score_records(chain_exp, defects.swap_table_values(merged, "ta", (2, 1), (3, 1)))  # across the break
    assert ("table_exact", "ta+tb") in _failed(swapped)
    omitted = score_records(chain_exp, defects.omit_text(merged, "pa", "\n" + PB))
    assert ("text_presence", "pb") in _failed(omitted) and ("text_span_hash", "pa+pb") in _failed(omitted)
    reordered = score_records(chain_exp, defects.reorder_columns(merged, "ta", [1, 0]))
    assert ("table_exact", "ta+tb") in _failed(reordered)


def test_merged_table_missing_the_continuation_rows_fails(chain_exp, chain_obs):
    merged = _merged(chain_obs)
    table = next(b for b in merged["blocks"] if b["id"] == "ta")["table"]
    table["cells"] = [c for c in table["cells"] if c["row"] < 2]
    table["n_rows"] = 2
    score = score_records(chain_exp, merged)
    assert ("table_exact", "ta+tb") in _failed(score)
    assert next(c for c in score["checks"] if c["name"] == "table_cell_match")["matched"] == 4


def test_merged_table_keeping_the_repeated_header_row_fails_strict(chain_exp, chain_obs):
    merged = _merged(chain_obs)
    table = next(b for b in merged["blocks"] if b["id"] == "ta")["table"]
    for cell in table["cells"]:
        if cell["row"] >= 2:
            cell["row"] += 1
    table["cells"] += [_cell(2, 0, "Group", True), _cell(2, 1, "Delta", True)]
    table["n_rows"] = 5
    assert ("table_exact", "ta+tb") in _failed(score_records(chain_exp, merged))


def test_split_chain_fragments_swapped_across_the_break_fail(chain_exp, chain_obs):
    blocks = {b["id"]: b for b in chain_obs["blocks"]}
    first, second = blocks["ta"]["table"]["cells"][3], blocks["tb"]["table"]["cells"][3]
    first["text"], second["text"] = second["text"], first["text"]
    assert ("table_exact", "ta+tb") in _failed(score_records(chain_exp, chain_obs))


def test_reference_crossing_a_page_must_not_contradict_start_or_end_page(chain_exp, chain_obs):
    for field, value in (("page", 3), ("end_page", 2)):
        obs = copy.deepcopy(chain_obs)
        entry = obs["references"]["entries"][0]
        del entry["block_id"]  # detach from the block chain so the record stays valid
        if field == "page":
            entry["page"] = value
            del entry["end_page"]
        else:
            entry["page"], entry["end_page"] = 1, value
        assert ("reference_segmentation_recall", "r3") in _failed(score_records(chain_exp, obs)), field


def _markdown_only(obs):
    out = copy.deepcopy(obs)
    for page in out["pages"]:
        del page["render"]
    for block in out["blocks"]:
        del block["bbox"]
    for entry in out["references"]["entries"]:
        del entry["page"]
        entry.pop("end_page", None)
    return out


def test_markdown_only_observation_is_scored_but_cannot_pass_strict_without_pages(exp, obs):
    obs = _markdown_only(obs)
    validate_record(obs)
    score = score_records(exp, obs)
    assert ("reference_segmentation_recall", "r1") in _failed(score)  # strict will not accept an unknown page
    assert not _failed(score, "tolerant")
    assert not score["strict_passed"] and score["tolerant_only_matches"] >= 2
    assert not [c for c in score["checks"] if c["name"] == "text_presence" and not c["passed"]]


def _with_spans(obs, markdown):
    out = copy.deepcopy(obs)
    block = out["blocks"][0]
    block["output_span"] = {"markdown_sha256": sha256_text(markdown), "start": 0, "end": 5,
                            "span_sha256": sha256_text(markdown[0:5])}
    return out


def test_output_span_round_trip_is_checked_when_markdown_is_supplied(exp, obs):
    markdown = "# Alpha Heading\n"
    spanned = _with_spans(obs, markdown)
    validate_record(spanned)
    assert score_records(exp, spanned, markdown=markdown)["strict_passed"]
    bad = score_records(exp, spanned, markdown=markdown.replace("Alpha", "Delta"))
    assert ("output_span_roundtrip", "<all>") in _failed(bad) and not bad["strict_passed"]
    missing = score_records(exp, spanned)
    assert missing["not_scored"] == [{"metric": "output_span_roundtrip", "subject": "<all>",
                                      "reason": "observation has output spans but no Markdown text was supplied"}]


# --- truncation and same-page chains ----------------------------------------------------------------


def test_truncated_expected_items_are_not_compared_and_say_why(exp, obs):
    exp["scope"]["kind"] = obs["scope"]["kind"] = "pages"  # truncation only exists in page-scoped records
    block = next(b for b in exp["blocks"] if b["id"] == "p2")
    block["truncated"] = "start"
    table = next(b for b in exp["blocks"] if b["id"] == "t1")
    table["truncated"] = "end"
    exp["references"]["entries"][1]["truncated"] = "both"
    damaged = defects.swap_table_values(defects.omit_text(obs, "p2"), "t1", (1, 1), (2, 1))
    damaged["references"]["entries"][1]["raw"] = "garbled"
    damaged["references"]["entries"][1]["raw_sha256"] = sha256_text("garbled")
    score = _rescore(exp, damaged)
    # No truncated item is scored; only the observation's now-unmatched extra entry still counts
    # against segmentation precision.
    assert _failed(score) == {("reference_segmentation_precision", "<all>")}
    reasons = {(n["metric"], n["subject"]): n["reason"] for n in score["not_scored"]}
    assert reasons[("text_presence", "p2")].startswith("expected block truncated at the scope boundary (start)")
    assert "(end)" in reasons[("table_cell_match", "t1")]
    assert "(both)" in reasons[("reference_segmentation_recall", "r2")]
    assert not [c for c in score["checks"] if c["subject"] in ("p2", "t1", "r2")]


def test_same_page_chain_is_scored_as_one_item(chain_exp, chain_obs):
    for record in (chain_exp, chain_obs):
        pb = next(b for b in record["blocks"] if b["id"] == "pb")
        pb["page"] = 1  # a column break: the continuation is later in reading order on the same page
        pb["bbox"] = {"x0": 10, "y0": 600, "x1": 500, "y1": 640}
    validate_record(chain_exp)
    assert score_records(chain_exp, chain_obs)["strict_passed"]
    merged = _merged(chain_obs)
    assert score_records(chain_exp, merged)["strict_passed"]
    assert ("text_presence", "pb") in _failed(score_records(chain_exp, defects.drop_sign(merged, "pa")))
