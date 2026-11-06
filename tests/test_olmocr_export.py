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
    export_checks,
    main,
    write_checks_jsonl,
)

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "benchmarks" / "extraction" / "examples" / "ocr_corpus_synthetic_example.draft_expectation.json"
CORPUS_PDF = REPO / "tests" / "fixtures" / "ocr_corpus" / "corpus.pdf"
SYNTHETIC_PDF_SHA256 = "eeb0b7f3716e738e03f623090f95d12cc368c87f592c7c6340d2bb199c1473eb"

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
        "geometry": {"unit": "pt", "origin": "top-left", "frame": "unrotated", "box": "cropbox", "rotation": 0, "width": 600, "height": 800},
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
    assert (0, 1, "right", "neighbour text has fewer than 1 alphanumeric character(s)") in relations
    assert (0, 1, "down", "colspan in this or the adjacent row shifts the row in upstream's HTML parsing") in relations
    skipped = {(s["row"], s["col"]): s["reason"] for s in result.skipped if s["kind"] == "table"}
    assert "spanned cell" in skipped[(1, 0)]
    assert "alphanumeric" in skipped[(0, 2)]


def test_equations_export_latex_or_report_skip():
    exp = _expectation([
        _block("e1", "equation", 1, 0, None, equation={"latex": "a=-b", "number": "(1)", "display": True}),
        _block("e2", "equation", 1, 1, None, equation={"latex": None}),
        _block("e3", "equation", 1, 2, "x = y", equation={"latex": None}),
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


def test_unicode_text_is_ascii_escaped_and_round_trips(tmp_path):
    exp = _expectation([_block("p", "paragraph", 1, 0, "Effect − 0.5 ≤ p, α = .05")])
    result = export_checks(exp, pdf_name="doc.pdf")
    out = tmp_path / "checks.jsonl"
    write_checks_jsonl(result.tests, out)
    raw = out.read_text(encoding="ascii")  # upstream reads the file without an encoding
    assert "\\u2212" in raw
    assert json.loads(raw)["text"] == "Effect − 0.5 ≤ p, α = .05"


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
    assert main([str(path), "--pdf", str(pdf), "--out", str(out), "--report", str(report)]) == 1
    assert "does not match" in capsys.readouterr().err
    assert not out.exists()


def test_cli_refuses_draft_expectations(tmp_path, capsys):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    exp = _expectation([_block("p", "paragraph", 1, 0, "Some text")], status="draft")
    exp["document"]["pdf_sha256"] = hashlib.sha256(b"%PDF-synthetic").hexdigest()
    path = tmp_path / "exp.json"
    path.write_text(json.dumps(exp), encoding="utf-8")
    assert main([str(path), "--pdf", str(pdf), "--out", str(tmp_path / "o.jsonl"), "--report", str(tmp_path / "r.json")]) == 1
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


def test_same_page_continuations_are_exported_per_fragment_and_chain_reported():
    exp = _expectation([
        _block("a", "paragraph", 1, 0, "Text before a figure"),
        _block("f", "figure", 1, 1, None, graphic={"kind": "image"}),
        _block("b", "paragraph", 1, 2, "and text after it", continues_from="a"),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    assert len(_by_type(result, "present")) == 2 and len(_by_type(result, "order")) == 1
    [item] = [s for s in result.skipped if s["kind"] == "continuation"]
    assert item["block_id"] == "b" and "same or earlier page" in item["reason"]


def test_truncated_blocks_are_skipped_with_a_reason_and_leave_no_checks():
    cells = [_cell(0, 0, "h", is_header=True), _cell(1, 0, "v")]
    exp = _expectation(
        [
            _block("a", "paragraph", 1, 0, "Cut off start of a paragraph", truncated="start"),
            _block("m", "paragraph", 1, 1, "Whole paragraph"),
            _block("t", "table", 1, 2, None, table=_table(cells, 2, 1), truncated="end"),
        ],
        pages=(1, 2),
        scope={"kind": "pages", "physical_pages": [1, 2]},
    )
    exp["pages"] = [_page(1), _page(2)]
    exp["document"]["page_count"] = 3
    result = export_checks(exp, pdf_name="doc.pdf")
    assert [t["text"] for t in result.tests] == ["Whole paragraph"]
    skipped = {s["block_id"]: s for s in result.skipped if s["kind"] == "truncated"}
    assert set(skipped) == {"a", "t"} and skipped["a"]["truncated"] == "start"


def test_unreviewed_export_is_only_possible_for_the_synthetic_example():
    draft = _expectation([_block("p", "paragraph", 1, 0, "Text")], status="draft")
    with pytest.raises(RecordError, match="not reviewed"):
        export_checks(draft, pdf_name="doc.pdf")
    with pytest.raises(RecordError, match="synthetic-example-"):
        export_checks(draft, pdf_name="doc.pdf", allow_unreviewed_example=True)
    draft["record_id"] = "synthetic-example-draft"
    draft["document"]["pdf_sha256"] = SYNTHETIC_PDF_SHA256
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
    assert counts["skipped"] == 6 and counts["partial_table_relations"] == 0
    assert result.report()["expectation_review_status"] == "draft"


def test_cli_exports_the_example_only_with_the_explicit_flag(tmp_path, capsys):
    out = tmp_path / "checks.jsonl"
    args = [str(EXAMPLE), "--pdf", str(CORPUS_PDF), "--out", str(out), "--report", str(tmp_path / "report.json")]
    assert main(args) == 1
    assert "not reviewed" in capsys.readouterr().err and not out.exists()
    assert main([*args, "--allow-synthetic-example"]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 25


def test_cli_requires_a_report(tmp_path):
    with pytest.raises(SystemExit):
        main([str(EXAMPLE), "--pdf", str(CORPUS_PDF), "--out", str(tmp_path / "o.jsonl")])


def test_unreviewed_export_also_requires_the_committed_corpus_pdf():
    draft = _expectation([_block("p", "paragraph", 1, 0, "Some text")], status="draft")
    draft["record_id"] = "synthetic-example-other"
    with pytest.raises(RecordError, match="committed synthetic corpus"):
        export_checks(draft, pdf_name="doc.pdf", allow_unreviewed_example=True)


def test_record_level_high_uncertainty_is_refused():
    exp = _expectation([_block("p", "paragraph", 1, 0, "Some text")], provenance={
        "review": {"status": "reviewed", "reviewer": "reviewer-a", "reviewed_at": "2026-01-01T00:00:00Z", "basis": "pdf_inspection"},
        "uncertainty": {"level": "high"},
    })
    with pytest.raises(RecordError, match="uncertainty is high"):
        export_checks(exp, pdf_name="doc.pdf")


def _partials(result):
    return {(p["row"], p["col"], p["relation"], p["reason"]) for p in result.partial}


def test_two_header_rows_assert_only_row_zero_and_report_the_rest():
    cells = [
        _cell(0, 0, "Group A", is_header=True), _cell(0, 1, "Group B", is_header=True),
        _cell(1, 0, "Mean", is_header=True), _cell(1, 1, "SD", is_header=True),
        _cell(2, 0, "1.5"), _cell(2, 1, "0.2"),
    ]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(cells, 3, 2))])
    result = export_checks(exp, pdf_name="doc.pdf")
    tests = {t["cell"]: t for t in _by_type(result, "table")}
    assert tests["1.5"]["top_heading"] == "Group A"
    assert any(p[2] == "top_heading" and "further header" in p[3] for p in _partials(result))


def test_header_not_in_row_zero_and_spanned_headers_are_not_asserted():
    cells = [
        _cell(0, 0, "x"), _cell(0, 1, "y"),
        _cell(1, 0, "Mean", is_header=True), _cell(1, 1, "Wide", is_header=True, colspan=1),
        _cell(2, 0, "1.5"), _cell(2, 1, "0.2"),
    ]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(cells, 3, 2))])
    result = export_checks(exp, pdf_name="doc.pdf")
    tests = {t["cell"]: t for t in _by_type(result, "table")}
    assert "top_heading" not in tests["1.5"]
    assert any("not in the first row" in p[3] for p in _partials(result))

    spanned = [
        _cell(0, 0, "Group", is_header=True, colspan=2),
        _cell(1, 0, "1.5"), _cell(1, 1, "0.2"),
    ]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(spanned, 2, 2))])
    result = export_checks(exp, pdf_name="doc.pdf")
    assert all("top_heading" not in t for t in _by_type(result, "table"))
    assert any(p[2] == "top_heading" and p[3] == "spanned header" for p in _partials(result))


