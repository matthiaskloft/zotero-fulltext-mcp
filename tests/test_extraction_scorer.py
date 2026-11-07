"""Extraction scorer and defect injectors: purely synthetic records, no PDFs or real text."""

import copy
import json

import pytest

from extraction import defects, options_sha256, sha256_bytes, sha256_text, validate_record
from extraction.scorer import (
    METRIC_VERSIONS,
    _assign,
    _assign_groups,
    _Checks,
    _strip_markup,
    _verdict,
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
            _block("e1", "equation", 2, 5, None, equation={"latex": "y=-0.5x+1", "number": "(1)", "display": True}),
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
    for entry in obs.get("references", {}).get("entries", []):
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
    assert ("reading_order", "p2") in _failed(score)


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
    assert score["strict_failures"] == 0
    assert score["verdict"] == "incomplete" and not score["strict_passed"]  # unscored items: not a pass
    assert score["unscored_items"] == len([n for n in score["not_scored"] if n["metric"] != "text_span_hash"]) >= 1
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


def test_observation_without_references_is_an_empty_list_where_references_are_expected(exp, obs):
    del obs["references"]
    score = _rescore(exp, obs)
    assert score["verdict"] == "failed"
    assert {("reference_segmentation_recall", "r1"), ("reference_segmentation_recall", "r2")} <= _failed(score)
    # Where the expectation has no references, a missing section costs nothing.
    del exp["references"]
    assert _rescore(exp, obs)["verdict"] == "passed"


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
    # No truncated item is scored, and the garbled entry on the truncated entry's page is not
    # charged as an extra either: it is listed instead.
    assert not _failed(score) and score["verdict"] == "incomplete"
    assert any(n["metric"] == "reference_segmentation_precision" and n["subject"] == "r2"
               for n in score["not_scored"])
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


# --- strict transcription vs layout, order, verdict, tables, captions, references ---------------------

BOX = {"x0": 10, "y0": 700, "x1": 500, "y1": 740}


def _set_text(obs, block_id, text):
    block = next(b for b in obs["blocks"] if b["id"] == block_id)
    block["text"], block["text_sha256"] = text, sha256_text(text)


def _metric(score, name, mode="strict"):
    return next((m for m in score["metrics"] if m["name"] == name and m["mode"] == mode), None)


def _decisive(score):
    return {(c["name"], c["subject"]) for c in score["checks"] if c["decides"] and not c["passed"]}


def test_strict_transcription_ignores_wraps_whitespace_heading_markup_and_block_boundaries(exp, obs):
    _set_text(obs, "h1", "# Alpha Heading")
    _set_text(obs, "p2", "Second paragraph\nwith Mixed   Case and\nx = 3.14.")
    for block in obs["blocks"]:
        if block["reading_order"] >= 2:
            block["reading_order"] += 1
    p1 = next(b for b in obs["blocks"] if b["id"] == "p1")
    _set_text(obs, "p1", "The effect is -0.5 at")
    second = _block("p1b", "paragraph", 1, 2, "p < .05 for Group A.")
    obs["blocks"].append(second)
    score = _rescore(exp, obs)
    assert score["verdict"] == "passed", _decisive(score)
    # Layout and byte-exactness are reported, but do not decide the verdict.
    assert _metric(score, "text_segmentation")["value"] < 1
    assert _metric(score, "text_span_hash")["value"] < 1
    assert p1["text"] == "The effect is -0.5 at"


@pytest.mark.parametrize("target,mutate,expected", [
    ("p2", lambda o: _set_text(o, "p2", "Second paragraph with Mixed Case and x = 314."), ("text_presence", "p2")),
    ("p1", lambda o: _set_text(o, "p1", "The effect is -0.5 at p > .05 for Group A."), ("text_presence", "p1")),
    ("p1", lambda o: _set_text(o, "p1", "The effect is -0.5 at p < 05 for Group A."), ("text_presence", "p1")),
    ("p1-case", lambda o: _set_text(o, "p1", "the effect is -0.5 at p < .05 for group a."), ("text_presence", "p1")),
    ("t1", lambda o: _cell_of(o, 1, 1).update(text="425"), ("table_exact", "t1")),
    ("t1", lambda o: _cell_of(o, 2, 2).update(text="1.25"), ("table_exact", "t1")),
    ("e1", lambda o: _equation_of(o).update(latex="y=-05x+1"), ("equation_match", "e1")),
    ("e1", lambda o: _equation_of(o).update(latex="y=-0.5x-1"), ("equation_match", "e1")),
    ("e1", lambda o: _equation_of(o).update(latex="y-0.5x+1"), ("equation_match", "e1")),
])
def test_strict_keeps_decimals_operators_signs_and_case(exp, obs, target, mutate, expected):
    mutate(obs)
    score = _rescore(exp, obs)
    assert expected in _decisive(score) and score["verdict"] == "failed"
    if "case" not in target:
        assert expected in _failed(score, "tolerant")  # tolerant never restores digits, signs or operators


def _cell_of(obs, row, col):
    table = next(b for b in obs["blocks"] if b["id"] == "t1")["table"]
    return next(c for c in table["cells"] if (c["row"], c["col"]) == (row, col))


def _equation_of(obs):
    return next(b for b in obs["blocks"] if b["id"] == "e1")["equation"]


def _order_expectation():
    record = _chain_expectation()
    record["record_id"] = "exp-order"
    order = {"h1": 0, "pa": 1, "ta": 3, "pb": 5, "tb": 6, "ra": 8, "rb": 9}
    for block in record["blocks"]:
        block["reading_order"] = order[block["id"]]
    record["blocks"] += [
        _block("d1", "paragraph", 1, 2, "Repeated sentence.", bbox=BOX),
        _block("dx", "paragraph", 2, 4, "Interleaved paragraph.", bbox=BOX),
        _block("d2", "paragraph", 2, 7, "Repeated sentence.", bbox=BOX),
    ]
    return record


def test_identical_clone_passes_reading_order_with_repeats_and_interleaved_chains():
    exp = _order_expectation()
    clone = _observation_of(exp)
    score = _rescore(exp, clone)
    assert score["verdict"] == "passed", _decisive(score)
    assert _metric(score, "reading_order")["value"] == 1.0
    merged = _merged(clone)
    assert _rescore(exp, merged)["verdict"] == "passed"


def test_displaced_block_fails_order_but_a_missing_block_does_not_cascade():
    exp = _order_expectation()
    clone = _observation_of(exp)
    displaced = _rescore(exp, defects.swap_reading_order(clone, "pa", "d1"))
    assert ("reading_order", "d1") in _failed(displaced)
    missing = _rescore(exp, defects.omit_text(clone, "dx"))
    assert _decisive(missing) == {("text_presence", "dx")}
    assert any(n["metric"] == "reading_order" and n["subject"] == "dx" for n in missing["not_scored"])


def test_split_fragment_must_sit_on_its_own_page_only_merged_blocks_may_move(chain_exp, chain_obs):
    pb = next(b for b in chain_obs["blocks"] if b["id"] == "pb")
    pb["page"] = 1
    pb["bbox"] = dict(BOX)
    score = _rescore(chain_exp, chain_obs)
    assert ("text_presence", "pb") in _failed(score) and "page(s) [1]" in next(
        c for c in score["checks"] if c["subject"] == "pb" and c["mode"] == "strict")["detail"]


def test_invented_content_lowers_text_precision(exp, obs):
    obs["blocks"].append(_block("pz", "paragraph", 1, 20, "An entirely invented claim.", bbox=BOX))
    score = _rescore(exp, obs)
    assert ("text_precision", "pz") in _decisive(score) and score["verdict"] == "failed"
    assert _failed(score, "tolerant") >= {("text_precision", "pz")}
    clean = _rescore(exp, _observation_of(exp))
    assert _metric(clean, "text_precision")["value"] == 1.0


# --- verdict ------------------------------------------------------------------------------------


def test_verdict_is_incomplete_when_nothing_decisive_ran(exp):
    exp["blocks"] = [_block("hd", "header", 1, 0, "Running header")]
    del exp["references"]
    obs = _observation_of(exp)
    obs["blocks"] = []
    score = _rescore(exp, obs)
    assert score["strict_checks"] == 0 and score["verdict"] == "incomplete" and not score["strict_passed"]


def test_mostly_unscored_expectation_is_incomplete_not_passed(exp, obs):
    for block in exp["blocks"]:
        if block["id"] in ("h1", "p1"):
            block["uncertainty"] = {"level": "high"}
    score = _rescore(exp, obs)
    assert score["strict_failures"] == 0 and score["verdict"] == "incomplete"
    assert score["unscored_items"] >= 2


def test_run_result_carries_verdict_and_unscored_count(exp, obs):
    next(b for b in exp["blocks"] if b["id"] == "p2")["uncertainty"] = {"level": "high"}
    score = _rescore(exp, obs)
    result = score_to_result("c1", "i1", score, "e" * 64)
    assert result["verdict"] == "incomplete" and result["unscored_items"] == score["unscored_items"] >= 1
    options = {}
    run = {
        "schema_version": 1, "record_kind": "run", "record_id": "run-2", "started_at": "2026-01-01T00:00:00Z",
        "metric_versions": METRIC_VERSIONS,
        "configurations": [{"id": "c1", "tool": "t", "version": "1", "options": options,
                            "options_sha256": options_sha256(options)}],
        "inputs": [{"id": "i1", "pdf_sha256": PDF_HASH, "expectation_sha256": "d" * 64}],
        "results": [result],
    }
    validate_record(run)


# --- tables -------------------------------------------------------------------------------------


def test_assignment_is_optimal_not_greedy():
    assert _assign([[6, 5], [6, None]]) == [1, 0]
    assert _assign([[None, None]]) == [None]
    assert _assign([[3], [4]]) == [None, 0]


def _second_table(exp_or_obs, block_id="t2", order=20):
    cells = [_cell(0, 0, "K", True), _cell(0, 1, "V", True), _cell(1, 0, "u"), _cell(1, 1, "7.5")]
    exp_or_obs["blocks"].append(_block(block_id, "table", 1, order, None, bbox=BOX,
                                       table={"n_rows": 2, "n_cols": 2, "cells": cells}))


def test_one_observed_table_cannot_satisfy_two_expected_tables(exp):
    exp["blocks"].append(copy.deepcopy(next(b for b in exp["blocks"] if b["id"] == "t1")))
    exp["blocks"][-1].update(id="t1b", reading_order=20, bbox=dict(BOX))
    obs = _observation_of(exp)
    obs["blocks"] = [b for b in obs["blocks"] if b["id"] != "t1b"]
    failed = {sub for name, sub in _failed(_rescore(exp, obs)) if name == "table_exact"}
    assert len(failed) == 1


def test_table_order_on_a_page_is_checked(exp):
    _second_table(exp)
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    score = _rescore(exp, defects.swap_reading_order(obs, "t1", "t2"))
    assert ("table_order", "t1->t2") in _failed(score)
    assert not [c for c in score["checks"] if c["name"] == "table_exact" and not c["passed"]]


def test_extra_cells_or_rows_fail_table_exact_even_when_every_expected_cell_matches(exp, obs):
    table = next(b for b in obs["blocks"] if b["id"] == "t1")["table"]
    table["n_rows"] = 4
    table["cells"] += [_cell(3, 0, "Extra"), _cell(3, 1, "9.9"), _cell(3, 2, "9.9")]
    score = _rescore(exp, obs)
    assert ("table_cell_match", "t1") not in _failed(score) and ("table_exact", "t1") in _failed(score)


def test_header_flag_is_compared_strictly_only(exp, obs):
    _cell_of(obs, 0, 0).pop("is_header")
    score = _rescore(exp, obs)
    assert ("table_exact", "t1") in _failed(score) and ("table_exact", "t1") not in _failed(score, "tolerant")
    assert score["tolerant_only_matches"] >= 1


def test_continuation_without_a_repeated_header_keeps_its_first_row(chain_exp):
    tb = next(b for b in chain_exp["blocks"] if b["id"] == "tb")["table"]
    tb["n_rows"] = 2
    tb["cells"] = [_cell(0, 0, "B"), _cell(0, 1, "+1.25"), _cell(1, 0, "C"), _cell(1, 1, "2.00")]
    obs = _observation_of(chain_exp)
    assert _rescore(chain_exp, obs)["verdict"] == "passed"
    assert _rescore(chain_exp, _merged(obs))["verdict"] == "passed"


# --- equations and captions ---------------------------------------------------------------------


def test_each_observed_equation_matches_one_expected_equation(exp):
    exp["blocks"].append(copy.deepcopy(next(b for b in exp["blocks"] if b["id"] == "e1")))
    exp["blocks"][-1].update(id="e2", reading_order=20, bbox=dict(BOX))
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    obs["blocks"] = [b for b in obs["blocks"] if b["id"] != "e2"]
    assert len({sub for name, sub in _failed(_rescore(exp, obs)) if name == "equation_match"}) == 1


def test_equation_number_and_display_flag_must_agree(exp, obs):
    _equation_of(obs)["number"] = "(2)"
    assert ("equation_match", "e1") in _failed(_rescore(exp, obs))
    obs = _observation_of(exp)
    _equation_of(obs)["display"] = False
    assert ("equation_match", "e1") in _failed(_rescore(exp, obs))


def test_caption_survives_an_extra_earlier_table_but_not_a_wrong_link_or_page(exp, obs):
    for block in obs["blocks"]:
        if block["reading_order"] >= 2:
            block["reading_order"] += 1
    extra = _block("t0", "table", 1, 2, None, bbox=BOX, table={"n_rows": 1, "n_cols": 1, "cells": [_cell(0, 0, "Zed")]})
    obs["blocks"].append(extra)
    assert ("caption_match", "c1") not in _failed(_rescore(exp, obs))
    next(b for b in obs["blocks"] if b["id"] == "c1")["caption_for"] = "t0"
    assert ("caption_match", "c1") in _failed(_rescore(exp, obs))
    moved = _observation_of(exp)
    next(b for b in moved["blocks"] if b["id"] == "c1")["page"] = 2
    assert ("caption_match", "c1") in _failed(_rescore(exp, moved))


# --- references ---------------------------------------------------------------------------------


def test_correct_entry_matching_an_unscored_expected_entry_does_not_lower_precision(exp, obs):
    exp["scope"]["kind"] = obs["scope"]["kind"] = "pages"
    exp["references"]["entries"][1]["truncated"] = "end"
    score = _rescore(exp, obs)
    precision = _metric(score, "reference_segmentation_precision")
    assert (precision["value"], precision["n"]) == (1.0, 1)
    assert score["verdict"] == "incomplete"


def test_unaligned_entries_are_listed_and_tolerant_mode_uses_its_own_alignment(exp, obs):
    entry = _entry(obs, "r1")
    entry["raw"] = RAW1.upper()
    entry["raw_sha256"] = sha256_text(entry["raw"])
    score = _rescore(exp, obs)
    reasons = {(n["metric"], n["subject"]): n["reason"] for n in score["not_scored"]}
    assert "strict mode" in reasons[("reference_field_accuracy", "r1")]
    assert "strict mode" in reasons[("reference_doi_absent_correct", "r1")]
    tolerant = {(c["name"], c["subject"]) for c in score["checks"] if c["mode"] == "tolerant"}
    assert {("reference_field_accuracy", "r1"), ("reference_doi_absent_correct", "r1")} <= tolerant
    assert score["verdict"] == "failed"


def test_reference_ordinals_year_raw_and_empty_fields(exp, obs):
    _entry(exp, "r1")["fields"]["year_raw"] = "2020"
    _entry(exp, "r2")["fields"] = {}
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    assert all(c["total"] > 0 for c in _rescore(exp, obs)["checks"])  # no vacuous 0/0 checks
    _entry(obs, "r1")["fields"]["year_raw"] = "2020a"
    check = next(c for c in _rescore(exp, obs)["checks"] if c["name"] == "reference_field_accuracy"
                 and c["mode"] == "strict")
    assert (check["matched"], check["total"]) == (3, 4) and "year_raw" in check["detail"]
    obs = _observation_of(exp)
    _entry(obs, "r1")["ordinal"] = 3
    assert ("reference_ordinal", "r1") in _failed(_rescore(exp, obs))


def test_expected_entries_with_identical_raw_text_need_two_observed_entries(exp):
    twin = copy.deepcopy(_entry(exp, "r1"))
    twin.update(id="r9", ordinal=3)
    del twin["block_id"]
    exp["references"]["entries"].append(twin)
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    obs["references"]["entries"] = [e for e in obs["references"]["entries"] if e["id"] != "r9"]
    assert len({sub for name, sub in _failed(_rescore(exp, obs)) if name == "reference_segmentation_recall"}) == 1


def test_observed_entries_outside_the_expectation_scope_are_ignored(exp):
    exp["scope"] = {"kind": "pages", "physical_pages": [2]}
    exp["pages"] = [_page(2)]
    exp["blocks"] = [b for b in exp["blocks"] if b["page"] == 2]
    obs = _observation_of(exp)
    obs["scope"] = {"kind": "pages", "physical_pages": [1, 2]}
    obs["pages"] = [_page(1), _page(2)]
    obs["references"]["entries"].append({"id": "r7", "ordinal": 3, "page": 1, "raw": "[0] Elsewhere.",
                                         "doi": {"status": "not_found"}})
    score = _rescore(exp, obs)
    assert _metric(score, "reference_segmentation_precision")["value"] == 1.0
    assert score["verdict"] == "passed"


def test_score_files_refuses_a_pdf_hash_mismatch(exp, obs, tmp_path):
    obs["document"]["pdf_sha256"] = "c" * 64
    (tmp_path / "e.json").write_text(json.dumps(exp), encoding="utf-8")
    (tmp_path / "o.json").write_text(json.dumps(obs), encoding="utf-8")
    with pytest.raises(ScoringError, match="pdf_sha256 mismatch"):
        score_files(tmp_path / "e.json", tmp_path / "o.json")


def test_chain_with_an_uncertain_fragment_is_not_hash_scored(chain_exp, chain_obs):
    next(b for b in chain_exp["blocks"] if b["id"] == "pb")["uncertainty"] = {"level": "high"}
    score = _rescore(chain_exp, _merged(chain_obs))
    assert not [c for c in score["checks"] if c["name"] == "text_span_hash" and c["subject"] == "pa+pb"]
    assert score["strict_failures"] == 0 and score["verdict"] == "incomplete"


def test_defects_reject_hyphens_and_identical_columns(obs):
    _set_text(obs, "p2", "A well-known fact, 3-4 times.")
    with pytest.raises(ValueError):
        defects.drop_sign(obs, "p2")
    table = next(b for b in obs["blocks"] if b["id"] == "t1")["table"]
    for cell in table["cells"]:
        if cell["col"] == 2:
            cell["text"] = next(c["text"] for c in table["cells"] if c["col"] == 1 and c["row"] == cell["row"])
    with pytest.raises(ValueError):
        defects.reorder_columns(obs, "t1", [0, 2, 1])


# --- tolerant-only matches, remaining strict gaps -----------------------------------------------------


def test_tolerant_mode_is_never_below_strict_in_the_same_check():
    checks = _Checks()
    checks.add("text_presence", "x", (1, 1), (0, 1))
    assert [c["passed"] for c in checks.checks] == [True, True]


def test_unicode_whitespace_and_latex_spacing_are_tolerant_only(exp):
    _set_text(exp, "p2", "The final value is 3.14.")
    obs = _observation_of(exp)
    _set_text(obs, "p2", "The final value is ３.１４.")  # full-width digits: only NFKC maps them
    _equation_of(obs)["latex"] = "y = -0.5x + 1"
    score = _rescore(exp, obs)
    assert {("text_presence", "p2"), ("equation_match", "e1")} <= _failed(score)
    assert not {("text_presence", "p2"), ("equation_match", "e1")} & _failed(score, "tolerant")
    assert score["tolerant_only_matches"] >= 2 and score["verdict"] == "failed"


def test_doi_prefix_is_tolerant_only(exp, obs):
    _entry(obs, "r2")["doi"] = {"status": "present", "value": "doi:10.0000/Synthetic"}
    score = _rescore(exp, obs)
    assert ("reference_doi_recovery", "r2") in _failed(score)
    assert ("reference_doi_recovery", "r2") not in _failed(score, "tolerant")


def test_uncertain_equations_and_page_spanning_captions_are_not_scored(exp, obs):
    next(b for b in exp["blocks"] if b["id"] == "e1")["uncertainty"] = {"level": "high"}
    _equation_of(obs)["latex"] = "z"
    exp["blocks"].append(_block("c1b", "caption", 2, 20, "Table 1 (continued).", continues_from="c1", bbox=dict(BOX)))
    obs["blocks"].append(_block("c1b", "caption", 2, 20, "Table 1 (continued).", continues_from="c1", bbox=dict(BOX)))
    score = _rescore(exp, obs)
    reasons = {(n["metric"], n["subject"]): n["reason"] for n in score["not_scored"]}
    assert "high uncertainty" in reasons[("equation_match", "e1")]
    assert "spans pages" in reasons[("caption_match", "c1")] and ("caption_match", "c1b") in reasons
    assert not [c for c in score["checks"] if c["subject"] in ("e1", "c1", "c1b") and c["name"] != "text_precision"]


def test_captions_of_two_figures_must_keep_their_figures(exp):
    for n, order in (("fa", 20), ("fb", 21)):
        exp["blocks"].append(_block(n, "figure", 1, order, None, graphic={"kind": "chart"},
                                    bbox={"x0": 10, "y0": 100 + 50 * (order - 20), "x1": 500, "y1": 140 + 50 * (order - 20)}))
    exp["blocks"] += [
        _block("ca", "caption", 1, 22, "Figure 1. First.", caption_for="fa", bbox={"x0": 10, "y0": 300, "x1": 500, "y1": 330}),
        _block("cb", "caption", 1, 23, "Figure 2. Second.", caption_for="fb", bbox={"x0": 10, "y0": 340, "x1": 500, "y1": 370}),
    ]
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    next(b for b in obs["blocks"] if b["id"] == "ca")["caption_for"] = "fb"
    next(b for b in obs["blocks"] if b["id"] == "cb")["caption_for"] = "fa"
    failed = _failed(_rescore(exp, obs))
    assert {("caption_match", "ca"), ("caption_match", "cb")} <= failed


def test_reference_end_page_must_agree_when_stated(chain_exp):
    chain_exp["document"]["page_count"] = 4
    chain_exp["scope"]["physical_pages"] = [1, 2, 3, 4]
    chain_exp["pages"].append(_page(4))
    obs = _observation_of(chain_exp)
    assert _rescore(chain_exp, obs)["verdict"] == "passed"
    entry = obs["references"]["entries"][0]
    del entry["block_id"]
    entry["end_page"] = 4
    assert ("reference_segmentation_recall", "r3") in _failed(_rescore(chain_exp, obs))


# --- second review round -----------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("## **Alpha Heading**", "Alpha Heading"),
    ("# of trials was 5", "of trials was 5"),
    ("## 1. Introduction", "1. Introduction"),
    ("1. Introduction", "1. Introduction"),
    ("- first item", "first item"),
    ("- a\n- b", "a\nb"),
    ("text\n- 5", "text\n- 5"),
    ("see # of trials", "see # of trials"),
    ("The **effect** is __large__", "The effect is large"),
    ("**Alpha\nHeading** done", "Alpha\nHeading done"),
    ("a * b", "a * b"),
    ("x**2 + y**2", "x**2 + y**2"),
    ("x**2 and y** z", "x**2 and y** z"),
    ("a ** b ** c", "a ** b ** c"),
    ("2 * 3 * 4", "2 * 3 * 4"),
    ("snake_case_name and __init__x", "snake_case_name and __init__x"),
    ("_italic_ and *italic*", "_italic_ and *italic*"),
    ("**bold**text", "**bold**text"),
    ("- 5 apples", "5 apples"),
])
def test_markup_stripping_is_narrow(raw, expected):
    assert _strip_markup(raw) == expected


