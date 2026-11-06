"""olmOCR-bench export adapter: purely synthetic records; olmOCR itself is never imported."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from extraction import RecordError, load_record, load_reviewed_expectation, sha256_text
from extraction.olmocr_export import (
    NOT_VERIFIABLE,
    OLMOCR_REFERENCE,
    candidate_markdown_name,
    export_checks,
    main,
    write_checks_jsonl,
)

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "benchmarks" / "extraction" / "examples" / "ocr_corpus_synthetic_example.draft_expectation.json"
CORPUS_PDF = REPO / "tests" / "fixtures" / "ocr_corpus" / "corpus.pdf"

# Fields each olmOCR test class accepts, copied from olmocr/bench/tests.py at OLMOCR_REFERENCE's
# commit. load_single_test builds the dataclass with **data, so an extra key is a TypeError.
_BASE = {"pdf", "page", "id", "type", "max_diffs", "checked", "url"}
OLMOCR_FIELDS = {
    "present": _BASE | {"text", "case_sensitive", "first_n", "last_n"},
    "order": _BASE | {"before", "after"},
    "table": _BASE | {"cell", "up", "down", "left", "right", "top_heading", "left_heading", "ignore_markdown_tables"},
    "math": _BASE | {"math", "ignore_dollar_delimited"},
}
OLMOCR_REQUIRED = {
    "present": {"pdf", "page", "id", "type", "text"},
    "order": {"pdf", "page", "id", "type", "before", "after"},
    "table": {"pdf", "page", "id", "type", "cell"},
    "math": {"pdf", "page", "id", "type", "math"},
}


def _page(number):
    return {
        "physical_page": number,
        "printed_label": None,
        "geometry": {"unit": "pt", "origin": "top-left", "box": "cropbox", "rotation": 0, "width": 600, "height": 800},
        "render": {"renderer": "synthetic-renderer", "renderer_version": "1.0"},
    }


def _block(block_id, kind, page, order, text=None, **extra):
    block = {
        "id": block_id, "type": kind, "page": page, "reading_order": order,
        "bbox": {"x0": 10, "y0": 10 + 20 * order, "x1": 500, "y1": 25 + 20 * order},
    }
    block["text"] = text
    if text is not None:
        block["text_sha256"] = sha256_text(text)
    block.update(extra)
    return block


def _cell(row, col, text, **extra):
    return {"row": row, "col": col, "text": text, **extra}


def _expectation(blocks, pages=(1,), status="reviewed", **extra):
    record = {
        "schema_version": 1,
        "record_kind": "expectation",
        "record_id": "synthetic-exp",
        "document": {"pdf_sha256": "a" * 64, "title": "Synthetic", "page_count": max(pages)},
        "scope": {"kind": "pages", "physical_pages": list(pages)},
        "pages": [_page(p) for p in pages],
        "blocks": blocks,
        "provenance": {"review": {"status": status}},
    }
    if status == "reviewed":
        record["provenance"]["review"].update(
            reviewer="reviewer-a", reviewed_at="2026-01-01T00:00:00Z", basis="pdf_inspection"
        )
    record.update(extra)
    return record


def _table(cells, n_rows, n_cols):
    return {"n_rows": n_rows, "n_cols": n_cols, "cells": cells}


def _by_type(result, kind):
    return [t for t in result.tests if t["type"] == kind]


def test_text_blocks_become_strict_presence_checks():
    exp = _expectation([
        _block("h", "heading", 1, 0, "Alpha Heading"),
        _block("p", "paragraph", 1, 1, "Value is -0.5 at p < .05."),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    present = _by_type(result, "present")
    assert [t["text"] for t in present] == ["Alpha Heading", "Value is -0.5 at p < .05."]
    for test in present:
        assert test["pdf"] == "doc.pdf" and test["page"] == 1
        assert test["max_diffs"] == 0 and test["case_sensitive"] is True
    assert present[0]["id"] == "synthetic-exp.h.present"
    assert result.sources[present[0]["id"]] == "h"


def test_order_checks_link_consecutive_text_blocks_on_one_page():
    exp = _expectation([
        _block("a", "paragraph", 1, 0, "First text"),
        _block("t", "table", 1, 1, None, table=_table([_cell(0, 0, "x")], 1, 1)),
        _block("b", "paragraph", 1, 2, "Second text"),
        _block("c", "paragraph", 1, 3, "Third text"),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    pairs = [(t["before"], t["after"]) for t in _by_type(result, "order")]
    assert pairs == [("First text", "Second text"), ("Second text", "Third text")]
    assert all(t["max_diffs"] == 0 for t in _by_type(result, "order"))


def test_order_uses_reading_order_not_block_listing_order():
    exp = _expectation([
        _block("late", "paragraph", 1, 5, "Later text"),
        _block("early", "paragraph", 1, 1, "Earlier text"),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    [order] = _by_type(result, "order")
    assert (order["before"], order["after"]) == ("Earlier text", "Later text")


def test_cross_page_and_identical_text_order_pairs_are_skipped_not_dropped():
    exp = _expectation(
        [
            _block("a", "paragraph", 1, 0, "Shared text"),
            _block("b", "paragraph", 1, 1, "Shared text"),
            _block("c", "paragraph", 2, 2, "Other page"),
        ],
        pages=(1, 2),
    )
    result = export_checks(exp, pdf_name="doc.pdf")
    assert _by_type(result, "order") == []
    reasons = [s["reason"] for s in result.skipped if s["kind"] == "order"]
    assert any("different pages" in r for r in reasons)
    assert any("identical text" in r for r in reasons)


def test_table_cells_export_neighbours_and_headings():
    cells = [
        _cell(0, 0, "Group", is_header=True), _cell(0, 1, "Mean", is_header=True),
        _cell(1, 0, "Treat"), _cell(1, 1, "-1.50"),
        _cell(2, 0, "Ctrl"), _cell(2, 1, "2.00"),
    ]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(cells, 3, 2))])
    result = export_checks(exp, pdf_name="doc.pdf")
    tests = {t["id"]: t for t in _by_type(result, "table")}
    assert len(tests) == 6
    value = tests["synthetic-exp.t.table.r1c1"]
    assert value["cell"] == "-1.50"
    assert (value["up"], value["down"], value["left"]) == ("Mean", "2.00", "Treat")
    assert "right" not in value  # edge of the table: nothing to assert
    assert value["top_heading"] == "Mean"
    assert "left_heading" not in value  # first column carries no is_header cell
    header = tests["synthetic-exp.t.table.r0c1"]
    assert "top_heading" not in header and header["left"] == "Group"
    assert result.partial == []


def test_table_left_heading_and_empty_or_spanned_cells_are_reported():
    cells = [
        _cell(0, 0, "Row A", is_header=True), _cell(0, 1, "7"), _cell(0, 2, ""),
        _cell(1, 0, "Merged", colspan=2), _cell(1, 2, "9"),
    ]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(cells, 2, 3))])
    result = export_checks(exp, pdf_name="doc.pdf")
    tests = {t["cell"]: t for t in _by_type(result, "table")}
    assert tests["7"]["left_heading"] == "Row A"
    # The empty neighbour at (0,2) cannot be asserted; the spanned neighbour below (0,1) neither.
    relations = {(p["row"], p["col"], p["relation"], p["reason"]) for p in result.partial}
    assert (0, 1, "right", "empty neighbour") in relations
    assert (0, 1, "down", "spanned neighbour") in relations
    skipped = {(s["row"], s["col"]): s["reason"] for s in result.skipped if s["kind"] == "table"}
    assert "spanned cell" in skipped[(1, 0)]
    assert "no alphanumeric" in skipped[(0, 2)]


def test_equations_export_latex_or_report_skip():
    exp = _expectation([
        _block("e1", "equation", 1, 0, None, equation={"latex": "a=-b", "number": "(1)", "display": True}),
        _block("e2", "equation", 1, 1, None, equation={"latex": None}),
        _block("e3", "equation", 1, 2, "x = y", equation={}),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    [math] = _by_type(result, "math")
    assert math["math"] == "a=-b" and math["id"] == "synthetic-exp.e1.math"
    skipped = {s["block_id"]: s["reason"] for s in result.skipped if s["kind"] == "math"}
    assert set(skipped) == {"e2", "e3"} and "no LaTeX" in skipped["e2"]


def test_everything_inexpressible_is_reported_as_skipped():
    exp = _expectation(
        [
            _block("hdr", "header", 1, 0, "Running head"),
            _block("fig", "figure", 1, 1, None, graphic={"kind": "chart"}),
            _block("oth", "other", 1, 2, "stray"),
            _block("blank", "paragraph", 1, 3, "**"),
            _block("none", "paragraph", 1, 4),
            _block("ref", "reference_entry", 1, 5, "[1] A. Author. Title. 2020."),
            _block("unsure", "paragraph", 1, 6, "Murky text", uncertainty={"level": "high"}),
        ],
        pages=(1,),
        references={
            "entries": [{"id": "r1", "ordinal": 1, "page": 1, "block_id": "ref", "raw": "[1] A. Author. Title. 2020.",
                         "raw_sha256": sha256_text("[1] A. Author. Title. 2020."), "doi": {"status": "absent_in_source"}}],
            "markers": [{"id": "m1", "page": 1, "text": "[1]", "target_status": "resolved", "entry_ids": ["r1"]}],
        },
    )
    exp["pages"][0]["body_start"] = {"block_id": "ref"}
    result = export_checks(exp, pdf_name="doc.pdf")
    assert [t["text"] for t in _by_type(result, "present")] == ["[1] A. Author. Title. 2020."]
    skipped = {(s["kind"], s.get("block_id")) for s in result.skipped}
    assert {("absent", "hdr"), ("figure", "fig"), ("block", "oth"), ("present", "blank"), ("present", "none"),
            ("block", "unsure"), ("page_anchor", "ref"), ("reference_entry", "ref"), ("citation_marker", None)} <= skipped
    # Every block id is accounted for either by a check or by a skip.
    covered = set(result.sources.values()) | {s.get("block_id") for s in result.skipped}
    assert {b["id"] for b in exp["blocks"]} <= covered
    counts = result.counts()
    assert counts["total"] == len(result.tests) and counts["skipped"] == len(result.skipped)


def test_drafts_and_wrong_record_kinds_are_refused():
    draft = _expectation([_block("p", "paragraph", 1, 0, "Text")], status="draft")
    with pytest.raises(RecordError, match="not reviewed"):
        export_checks(draft, pdf_name="doc.pdf")
    observation = copy.deepcopy(_expectation([_block("p", "paragraph", 1, 0, "Text")]))
    observation["record_kind"] = "observation"
    with pytest.raises(RecordError):
        export_checks(observation, pdf_name="doc.pdf")
    reviewed = _expectation([_block("p", "paragraph", 1, 0, "Text")])
    for bad in ("", "sub/doc.pdf", "sub\\doc.pdf"):
        with pytest.raises(ValueError):
            export_checks(reviewed, pdf_name=bad)


def test_jsonl_lines_only_use_fields_olmocr_accepts(tmp_path):
    cells = [_cell(0, 0, "h", is_header=True), _cell(1, 0, "v")]
    exp = _expectation([
        _block("p", "paragraph", 1, 0, "Some text"),
        _block("q", "paragraph", 1, 1, "More text"),
        _block("t", "table", 1, 2, None, table=_table(cells, 2, 1)),
        _block("e", "equation", 1, 3, None, equation={"latex": "x^2"}),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    out = tmp_path / "checks.jsonl"
    write_checks_jsonl(result.tests, out)
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert lines == result.tests
    assert {t["type"] for t in lines} == {"present", "order", "table", "math"}
    for test in lines:
        assert set(test) <= OLMOCR_FIELDS[test["type"]], test
        assert OLMOCR_REQUIRED[test["type"]] <= set(test), test
        assert isinstance(test["page"], int) and test["page"] >= 1
    assert len({t["id"] for t in lines}) == len(lines)
    assert "\r" not in out.read_text(encoding="utf-8")


def test_unicode_text_is_written_verbatim(tmp_path):
    exp = _expectation([_block("p", "paragraph", 1, 0, "Effect − 0.5 ≤ p, α = .05")])
    result = export_checks(exp, pdf_name="doc.pdf")
    out = tmp_path / "checks.jsonl"
    write_checks_jsonl(result.tests, out)
    assert json.loads(out.read_text(encoding="utf-8"))["text"] == "Effect − 0.5 ≤ p, α = .05"


def test_report_carries_links_counts_and_limits():
    exp = _expectation([_block("p", "paragraph", 1, 0, "Some text"), _block("f", "figure", 1, 1, None, graphic={"kind": "image"})])
    report = export_checks(exp, pdf_name="doc.pdf").report()
    json.dumps(report)
    assert report["record_id"] == "synthetic-exp" and report["pdf_name"] == "doc.pdf"
    assert report["olmocr_reference"] == OLMOCR_REFERENCE
    assert report["tests"] == [{"id": "synthetic-exp.p.present", "type": "present", "page": 1, "block_id": "p"}]
    assert report["counts"]["present"] == 1 and report["counts"]["skipped"] == 1
    assert report["not_verifiable_by_olmocr"] == list(NOT_VERIFIABLE)
    assert any("exact location" in item for item in NOT_VERIFIABLE)
    assert any("chunk coverage" in item for item in NOT_VERIFIABLE)


def test_candidate_markdown_name_matches_olmocr_layout():
    assert candidate_markdown_name("corpus.pdf", 3) == "corpus_pg3_repeat1.md"
    assert candidate_markdown_name("corpus.pdf", 3, repeat=2) == "corpus_pg3_repeat2.md"


def test_cli_checks_pdf_hash_and_writes_outputs(tmp_path, capsys):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    exp = _expectation([_block("p", "paragraph", 1, 0, "Some text")])
    exp["document"]["pdf_sha256"] = hashlib.sha256(b"%PDF-synthetic").hexdigest()
    path = tmp_path / "exp.json"
    path.write_text(json.dumps(exp), encoding="utf-8")
    out, report = tmp_path / "checks.jsonl", tmp_path / "report.json"

    assert main([str(path), "--pdf", str(pdf), "--out", str(out), "--report", str(report)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["pdf"] == "doc.pdf"
    assert json.loads(report.read_text(encoding="utf-8"))["counts"]["present"] == 1

    pdf.write_bytes(b"%PDF-changed")
    out.unlink()
    assert main([str(path), "--pdf", str(pdf), "--out", str(out)]) == 1
    assert "does not match" in capsys.readouterr().err
    assert not out.exists()


def test_cli_refuses_draft_expectations(tmp_path, capsys):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    exp = _expectation([_block("p", "paragraph", 1, 0, "Some text")], status="draft")
    exp["document"]["pdf_sha256"] = hashlib.sha256(b"%PDF-synthetic").hexdigest()
    path = tmp_path / "exp.json"
    path.write_text(json.dumps(exp), encoding="utf-8")
    assert main([str(path), "--pdf", str(pdf), "--out", str(tmp_path / "o.jsonl")]) == 1
    assert "not reviewed" in capsys.readouterr().err


def test_cross_page_continuations_are_reported_not_exported_as_chains():
    exp = _expectation(
        [
            _block("a", "paragraph", 1, 0, "Text that runs on"),
            _block("b", "paragraph", 2, 1, "onto the next page", continues_from="a"),
        ],
        pages=(1, 2),
    )
    result = export_checks(exp, pdf_name="doc.pdf")
    assert [t["text"] for t in _by_type(result, "present")] == ["Text that runs on", "onto the next page"]
    [item] = [s for s in result.skipped if s["kind"] == "continuation"]
    assert item["block_id"] == "b" and item["continues_from"] == "a" and "one page" in item["reason"]


def test_unreviewed_export_is_only_possible_for_the_synthetic_example():
    draft = _expectation([_block("p", "paragraph", 1, 0, "Text")], status="draft")
    with pytest.raises(RecordError, match="not reviewed"):
        export_checks(draft, pdf_name="doc.pdf")
    with pytest.raises(RecordError, match="synthetic-example-"):
        export_checks(draft, pdf_name="doc.pdf", allow_unreviewed_example=True)
    draft["record_id"] = "synthetic-example-draft"
    report = export_checks(draft, pdf_name="doc.pdf", allow_unreviewed_example=True).report()
    assert report["expectation_review_status"] == "draft"


def test_committed_example_is_an_unmistakable_unreviewed_synthetic_draft():
    assert "synthetic_example" in EXAMPLE.name and "draft" in EXAMPLE.name
    expectation = load_record(EXAMPLE, kind="expectation")
    review = expectation["provenance"]["review"]
    assert review["status"] == "draft" and "basis" not in review
    assert review["reviewer"].startswith("synthetic-example-")
    assert expectation["record_id"].startswith("synthetic-example-")
    assert "NOT REVIEWED TRUTH" in expectation["provenance"]["uncertainty"]["note"]
    with pytest.raises(RecordError, match="not reviewed"):
        load_reviewed_expectation(EXAMPLE)
    with pytest.raises(RecordError, match="not reviewed"):
        export_checks(expectation, pdf_name="corpus.pdf")
    assert hashlib.sha256(CORPUS_PDF.read_bytes()).hexdigest() == expectation["document"]["pdf_sha256"]
    result = export_checks(expectation, pdf_name="corpus.pdf", allow_unreviewed_example=True)
    counts = result.counts()
    assert (counts["present"], counts["order"], counts["table"], counts["math"]) == (7, 5, 12, 1)
    assert counts["skipped"] == 4 and counts["partial_table_relations"] == 0
    assert result.report()["expectation_review_status"] == "draft"


def test_cli_exports_the_example_only_with_the_explicit_flag(tmp_path, capsys):
    out = tmp_path / "checks.jsonl"
    args = [str(EXAMPLE), "--pdf", str(CORPUS_PDF), "--out", str(out)]
    assert main(args) == 1
    assert "not reviewed" in capsys.readouterr().err and not out.exists()
    assert main([*args, "--allow-synthetic-example"]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 25
