from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import fitz
import pymupdf4llm

from .math_detection import detect_math

PRIMARY_TOOL = "pymupdf4llm.to_markdown"
FALLBACK_TOOL = "pymupdf.get_text"


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
        markdown = _extract_plain_text(args.source)
    elif args.image_dir is not None:
        args.image_dir.mkdir(parents=True, exist_ok=True)
        markdown = pymupdf4llm.to_markdown(
            args.source,
            write_images=True,
            image_path=str(args.image_dir),
            image_format="png",
            image_size_limit=0.05,
            dpi=150,
        )
    else:
        markdown = pymupdf4llm.to_markdown(args.source)
    markdown = markdown.encode("utf-8", errors="replace").decode("utf-8")
    args.output.write_text(markdown, encoding="utf-8", newline="\n")

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


def _extract_plain_text(source: Path) -> str:
    sections: list[str] = []
    with fitz.open(source) as document:
        for page_index, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if text:
                sections.append(f"## Page {page_index}\n\n{text}")
    return "\n\n".join(sections)


if __name__ == "__main__":
    sys.exit(main())