def test_real_pymupdf4llm_output_passes_strict(exp):
    exp["blocks"].append(_block("li", "list_item", 1, 20, "first item", bbox=dict(BOX)))
    obs = _observation_of(exp)
    _set_text(obs, "h1", "## **Alpha Heading**")
    _set_text(obs, "li", "- first item")
    _set_text(obs, "p1", "The **effect** is -0.5 at p < .05 for Group A.")
    score = _rescore(exp, obs)
    assert score["verdict"] == "passed", _decisive(score)
    # Markup is not a licence to change content: a lost sign or wrong digit still fails.
    _set_text(obs, "p1", "The **effect** is 0.5 at p < .05 for Group A.")
    assert ("text_presence", "p1") in _decisive(_rescore(exp, obs))


def test_expected_text_and_cells_are_never_stripped(exp, obs):
    _set_text(exp, "p2", "# of trials was 5")
    obs = _observation_of(exp)
    _set_text(obs, "p2", "of trials was 5")
    assert ("text_presence", "p2") in _decisive(_rescore(exp, obs))
    exp2 = _expectation()
    table = next(b for b in exp2["blocks"] if b["id"] == "t1")["table"]
    next(c for c in table["cells"] if (c["row"], c["col"]) == (1, 1))["text"] = "# 7"
    obs2 = _observation_of(exp2)
    _cell_of(obs2, 1, 1)["text"] = "7"
    assert ("table_exact", "t1") in _decisive(_rescore(exp2, obs2))


