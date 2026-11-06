"""Export a reviewed expectation as olmOCR-bench checks (roadmap E1, package C).

This is a thin one-way adapter: the internal expectation stays the only answer key and the
exporter derives every check from it. It imports nothing from olmOCR, so it needs no olmOCR
install; scoring the exported file is done separately in an isolated environment
(``docs/olmocr-bench-feasibility.md``).

The target format is olmOCR-bench's JSONL, one test per line, as defined by
``olmocr/bench/tests.py`` at the commit pinned in ``OLMOCR_REFERENCE``. Its ``load_single_test``
builds each dataclass with ``**data``, so a line may carry only that class's own fields; links
back to our blocks therefore live in the sidecar report (and in the test ``id``), never in the
JSONL. Every expectation element that does not become a check is reported as skipped with a
reason, so nothing is dropped silently.

Everything is exported strict (``max_diffs`` 0, case-sensitive). olmOCR still applies its own
normalisation before comparing (NFC, whitespace, Markdown emphasis, dash/quote variants,
``<br>``), so the checks are strict within that normalisation, not byte-exact.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .records import RecordError, load_record, load_reviewed_expectation, sha256_bytes, validate_record

EXPORTER_VERSION = "1"
SYNTHETIC_EXAMPLE_PREFIX = "synthetic-example-"
# SHA-256 of the committed tests/fixtures/ocr_corpus/corpus.pdf: an unreviewed record may only be
# exported for that document.
SYNTHETIC_EXAMPLE_PDF_SHA256 = "eeb0b7f3716e738e03f623090f95d12cc368c87f592c7c6340d2bb199c1473eb"
# Text blocks need at least this many alphanumeric characters for a presence/order check; shorter
# strings match almost anywhere on a page and would pass vacuously. Table cells need only one.
MIN_TEXT_ALNUM = 3

OLMOCR_REFERENCE = {
    "repository": "https://github.com/allenai/olmocr",
    "version": "v0.4.27",
    "commit": "1e139a5ea61f1668164e6b63357d7284a8391615",
    "files": ["olmocr/bench/README.md", "olmocr/bench/tests.py", "olmocr/bench/benchmark.py"],
}

# Blocks whose ``text`` becomes a presence check and takes part in ordering checks.
TEXT_TYPES = frozenset({"heading", "paragraph", "list_item", "caption", "footnote", "reference_entry"})
# Page furniture: the artifact does not say an extractor must omit it, so no "absent" check is made.
FURNITURE_TYPES = frozenset({"header", "footer", "page_number"})

# What the olmOCR scorer cannot establish; carried into every report so the limits travel with
# the numbers. Our own checks must cover these.
NOT_VERIFIABLE = (
    "exact location: checks are page-level and position-insensitive (bounding boxes unused)",
    "text_sha256 / exact span round-trips: olmOCR normalises Unicode, whitespace, emphasis, dashes and quotes",
    "chunk coverage: olmOCR scores one page's Markdown, not passage or chunk boundaries",
    "cross-page reading order: order checks relate two texts on a single page",
    "reference parsing, identifier recovery and citation-marker links",
    "figure/graphic content and caption-to-figure links (only the caption text is checked)",
    "order when one text occurs inside the other, or when the later text also occurs earlier on the page"
    " (olmOCR accepts any pair of matches; contained-text pairs are skipped, earlier duplicates are not detected)",
    "text containing '_' or '*' in a text block (olmOCR strips emphasis page-wide with a lazy regex, so"
    " any such marker can pair with one elsewhere on the page); such blocks are skipped, not checked",
    "table cells containing '|' (upstream's Markdown table parser splits on every '|'); such cells are skipped",
    "vertical relations across a colspan row (upstream's HTML parser shifts the rest of the row); dropped",
    "relations at table edges: olmOCR ignores a neighbour relation where the table has no neighbour",
)

_RELATIONS = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}


@dataclass
class ExportResult:
    """Checks plus everything that did not become one."""

    record_id: str
    pdf_name: str
    review_status: str = "reviewed"
    tests: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # test id -> expectation block id
    skipped: list[dict[str, Any]] = field(default_factory=list)
    partial: list[dict[str, Any]] = field(default_factory=list)  # table relations left out of an emitted check

    def counts(self) -> dict[str, int]:
        counts = {"present": 0, "order": 0, "table": 0, "math": 0}
        for test in self.tests:
            counts[test["type"]] += 1
        counts["total"] = len(self.tests)
        counts["skipped"] = len(self.skipped)
        counts["partial_table_relations"] = len(self.partial)
        return counts

    def report(self) -> dict[str, Any]:
        return {
            "exporter_version": EXPORTER_VERSION,
            "olmocr_reference": OLMOCR_REFERENCE,
            "record_id": self.record_id,
            "expectation_review_status": self.review_status,
            "pdf_name": self.pdf_name,
            "counts": self.counts(),
            "tests": [
                {"id": t["id"], "type": t["type"], "page": t["page"], "block_id": self.sources[t["id"]]}
                for t in self.tests
            ],
            "skipped": self.skipped,
            "partial": self.partial,
            "not_verifiable_by_olmocr": list(NOT_VERIFIABLE),
        }


def _has_content(text: Any, minimum: int = 1) -> bool:
    """olmOCR rejects empty search text, and punctuation-only text matches almost anything."""
    return isinstance(text, str) and sum(ch.isalnum() for ch in text) >= minimum


_DASHES_QUOTES = str.maketrans(
    {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"',
        "\u2013": "-", "\u2014": "-", "\u2011": "-", "\u2012": "-", "\u2212": "-", "\u00b5": "\u03bc",
    }
)


def _comparable(text: str) -> str:
    """Stand-in for olmOCR's normalisation, used only to detect one text inside another."""
    text = re.sub(r"<br/?>", " ", text)
    text = unicodedata.normalize("NFC", text).translate(_DASHES_QUOTES)
    return re.sub(r"\s+", " ", text.replace("*", "").replace("_", "")).strip()


