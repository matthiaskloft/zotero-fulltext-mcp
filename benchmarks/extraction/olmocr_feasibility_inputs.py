"""Build the inputs of the olmOCR-bench feasibility run (docs/olmocr-bench-feasibility.md).

Writes an olmOCR benchmark directory under ``WORK``::

    WORK/bench/pdfs/corpus.pdf
    WORK/bench/ocr_corpus.jsonl                       exported checks
    WORK/bench/pymupdf4llm/corpus_pg<N>_repeat1.md    the repository's own Markdown, split by page
    WORK/bench/defective/...                          same, with four injected defects
    WORK/bench/mathcontrol/...                        same, plus a equivalent equation as $$...$$
    WORK/bench/mathwrong/...                          same, plus a wrong equation as $$...$$
    WORK/export_report.json

It needs the project environment (pymupdf4llm) but not olmOCR; scoring is a separate step in the
isolated environment. Run with ``benchmarks`` on ``PYTHONPATH``::

    python -m extraction.olmocr_feasibility_inputs WORK
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .olmocr_export import export_checks, write_checks_jsonl
from .records import load_record

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PDF = REPO_ROOT / "tests" / "fixtures" / "ocr_corpus" / "corpus.pdf"
EXPECTATION = Path(__file__).resolve().parent / "examples" / "ocr_corpus_synthetic_example.draft_expectation.json"
PDF_NAME = "corpus.pdf"

# Deliberately not the fixture's string (which has ``\,`` after ``w_i``): the control must pass
# through upstream's KaTeX render comparison, not its exact-string shortcut. The wrong variant
# changes a product to a sum and must fail the same comparison.
_EQUATION = r"$$\bar{x}_{w} = \frac{\sum_{i=1}^{n} w_i x_i}{\sum_{i=1}^{n} w_i}$$"
_WRONG_EQUATION = r"$$\bar{x}_{w} = \frac{\sum_{i=1}^{n} w_i + x_i}{\sum_{i=1}^{n} w_i}$$"
_INTRO = (
    "This document exists to be converted, not read. Prose paragraphs are present so that crops have "
    "neighbouring text to be located by, mirroring how a real article reads around its display elements."
)
_LEAD = "CORPUSMARK-EQ-001 A numbered display equation, the standard weighted arithmetic mean:"
_TRAIL = "The weights are normalised so that the result stays bounded."


def _md_name(page: int) -> str:
    return f"corpus_pg{page}_repeat1.md"


def extract_pages(work: Path) -> dict[int, str]:
    """Run the repository's extraction entry point with default options; split Markdown by page."""
    out = work / "extract" / "full.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "zotero_pdf_text._extract_markdown", str(CORPUS_PDF), str(out)], check=True)
    text = out.read_text(encoding="utf-8")
    pages: dict[int, str] = {}
    offset = 0
    for number, length in json.loads(out.with_suffix(".pages.json").read_text(encoding="utf-8")):
        pages[int(number)] = text[offset : offset + length]
        offset += length
    return pages


def _must_replace(text: str, old: str, new: str) -> str:
    if old not in text:
        raise RuntimeError(f"expected text not found in the extracted Markdown: {old[:40]!r}")
    return text.replace(old, new)


def defective_pages(pages: dict[int, str]) -> dict[int, str]:
    """Four injected defects: two table values swapped, a decimal point dropped, a sentence
    omitted and two paragraphs swapped."""
    out = dict(pages)
    p3 = out[3]
    p3 = _must_replace(p3, "|Baseline|12.40|1.15|", "|Baseline|15.82|1.15|")
    p3 = _must_replace(p3, "|Treatment|15.82|1.640|", "|Treatment|12.40|1.640|")
    out[3] = _must_replace(p3, "1.640", "1640")
    p1 = _must_replace(out[1], _TRAIL, "")
    p1 = _must_replace(p1, _INTRO, "@@INTRO@@")
    p1 = _must_replace(p1, _LEAD, _INTRO)
    out[1] = _must_replace(p1, "@@INTRO@@", _LEAD)
    return out


def build(work: Path) -> dict[str, int]:
    bench = work / "bench"
    (bench / "pdfs").mkdir(parents=True, exist_ok=True)
    shutil.copy(CORPUS_PDF, bench / "pdfs" / PDF_NAME)

    result = export_checks(load_record(EXPECTATION, kind="expectation"), pdf_name=PDF_NAME, allow_unreviewed_example=True)
    write_checks_jsonl(result.tests, bench / "ocr_corpus.jsonl")
    (work / "export_report.json").write_text(json.dumps(result.report(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    pages = extract_pages(work)
    control = dict(pages)
    control[1] = pages[1] + "\n" + _EQUATION + "\n"
    wrong = dict(pages)
    wrong[1] = pages[1] + "\n" + _WRONG_EQUATION + "\n"
    candidates = (("pymupdf4llm", pages), ("defective", defective_pages(pages)), ("mathcontrol", control), ("mathwrong", wrong))
    for name, candidate in candidates:
        folder = bench / name
        folder.mkdir(exist_ok=True)
        for number, text in candidate.items():
            (folder / _md_name(number)).write_text(text, encoding="utf-8", newline="\n")
    return result.counts()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m extraction.olmocr_feasibility_inputs", description=__doc__.split("\n")[0])
    parser.add_argument("work", type=Path, help="empty directory outside the repository")
    args = parser.parse_args(argv)
    counts = build(args.work)
    print(f"benchmark directory written to {args.work / 'bench'}; {counts['total']} checks, {counts['skipped']} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