def test_arithmetic_survives_markup_stripping(exp):
    _set_text(exp, "p2", "Compute a * b and x**2 + y**2 here.")
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    _set_text(obs, "p2", "Compute a b and x2 + y2 here.")
    assert ("text_presence", "p2") in _decisive(_rescore(exp, obs))


def _paged(exp):
    exp["scope"] = {"kind": "pages", "physical_pages": [2]}
    exp["pages"] = [_page(2)]
    exp["blocks"] = [b for b in exp["blocks"] if b["page"] == 2]


def test_ordinals_need_one_constant_offset_in_page_scoped_records(exp):
    _paged(exp)
    obs = _observation_of(exp)
    obs["scope"] = {"kind": "pages", "physical_pages": [2]}
    obs["pages"] = [_page(2)]
    for entry in obs["references"]["entries"]:
        entry["ordinal"] += 10
    assert _rescore(exp, obs)["verdict"] == "passed"
    obs["references"]["entries"][1]["ordinal"] += 3
    assert len({sub for name, sub in _decisive(_rescore(exp, obs)) if name == "reference_ordinal"}) == 1


def test_ordinals_are_absolute_in_complete_document_records(exp, obs):
    for entry in obs["references"]["entries"]:
        entry["ordinal"] += 10
    assert {("reference_ordinal", "r1"), ("reference_ordinal", "r2")} <= _decisive(_rescore(exp, obs))


