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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .records import RecordError, load_reviewed_expectation, sha256_bytes, validate_record

EXPORTER_VERSION = "1"

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
)

_RELATIONS = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}


@dataclass
class ExportResult:
    """Checks plus everything that did not become one."""

    record_id: str
    pdf_name: str
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


def candidate_markdown_name(pdf_name: str, page: int, repeat: int = 1) -> str:
    """File name olmOCR's benchmark expects for one page of a candidate's Markdown."""
    stem = pdf_name[: -len(".pdf")] if pdf_name.lower().endswith(".pdf") else pdf_name
    return f"{stem}_pg{page}_repeat{repeat}.md"


def _has_content(text: Any) -> bool:
    """olmOCR rejects empty search text, and punctuation-only text matches almost anything."""
    return isinstance(text, str) and any(ch.isalnum() for ch in text)


def export_checks(expectation: dict[str, Any], *, pdf_name: str) -> ExportResult:
    """Derive olmOCR-bench checks from a validated, reviewed expectation.

    ``pdf_name`` is the name of the PDF inside the benchmark's ``pdfs/`` folder; it is the
    ``pdf`` field of every check.
    """
    kind = validate_record(expectation)
    if kind != "expectation":
        raise RecordError([f"expected an expectation record, got {kind}"])
    if expectation["provenance"]["review"]["status"] != "reviewed":
        raise RecordError(["expectation is not reviewed; drafts are not ground truth"])
    if not pdf_name or "/" in pdf_name or "\\" in pdf_name:
        raise ValueError("pdf_name must be a bare file name")

    result = ExportResult(record_id=expectation["record_id"], pdf_name=pdf_name)
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
        if block.get("uncertainty", {}).get("level") == "high":
            skip("block", "high uncertainty: the expectation says not to score this strictly", block=block)
        elif btype == "table":
            _export_table(block, rid, emit, skip, result)
        elif btype == "equation":
            _export_equation(block, rid, emit, skip)
        elif btype in TEXT_TYPES:
            if _has_content(block.get("text")):
                emit(f"{rid}.{block['id']}.present", block, "present", text=block["text"], case_sensitive=True)
                eligible.append(block)
            else:
                skip("present", "block has no text with alphanumeric content", block=block)
        elif btype in FURNITURE_TYPES:
            skip("absent", "page furniture: the artifact does not require extractors to omit it", block=block)
        elif btype == "figure":
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
        elif before["text"] == after["text"]:
            skip("order", "identical text in both blocks: the check could not tell them apart", block=before, after_block_id=after["id"])
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
            skip("citation_marker", "citation-marker links are not olmOCR-bench checks", block_id=marker.get("block_id"), page=marker["page"], marker_id=marker["id"])

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

    for cell in sorted(table["cells"], key=lambda c: (c["row"], c["col"])):
        r, c = cell["row"], cell["col"]
        where = {"row": r, "col": c}
        if not _has_content(cell["text"]):
            skip("table", "cell text has no alphanumeric content", block=block, **where)
            continue
        if spanned(cell):
            skip("table", "spanned cell: olmOCR relations depend on its rowspan/colspan handling", block=block, **where)
            continue
        fields: dict[str, Any] = {"cell": cell["text"]}
        for name, (dr, dc) in _RELATIONS.items():
            neighbour = grid.get((r + dr, c + dc))
            if neighbour is None:
                continue  # edge of the table: nothing to assert
            if spanned(neighbour) or not _has_content(neighbour["text"]):
                reason = "spanned neighbour" if spanned(neighbour) else "empty neighbour"
                result.partial.append({"block_id": block["id"], "page": block["page"], "relation": name, "reason": reason, **where})
            else:
                fields[name] = neighbour["text"]
        if not cell.get("is_header"):
            heading = _heading(grid, r, c, vertical=True)
            if heading:
                fields["top_heading"] = heading
            heading = _heading(grid, r, c, vertical=False)
            if heading:
                fields["left_heading"] = heading
        emit(f"{rid}.{block['id']}.table.r{r}c{c}", block, "table", **fields)


def _heading(grid: dict[tuple[int, int], dict[str, Any]], r: int, c: int, *, vertical: bool) -> str | None:
    """Nearest header cell above (vertical) or to the left of (r, c), if it has usable text."""
    steps = range(r - 1, -1, -1) if vertical else range(c - 1, -1, -1)
    for step in steps:
        cell = grid.get((step, c) if vertical else (r, step))
        if cell is not None and cell.get("is_header"):
            return cell["text"] if _has_content(cell["text"]) else None
    return None


def write_checks_jsonl(tests: list[dict[str, Any]], path: str | Path) -> None:
    """Write one JSON test per line, UTF-8, in the layout olmOCR's ``load_tests`` reads."""
    lines = (json.dumps(test, ensure_ascii=False) for test in tests)
    Path(path).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m extraction.olmocr_export",
        description="Export a reviewed expectation as olmOCR-bench checks (JSONL) plus a sidecar report.",
    )
    parser.add_argument("expectation", type=Path, help="reviewed expectation record (JSON)")
    parser.add_argument("--pdf", type=Path, required=True, help="the PDF the expectation describes; its SHA-256 must match")
    parser.add_argument("--pdf-name", help="name of the PDF in the benchmark's pdfs/ folder (default: the --pdf file name)")
    parser.add_argument("--out", type=Path, required=True, help="output checks file (.jsonl)")
    parser.add_argument("--report", type=Path, help="output report (JSON): counts, skipped elements, check-to-block links")
    args = parser.parse_args(argv)

    try:
        expectation = load_reviewed_expectation(args.expectation)
        actual = sha256_bytes(args.pdf.read_bytes())
        if actual != expectation["document"]["pdf_sha256"]:
            raise RecordError([f"--pdf hash {actual} does not match the expectation's pdf_sha256"])
        result = export_checks(expectation, pdf_name=args.pdf_name or args.pdf.name)
    except (RecordError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    write_checks_jsonl(result.tests, args.out)
    if args.report:
        args.report.write_text(json.dumps(result.report(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    counts = result.counts()
    print(f"{counts['total']} checks written to {args.out}; {counts['skipped']} skipped, {counts['partial_table_relations']} partial table relations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
