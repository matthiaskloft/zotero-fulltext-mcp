"""Pressure-test OCR recognition quality against the synthetic corpus, with a live model.

The CI benchmark scores classification (which prompt) offline; this harness scores *recognition*
(did the notation survive) and therefore needs a running model -- so it lives in tools/, not the
test suite. Point it at any Ollama-served OCR model to see how much of the corpus's known notation
that model actually recovers, and to compare models on the same crops:

    python tools/score_recognition.py                       # the configured default model
    python tools/score_recognition.py --model glm-ocr:q4_K_M
    python tools/score_recognition.py --model glm-ocr:q8_0 --json > q8.json

Recognition metric and token-matching policy live in benchmarks/recognition.py (unit-tested in
tests/test_recognition_scoring.py); this tool only feeds real model output into it.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "ocr_corpus"


def _crops_by_marker(body: str, refs) -> dict[str, list]:
    """Map each CORPUSMARK token to the crops that follow it (the corpus test's convention)."""
    import re

    marker_re = re.compile(r"CORPUSMARK-[A-Z]+-\d+")
    by_marker: dict[str, list] = {}
    for ref in refs:
        preceding = [m for m in marker_re.finditer(body) if m.start() < ref.span[0]]
        if preceding:
            by_marker.setdefault(preceding[-1].group(0), []).append(ref)
    return by_marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="override the configured OCR model (e.g. glm-ocr:q4_K_M)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results instead of a report")
    args = parser.parse_args(argv)

    import pymupdf4llm

    from zotero_pdf_text._ollama_client import generate, probe
    from zotero_pdf_text.config import ImageOcrSettings
    from zotero_pdf_text.image_ocr import TASK_PROMPTS, find_crop_refs

    from recognition import RecognitionReport, corpus_expected_tokens, score_element

    settings = ImageOcrSettings(model=args.model) if args.model else ImageOcrSettings()
    status = probe(settings.base_url, settings.model)
    if not status.ok:
        raise SystemExit(f"model not reachable: {status.detail}")

    expected = corpus_expected_tokens()
    classes = json.loads((CORPUS_DIR / "expected.json").read_text(encoding="utf-8"))["elements"]

    with tempfile.TemporaryDirectory() as tmp:
        images = Path(tmp) / "images"
        images.mkdir()
        body = pymupdf4llm.to_markdown(
            str(CORPUS_DIR / "corpus.pdf"), write_images=True, image_path=str(images),
            image_format="png", image_size_limit=0.05, dpi=150,
        )
        by_marker = _crops_by_marker(body, find_crop_refs(body, images))

        results = []
        for marker, tokens in sorted(expected.items()):
            crops = by_marker.get(marker)
            if not crops:
                continue  # element produced no crop in this conversion; nothing to recognise
            text = generate(
                settings.base_url, settings.model,
                TASK_PROMPTS[classes[marker]["expected_class"]],
                crops[0].png_path, timeout=settings.per_image_timeout_seconds,
            )
            results.append((score_element(marker, text, tokens), text))

    report = RecognitionReport(tuple(r for r, _ in results))
    if args.json:
        print(json.dumps({
            "model": settings.model,
            "micro_recall": report.micro_recall,
            "macro_recall": report.macro_recall,
            "elements": [
                {"id": r.element_id, "recall": r.recall, "found": list(r.found),
                 "missing": list(r.missing), "output": text}
                for r, text in results
            ],
        }, indent=2))
    else:
        print(f"model: {settings.model}")
        print(report.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