def test_extra_copies_joined_words_and_appended_text_count_as_invented(exp, obs):
    exp["blocks"].append(_block("d1", "paragraph", 1, 20, "Repeated sentence.", bbox=dict(BOX)))
    obs = _observation_of(exp)
    obs["blocks"].append(_block("dup", "paragraph", 1, 21, "Repeated sentence.", bbox={"x0": 10, "y0": 650, "x1": 500, "y1": 690}))
    failed = {sub for name, sub in _decisive(_rescore(exp, obs)) if name == "text_precision"}
    assert len(failed) == 1 and failed <= {"d1", "dup"}
    for text in ("Alpha Heading2", "xAlpha Heading", "Alpha Headings"):
        bad = _observation_of(exp)
        _set_text(bad, "h1", text)
        assert ("text_presence", "h1") in _decisive(_rescore(exp, bad)), text


def test_every_observed_block_counts_for_precision_including_unrecorded_headers(exp, obs):
    obs["blocks"].append(_block("hd", "header", 1, 20, "Journal of Things 12", bbox=dict(BOX)))
    score = _rescore(exp, obs)
    assert ("text_precision", "hd") in _decisive(score) and score["verdict"] == "failed"
    exp["blocks"].append(_block("hd", "header", 1, 20, "Journal of Things 12", bbox=dict(BOX)))
    assert _rescore(exp, _observation_of(exp))["verdict"] == "passed"


