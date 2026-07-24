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
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "ocr_corpus"


def _ocr_settings(model_override, config_path):
    """Resolve the OCR runtime the same way the ``ocr-images`` command does.

    Honour the user's configured host/port/model/timeout when a project config exists (via the
    standard resolution contract), and fall back to built-in defaults only when there is none -- a
    benchmark run without a full project config is legitimate. An *explicit* ``--config`` that does
    not exist is an error, not a fallback: silently benchmarking against default host/model when
    the user pointed at a specific config would misattribute the resulting scores. ``--model``
    overrides just that one field, leaving the configured connection settings intact.
    """
    from zotero_pdf_text.config import ImageOcrSettings, load_config, resolve_config_path

    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise SystemExit(f"--config not found: {config_path}")
    else:
        path = resolve_config_path()
    settings = load_config(path).image_ocr if path.exists() else ImageOcrSettings()
    return replace(settings, model=model_override) if model_override else settings


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
    parser.add_argument("--config", help="path to the project config (default: standard resolution)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results instead of a report")
    args = parser.parse_args(argv)

    import pymupdf4llm

    from zotero_pdf_text._ollama_client import generate, probe
    from zotero_pdf_text.image_ocr import TASK_PROMPTS, find_crop_refs

    from recognition import corpus_expected_tokens, score

    settings = _ocr_settings(args.model, args.config)
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

        outputs = {}
        for marker in sorted(expected):
            crops = by_marker.get(marker)
            if not crops:
                continue  # no crop produced; score() weighs it as a zero-recall miss, not a gap
            outputs[marker] = generate(
                settings.base_url, settings.model,
                TASK_PROMPTS[classes[marker]["expected_class"]],
                crops[0].png_path, timeout=settings.per_image_timeout_seconds,
            )

    report = score(outputs, expected)
    if args.json:
        print(json.dumps({
            "model": settings.model,
            "micro_recall": report.micro_recall,
            "macro_recall": report.macro_recall,
            "elements": [
                {"id": r.element_id, "recall": r.recall, "found": list(r.found),
                 "missing": list(r.missing), "output": outputs.get(r.element_id, "")}
                for r in report.results
            ],
        }, indent=2))
    else:
        print(f"model: {settings.model}")
        print(report.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
