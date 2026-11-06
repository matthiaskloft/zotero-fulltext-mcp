"""Extraction-evaluation artifact schemas: purely synthetic records, no PDFs or real text."""

import copy
import json

import pytest

from extraction import (
    RECORD_KINDS,
    RecordError,
    load_record,
    load_reviewed_expectation,
    options_sha256,
    schema_path,
    sha256_text,
    validate_record,
)

PDF_HASH = "a" * 64
IMAGE_HASH = "b" * 64


def _page(number: int, **extra):
    page = {
        "physical_page": number,
        "printed_label": "iv" if number == 1 else None,
        "geometry": {"unit": "pt", "origin": "top-left", "width": 600, "height": 800},
        "render": {"renderer": "synthetic-renderer", "renderer_version": "1.0", "dpi": 144, "image_sha256": IMAGE_HASH},
    }
    page.update(extra)
    return page


def _block(block_id, kind, page, order, text=None, **extra):
    block = {
        "id": block_id, "type": kind, "page": page, "reading_order": order,
        "bbox": {"x0": 10, "y0": 10 + 50 * order, "x1": 500, "y1": 40 + 50 * order},
    }
    if text is not None:
        block["text"] = text
        block["text_sha256"] = sha256_text(text)
    block.update(extra)
    return block


def _layout(kind: str) -> dict:
    record = {
        "schema_version": 1,
        "record_kind": kind,
        "record_id": f"{kind}-synthetic-1",
        "document": {"pdf_sha256": PDF_HASH, "title": "Synthetic Test Document", "page_count": 3},
        "scope": {"kind": "pages", "physical_pages": [1, 2]},
        "pages": [
            _page(1, body_start={"block_id": "b1"}, body_end={"block_id": "b4"}),
            _page(2),
        ],
        "blocks": [
            _block("b1", "heading", 1, 0, "Alpha Heading"),
            _block("b2", "paragraph", 1, 1, "Value is -0.5 at p < .05."),
            _block("b3", "table", 1, 2, None, table={"n_rows": 2, "n_cols": 2, "cells": [
                {"row": 0, "col": 0, "text": "x", "is_header": True},
                {"row": 0, "col": 1, "text": "y", "is_header": True},
                {"row": 1, "col": 0, "text": "-1.0"},
                {"row": 1, "col": 1, "text": "2.50"},
            ]}),
            _block("b4", "caption", 1, 3, "Table 1. Synthetic values.", caption_for="b3"),
            _block("b5", "equation", 2, 4, None, equation={"latex": "a=-b", "number": "(1)", "display": True}),
            _block("b6", "figure", 2, 5, None, graphic={"kind": "chart"}),
            _block("b7", "reference_entry", 2, 6, "[1] A. Author. Synthetic paper. 2020."),
        ],
        "references": {
            "entries": [
                {"id": "r1", "ordinal": 1, "page": 2, "block_id": "b7", "raw": "[1] A. Author. Synthetic paper. 2020.",
                 "raw_sha256": sha256_text("[1] A. Author. Synthetic paper. 2020."),
                 "fields": {"title": "Synthetic paper", "authors": [{"family": "Author", "given": "A."}], "year": 2020},
                 "doi": {"status": "absent_in_source"}},
                {"id": "r2", "ordinal": 2, "page": 2, "raw": "[2] B. Writer. Another. 2021. doi:10.0000/synthetic",
                 "doi": {"status": "present", "value": "10.0000/synthetic"}},
            ],
            "markers": [{"id": "m1", "page": 1, "block_id": "b2", "text": "[1]", "entry_ids": ["r1"]}],
        },
    }
    if kind == "expectation":
        record["provenance"] = {
            "proposed_by": {"tool": "synthetic-extractor", "version": "0.1"},
            "review": {"status": "reviewed", "reviewer": "reviewer-a", "reviewed_at": "2026-01-01T00:00:00Z",
                       "basis": "page_image_inspection"},
            "uncertainty": {"level": "low"},
        }
    else:
        record["provenance"] = {"produced_by": {"tool": "synthetic-extractor", "version": "0.1", "model": "none"},
                                "created_at": "2026-01-01T00:00:00Z"}
        for entry in record["references"]["entries"]:
            entry["doi"] = {"status": "not_found"} if entry["doi"]["status"] != "present" else entry["doi"]
    return record