def test_explicit_null_text_is_listed_not_silently_passed(exp):
    exp["blocks"].append(_block("pn", "paragraph", 1, 20, None, bbox=dict(BOX)))
    exp["blocks"][-1]["text"] = None
    score = _rescore(exp, _observation_of(exp))
    assert any(n["subject"] == "pn" and "no text" in n["reason"] for n in score["not_scored"])
    assert score["verdict"] == "incomplete"


def test_expected_figures_are_checked_for_presence(exp):
    exp["blocks"].append(_block("fa", "figure", 1, 20, None, graphic={"kind": "chart"}, bbox=dict(BOX)))
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    obs["blocks"] = [b for b in obs["blocks"] if b["id"] != "fa"]
    score = _rescore(exp, obs)
    assert ("figure_match", "fa") in _decisive(score) and score["verdict"] == "failed"


def test_not_assessed_doi_is_exempt_but_citation_markers_are_listed(exp):
    _entry(exp, "r2")["doi"] = {"status": "not_assessed"}
    assert _rescore(exp, _observation_of(exp))["verdict"] == "passed"
    exp["references"]["markers"] = [{"id": "m1", "page": 1, "text": "[1]", "target_status": "resolved",
                                     "entry_ids": ["r1"]}]
    score = _rescore(exp, _observation_of(exp))
    assert {"metric": "reference_markers", "subject": "<all>",
            "reason": "citation-marker links are not scored"} in score["not_scored"]
    assert score["verdict"] == "incomplete"


def test_extra_observed_tables_equations_and_captions_fail(exp, obs):
    base = _rescore(exp, obs)
    assert base["verdict"] == "passed"
    extra_table = copy.deepcopy(obs)
    extra_table["blocks"].append(_block("tx", "table", 1, 20, None, bbox=dict(BOX),
                                        table={"n_rows": 1, "n_cols": 1, "cells": [_cell(0, 0, "Zed")]}))
    assert ("table_precision", "tx") in _decisive(_rescore(exp, extra_table))
    extra_eq = copy.deepcopy(obs)
    extra_eq["blocks"].append(_block("ex", "equation", 2, 20, None, bbox=dict(BOX),
                                     equation={"latex": "z=1", "number": "(9)", "display": True}))
    assert ("equation_precision", "ex") in _decisive(_rescore(exp, extra_eq))
    extra_cap = copy.deepcopy(obs)
    extra_cap["blocks"].append(_block("cx", "caption", 1, 20, "Table 9. Invented.", bbox=dict(BOX)))
    assert ("caption_precision", "cx") in _decisive(_rescore(exp, extra_cap))


def test_unscored_informational_items_do_not_make_the_verdict_incomplete():
    checks = _Checks()
    checks.add("text_presence", "x", (1, 1), (1, 1))
    checks.skip("text_span_hash", "x", "chain has unscored or textless fragments")
    assert _verdict(checks)[0] == "passed"
    checks.skip("text_presence", "y", "expected block marked high uncertainty")
    assert _verdict(checks)[0] == "incomplete"