def test_left_heading_comes_only_from_column_zero():
    cells = [
        _cell(0, 0, "Row A", is_header=True), _cell(0, 1, "7"), _cell(0, 2, "8"),
    ]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(cells, 1, 3))])
    tests = {t["cell"]: t for t in _by_type(export_checks(exp, pdf_name="doc.pdf"), "table")}
    assert tests["7"]["left_heading"] == "Row A" and tests["8"]["left_heading"] == "Row A"
    mid = [_cell(0, 0, "7"), _cell(0, 1, "Row B", is_header=True), _cell(0, 2, "8")]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(mid, 1, 3))])
    result = export_checks(exp, pdf_name="doc.pdf")
    assert all("left_heading" not in t for t in _by_type(result, "table"))
    assert any("not in the first row" in p[3] for p in _partials(result))


def test_order_is_skipped_when_one_text_contains_the_other():
    exp = _expectation([
        _block("a", "paragraph", 1, 0, "The effect was large and significant overall."),
        _block("b", "paragraph", 1, 1, "was large and significant"),
        _block("c", "paragraph", 1, 2, "Closing remarks follow here."),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    pairs = [(t["before"], t["after"]) for t in _by_type(result, "order")]
    assert pairs == [("was large and significant", "Closing remarks follow here.")]
    assert any(s["kind"] == "order" and "contains the other" in s["reason"] for s in result.skipped)
    # Containment is judged on normalised text: dashes, quotes, micro sign and <br> do not hide it.
    for outer, inner in (
        ("Dose was 5 \u2212 3 units here", "5 - 3 units"),
        ("He said \u201cstop\u201d twice", 'said "stop"'),
        ("Dose 5 \u00b5g per day", "5 \u03bcg per"),
        ("First line<br>second line follows", "line second line"),
    ):
        exp = _expectation([_block("a", "paragraph", 1, 0, outer), _block("b", "paragraph", 1, 1, inner)])
        assert _by_type(export_checks(exp, pdf_name="doc.pdf"), "order") == [], outer


def test_any_underscore_or_asterisk_in_a_text_block_is_skipped_but_not_in_cells():
    cells = [_cell(0, 0, "0.42*"), _cell(0, 1, "snake_case"), _cell(0, 2, "plain")]
    exp = _expectation([
        _block("p", "paragraph", 1, 0, "x_i and a lone marker here"),
        _block("q", "paragraph", 1, 1, "balanced _emphasis_ text"),
        _block("r", "paragraph", 1, 2, "a **bold** claim stands"),
        _block("s", "paragraph", 1, 3, "plain text without markers"),
        _block("t", "table", 1, 4, None, table=_table(cells, 1, 3)),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    assert [t["text"] for t in _by_type(result, "present")] == ["plain text without markers"]
    skipped = {s["block_id"]: s["reason"] for s in result.skipped if s["kind"] == "present"}
    assert set(skipped) == {"p", "q", "r"} and all("page-wide" in r for r in skipped.values())
    # Cells are normalised one by one upstream, so emphasis characters are exported.
    tests = {t["cell"]: t for t in _by_type(result, "table")}
    assert set(tests) == {"0.42*", "snake_case", "plain"}
    assert tests["0.42*"]["right"] == "snake_case"


def test_pipe_in_a_cell_or_neighbour_is_not_asserted():
    cells = [
        _cell(0, 0, "a|b"), _cell(0, 1, "fine"), _cell(0, 2, "ok"),
        _cell(1, 0, "head", is_header=True), _cell(1, 1, "x"), _cell(1, 2, "y"),
    ]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(cells, 2, 3))])
    result = export_checks(exp, pdf_name="doc.pdf")
    tests = {t["cell"]: t for t in _by_type(result, "table")}
    assert "a|b" not in tests
    assert any(s["kind"] == "table" and "'|'" in s["reason"] and s["col"] == 0 and s["row"] == 0 for s in result.skipped)
    assert "left" not in tests["fine"] and "up" not in tests["head"]
    reasons = {(p["row"], p["col"], p["relation"]): p["reason"] for p in result.partial}
    assert "'|'" in reasons[(0, 1, "left")] and "'|'" in reasons[(1, 0, "up")]
    # A pipe in the header cell is dropped from the heading, not asserted.
    hdr = [_cell(0, 0, "H|1", is_header=True), _cell(1, 0, "val")]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(hdr, 2, 1))])
    result = export_checks(exp, pdf_name="doc.pdf")
    assert all("top_heading" not in t for t in _by_type(result, "table"))
    assert any(p["relation"] == "top_heading" and "'|'" in p["reason"] for p in result.partial)