def _unusable(text: Any, minimum: int) -> str | None:
    """Reason a text *block* cannot become a check, or None.

    olmOCR strips ``_x_`` / ``*x*`` over the whole page with a lazy regex, so a block's own marker
    balance protects nothing: any ``_`` or ``*`` can pair with one elsewhere on the page.
    """
    if not _has_content(text, minimum):
        return f"text has fewer than {minimum} alphanumeric character(s)"
    if "_" in text or "*" in text:
        return "text contains '_' or '*': olmOCR's page-wide emphasis stripping could pair it with markers elsewhere on the page"
    return None


def _unusable_cell(text: Any) -> str | None:
    """Reason a table cell (or neighbour/header text) cannot be asserted, or None.

    Cells are normalised one by one upstream, so emphasis characters are fine here; a ``|`` is not,
    because the Markdown table parser splits on every one.
    """
    if not _has_content(text, 1):
        return "text has fewer than 1 alphanumeric character(s)"
    if "|" in text:
        return "text contains '|', which upstream's Markdown table parser splits on"
    return None


def export_checks(expectation: dict[str, Any], *, pdf_name: str, allow_unreviewed_example: bool = False) -> ExportResult:
    """Derive olmOCR-bench checks from a validated, reviewed expectation.

    ``pdf_name`` is the name of the PDF inside the benchmark's ``pdfs/`` folder; it is the
    ``pdf`` field of every check.

    ``allow_unreviewed_example`` lets a ``draft`` expectation through, solely for the committed
    synthetic example (no schema ``basis`` value honestly describes a hand-written fixture, so it
    is a draft rather than a falsely "reviewed" record). The report then says it is not truth.
    """
    kind = validate_record(expectation)
    if kind != "expectation":
        raise RecordError([f"expected an expectation record, got {kind}"])
    if expectation["provenance"].get("uncertainty", {}).get("level") == "high":
        raise RecordError(["record-level uncertainty is high: the expectation says it must not be scored strictly"])
    status = expectation["provenance"]["review"]["status"]
    if status != "reviewed" and not allow_unreviewed_example:
        raise RecordError(["expectation is not reviewed; drafts are not ground truth"])
    if status != "reviewed" and not expectation["record_id"].startswith(SYNTHETIC_EXAMPLE_PREFIX):
        raise RecordError([f"only records whose id starts with {SYNTHETIC_EXAMPLE_PREFIX!r} may be exported unreviewed"])
    if status != "reviewed" and expectation["document"]["pdf_sha256"] != SYNTHETIC_EXAMPLE_PDF_SHA256:
        raise RecordError(["an unreviewed record may only be exported for the committed synthetic corpus PDF"])
    if not pdf_name or "/" in pdf_name or "\\" in pdf_name:
        raise ValueError("pdf_name must be a bare file name")

    result = ExportResult(record_id=expectation["record_id"], pdf_name=pdf_name, review_status=status)
    rid = result.record_id

    def skip(kind: str, reason: str, *, block: dict[str, Any] | None = None, **extra: Any) -> None:
        item: dict[str, Any] = {"kind": kind, "reason": reason}
        if block is not None:
            item.update(block_id=block["id"], page=block["page"])
        item.update(extra)
        result.skipped.append(item)

    def emit(test_id: str, block: dict[str, Any], test_type: str, **fields: Any) -> None:
        test = {"pdf": pdf_name, "page": block["page"], "id": test_id, "type": test_type, "max_diffs": 0}
        test.update(fields)
        result.tests.append(test)
        result.sources[test_id] = block["id"]

    ordered = sorted(expectation["blocks"], key=lambda b: b["reading_order"])
    eligible: list[dict[str, Any]] = []
    for block in ordered:
        btype = block["type"]
        if "truncated" in block:
            skip(
                "truncated",
                "content cut off at the scope boundary: the expected text is a fragment of the block,"
                " so no check is exported that could be false for the whole",
                block=block,
                truncated=block["truncated"],
            )
            continue
        if "continues_from" in block:
            skip(
                "continuation",
                "block continues an earlier block (same or earlier page): olmOCR scores one page, so"
                " this fragment is checked on its own and the chain itself is not checked",
                block=block,
                continues_from=block["continues_from"],
            )
        if "caption_for" in block:
            skip("caption_link", "caption links to a figure/table are not olmOCR-bench checks", block=block, caption_for=block["caption_for"])
        number = (block.get("equation") or {}).get("number")
        if number:
            skip("equation_number", "printed equation numbers are not olmOCR-bench checks", block=block, number=number)
        if block.get("uncertainty", {}).get("level") == "high":
            skip("block", "high uncertainty: the expectation says not to score this strictly", block=block)
        elif btype == "table":
            if isinstance(block.get("text"), str) and block.get("table"):
                skip("text", "text on a table block is not exported; the table is checked through its cells", block=block)
            _export_table(block, rid, emit, skip, result)
        elif btype == "equation":
            if isinstance(block.get("text"), str) and (block.get("equation") or {}).get("latex"):
                skip("text", "text on an equation block is not exported; the equation is checked through its LaTeX", block=block)
            _export_equation(block, rid, emit, skip)
        elif btype in TEXT_TYPES:
            reason = _unusable(block.get("text"), MIN_TEXT_ALNUM)
            if reason is None:
                emit(f"{rid}.{block['id']}.present", block, "present", text=block["text"], case_sensitive=True)
                eligible.append(block)
            else:
                skip("present", reason, block=block)
        elif btype in FURNITURE_TYPES:
            skip("absent", "page furniture: the artifact does not require extractors to omit it", block=block)
        elif btype == "figure":
            if isinstance(block.get("text"), str) and block["text"].strip():
                skip("figure", "text inside the figure is not exported; only the caption is checked", block=block)
            else:
                skip("figure", "graphic without text: nothing expressible in olmOCR-bench", block=block)
        else:
            skip("block", f"block type {btype!r} has no olmOCR-bench check", block=block)

    for before, after in zip(eligible, eligible[1:], strict=False):
        if before["page"] != after["page"]:
            skip(
                "order",
                "consecutive text blocks on different pages; olmOCR order checks are single-page",
                block=before,
                after_block_id=after["id"],
            )
        elif _comparable(before["text"]) == _comparable(after["text"]):
            skip("order", "identical text in both blocks: the check could not tell them apart", block=before, after_block_id=after["id"])
        elif _comparable(before["text"]) in _comparable(after["text"]) or _comparable(after["text"]) in _comparable(before["text"]):
            skip("order", "one text contains the other: olmOCR would pass the check in either order", block=before, after_block_id=after["id"])
        else:
            emit(f"{rid}.{before['id']}.before.{after['id']}.order", before, "order", before=before["text"], after=after["text"])

    for page in expectation["pages"]:
        for name in ("body_start", "body_end"):
            if name in page:
                skip("page_anchor", f"{name} anchors have no olmOCR-bench equivalent", page=page["physical_page"], block_id=page[name]["block_id"])
    refs = expectation.get("references")
    if refs:
        for entry in refs["entries"]:
            skip("reference_entry", "reference parsing and identifier recovery are not olmOCR-bench checks", block_id=entry.get("block_id"), page=entry["page"], entry_id=entry["id"])
        for marker in refs["markers"]:
            skip("citation_marker", "citation-marker links are not olmOCR-bench checks", block_id=marker.get("block_id"), page=marker["page"], marker_id=marker["id"], target_status=marker["target_status"])

    ids = [t["id"] for t in result.tests]
    if len(set(ids)) != len(ids):  # unreachable for validated records; olmOCR rejects duplicates outright
        raise RecordError(["exported check ids are not unique"])
    return result


