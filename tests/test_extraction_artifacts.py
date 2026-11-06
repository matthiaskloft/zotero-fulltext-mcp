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
    verify_output_spans,
)

PDF_HASH = "a" * 64
IMAGE_HASH = "b" * 64


def _page(number: int, **extra):
    page = {
        "physical_page": number,
        "printed_label": "iv" if number == 1 else None,
        "geometry": {"unit": "pt", "origin": "top-left", "frame": "unrotated", "box": "cropbox", "rotation": 0, "width": 600, "height": 800},
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


RAW_1 = "[1] A. Author. Synthetic paper. 2020."
RAW_2 = "[2] B. Writer. Another. 2021. doi:10.0000/synthetic"


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
            _block("b7", "reference_entry", 2, 6, RAW_1),
        ],
        "references": {
            "entries": [
                {"id": "r1", "ordinal": 1, "page": 2, "block_id": "b7", "raw": RAW_1, "raw_sha256": sha256_text(RAW_1),
                 "fields": {"title": "Synthetic paper", "authors": [{"family": "Author", "given": "A."}],
                            "year": 2020, "year_raw": "2020a"},
                 "doi": {"status": "absent_in_source"}},
                {"id": "r2", "ordinal": 2, "page": 2, "raw": RAW_2, "raw_sha256": sha256_text(RAW_2),
                 "doi": {"status": "present", "value": "10.0000/synthetic"}},
            ],
            "markers": [{"id": "m1", "page": 1, "block_id": "b2", "text": "[1]", "target_status": "resolved",
                         "entry_ids": ["r1"]}],
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


def _write(tmp_path, record, name="record.json"):
    path = tmp_path / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _rejected(record, match):
    with pytest.raises(RecordError, match=match):
        validate_record(record)


@pytest.mark.parametrize("make", [lambda: _layout("expectation"), lambda: _layout("observation"), _run])
def test_valid_records_pass_and_round_trip(make, tmp_path):
    record = make()
    kind = validate_record(record)
    assert load_record(_write(tmp_path, record), kind=kind) == record


def test_schemas_exist_and_declare_version():
    for kind in RECORD_KINDS:
        schema = json.loads(schema_path(kind).read_text(encoding="utf-8"))
        assert schema["properties"]["schema_version"] == {"const": 1}
        assert schema["properties"]["record_kind"] == {"const": kind}
    with pytest.raises(ValueError):
        schema_path("other")


def test_observation_cannot_be_relabelled_as_expectation(tmp_path):
    observation = _layout("observation")
    forged = {**observation, "record_kind": "expectation"}
    _rejected(forged, "produced_by|review")
    graft = copy.deepcopy(observation)
    graft["provenance"]["review"] = _layout("expectation")["provenance"]["review"]
    _rejected(graft, "review")
    forged["provenance"] = {"proposed_by": {"tool": "x", "version": "1"}}
    _rejected(forged, "'review' is a required property")
    with pytest.raises(RecordError, match="expected a expectation"):
        load_reviewed_expectation(_write(tmp_path, observation))


def test_draft_expectation_is_valid_but_not_loadable_as_truth(tmp_path):
    draft = _layout("expectation")
    draft["provenance"]["review"] = {"status": "draft"}
    # Drafts are exempt from the reviewed-only requirements.
    del draft["blocks"][1]["text_sha256"]
    del draft["pages"][0]["render"]["image_sha256"]
    validate_record(draft)
    with pytest.raises(RecordError, match="not reviewed"):
        load_reviewed_expectation(_write(tmp_path, draft))
    assert load_reviewed_expectation(_write(tmp_path, _layout("expectation")))["record_kind"] == "expectation"


# --- expectations: structure and cross-checks ---------------------------------------------------

def _m(fn, match):
    return pytest.param(fn, match, id=fn.__name__)


def _drop_reviewer(r):
    del r["provenance"]["review"]["reviewer"]


def _personal_name_reviewer(r):
    r["provenance"]["review"]["reviewer"] = "Jane Doe"


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
    r["references"]["entries"][0]["page"] = 4
    r["references"]["entries"][1]["page"] = 4


def _block_off_scope(r):
    r["blocks"][0]["page"] = 3


def _hash_mismatch(r):
    r["blocks"][1]["text"] = "Value is 0.5 at p < .05."


def _bbox_inverted(r):
    r["blocks"][0]["bbox"]["x1"] = 1


def _bbox_outside(r):
    r["blocks"][0]["bbox"]["x1"] = 9999


def _bbox_slight_overshoot(r):
    r["blocks"][0]["bbox"]["y0"] = -2.05


def _anchor_wrong_page(r):
    r["pages"][1]["body_start"] = {"block_id": "b1"}


def _anchor_unknown(r):
    r["pages"][0]["body_end"] = {"block_id": "zzz"}


def _anchor_order(r):
    r["pages"][0]["body_start"] = {"block_id": "b4"}
    r["pages"][0]["body_end"] = {"block_id": "b1"}


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


def _marker_dup(r):
    r["references"]["markers"].append(copy.deepcopy(r["references"]["markers"][0]))


def _marker_status_mismatch(r):
    r["references"]["markers"][0]["target_status"] = "no_target"


def _marker_out_of_scope_in_complete(r):
    r["scope"] = {"kind": "complete_document", "physical_pages": [1, 2, 3]}
    r["pages"].append(_page(3))
    r["references"]["markers"][0].update(target_status="out_of_scope", entry_ids=[])


def _marker_missing_status(r):
    del r["references"]["markers"][0]["target_status"]


def _entry_dup(r):
    r["references"]["entries"][1]["id"] = "r1"


def _ordinal_dup(r):
    r["references"]["entries"][1]["ordinal"] = 1


def _entry_hash(r):
    r["references"]["entries"][0]["raw"] += "x"


def _entry_unknown_block(r):
    r["references"]["entries"][0]["block_id"] = "nope"


def _entry_block_page_mismatch(r):
    r["references"]["entries"][0]["page"] = 1


def _entry_off_scope(r):
    r["references"]["entries"][0]["page"] = 3


def _entry_end_page_without_chain(r):
    r["references"]["entries"][0]["end_page"] = 2


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


def _missing_rotation(r):
    del r["pages"][0]["geometry"]["rotation"]


def _missing_box(r):
    del r["pages"][0]["geometry"]["box"]


def _bad_rotation(r):
    r["pages"][0]["geometry"]["rotation"] = 45


def _normalized_wrong_size(r):
    r["pages"][0]["geometry"].update(unit="normalized", width=600, height=800)


def _px_without_dpi(r):
    r["pages"][0]["geometry"]["unit"] = "px"
    del r["pages"][0]["render"]["dpi"]


def _wrong_version(r):
    r["schema_version"] = 2


def _missing_bbox(r):
    del r["blocks"][0]["bbox"]


def _missing_render(r):
    del r["pages"][0]["render"]


def _output_span_in_expectation(r):
    r["blocks"][0]["output_span"] = {"markdown_sha256": "f" * 64, "start": 0, "end": 3}


def _impossible_review_date(r):
    r["provenance"]["review"]["reviewed_at"] = "2026-13-45T00:00:00Z"


def _reviewer_is_tool(r):
    r["provenance"]["review"]["reviewer"] = "Synthetic-Extractor"


def _no_image_hash(r):
    del r["pages"][1]["render"]["image_sha256"]


def _unhashed_text(r):
    del r["blocks"][1]["text_sha256"]


def _unhashed_raw(r):
    del r["references"]["entries"][0]["raw_sha256"]


def _empty_table(r):
    r["blocks"][2]["table"] = {"n_rows": 1, "n_cols": 1, "cells": []}


def _no_table_payload(r):
    del r["blocks"][2]["table"]


def _no_graphic(r):
    del r["blocks"][5]["graphic"]


def _no_equation(r):
    del r["blocks"][4]["equation"]


def _no_text_key(r):
    r["blocks"][0].pop("text")
    r["blocks"][0].pop("text_sha256")


# continuation chains: block b8 (table, page 2) continues b3 (table, page 1)
def _chain(r, **over):
    r["blocks"].append(_block("b8", "table", 2, 7, None, continues_from="b3",
                              table={"n_rows": 1, "n_cols": 2, "cells": [{"row": 0, "col": 0, "text": "3"},
                                                                          {"row": 0, "col": 1, "text": "4"}]}))
    r["blocks"][-1].update(over)


def _chain_unknown(r):
    _chain(r, continues_from="nope")


def _chain_other_type(r):
    _chain(r, continues_from="b2")


def _chain_backwards(r):
    _chain(r, reading_order=2)


def _chain_forked(r):
    _chain(r)
    r["blocks"].append(_block("b9", "table", 2, 8, None, continues_from="b3",
                              table={"n_rows": 1, "n_cols": 2, "cells": [{"row": 0, "col": 0, "text": "5"}]}))


def _chain_ncols(r):
    _chain(r)
    r["blocks"][-1]["table"]["n_cols"] = 3


@pytest.mark.parametrize("mutate,match", [
    _m(_drop_reviewer, "'reviewer' is a required property"),
    _m(_personal_name_reviewer, "does not match"),
    _m(_no_basis, "'basis' is a required property"),
    _m(_dup_block, "duplicate block id"),
    _m(_dup_order, "duplicate reading_order"),
    _m(_bad_caption, "unknown block 'nope'"),
    _m(_caption_to_paragraph, "points at a paragraph"),
    _m(_page_over_count, "exceeds page_count"),
    _m(_block_off_scope, "outside the scope"),
    _m(_hash_mismatch, "text_sha256 does not match"),
    _m(_bbox_inverted, "x0>x1"),
    _m(_bbox_outside, "outside the page geometry"),
    _m(_bbox_slight_overshoot, "outside the page geometry"),
    _m(_anchor_wrong_page, "body_start must name a block on that page"),
    _m(_anchor_unknown, "body_end must name a block on that page"),
    _m(_anchor_order, "body_start follows body_end"),
    _m(_cell_outside, "exceeds the grid"),
    _m(_cells_overlap, "cells overlap"),
    _m(_table_on_paragraph, "table payload but type"),
    _m(_marker_unknown_entry, "unknown reference entry"),
    _m(_marker_wrong_page, "block_id must name a block on the marker's page"),
    _m(_marker_dup, "duplicate marker id"),
    _m(_marker_status_mismatch, "non-empty exactly when"),
    _m(_marker_out_of_scope_in_complete, "cannot be out_of_scope"),
    _m(_marker_missing_status, "'target_status' is a required property"),
    _m(_entry_dup, "duplicate reference entry id"),
    _m(_ordinal_dup, "duplicate reference ordinal"),
    _m(_entry_hash, "raw_sha256 does not match"),
    _m(_entry_unknown_block, "unknown block 'nope'"),
    _m(_entry_block_page_mismatch, "page differs from its block"),
    _m(_entry_off_scope, "outside the scope"),
    _m(_entry_end_page_without_chain, "chain ends on the same page"),
    _m(_complete_but_partial, "must list every physical page"),
    _m(_pages_scope_mismatch, "pages must cover exactly"),
    _m(_present_doi_without_value, "'value' is a required property"),
    _m(_absent_doi_with_value, "should not be valid"),
    _m(_unknown_field, "Additional properties"),
    _m(_bad_unit, "is not one of"),
    _m(_missing_origin, "'origin' is a required property"),
    _m(_missing_rotation, "'rotation' is a required property"),
    _m(_missing_box, "'box' is a required property"),
    _m(_bad_rotation, "45 is not one of"),
    _m(_normalized_wrong_size, "1 was expected"),
    _m(_px_without_dpi, "'dpi' is a required property"),
    _m(_wrong_version, "1 was expected"),
    _m(_missing_bbox, "'bbox' is a required property"),
    _m(_missing_render, "'render' is a required property"),
    _m(_output_span_in_expectation, "should not be valid"),
    _m(_impossible_review_date, "not a valid timestamp"),
    _m(_reviewer_is_tool, "must not be the proposing tool"),
    _m(_no_image_hash, "needs render.image_sha256"),
    _m(_unhashed_text, "needs text_sha256"),
    _m(_unhashed_raw, "needs raw_sha256"),
    _m(_empty_table, "at least one cell"),
    _m(_no_table_payload, "at least one cell"),
    _m(_no_graphic, "needs a graphic payload"),
    _m(_no_equation, "needs an equation payload"),
    _m(_no_text_key, "must state text"),
    _m(_chain_unknown, "continues unknown block"),
    _m(_chain_other_type, "different type"),
    _m(_chain_backwards, "earlier in reading order"),
    _m(_chain_forked, "more than one block"),
    _m(_chain_ncols, "differ in n_cols"),
])
def test_invalid_expectations_are_rejected(mutate, match):
    record = _layout("expectation")
    mutate(record)
    _rejected(record, match)


def test_review_checks_do_not_apply_to_observations():
    observation = _layout("observation")
    _dup_block(observation)
    _rejected(observation, "duplicate block id")


def test_all_findings_are_reported_together():
    record = _layout("expectation")
    _dup_block(record)
    _marker_unknown_entry(record)
    with pytest.raises(RecordError) as caught:
        validate_record(record)
    assert len(caught.value.errors) >= 2


def test_valid_continuation_chains_and_page_crossing_entries():
    record = _layout("expectation")
    _chain(record)
    validate_record(record)

    crossing = _layout("expectation")
    crossing["scope"]["physical_pages"] = [1, 2, 3]
    crossing["pages"].append(_page(3))
    crossing["blocks"].append(_block("b8", "reference_entry", 3, 7, "tail", continues_from="b7"))
    crossing["references"]["entries"][0]["end_page"] = 3
    validate_record(crossing)
    wrong_end = copy.deepcopy(crossing)
    wrong_end["references"]["entries"][0]["end_page"] = 2
    _rejected(wrong_end, "end_page")
    no_end = copy.deepcopy(crossing)
    del no_end["references"]["entries"][0]["end_page"]
    _rejected(no_end, "end_page must be 3")


def test_geometry_frames_and_observation_margin():
    # Frame and rotation are explicit; a displayed frame is a distinct declaration.
    rotated = _layout("expectation")
    rotated["pages"][0]["geometry"].update(rotation=90, box="mediabox", frame="displayed", width=800, height=600)
    for block in rotated["blocks"]:
        if block["page"] == 1:
            block["bbox"]["x1"] = 500
            block["bbox"]["y1"] = min(block["bbox"]["y1"], 590)
            block["bbox"]["y0"] = min(block["bbox"]["y0"], 580)
    validate_record(rotated)
    normalized = _layout("expectation")
    normalized["pages"][0]["geometry"].update(unit="normalized", width=1, height=1)
    for block in normalized["blocks"]:
        if block["page"] == 1:
            block["bbox"] = {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}
    validate_record(normalized)
    # Observations stay recordable: small overshoot and inverted boxes pass, absurd ones do not.
    observation = _layout("observation")
    _bbox_slight_overshoot(observation)
    validate_record(observation)
    observation["blocks"][0]["bbox"].update(x0=400, x1=100)
    validate_record(observation)
    observation["blocks"][0]["bbox"].update(x0=10, x1=500, y0=-500)
    _rejected(observation, "outside the page geometry")
    norm_obs = _layout("observation")
    for page in norm_obs["pages"]:
        page["geometry"].update(unit="normalized", width=1, height=1)
    for block in norm_obs["blocks"]:
        block["bbox"] = {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}
    norm_obs["blocks"][0]["bbox"]["y0"] = -0.01
    validate_record(norm_obs)
    norm_obs["blocks"][0]["bbox"]["y0"] = -0.6
    _rejected(norm_obs, "outside the page geometry")


def test_px_requires_displayed_frame_and_page_render():
    record = _layout("expectation")
    for page in record["pages"]:
        page["geometry"].update(unit="px", frame="displayed", width=1200, height=1600)
    for block in record["blocks"]:
        block["bbox"] = {"x0": 10, "y0": 10, "x1": 500, "y1": 40}
    validate_record(record)
    record["pages"][0]["geometry"]["frame"] = "unrotated"
    _rejected(record, "'displayed' was expected")
    observation = _layout("observation")
    for page in observation["pages"]:
        page["geometry"].update(unit="px", frame="displayed", width=1200, height=1600)
        del page["render"]
    _rejected(observation, "'render' is a required property")


def test_real_rotated_pdf_frames():
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((500, 700), "corner")
    page.set_rotation(90)
    data = doc.tobytes()
    doc.close()

    # Separately opened document: /Rotate is visible and get_text is in the unrotated box.
    page = pymupdf.open("pdf", data)[0]
    assert (page.rotation, page.rect.width, page.rect.height) == (90, 800, 600)
    assert (page.mediabox.width, page.mediabox.height) == (600, 800)
    word = page.get_text("words")[0]
    unrotated = _layout("observation")
    unrotated["document"]["page_count"] = 1
    unrotated["scope"]["physical_pages"] = [1]
    unrotated["pages"] = [_page(1)]
    unrotated["pages"][0]["geometry"].update(
        rotation=page.rotation, width=page.mediabox.width, height=page.mediabox.height)
    unrotated["blocks"] = [_block("w1", "paragraph", 1, 0, "corner")]
    unrotated["blocks"][0]["bbox"] = dict(zip(("x0", "y0", "x1", "y1"), word[:4]))
    del unrotated["references"]
    validate_record(unrotated)
    box = unrotated["blocks"][0]["bbox"]
    assert box["x1"] <= 600 and box["y1"] <= 800

    # After remove_rotation (as pymupdf4llm does) the open page is displayed-frame and reads 0.
    page.remove_rotation()
    assert (page.rotation, page.rect.width, page.rect.height) == (0, 800, 600)
    word = page.get_text("words")[0]
    displayed = copy.deepcopy(unrotated)
    displayed["pages"][0]["geometry"].update(frame="displayed", rotation=90, width=800, height=600)
    displayed["blocks"][0]["bbox"] = dict(zip(("x0", "y0", "x1", "y1"), word[:4]))
    validate_record(displayed)
    assert displayed["blocks"][0]["bbox"] != unrotated["blocks"][0]["bbox"]


def test_markdown_only_observation_may_omit_geometry_details():
    observation = _layout("observation")
    for page in observation["pages"]:
        del page["render"]
    for block in observation["blocks"]:
        del block["bbox"]
    for entry in observation["references"]["entries"]:
        del entry["page"]
    validate_record(observation)


def test_output_spans_round_trip():
    markdown = "# Alpha Heading\n\nValue is -0.5 at p < .05.\n"
    observation = _layout("observation")
    digest = sha256_text(markdown)
    observation["blocks"][0]["output_span"] = {
        "markdown_sha256": digest, "start": 0, "end": 15, "span_sha256": sha256_text(markdown[0:15])}
    validate_record(observation)
    assert verify_output_spans(observation, markdown) == []
    assert verify_output_spans(observation, markdown + "x")
    observation["blocks"][0]["output_span"]["span_sha256"] = sha256_text("# Alpha Heading ")
    assert "span_sha256" in verify_output_spans(observation, markdown)[0]
    observation["blocks"][0]["output_span"]["end"] = 999
    observation["blocks"][0]["output_span"].pop("span_sha256")
    assert "beyond" in verify_output_spans(observation, markdown)[0]
    backwards = copy.deepcopy(observation)
    backwards["blocks"][0]["output_span"].update(start=10, end=2)
    _rejected(backwards, "end before start")
    mixed = copy.deepcopy(observation)
    mixed["blocks"][0]["output_span"]["end"] = 15
    mixed["blocks"][1]["output_span"] = {"markdown_sha256": "f" * 64, "start": 0, "end": 1}
    _rejected(mixed, "one Markdown output")


def test_missing_doi_is_distinct_from_parse_failure():
    expectation = _layout("expectation")
    assert expectation["references"]["entries"][0]["doi"] == {"status": "absent_in_source"}
    validate_record(expectation)
    observation = _layout("observation")
    observation["references"]["entries"][0]["doi"] = {"status": "parse_failed"}
    validate_record(observation)
    expectation["references"]["entries"][0]["doi"] = {"status": "parse_failed"}
    _rejected(expectation, "is not one of")
    observation["references"]["entries"][0]["doi"] = {"status": "absent_in_source"}
    _rejected(observation, "is not one of")


def test_complete_document_scope_accepted():
    record = _layout("expectation")
    record["scope"] = {"kind": "complete_document", "physical_pages": [1, 2, 3]}
    record["pages"].append(_page(3))
    validate_record(record)


def test_out_of_scope_marker_in_page_scoped_record():
    record = _layout("expectation")
    record["references"]["markers"][0].update(target_status="out_of_scope", entry_ids=[])
    validate_record(record)


def test_observation_timestamp_must_be_real():
    observation = _layout("observation")
    observation["provenance"]["created_at"] = "2026-02-30T00:00:00Z"
    _rejected(observation, "not a valid timestamp")


# --- runs ---------------------------------------------------------------------------------------

def _run_unknown_config(r):
    r["results"][0]["configuration_id"] = "zz"


def _run_unknown_input(r):
    r["results"][0]["input_id"] = "zz"


def _run_duplicate_pair(r):
    r["results"].append(copy.deepcopy(r["results"][0]))


def _run_no_results(r):
    r["results"] = []


def _run_missing_pair(r):
    r["results"].pop()


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


def _run_duplicate_metric(r):
    r["results"][0]["metrics"].append(copy.deepcopy(r["results"][0]["metrics"][0]))


def _run_options_hash(r):
    r["configurations"][0]["options"]["dpi"] = 72


def _run_dup_config(r):
    r["configurations"][1]["id"] = "c1"


def _run_dup_input(r):
    r["inputs"].append(copy.deepcopy(r["inputs"][0]))


def _run_time_order(r):
    r["finished_at"] = "2025-12-31T00:00:00Z"


def _run_impossible_date(r):
    r["started_at"] = "2026-13-01T00:00:00Z"


def _run_bad_mode(r):
    r["results"][0]["metrics"][0]["mode"] = "lenient"


def _run_negative_latency(r):
    r["results"][0]["latency_seconds"] = -1


def _run_bad_status(r):
    r["results"][1]["status"] = "exploded"


@pytest.mark.parametrize("mutate,match", [
    _m(_run_unknown_config, "unknown configuration"),
    _m(_run_unknown_input, "unknown input"),
    _m(_run_duplicate_pair, "duplicate result"),
    _m(_run_no_results, "missing result"),
    _m(_run_missing_pair, "missing result"),
    _m(_run_ok_without_hash, "'output_sha256' is a required property"),
    _m(_run_failure_without_error, "'error' is a required property"),
    _m(_run_ok_with_error, "should not be valid"),
    _m(_run_failure_with_metrics, "should not be valid"),
    _m(_run_unpinned_metric, "missing from metric_versions"),
    _m(_run_metric_version_drift, "!= pinned"),
    _m(_run_duplicate_metric, "more than once"),
    _m(_run_options_hash, "options_sha256 does not match"),
    _m(_run_dup_config, "duplicate configuration id"),
    _m(_run_dup_input, "duplicate input id"),
    _m(_run_time_order, "finished_at precedes"),
    _m(_run_impossible_date, "not a valid timestamp"),
    _m(_run_bad_mode, "is not one of"),
    _m(_run_negative_latency, "less than the minimum"),
    _m(_run_bad_status, "is not one of"),
])
def test_invalid_runs_are_rejected(mutate, match):
    record = _run()
    mutate(record)
    _rejected(record, match)


def test_skipped_pairs_must_be_recorded_not_omitted():
    record = _run()
    record["results"][1].update(status="skipped", error={"kind": "skipped", "message": "not run"})
    validate_record(record)


@pytest.mark.parametrize("bad", [None, [], {}, {"record_kind": "ground_truth"}])
def test_non_records_are_rejected(bad):
    with pytest.raises(RecordError):
        validate_record(bad)


def test_load_record_pins_kind(tmp_path):
    with pytest.raises(RecordError, match="expected a expectation"):
        load_record(_write(tmp_path, _run()), kind="expectation")


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_load_record_rejects_non_finite_numbers(tmp_path, literal):
    record = _run()
    record["results"][0]["latency_seconds"] = "PLACEHOLDER"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(record).replace('"PLACEHOLDER"', literal), encoding="utf-8")
    with pytest.raises(RecordError, match="non-finite"):
        load_record(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RecordError, match="invalid JSON"):
        load_record(path)