def _run() -> dict:
    options = {"dpi": 144}
    return {
        "schema_version": 1, "record_kind": "run", "record_id": "run-1",
        "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:05:00Z",
        "environment": {"python": "3.11"},
        "metric_versions": {"text_presence": "1"},
        "configurations": [{"id": "c1", "tool": "synthetic-extractor", "version": "0.1", "options": options,
                            "options_sha256": options_sha256(options)},
                           {"id": "c2", "tool": "missing-tool", "version": "0", "options_sha256": "c" * 64}],
        "inputs": [{"id": "i1", "pdf_sha256": PDF_HASH, "expectation_sha256": "d" * 64, "physical_pages": [1, 2]}],
        "results": [
            {"configuration_id": "c1", "input_id": "i1", "status": "ok", "output_sha256": "e" * 64,
             "latency_seconds": 1.5, "resources": {"cpu_seconds": 1.2, "peak_memory_mb": 300, "cost_usd": 0},
             "metrics": [{"name": "text_presence", "version": "1", "mode": "strict", "value": 0.9, "n": 10},
                         {"name": "text_presence", "version": "1", "mode": "tolerant", "value": 0.95, "n": 10}]},
            {"configuration_id": "c2", "input_id": "i1", "status": "missing_backend",
             "error": {"kind": "BackendNotInstalled", "message": "backend not found"}},
        ],
    }


@pytest.mark.parametrize("make", [lambda: _layout("expectation"), lambda: _layout("observation"), _run])
def test_valid_records_pass_and_round_trip(make, tmp_path):
    record = make()
    kind = validate_record(record)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert load_record(path, kind=kind) == record


def test_schemas_exist_and_declare_version():
    for kind in RECORD_KINDS:
        schema = json.loads(schema_path(kind).read_text(encoding="utf-8"))
        assert schema["properties"]["schema_version"] == {"const": 1}
        assert schema["properties"]["record_kind"] == {"const": kind}
    with pytest.raises(ValueError):
        schema_path("other")


def test_observation_cannot_pass_as_reviewed_expectation(tmp_path):
    observation = _layout("observation")
    # Relabelling alone is not enough: the provenance shape is different.
    forged = {**observation, "record_kind": "expectation"}
    with pytest.raises(RecordError):
        validate_record(forged)
    # Grafting a review block onto an observation is rejected too.
    graft = copy.deepcopy(observation)
    graft["provenance"]["review"] = _layout("expectation")["provenance"]["review"]
    with pytest.raises(RecordError):
        validate_record(graft)
    # A complete forgery that claims to be an expectation but lacks review is rejected.
    forged["provenance"] = {"proposed_by": {"tool": "x", "version": "1"}}
    with pytest.raises(RecordError):
        validate_record(forged)
    path = tmp_path / "obs.json"
    path.write_text(json.dumps(observation), encoding="utf-8")
    with pytest.raises(RecordError, match="expected a expectation"):
        load_reviewed_expectation(path)


def test_draft_expectation_is_valid_but_not_loadable_as_truth(tmp_path):
    draft = _layout("expectation")
    draft["provenance"]["review"] = {"status": "draft"}
    validate_record(draft)
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft), encoding="utf-8")
    with pytest.raises(RecordError, match="not reviewed"):
        load_reviewed_expectation(path)
    reviewed = _layout("expectation")
    path.write_text(json.dumps(reviewed), encoding="utf-8")
    assert load_reviewed_expectation(path)["record_kind"] == "expectation"