def _export_equation(block: dict[str, Any], rid: str, emit: Any, skip: Any) -> None:
    latex = (block.get("equation") or {}).get("latex")
    if isinstance(latex, str) and latex.strip():
        emit(f"{rid}.{block['id']}.math", block, "math", math=latex)
    else:
        skip("math", "equation has no LaTeX in the expectation", block=block)


def _export_table(block: dict[str, Any], rid: str, emit: Any, skip: Any, result: ExportResult) -> None:
    table = block.get("table")
    if table is None:
        skip("table", "table block has no cell grid in the expectation", block=block)
        return
    grid = {}
    for cell in table["cells"]:
        for r in range(cell["row"], cell["row"] + cell.get("rowspan", 1)):
            for c in range(cell["col"], cell["col"] + cell.get("colspan", 1)):
                grid[(r, c)] = cell

    def spanned(cell: dict[str, Any]) -> bool:
        return cell.get("rowspan", 1) > 1 or cell.get("colspan", 1) > 1

    # First column of a colspan cell, per row: upstream's HTML parser shifts everything to its right.
    shifted_from: dict[int, int] = {}
    for cell in table["cells"]:
        if cell.get("colspan", 1) > 1:
            for r in range(cell["row"], cell["row"] + cell.get("rowspan", 1)):
                shifted_from[r] = min(cell["col"], shifted_from.get(r, cell["col"]))

    def shifted(row: int, col: int) -> bool:
        return row in shifted_from and col > shifted_from[row]

    def partial(where: dict[str, int], relation: str, reason: str) -> None:
        result.partial.append({"block_id": block["id"], "page": block["page"], "relation": relation, "reason": reason, **where})

    for cell in sorted(table["cells"], key=lambda c: (c["row"], c["col"])):
        r, c = cell["row"], cell["col"]
        where = {"row": r, "col": c}
        reason = _unusable_cell(cell["text"])
        if reason is not None:
            skip("table", f"cell {reason}", block=block, **where)
            continue
        if spanned(cell):
            skip("table", "spanned cell: olmOCR relations depend on its rowspan/colspan handling", block=block, **where)
            continue
        fields: dict[str, Any] = {"cell": cell["text"]}
        for name, (dr, dc) in _RELATIONS.items():
            neighbour = grid.get((r + dr, c + dc))
            if neighbour is None:
                continue  # edge of the table: olmOCR ignores the relation there too
            if dr and (shifted(r, c) or shifted(r + dr, c)):
                partial(where, name, "colspan in this or the adjacent row shifts the row in upstream's HTML parsing")
            elif spanned(neighbour):
                partial(where, name, "spanned neighbour")
            elif (why := _unusable_cell(neighbour["text"])) is not None:
                partial(where, name, f"neighbour {why}")
            else:
                fields[name] = neighbour["text"]
        if not cell.get("is_header"):
            for vertical in (True, False):
                _add_heading(fields, grid, cell, vertical, partial, spanned)
        emit(f"{rid}.{block['id']}.table.r{r}c{c}", block, "table", **fields)