def test_hash_helpers_are_unnormalized():
    assert sha256_text("a") != sha256_text("A")
    assert sha256_text("-1") != sha256_text("1")
    assert options_sha256({"b": 1, "a": 2}) == options_sha256({"a": 2, "b": 1})


def _dup_page(r):
    r["pages"][1] = copy.deepcopy(r["pages"][0])


def _hash_without_text(r):
    r["blocks"][2]["text_sha256"] = "a" * 64


def _caption_for_on_paragraph(r):
    r["blocks"][1]["caption_for"] = "b3"


def _entry_end_outside_scope(r):
    r["references"]["entries"][1]["end_page"] = 3


def _entry_end_before_start(r):
    r["references"]["entries"][1]["end_page"] = 2


def _marker_outside_scope(r):
    del r["references"]["markers"][0]["block_id"]
    r["references"]["markers"][0]["page"] = 3


@pytest.mark.parametrize("mutate,match", [
    _m(_dup_page, "duplicate page entry"),
    _m(_hash_without_text, "has text_sha256 but no text"),
    _m(_caption_for_on_paragraph, "has caption_for but type"),
    _m(_entry_end_outside_scope, "ends on page 3"),
    _m(_entry_end_before_start, "end_page must come after"),
    _m(_marker_outside_scope, "marker 'm1' is on page 3"),
])
def test_further_invalid_expectations_are_rejected(mutate, match):
    record = _layout("expectation")
    mutate(record)
    _rejected(record, match)


