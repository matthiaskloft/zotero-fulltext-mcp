"""Rebuild the synthetic OCR validation corpus and report what conversion actually produced.

Two jobs, deliberately kept in one place:

1. Compile ``tests/fixtures/ocr_corpus/corpus.tex`` to a PDF. The PDF is committed so that CI --
   which has no LaTeX toolchain -- can run the classification tests against real extracted crops.
2. Run the real conversion path over that PDF and print every crop it produced, tied back to the
   CORPUSMARK token in its neighbouring text, with the geometry a classifier would see.

Step 2 matters because which LaTeX constructs become image crops is an empirical property of the
extractor, not something to be assumed: some display math is drawn as vector graphics and cropped,
some survives as text. Ground truth is therefore recorded from an observed run and reviewed, never
written blind against elements that may produce no crop at all.

Usage:
    python tools/build_ocr_corpus.py            # rebuild the PDF, then report
    python tools/build_ocr_corpus.py --report   # report only, using the committed PDF
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "ocr_corpus"
CORPUS_TEX = CORPUS_DIR / "corpus.tex"
CORPUS_PDF = CORPUS_DIR / "corpus.pdf"

MARKER_RE = re.compile(r"CORPUSMARK-[A-Z]+-\d+")

# Raster tables the corpus \includegraphics's. They must be raster images, not LaTeX tabulars or
# TikZ node matrices: pymupdf4llm extracts any text layer as text, so only a table baked into
# pixels survives as an image CROP -- the real failure mode where a publisher's table defeats the
# text extractor. Generated with PyMuPDF (already a dependency), committed alongside the PDF.
TABLE_ASSETS = {
    "table_contingency.png": [
        ["", "Y = 0", "Y = 1"],
        ["X = 0", "0.16", "0.34"],
        ["X = 1", "0.24", "0.26"],
    ],
    "table_computational.png": [
        ["x", "p(x)", "F(x)"],
        ["0", "1/6", "1/6"],
        ["1", "2/6", "3/6"],
        ["2", "3/6", "6/6"],
    ],
}


def build_table_assets() -> None:
    """Render each table spec to a committed PNG whose content lives in pixels, not a text layer."""
    import fitz

    for name, rows in TABLE_ASSETS.items():
        ncol = max(len(r) for r in rows)
        nrow = len(rows)
        col_w, row_h = 96.0, 34.0
        width, height = ncol * col_w, nrow * row_h
        doc = fitz.open()
        page = doc.new_page(width=width, height=height)
        for i in range(nrow + 1):
            page.draw_line((0, i * row_h), (width, i * row_h), color=(0, 0, 0), width=0.7)
        for j in range(ncol + 1):
            page.draw_line((j * col_w, 0), (j * col_w, height), color=(0, 0, 0), width=0.7)
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                if value:
                    cell = fitz.Rect(c * col_w, r * row_h + 9, (c + 1) * col_w, (r + 1) * row_h)
                    page.insert_textbox(cell, value, fontsize=13, align=1)
        page.get_pixmap(dpi=150).save(str(CORPUS_DIR / name))
    print(f"built {len(TABLE_ASSETS)} table asset(s) in {CORPUS_DIR.relative_to(REPO_ROOT)}")


def build_pdf() -> None:
    """Compile the corpus with pdflatex in a scratch directory, then publish the PDF."""
    latex = shutil.which("pdflatex")
    if latex is None:
        raise SystemExit(
            "pdflatex not found. Install a LaTeX distribution (MiKTeX, TeX Live, MacTeX) to "
            "rebuild the corpus, or pass --report to use the committed PDF."
        )
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copy(CORPUS_TEX, work / "corpus.tex")
        for asset in TABLE_ASSETS:  # \includegraphics resolves these next to corpus.tex
            shutil.copy(CORPUS_DIR / asset, work / asset)
        # Twice: the second pass settles page numbers and any reference-dependent layout, so the
        # committed PDF is byte-stable across rebuilds rather than shifting on the next run.
        for _ in range(2):
            result = subprocess.run(
                [latex, "-interaction=nonstopmode", "-halt-on-error", "corpus.tex"],
                cwd=work,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                sys.stderr.write(result.stdout[-4000:])
                raise SystemExit("pdflatex failed; see the log above.")
        shutil.copy(work / "corpus.pdf", CORPUS_PDF)
    print(f"built {CORPUS_PDF.relative_to(REPO_ROOT)} ({CORPUS_PDF.stat().st_size} bytes)")


def report() -> None:
    """Convert the corpus PDF and print each observed crop against its CORPUSMARK token."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    import pymupdf4llm

    from zotero_pdf_text.image_ocr import find_crop_refs

    with tempfile.TemporaryDirectory() as tmp:
        images_dir = Path(tmp) / "images"
        images_dir.mkdir()
        body = pymupdf4llm.to_markdown(
            str(CORPUS_PDF),
            write_images=True,
            image_path=str(images_dir),
            image_format="png",
            image_size_limit=0.05,
            dpi=150,
        )
        refs = find_crop_refs(body, images_dir)

        print(f"\n{len(refs)} crop(s) produced from {CORPUS_PDF.name}\n")
        print(f"{'marker':<22} {'pixels':>11} {'aspect':>7}  context")
        print("-" * 92)
        seen: set[str] = set()
        for ref in refs:
            marker = _nearest_marker(body, ref.span[0])
            seen.add(marker)
            context = (ref.text_after or ref.text_before)[:36]
            pixels = f"{ref.width}x{ref.height}" if ref.exists else "-"
            aspect = f"{ref.aspect:.2f}" if ref.exists else "-"
            print(
                f"{marker:<22} {pixels:>11} {aspect:>7}  "
                f"{context.encode('ascii', 'replace').decode()}"
            )

        declared = set(MARKER_RE.findall(CORPUS_TEX.read_text(encoding="utf-8")))
        missing = sorted(declared - seen)
        if missing:
            print(
                "\nDeclared in corpus.tex but produced no crop "
                "(extracted as text, or not extracted at all):"
            )
            for marker in missing:
                print(f"  {marker}")


def _nearest_marker(body: str, position: int) -> str:
    """The last CORPUSMARK token appearing before a crop, i.e. the element that produced it."""
    matches = [match for match in MARKER_RE.finditer(body) if match.start() < position]
    return matches[-1].group(0) if matches else "(none)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="store_true",
        help="Skip compilation and report against the committed PDF.",
    )
    args = parser.parse_args(argv)
    if not args.report:
        build_table_assets()
        build_pdf()
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