def _add_heading(fields: dict[str, Any], grid: dict[tuple[int, int], dict[str, Any]], cell: dict[str, Any], vertical: bool, partial: Any, spanned: Any) -> None:
    """Add ``top_heading`` (header in row 0) or ``left_heading`` (header in column 0).

    Upstream's Markdown parser treats only row 0 as the header row, so a header elsewhere, a
    spanned header or an unusable one cannot be asserted; those cases are recorded as partial.
    """
    r, c = cell["row"], cell["col"]
    name = "top_heading" if vertical else "left_heading"
    along = range(r) if vertical else range(c)
    headers = []
    for i in along:
        candidate = grid.get((i, c) if vertical else (r, i))
        if candidate is not None and candidate.get("is_header"):
            headers.append((i, candidate))
    if not headers:
        return  # the cell has no header: nothing to assert
    where = {"row": r, "col": c}
    index, first = headers[0]
    if index != 0:
        partial(where, name, "header is not in the first row/column: olmOCR only treats row 0 as header")
    elif spanned(first):
        partial(where, name, "spanned header")
    elif (why := _unusable_cell(first["text"])) is not None:
        partial(where, name, f"header {why}")
    else:
        fields[name] = first["text"]
        if len(headers) > 1:
            partial(where, name, "further header rows/columns ignored: only the first is asserted")