def _drop_reviewer(r):
    del r["provenance"]["review"]["reviewer"]


def _blank_reviewer(r):
    r["provenance"]["review"]["reviewer"] = "  "


def _no_basis(r):
    del r["provenance"]["review"]["basis"]


def _dup_block(r):
    r["blocks"][1]["id"] = "b1"


def _dup_order(r):
    r["blocks"][1]["reading_order"] = 0


def _bad_caption(r):
    r["blocks"][3]["caption_for"] = "nope"


def _caption_to_paragraph(r):
    r["blocks"][3]["caption_for"] = "b2"


def _page_over_count(r):
    r["scope"]["physical_pages"] = [1, 4]
    r["pages"][1]["physical_page"] = 4
    r["blocks"][4]["page"] = 4


def _block_off_scope(r):
    r["blocks"][0]["page"] = 3


def _hash_mismatch(r):
    r["blocks"][1]["text"] = "Value is 0.5 at p < .05."


def _bbox_inverted(r):
    r["blocks"][0]["bbox"]["x1"] = 1


def _bbox_outside(r):
    r["blocks"][0]["bbox"]["x1"] = 9999


def _anchor_wrong_page(r):
    r["pages"][1]["body_start"] = {"block_id": "b1"}


def _anchor_unknown(r):
    r["pages"][0]["body_end"] = {"block_id": "zzz"}


def _cell_outside(r):
    r["blocks"][2]["table"]["cells"][3]["col"] = 5


def _cells_overlap(r):
    r["blocks"][2]["table"]["cells"][3].update(row=0, col=0)


def _table_on_paragraph(r):
    r["blocks"][1]["table"] = r["blocks"][2]["table"]


def _marker_unknown_entry(r):
    r["references"]["markers"][0]["entry_ids"] = ["r9"]


def _marker_wrong_page(r):
    r["references"]["markers"][0]["page"] = 2


def _entry_dup(r):
    r["references"]["entries"][1]["id"] = "r1"


def _ordinal_dup(r):
    r["references"]["entries"][1]["ordinal"] = 1


def _entry_hash(r):
    r["references"]["entries"][0]["raw"] += "x"


def _complete_but_partial(r):
    r["scope"]["kind"] = "complete_document"


def _pages_scope_mismatch(r):
    r["pages"].pop()


def _present_doi_without_value(r):
    r["references"]["entries"][1]["doi"] = {"status": "present"}


def _absent_doi_with_value(r):
    r["references"]["entries"][0]["doi"] = {"status": "absent_in_source", "value": "10.1/x"}


def _unknown_field(r):
    r["blocks"][0]["confidence"] = 0.9


def _bad_unit(r):
    r["pages"][0]["geometry"]["unit"] = "cm"


def _missing_origin(r):
    del r["pages"][0]["geometry"]["origin"]


def _wrong_version(r):
    r["schema_version"] = 2


@pytest.mark.parametrize("mutate", [
    _drop_reviewer, _blank_reviewer, _no_basis,
    _dup_block, _dup_order, _bad_caption, _caption_to_paragraph, _page_over_count, _block_off_scope,
    _hash_mismatch, _bbox_inverted, _bbox_outside, _anchor_wrong_page, _anchor_unknown,
    _cell_outside, _cells_overlap, _table_on_paragraph,
    _marker_unknown_entry, _marker_wrong_page, _entry_dup, _ordinal_dup, _entry_hash,
    _complete_but_partial, _pages_scope_mismatch,
    _present_doi_without_value, _absent_doi_with_value,
    _unknown_field, _bad_unit, _missing_origin, _wrong_version,
])
def test_invalid_expectations_are_rejected(mutate):
    record = _layout("expectation")
    mutate(record)
    with pytest.raises(RecordError):
        validate_record(record)


def test_review_checks_do_not_apply_to_observations():
    # Layout rules are shared; review rules are expectation-only. Same defect, same rejection.
    observation = _layout("observation")
    _dup_block(observation)
    with pytest.raises(RecordError, match="duplicate block id"):
        validate_record(observation)