def test_vertical_relations_across_a_colspan_row_are_dropped_to_partial():
    cells = [
        _cell(0, 0, "A"), _cell(0, 1, "B"), _cell(0, 2, "C"),
        _cell(1, 0, "wide", colspan=2), _cell(1, 2, "D"),
        _cell(2, 0, "E"), _cell(2, 1, "F"), _cell(2, 2, "G"),
    ]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(cells, 3, 3))])
    result = export_checks(exp, pdf_name="doc.pdf")
    tests = {t["cell"]: t for t in _by_type(result, "table")}
    # Cells right of the span lose up/down relations that cross the colspan row.
    assert "down" not in tests["C"] and "up" not in tests["D"] and "down" not in tests["D"] and "up" not in tests["G"]
    # Cells left of the span are unaffected, and horizontal relations stay.
    assert tests["A"]["right"] == "B" and tests["E"]["right"] == "F"
    assert tests["C"]["left"] == "B" and tests["G"]["left"] == "F"
    # Rows without any colspan neighbour keep vertical relations.
    reasons = {(p["row"], p["col"], p["relation"]) for p in result.partial if "colspan" in p["reason"]}
    assert {(0, 2, "down"), (1, 2, "up"), (1, 2, "down"), (2, 2, "up")} <= reasons
    plain = [_cell(0, 0, "A"), _cell(0, 1, "B"), _cell(1, 0, "C"), _cell(1, 1, "D")]
    exp = _expectation([_block("t", "table", 1, 0, None, table=_table(plain, 2, 2))])
    assert _by_type(export_checks(exp, pdf_name="doc.pdf"), "table")[3]["up"] == "B"