def test_equal_cell_matches_prefer_the_identical_grid(exp, obs):
    table = next(b for b in obs["blocks"] if b["id"] == "t1")
    wider = copy.deepcopy(table)
    wider.update(id="t1w", reading_order=20, bbox=dict(BOX))
    wider["table"]["n_rows"] = 4
    wider["table"]["cells"] += [_cell(3, 0, "Extra"), _cell(3, 1, "1"), _cell(3, 2, "2")]
    for block in obs["blocks"]:
        if block["reading_order"] >= 2 and block is not wider:
            block["reading_order"] += 1
    wider["reading_order"] = 2  # earlier than the exact copy, so a first-found tie-break would pick it
    obs["blocks"].append(wider)
    score = _rescore(exp, obs)
    assert ("table_exact", "t1") not in _failed(score)


def test_assignment_runs_per_page_group_and_stays_optimal():
    n = 20
    scores = [[(5 if i == j else 1) if i // 10 == j // 10 else None for j in range(n)] for i in range(n)]
    assert _assign_groups(scores) == list(range(n))
    assert _assign_groups([]) == []


def test_score_files_keeps_crlf_in_the_markdown_text(exp, obs, tmp_path):
    markdown = "# Alpha Heading\r\nbody\r\n"
    spanned = _with_spans(obs, markdown)
    (tmp_path / "e.json").write_text(json.dumps(exp), encoding="utf-8")
    (tmp_path / "o.json").write_text(json.dumps(spanned), encoding="utf-8")
    (tmp_path / "m.md").write_bytes(markdown.encode("utf-8"))
    score = score_files(tmp_path / "e.json", tmp_path / "o.json", markdown_path=tmp_path / "m.md")
    assert score["verdict"] == "passed", _decisive(score)


def test_merged_block_attributed_to_the_later_page_still_passes(chain_exp, chain_obs):
    merged = _merged(chain_obs)
    block = next(b for b in merged["blocks"] if b["id"] == "pa")
    block["page"] = 2
    score = _rescore(chain_exp, merged)
    assert ("text_presence", "pa") not in _failed(score) and ("text_presence", "pb") not in _failed(score)
    assert ("reading_order", "pb") not in _failed(score)


def test_one_of_two_identical_paragraphs_dropped_is_missing_not_displaced(exp):
    for name, order in (("d1", 20), ("d2", 21)):
        exp["blocks"].append(_block(name, "paragraph", 1, order, "Repeated sentence.",
                                    bbox={"x0": 10, "y0": 600 + 40 * (order - 20), "x1": 500, "y1": 630 + 40 * (order - 20)}))
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    score = _rescore(exp, defects.omit_text(obs, "d2"))
    assert ("text_presence", "d2") in _failed(score)
    assert not [c for c in score["checks"] if c["name"] == "reading_order" and not c["passed"]]


def test_identical_expected_captions_need_two_observed_captions(exp):
    exp["blocks"].append(_block("c2", "caption", 1, 20, "Table 1. Synthetic group means.", bbox=dict(BOX)))
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    obs["blocks"] = [b for b in obs["blocks"] if b["id"] != "c2"]
    assert ("caption_match", "c2") in _decisive(_rescore(exp, obs))


# --- review of PR 173 --------------------------------------------------------------------------


def test_merged_block_with_fragments_in_reverse_order_fails(chain_exp, chain_obs):
    for later_page in (False, True):
        merged = _merged(chain_obs)
        block = next(b for b in merged["blocks"] if b["id"] == "pa")
        _set_text(merged, "pa", PB + "\n" + PA)
        if later_page:
            block["page"] = 2
        score = _rescore(chain_exp, merged)
        assert score["verdict"] == "failed", later_page
        assert {("text_presence", "pa"), ("text_presence", "pb")} & _decisive(score), later_page


def test_reference_recorded_as_block_and_entry_is_covered_once(exp, obs):
    obs["blocks"].append(_block("pz", "paragraph", 2, 20, RAW1, bbox=dict(BOX)))
    score = _rescore(exp, obs)
    assert {("text_precision", "r1b"), ("text_precision", "pz")} & _decisive(score)
    assert score["verdict"] == "failed"
    assert _rescore(exp, _observation_of(exp))["verdict"] == "passed"


def test_out_of_scope_copy_cannot_use_up_an_expected_occurrence(exp):
    _paged(exp)
    exp["blocks"].append(_block("hd", "header", 2, 20, "Journal of Things 12", bbox=dict(BOX)))
    obs = _observation_of(exp)
    obs["scope"] = {"kind": "pages", "physical_pages": [1, 2]}
    obs["pages"] = [_page(1), _page(2)]
    obs["blocks"].append(_block("hd1", "header", 1, 21, "Journal of Things 12", bbox=dict(BOX)))
    score = _rescore(exp, obs)
    assert ("text_precision", "hd") not in _decisive(score), _decisive(score)
    assert score["verdict"] == "passed"


def test_text_on_a_matched_table_or_equation_block_is_not_invented(exp, obs):
    _set_text(obs, "e1", "y=-0.5x+1")
    _set_text(obs, "t1", "Group Mean Delta Control 4.25 -0.50 Treated 5.75 +1.25")
    score = _rescore(exp, obs)
    assert score["verdict"] == "passed", _decisive(score)
    assert ("text_precision", "e1") in {(c["name"], c["subject"]) for c in score["checks"]}
    # The same text on a block that matched nothing is still invented.
    _equation_of(obs)["latex"] = "z=7"
    extra = _block("ex", "equation", 2, 20, "y=-0.5x+1", bbox=dict(BOX),
                   equation={"latex": "q=1", "number": "(8)", "display": True})
    obs["blocks"].append(extra)
    failed = _decisive(_rescore(exp, obs))
    assert ("text_precision", "ex") in failed and ("equation_match", "e1") in failed


def test_reference_blocks_without_a_references_section_are_still_compared(exp):
    del exp["references"]
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    lost = copy.deepcopy(obs)
    lost["blocks"] = [b for b in lost["blocks"] if b["type"] != "reference_entry"]
    score = _rescore(exp, lost)
    assert {("text_presence", "r1b"), ("text_presence", "r2b")} <= _decisive(score)
    assert score["verdict"] == "failed"


def test_matched_payload_text_must_only_serialize_the_payload(exp, obs):
    markdown_table = ("| Group | Mean | Delta |\n| --- | --- | --- |\n| Control | 4.25 | -0.50 |\n"
                      "| Treated | 5.75 | +1.25 |")
    _set_text(obs, "t1", markdown_table)
    _set_text(obs, "e1", "y = -0.5x + 1")
    score = _rescore(exp, obs)
    assert score["verdict"] == "passed", _decisive(score)
    for block_id, text in (("e1", "Invented clinical result: treatment cures all patients."),
                           ("t1", markdown_table + " Invented clinical result.")):
        bad = copy.deepcopy(obs)
        _set_text(bad, block_id, text)
        failed = _decisive(_rescore(exp, bad))
        assert ("text_precision", block_id) in failed, block_id
        assert ("equation_match", "e1") not in failed and ("table_exact", "t1") not in failed


def test_identical_adjacent_fragments_merged_on_either_page_pass(chain_exp, chain_obs):
    same = "Same words repeat here."
    for record in (chain_exp, chain_obs):
        _set_text(record, "pa", same)
        _set_text(record, "pb", same)
    assert _rescore(chain_exp, chain_obs)["verdict"] == "passed"
    for later_page in (False, True):
        merged = _merged(chain_obs)
        _set_text(merged, "pa", same + "\n" + same)
        if later_page:
            next(b for b in merged["blocks"] if b["id"] == "pa")["page"] = 2
        score = _rescore(chain_exp, merged)
        assert score["verdict"] == "passed", (later_page, _decisive(score))
    one_copy = _merged(chain_obs)
    _set_text(one_copy, "pa", same)
    assert {("text_presence", "pa"), ("text_presence", "pb")} & _decisive(_rescore(chain_exp, one_copy))


def test_payload_text_does_not_use_up_a_genuine_paragraphs_occurrence(exp):
    exp["blocks"].append(_block("pq", "paragraph", 2, 20, "y=-0.5x+1", bbox=dict(BOX)))
    obs = _observation_of(exp)
    _set_text(obs, "e1", "y=-0.5x+1")  # serializes the matched equation's LaTeX
    score = _rescore(exp, obs)
    assert ("text_precision", "pq") not in _decisive(score), _decisive(score)
    assert score["verdict"] == "passed"
    # The paragraph must still be real: without it the repeated text is invented.
    obs["blocks"] = [b for b in obs["blocks"] if b["id"] != "pq"]
    obs["blocks"].append(_block("pq2", "paragraph", 2, 21, "y=-0.5x+1", bbox={"x0": 10, "y0": 650, "x1": 500, "y1": 690}))
    exp["blocks"] = [b for b in exp["blocks"] if b["id"] != "pq"]
    assert ("text_precision", "pq2") in _decisive(_rescore(exp, obs))


def test_cross_block_coincidence_does_not_steal_a_real_blocks_text(exp):
    for n, (text, order) in enumerate((("A Beta", 20), ("Gamma", 21), ("Beta Gamma", 22))):
        y = 600 + 40 * n
        exp["blocks"].append(_block(f"w{n}", "paragraph", 1, order, text, bbox={"x0": 10, "y0": y, "x1": 500, "y1": y + 30}))
    obs = _observation_of(exp)
    score = _rescore(exp, obs)
    assert score["verdict"] == "passed", _decisive(score)
    assert not [c for c in score["checks"] if c["name"] == "text_precision" and not c["passed"]]
    # Real invented text next to the coincidence is still caught.
    obs["blocks"].append(_block("wz", "paragraph", 1, 23, "Beta Beta", bbox={"x0": 10, "y0": 730, "x1": 500, "y1": 760}))
    assert ("text_precision", "wz") in _decisive(_rescore(exp, obs))


def test_merged_chain_displaced_before_unrelated_content_fails(chain_exp, chain_obs):
    merged = _merged(chain_obs)
    assert _rescore(chain_exp, merged)["verdict"] == "passed"
    displaced = _rescore(chain_exp, defects.swap_reading_order(merged, "h1", "pa"))
    assert ("reading_order", "pa") in _failed(displaced) and displaced["verdict"] == "failed"


def _with_footnote(record):
    for block in record["blocks"]:
        if block["reading_order"] >= 2:
            block["reading_order"] += 1
    record["blocks"].append(_block("fn", "footnote", 1, 2, "1. A footnote between the halves.",
                                   bbox={"x0": 10, "y0": 740, "x1": 500, "y1": 770}))


def test_footnote_interleaved_between_merged_fragments_is_still_fine(chain_exp, chain_obs):
    _with_footnote(chain_exp)
    obs = _observation_of(chain_exp)
    assert _rescore(chain_exp, obs)["verdict"] == "passed"
    merged = _merged(obs)  # one block holds both halves; the footnote sits after it
    score = _rescore(chain_exp, merged)
    assert score["verdict"] == "passed", _decisive(score)
    # ...but the same merged block pushed behind the footnote is out of order.
    behind = defects.swap_reading_order(merged, "pa", "fn")
    assert ("reading_order", "pa") in _failed(_rescore(chain_exp, behind)) or (
        "reading_order", "fn") in _failed(_rescore(chain_exp, behind))


def test_a_whole_block_is_preferred_over_a_coincidence_inside_another_block(exp, obs):
    exp["blocks"].append(_block("wb", "paragraph", 1, 20, "Beta", bbox={"x0": 10, "y0": 600, "x1": 500, "y1": 630}))
    obs = _observation_of(exp)
    obs["blocks"].append(_block("wa", "paragraph", 1, 19, "Alpha Beta", bbox={"x0": 10, "y0": 560, "x1": 500, "y1": 590}))
    failed = {sub for name, sub in _decisive(_rescore(exp, obs)) if name == "text_precision"}
    # The invented "Alpha Beta" is flagged, not the genuine "Beta" block that follows it.
    assert failed == {"wa"}


def test_constrained_equation_claims_its_candidate_before_a_looser_twin(exp):
    for name, order, display in (("xa", 20, None), ("xb", 21, True)):
        payload = {"latex": "x=1", "number": None}
        if display is not None:
            payload["display"] = display
        exp["blocks"].append(_block(name, "equation", 2, order, None, equation=payload,
                                    bbox={"x0": 10, "y0": 300 + 40 * (order - 20), "x1": 500, "y1": 330 + 40 * (order - 20)}))
    obs = _observation_of(exp)
    next(b for b in obs["blocks"] if b["id"] == "xa")["equation"]["display"] = True   # listed first
    next(b for b in obs["blocks"] if b["id"] == "xb")["equation"]["display"] = False
    score = _rescore(exp, obs)
    assert {("equation_match", "xa"), ("equation_match", "xb")}.isdisjoint(_failed(score)), _failed(score)
    assert score["verdict"] == "passed"
    # Results are still reported in reading order.
    assert [c["subject"] for c in score["checks"] if c["name"] == "equation_match" and c["mode"] == "strict"] == [
        "e1", "xa", "xb"]


def test_linked_caption_claims_its_candidate_before_an_unlinked_twin(exp):
    for block in exp["blocks"]:
        if block["reading_order"] >= 1:
            block["reading_order"] += 1
    exp["blocks"].append(_block("c0", "caption", 1, 1, "Table 1. Synthetic group means.",
                                bbox={"x0": 10, "y0": 700, "x1": 500, "y1": 730}))
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    assert [b["id"] for b in obs["blocks"] if b["type"] == "caption"] == ["c1", "c0"]
    score = _rescore(exp, obs)
    assert [c["subject"] for c in score["checks"] if c["name"] == "caption_match" and c["mode"] == "strict"] == [
        "c0", "c1"]


def test_page_two_text_claiming_an_earlier_reading_order_is_displaced(exp):
    exp["blocks"].append(_block("q2", "paragraph", 2, 20, "A late paragraph on page two.",
                                bbox={"x0": 10, "y0": 650, "x1": 500, "y1": 690}))
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    for block in obs["blocks"]:
        block["reading_order"] += 1
    next(b for b in obs["blocks"] if b["id"] == "q2")["reading_order"] = 0  # before every page-1 block
    score = _rescore(exp, obs)
    assert ("reading_order", "q2") in _failed(score) and score["verdict"] == "failed"
    assert ("text_presence", "q2") not in _failed(score)  # it is on the right page, just out of order


def test_expected_payload_text_pays_for_one_occurrence_only(exp):
    _set_text(exp, "e1", "y=-0.5x+1")  # the expectation prints the equation's text as well
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    obs["blocks"].append(_block("pz", "paragraph", 2, 20, "y=-0.5x+1", bbox=dict(BOX)))
    score = _rescore(exp, obs)
    assert ("text_precision", "pz") in _decisive(score) and score["verdict"] == "failed"
    # A genuine expected paragraph with the same text still covers its own copy.
    exp["blocks"].append(_block("pq", "paragraph", 2, 20, "y=-0.5x+1", bbox=dict(BOX)))
    assert _rescore(exp, _observation_of(exp))["verdict"] == "passed"


def test_table_order_is_ranked_by_document_wide_reading_order_across_pages(exp):
    cells = [_cell(0, 0, "K", True), _cell(0, 1, "V", True), _cell(1, 0, "u"), _cell(1, 1, "7.5")]
    exp["blocks"].append(_block("t2", "table", 2, 20, None, bbox=dict(BOX),
                                table={"n_rows": 2, "n_cols": 2, "cells": cells}))
    obs = _observation_of(exp)
    assert _rescore(exp, obs)["verdict"] == "passed"
    swapped = _rescore(exp, defects.swap_reading_order(obs, "t1", "t2"))
    assert ("table_order", "t1->t2") in _failed(swapped)


def test_table_cell_with_literal_pipes_still_serializes_as_payload(exp, obs):
    for record in (exp, obs):
        _cell_of(record, 1, 0)["text"] = "|Control|"
    markdown = ("| Group | Mean | Delta |\n| --- | --- | --- |\n| |Control| | 4.25 | -0.50 |\n"
                "| Treated | 5.75 | +1.25 |")
    _set_text(obs, "t1", markdown)
    score = _rescore(exp, obs)
    assert score["verdict"] == "passed", _decisive(score)
    _set_text(obs, "t1", markdown + "\n| Invented | row |")
    assert ("text_precision", "t1") in _decisive(_rescore(exp, obs))


def test_appended_payloadless_table_or_equation_blocks_fail_precision(exp, obs):
    assert _rescore(exp, obs)["verdict"] == "passed"
    for kind, page, metric in (("table", 1, "table_precision"), ("equation", 2, "equation_precision")):
        bad = copy.deepcopy(obs)
        bad["blocks"].append(_block("ghost", kind, page, 20, None, bbox=dict(BOX)))
        score = _rescore(exp, bad)
        assert (metric, "ghost") in _decisive(score) and score["verdict"] == "failed", kind
    # Its text being accounted for elsewhere is enough: the extractor emitted the paragraph as a table.
    moved = copy.deepcopy(obs)
    moved["blocks"] = [b for b in moved["blocks"] if b["id"] != "p2"]
    moved["blocks"].append(_block("ghost", "table", 1, 2 + 100, "Second paragraph with Mixed Case and x = 3.14.",
                                  bbox=dict(BOX)))
    score = _rescore(exp, moved)
    assert ("table_precision", "ghost") not in _decisive(score)


def test_payloadless_blocks_are_not_scored_when_expected_items_on_the_page_cannot_be_compared(exp, obs):
    next(b for b in exp["blocks"] if b["id"] == "t1")["uncertainty"] = {"level": "high"}
    next(b for b in exp["blocks"] if b["id"] == "e1")["uncertainty"] = {"level": "high"}
    obs = _observation_of(exp)
    obs["blocks"].append(_block("gt", "table", 1, 20, None, bbox=dict(BOX)))
    obs["blocks"].append(_block("ge", "equation", 2, 21, None, bbox=dict(BOX)))
    score = _rescore(exp, obs)
    listed = {(n["metric"], n["subject"]) for n in score["not_scored"]}
    assert {("table_precision", "gt"), ("equation_precision", "ge")} <= listed
    assert not {("table_precision", "gt"), ("equation_precision", "ge")} & _decisive(score)
    assert score["verdict"] == "incomplete"


def test_partially_payloaded_observed_table_chain_stays_in_the_inventory(chain_exp, chain_obs):
    del next(b for b in chain_obs["blocks"] if b["id"] == "tb")["table"]
    score = _rescore(chain_exp, chain_obs)
    assert {("table_exact", "ta+tb"), ("table_precision", "ta"), ("table_precision", "tb")} & _decisive(score)
    assert score["verdict"] == "failed"