def test_all_findings_are_reported_together():
    record = _layout("expectation")
    _dup_block(record)
    _marker_unknown_entry(record)
    with pytest.raises(RecordError) as caught:
        validate_record(record)
    assert len(caught.value.errors) >= 2


def test_missing_doi_is_distinct_from_parse_failure():
    expectation = _layout("expectation")
    assert expectation["references"]["entries"][0]["doi"] == {"status": "absent_in_source"}
    validate_record(expectation)
    observation = _layout("observation")
    observation["references"]["entries"][0]["doi"] = {"status": "parse_failed"}
    validate_record(observation)
    # The vocabularies do not overlap: a parse failure cannot be claimed as reviewed truth.
    expectation["references"]["entries"][0]["doi"] = {"status": "parse_failed"}
    with pytest.raises(RecordError):
        validate_record(expectation)
    observation["references"]["entries"][0]["doi"] = {"status": "absent_in_source"}
    with pytest.raises(RecordError):
        validate_record(observation)


def test_complete_document_scope_accepted():
    record = _layout("expectation")
    record["scope"] = {"kind": "complete_document", "physical_pages": [1, 2, 3]}
    record["pages"].append(_page(3))
    validate_record(record)


def _run_unknown_config(r):
    r["results"][0]["configuration_id"] = "zz"


def _run_unknown_input(r):
    r["results"][0]["input_id"] = "zz"


def _run_duplicate_pair(r):
    r["results"].append(copy.deepcopy(r["results"][0]))


def _run_ok_without_hash(r):
    del r["results"][0]["output_sha256"]


def _run_failure_without_error(r):
    del r["results"][1]["error"]


def _run_ok_with_error(r):
    r["results"][0]["error"] = {"kind": "x", "message": "y"}


def _run_failure_with_metrics(r):
    r["results"][1]["metrics"] = r["results"][0]["metrics"]


def _run_unpinned_metric(r):
    r["results"][0]["metrics"][0]["name"] = "other_metric"


def _run_metric_version_drift(r):
    r["results"][0]["metrics"][0]["version"] = "2"


def _run_options_hash(r):
    r["configurations"][0]["options"]["dpi"] = 72


def _run_dup_config(r):
    r["configurations"][1]["id"] = "c1"


def _run_time_order(r):
    r["finished_at"] = "2025-12-31T00:00:00Z"


def _run_bad_mode(r):
    r["results"][0]["metrics"][0]["mode"] = "lenient"


def _run_negative_latency(r):
    r["results"][0]["latency_seconds"] = -1


def _run_bad_status(r):
    r["results"][1]["status"] = "exploded"


@pytest.mark.parametrize("mutate", [
    _run_unknown_config, _run_unknown_input, _run_duplicate_pair, _run_ok_without_hash,
    _run_failure_without_error, _run_ok_with_error, _run_failure_with_metrics, _run_unpinned_metric,
    _run_metric_version_drift, _run_options_hash, _run_dup_config, _run_time_order, _run_bad_mode,
    _run_negative_latency, _run_bad_status,
])
def test_invalid_runs_are_rejected(mutate):
    record = _run()
    mutate(record)
    with pytest.raises(RecordError):
        validate_record(record)


@pytest.mark.parametrize("bad", [None, [], {}, {"record_kind": "ground_truth"}])
def test_non_records_are_rejected(bad):
    with pytest.raises(RecordError):
        validate_record(bad)


def test_load_record_pins_kind(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(_run()), encoding="utf-8")
    with pytest.raises(RecordError, match="expected a expectation"):
        load_record(path, kind="expectation")


def test_hash_helpers_are_unnormalized():
    assert sha256_text("a") != sha256_text("A")
    assert sha256_text("-1") != sha256_text("1")
    assert options_sha256({"b": 1, "a": 2}) == options_sha256({"a": 2, "b": 1})