def test_too_little_alphanumeric_content_is_skipped():
    exp = _expectation([_block("a", "paragraph", 1, 0, "12"), _block("b", "paragraph", 1, 1, "Fig 1")])
    result = export_checks(exp, pdf_name="doc.pdf")
    assert [t["text"] for t in _by_type(result, "present")] == ["Fig 1"]
    assert any(s["block_id"] == "a" and "fewer than 3" in s["reason"] for s in result.skipped)


def test_ignored_fields_get_item_level_skip_entries():
    cells = [_cell(0, 0, "h", is_header=True), _cell(1, 0, "v")]
    exp = _expectation([
        _block("t", "table", 1, 0, "h v", table=_table(cells, 2, 1)),
        _block("c", "caption", 1, 1, "Table 1 shows values", caption_for="t"),
        _block("e", "equation", 1, 2, "x squared", equation={"latex": "x^2", "number": "(2)"}),
        _block("f", "figure", 1, 3, "axis label", graphic={"kind": "chart"}),
        _block("g", "figure", 1, 4, None, graphic={"kind": "image"}),
    ])
    result = export_checks(exp, pdf_name="doc.pdf")
    kinds = {(s["kind"], s["block_id"]) for s in result.skipped}
    assert {("text", "t"), ("text", "e"), ("caption_link", "c"), ("equation_number", "e"), ("figure", "f"), ("figure", "g")} <= kinds
    reasons = {s["block_id"]: s["reason"] for s in result.skipped if s["kind"] == "figure"}
    assert "inside the figure" in reasons["f"] and "without text" in reasons["g"]


def test_feasibility_inputs_script_builds_the_benchmark_directory(tmp_path):
    pytest.importorskip("pymupdf4llm")
    from extraction.olmocr_feasibility_inputs import build

    counts = build(tmp_path)
    bench = tmp_path / "bench"
    assert (bench / "pdfs" / "corpus.pdf").read_bytes() == CORPUS_PDF.read_bytes()
    assert counts["total"] == len((bench / "ocr_corpus.jsonl").read_text(encoding="ascii").splitlines())
    for name in ("pymupdf4llm", "defective", "mathcontrol", "mathwrong"):
        assert sorted(f.name for f in (bench / name).iterdir()) == [f"corpus_pg{n}_repeat1.md" for n in range(1, 7)]
    good = (bench / "pymupdf4llm" / "corpus_pg3_repeat1.md").read_text(encoding="utf-8")
    bad = (bench / "defective" / "corpus_pg3_repeat1.md").read_text(encoding="utf-8")
    assert "|Baseline|12.40|1.15|" in good and "|Baseline|15.82|1.15|" in bad and "1640" in bad
    control = (bench / "mathcontrol" / "corpus_pg1_repeat1.md").read_text(encoding="utf-8")
    wrong = (bench / "mathwrong" / "corpus_pg1_repeat1.md").read_text(encoding="utf-8")
    assert "$$" in control and "$$" in wrong and control != wrong
    # The control must differ textually from the fixture's LaTeX so that it exercises KaTeX
    # render comparison rather than upstream's exact-string shortcut.
    fixture_latex = next(b["equation"]["latex"] for b in load_record(EXAMPLE, kind="expectation")["blocks"] if b["id"] == "p1-eq1")
    assert fixture_latex not in control