def _validates_within(record, seconds=10):
    import threading

    outcome = {}

    def run():
        try:
            validate_record(record)
            outcome["result"] = "valid"
        except RecordError as exc:
            outcome["result"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "validator did not terminate"
    return outcome["result"]


@pytest.mark.parametrize("kind", ["expectation", "observation"])
def test_continuation_cycles_terminate_and_are_rejected(kind):
    self_loop = _layout(kind)
    self_loop["blocks"][-1]["continues_from"] = "b7"
    result = _validates_within(self_loop)
    assert isinstance(result, RecordError) and "earlier in reading order" in str(result)

    two_cycle = _layout(kind)
    two_cycle["blocks"].append(_block("b8", "reference_entry", 2, 7, "tail"))
    two_cycle["blocks"][-1]["continues_from"] = "b7"
    two_cycle["blocks"][-2]["continues_from"] = "b8"
    result = _validates_within(two_cycle)
    assert isinstance(result, RecordError) and "earlier in reading order" in str(result)


def test_same_page_continuation_is_valid_without_end_page():
    record = _layout("expectation")
    record["blocks"].append(_block("b8", "reference_entry", 2, 7, "tail", continues_from="b7"))
    validate_record(record)
    record["references"]["entries"][0]["end_page"] = 2
    _rejected(record, "end_page")


def test_expectation_entry_block_rules():
    wrong_type = _layout("expectation")
    wrong_type["references"]["entries"][0]["block_id"] = "b2"
    wrong_type["references"]["entries"][0]["page"] = 1
    _rejected(wrong_type, "must link a reference_entry block")
    not_head = _layout("expectation")
    not_head["blocks"].append(_block("b8", "reference_entry", 2, 7, "tail", continues_from="b7"))
    not_head["references"]["entries"][1]["block_id"] = "b8"
    _rejected(not_head, "first block of a chain")
    shared = _layout("expectation")
    shared["references"]["entries"][1]["block_id"] = "b7"
    _rejected(shared, "used by more than one reference entry")
    # Observations may be looser.
    observation = _layout("observation")
    observation["references"]["entries"][1]["block_id"] = "b7"
    validate_record(observation)


def test_entry_end_page_requires_page():
    observation = _layout("observation")
    del observation["references"]["entries"][0]["page"]
    observation["references"]["entries"][0]["end_page"] = 2
    _rejected(observation, "end_page must come after")


def test_observations_keep_page_attribution_required():
    observation = _layout("observation")
    del observation["blocks"][0]["page"]
    _rejected(observation, "'page' is a required property")
    observation = _layout("observation")
    del observation["pages"][0]["geometry"]
    _rejected(observation, "'geometry' is a required property")
    observation = _layout("observation")
    del observation["references"]["markers"][0]["page"]
    _rejected(observation, "'page' is a required property")


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r["document"].update(page_count=3.0), "must be an integer"),
    (lambda r: r.update(schema_version=1.0), "must be an integer"),
    (lambda r: r["pages"][0]["geometry"].update(rotation=90.0), "must be an integer"),
    (lambda r: r["scope"].update(physical_pages=[1.0, 2]), "must contain integers"),
    (lambda r: r["blocks"][0].update(reading_order=True), "must be an integer"),
    (lambda r: r["pages"][0]["geometry"].update(width=float("nan")), "non-finite"),
    (lambda r: r["pages"][0]["geometry"].update(height=float("inf")), "non-finite"),
])
@pytest.mark.parametrize("kind", ["expectation", "observation"])
def test_non_integer_and_non_finite_numbers_are_rejected(kind, mutate, match):
    record = _layout(kind)
    mutate(record)
    _rejected(record, match)