def write_checks_jsonl(tests: list[dict[str, Any]], path: str | Path) -> None:
    """Write one JSON test per line in the layout olmOCR's ``load_tests`` reads.

    ASCII-only (``\\uXXXX`` escapes): upstream opens the file without an encoding, so raw UTF-8
    would be misread wherever the platform default is not UTF-8.
    """
    lines = (json.dumps(test, ensure_ascii=True) for test in tests)
    Path(path).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m extraction.olmocr_export",
        description="Export a reviewed expectation as olmOCR-bench checks (JSONL) plus a sidecar report.",
    )
    parser.add_argument("expectation", type=Path, help="reviewed expectation record (JSON)")
    parser.add_argument("--pdf", type=Path, required=True, help="the PDF the expectation describes; its SHA-256 must match")
    parser.add_argument("--pdf-name", help="name of the PDF in the benchmark's pdfs/ folder (default: the --pdf file name)")
    parser.add_argument(
        "--allow-synthetic-example",
        action="store_true",
        help="accept a draft expectation; only for the committed synthetic example, never for baseline data",
    )
    parser.add_argument("--out", type=Path, required=True, help="output checks file (.jsonl)")
    parser.add_argument("--report", type=Path, required=True, help="output report (JSON): counts, skipped elements, check-to-block links")
    args = parser.parse_args(argv)

    try:
        if args.allow_synthetic_example:
            expectation = load_record(args.expectation, kind="expectation")
        else:
            expectation = load_reviewed_expectation(args.expectation)
        actual = sha256_bytes(args.pdf.read_bytes())
        if actual != expectation["document"]["pdf_sha256"]:
            raise RecordError([f"--pdf hash {actual} does not match the expectation's pdf_sha256"])
        result = export_checks(expectation, pdf_name=args.pdf_name or args.pdf.name, allow_unreviewed_example=args.allow_synthetic_example)
    except (RecordError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    write_checks_jsonl(result.tests, args.out)
    args.report.write_text(json.dumps(result.report(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    counts = result.counts()
    print(f"{counts['total']} checks written to {args.out}; {counts['skipped']} skipped, {counts['partial_table_relations']} partial table relations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
