"""Run several extraction engines over the validation corpus and compare what they recovered.

The comparison logic and the selection policy are pure and unit-tested offline in
benchmarks/engines.py; this tool is the part that cannot be: it needs a served OCR model for the
crop path, marker-pdf (and a GPU to make it worth running) for the whole-document path, and it
measures wall time, which is a property of the machine rather than of the code.

    python tools/compare_engines.py                          # every engine available here
    python tools/compare_engines.py --engine crop-mvp        # just one
    python tools/compare_engines.py --json > comparison.json

An engine that is not installed is reported as unavailable and left OUT of the comparison -- never
silently scored zero, which would read as "this engine is bad" instead of "this engine was absent".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "ocr_corpus"
CORPUS_PDF = CORPUS_DIR / "corpus.pdf"

# marker-pdf runs minutes per page on a real paper; the corpus is short, so this is generous.
MARKER_TIMEOUT_SECONDS = 1800


class EngineUnavailable(RuntimeError):
    """The engine is not usable on this machine, with an actionable reason."""


def parse_nvidia_smi(returncode: int, stdout: str):
    """Turn one ``nvidia-smi --query-gpu=memory.total`` result into a Hardware verdict.

    Split out from the subprocess call so the parsing is testable without a GPU. Anything other than
    a clean run with at least one numeric line reads as "no usable GPU": a driver that answers but
    errors, or answers with ``[N/A]``, cannot be relied on to hold a layout model.
    """
    from engines import Hardware

    sizes = [line.strip() for line in stdout.splitlines() if line.strip().isdigit()]
    if returncode != 0 or not sizes:
        return Hardware(gpu_available=False)
    # Several GPUs: the largest is what one extraction run can use.
    return Hardware(gpu_available=True, gpu_vram_gb=max(int(size) for size in sizes) / 1024)


def detect_hardware():
    """Ask nvidia-smi what GPU is present -- no new dependency, and no import of torch.

    A missing nvidia-smi means no usable GPU as far as this harness is concerned. That is the right
    reading for the recommendation it feeds: both whole-document engines target CUDA, so a machine
    without the CUDA tooling behaves like a CPU machine whatever else it has.
    """
    from engines import Hardware

    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return Hardware(gpu_available=False)
    return parse_nvidia_smi(completed.returncode, completed.stdout)


def _convert_crops(images_dir: Path) -> str:
    """The pymupdf4llm conversion the crop path starts from (same settings as the corpus tests)."""
    import pymupdf4llm

    return pymupdf4llm.to_markdown(
        str(CORPUS_PDF), write_images=True, image_path=str(images_dir),
        image_format="png", image_size_limit=0.05, dpi=150,
    )


def run_crop_mvp(settings, hardware_mode: str):
    """Convert, classify, OCR each content crop, and splice: the ocr-images path, uncommitted.

    This reuses plan_crops/render_replacement/splice rather than reimplementing them. The point of
    the comparison is to measure the engine that actually ships, so any divergence between what is
    benchmarked and what runs would quietly make the numbers meaningless.
    """
    from engines import ENGINE_CROP_MVP, EngineRun

    from zotero_pdf_text._ollama_client import generate, probe
    from zotero_pdf_text.image_ocr import (
        CLASS_SKIP,
        TASK_PROMPTS,
        plan_crops,
        render_replacement,
        sanitize_ocr_output,
        splice,
    )

    status = probe(settings.base_url, settings.model)
    if not status.ok:
        raise EngineUnavailable(status.detail)

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        images_dir = Path(tmp) / "images"
        images_dir.mkdir()
        body = _convert_crops(images_dir)
        replacements = []
        for plan in plan_crops(body, images_dir, has_math=True):
            if plan.crop_class == CLASS_SKIP:
                continue
            ocr_text = sanitize_ocr_output(
                generate(
                    settings.base_url, settings.model, TASK_PROMPTS[plan.crop_class],
                    plan.ref.png_path, timeout=settings.per_image_timeout_seconds,
                )
            )
            replacements.append(
                (plan.ref.span, render_replacement(plan.crop_class, ocr_text, plan.ref.markup))
            )
        document = splice(body, replacements)
    return EngineRun(ENGINE_CROP_MVP, document, time.monotonic() - started, hardware_mode)


def run_marker(hardware_mode: str):
    """Re-extract the whole corpus with marker-pdf, in the same subprocess shape reconvert-math uses.

    The subprocess is not incidental: marker-pdf's torch/transformers import chain costs ~110s and
    is why the shipped code never imports it in-process. Benchmarking it any other way would measure
    a cost profile the pipeline does not have.
    """
    from engines import ENGINE_MARKER, EngineRun

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "output.md"
        images_dir = Path(tmp) / "images"
        images_dir.mkdir()
        try:
            subprocess.run(
                [
                    sys.executable, "-m", "zotero_pdf_text._extract_markdown_marker",
                    str(CORPUS_PDF), str(output), "--image-dir", str(images_dir),
                ],
                check=True, capture_output=True, text=True, timeout=MARKER_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:  # no interpreter/module at all
            raise EngineUnavailable(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise EngineUnavailable(f"marker-pdf timed out after {MARKER_TIMEOUT_SECONDS}s") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            # An absent marker-pdf surfaces as an ImportError from the extractor module. That is an
            # availability problem, not a quality one, so it must not be scored as a bad engine.
            if "marker" in stderr and ("ModuleNotFoundError" in stderr or "ImportError" in stderr):
                raise EngineUnavailable(
                    "marker-pdf is not installed; install the reconvert extra to compare it"
                ) from exc
            raise EngineUnavailable(f"marker-pdf failed: {stderr[-500:] or exc}") from exc
        document = output.read_text(encoding="utf-8")
    return EngineRun(ENGINE_MARKER, document, time.monotonic() - started, hardware_mode)


def run_mineru(hardware_mode: str):
    """MinerU is a candidate, not yet a runner.

    Reporting it as unavailable rather than omitting it keeps the third engine visible in the
    output: the comparison is the artifact that decides whether wiring it up is worth the
    dependency, so it should say out loud that it has not been asked yet.
    """
    raise EngineUnavailable(
        "no MinerU runner yet; add one here that returns its output Markdown as an EngineRun"
    )


RUNNERS = {"crop-mvp": run_crop_mvp, "marker": run_marker, "mineru": run_mineru}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", action="append", choices=sorted(RUNNERS),
                        help="run only this engine (repeatable; default: all)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    from engines import compare, recommend_engine
    from recognition import corpus_expected_tokens

    from zotero_pdf_text.config import ImageOcrSettings

    hardware = detect_hardware()
    hardware_mode = "gpu" if hardware.gpu_available else "cpu"
    settings = ImageOcrSettings()

    runs, unavailable = [], {}
    for name in args.engine or sorted(RUNNERS):
        runner = RUNNERS[name]
        try:
            runs.append(runner(settings, hardware_mode) if name == "crop-mvp" else runner(hardware_mode))
        except EngineUnavailable as exc:
            unavailable[name] = str(exc)

    comparison = compare(runs, corpus_expected_tokens())
    try:
        recommendation = recommend_engine(comparison, hardware)
    except (NotImplementedError, ValueError) as exc:
        recommendation = f"unavailable: {exc}"

    if args.json:
        print(json.dumps({
            "hardware": {"gpu_available": hardware.gpu_available, "gpu_vram_gb": hardware.gpu_vram_gb},
            "unavailable": unavailable,
            "recommendation": recommendation,
            "engines": [
                {"engine": s.engine, "macro_recall": s.macro_recall, "micro_recall": s.micro_recall,
                 "wall_seconds": s.wall_seconds, "seconds_per_element": s.seconds_per_element,
                 "hardware": s.hardware, "unanchored": list(s.unanchored)}
                for s in comparison.by_recall()
            ],
        }, indent=2))
    else:
        vram = f", {hardware.gpu_vram_gb:.1f} GB VRAM" if hardware.gpu_available else ""
        print(f"hardware: {hardware_mode}{vram}")
        for name, reason in sorted(unavailable.items()):
            print(f"skipped {name}: {reason}")
        print(comparison.report())
        print(f"recommended: {recommendation}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
