from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zotero-pdf-text-extract-markdown-marker")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--image-dir", type=Path, default=None, help="Save extracted figures as PNGs into this directory.")
    args = parser.parse_args(argv)

    # Imported here, not at module level: this script only ever runs as an isolated
    # subprocess (invoked by math_ocr.py), so the heavy marker-pdf/torch import chain
    # never has to be paid by the main process or by anything that merely imports
    # this package.
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    converter = PdfConverter(artifact_dict=create_model_dict())
    rendered = converter(str(args.source))
    text, _metadata, images = text_from_rendered(rendered)

    text = text.encode("utf-8", errors="replace").decode("utf-8")
    args.output.write_text(text, encoding="utf-8", newline="\n")

    if args.image_dir is not None and images:
        args.image_dir.mkdir(parents=True, exist_ok=True)
        for name, image in images.items():
            image.save(args.image_dir / name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