def test_verify_output_spans_reports_inverted_span():
    markdown = "abc"
    observation = _layout("observation")
    observation["blocks"][0]["output_span"] = {"markdown_sha256": sha256_text(markdown), "start": 2, "end": 1}
    assert "before it starts" in verify_output_spans(observation, markdown)[0]


def test_reviewed_table_grid_and_equation_latex():
    partial = _layout("expectation")
    partial["blocks"][2]["table"]["cells"].pop()
    _rejected(partial, "full grid")
    spanned = _layout("expectation")
    spanned["blocks"][2]["table"]["cells"] = [
        {"row": 0, "col": 0, "colspan": 2, "text": "head"},
        {"row": 1, "col": 0, "text": "-1.0"},
        {"row": 1, "col": 1, "text": ""},
    ]
    validate_record(spanned)
    no_latex = _layout("expectation")
    no_latex["blocks"][4]["equation"] = {"number": "(1)"}
    _rejected(no_latex, "must state latex")
    no_latex["blocks"][4]["equation"] = {"latex": None}
    validate_record(no_latex)


def test_truncated_content_in_page_scoped_records():
    record = _layout("expectation")
    record["blocks"][6]["truncated"] = "end"
    record["references"]["entries"][0]["truncated"] = "start"
    validate_record(record)
    record["blocks"][6]["truncated"] = "middle"
    _rejected(record, "is not one of")
    record["blocks"][6]["truncated"] = "both"
    record["scope"] = {"kind": "complete_document", "physical_pages": [1, 2, 3]}
    record["pages"].append(_page(3))
    entry_flag = record["references"]["entries"][0].pop("truncated")
    _rejected(record, "block 'b7' is truncated")
    del record["blocks"][6]["truncated"]
    record["references"]["entries"][0]["truncated"] = entry_flag
    _rejected(record, "reference entry 'r1' is truncated")


def test_missing_frame_is_rejected():
    record = _layout("expectation")
    del record["pages"][0]["geometry"]["frame"]
    _rejected(record, "'frame' is a required property")


def test_free_form_run_options_may_use_integer_field_names():
    record = _run()
    options = {"start": 0.5, "rotation": True, "nested": [{"page": 1.5}]}
    record["configurations"][0]["options"] = options
    record["configurations"][0]["options_sha256"] = options_sha256(options)
    validate_record(record)
    options["nested"][0]["page"] = float("nan")
    _rejected(record, "non-finite")
