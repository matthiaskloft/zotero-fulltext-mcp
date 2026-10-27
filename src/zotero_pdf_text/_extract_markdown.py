from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymupdf as fitz
import pymupdf4llm

from .math_detection import detect_math

PRIMARY_TOOL = "pymupdf4llm.to_markdown"
FALLBACK_TOOL = "pymupdf.get_text"
# Written next to the raw output: [[physical_page, char_count], ...] in output order, so the
# converter can split the Markdown back into its pages.
PAGES_SIDECAR_SUFFIX = ".pages.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zotero-pdf-text-extract-markdown")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tool", choices=[PRIMARY_TOOL, FALLBACK_TOOL], default=PRIMARY_TOOL)
    parser.add_argument("--image-dir", type=Path, default=None, help="Save extracted figures as PNGs into this directory.")
    args = parser.parse_args(argv)

    if args.tool == FALLBACK_TOOL:
        # No image support here on purpose: the fallback only runs after the primary
        # extractor already failed/timed out, so it stays a minimal, maximally robust
        # plain-text-only path rather than growing the same figure-extraction surface.
        pages = _extract_plain_text_pages(args.source)
    else:
        image_options: dict[str, object] = {}
        if args.image_dir is not None:
            args.image_dir.mkdir(parents=True, exist_ok=True)
            image_options = dict(
                write_images=True,
                image_path=str(args.image_dir),
                image_format="png",
                image_size_limit=0.05,
                dpi=150,
            )
        # page_chunks only splits the output: without it pymupdf4llm returns exactly these
        # per-page strings concatenated with no separator, so the Markdown is unchanged and the
        # page boundaries come for free. Header/footer removal keeps pymupdf4llm's defaults on
        # purpose; docs/conversion-text-decisions.md records the sample that decided it.
        chunks = pymupdf4llm.to_markdown(args.source, page_chunks=True, **image_options)
        pages = [(int(chunk["metadata"]["page_number"]), chunk["text"]) for chunk in chunks]
    # Replacement keeps every page text's length, so the recorded lengths still split the output.
    pages = [(page, text.encode("utf-8", errors="replace").decode("utf-8")) for page, text in pages]
    args.output.write_text("".join(text for _page, text in pages), encoding="utf-8", newline="\n")
    args.output.with_suffix(PAGES_SIDECAR_SUFFIX).write_text(
        json.dumps([[page, len(text)] for page, text in pages]), encoding="utf-8"
    )

    math_result = _safe_detect_math(args.source)
    if math_result is not None:
        sidecar_path = args.output.with_suffix(".math.json")
        sidecar_path.write_text(json.dumps(math_result.to_dict()), encoding="utf-8")
    return 0


def _safe_detect_math(source: Path):
    # Math detection is a best-effort signal, independent of which extraction tool ran --
    # a failure here (e.g. a corrupt PDF fitz can't reopen) must not fail the whole
    # conversion, so any error just means no sidecar gets written.
    try:
        return detect_math(source)
    except Exception:
        return None


def _extract_plain_text_pages(source: Path) -> list[tuple[int, str]]:
    """Return each page with text as "## Page N" plus that text; a separator ends the page before."""
    sections: list[tuple[int, str]] = []
    with fitz.open(source) as document:
        for page_index, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if text:
                sections.append((page_index, f"## Page {page_index}\n\n{text}"))
    return [(page, text + ("\n\n" if i + 1 < len(sections) else "")) for i, (page, text) in enumerate(sections)]


if __name__ == "__main__":
    sys.exit(main())
