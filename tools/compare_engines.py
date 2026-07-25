"""Run several extraction engines over the validation corpus and compare what they recovered.

The comparison logic and the selection policy are pure and unit-tested offline in
benchmarks/engines.py; this tool is the part that cannot be: it needs a served OCR model for the
crop path, marker-pdf (and a GPU to make it worth running) for the whole-document path, and it
measures wall time, which is a property of the machine rather than of the code.

    python tools/compare_engines.py                          # every engine available here
    python tools/compare_engines.py --engine crop-mvp        # just one
    python tools/compare_engines.py --config path/to/config.json
    python tools/compare_engines.py --json > comparison.json

An engine that is not installed is reported as unavailable and left OUT of the comparison -- never
silently scored zero, which would read as "this engine is bad" instead of "this engine was absent".
An engine that RAN and then failed or timed out is a different case: it answered, so it stays in the
comparison with an empty document and scores zero, with the reason reported beside it. Omitting it
would let a failing engine vanish from the ranking instead of placing last in it. If no engine was
even attempted, this exits non-zero rather than emitting an empty comparison.
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

from engines import (  # noqa: E402  (path set up above)
    ENGINE_CROP_MVP,
    ENGINE_MARKER,
    ENGINE_MINERU,
    EngineRun,
    Hardware,
    compare,
    recommend_engine,
)
from recognition import corpus_expected_tokens  # noqa: E402

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "ocr_corpus"
CORPUS_PDF = CORPUS_DIR / "corpus.pdf"

# marker-pdf runs minutes per page on a real paper; the corpus is short, so this is generous.
MARKER_TIMEOUT_SECONDS = 1800


class EngineUnavailable(RuntimeError):
    """The engine is absent or unusable on this machine, with an actionable reason.

    Strictly "was never asked", never "answered badly" -- the difference the comparison turns on.
    """


class EngineAttempted(RuntimeError):
    """The engine was present, started, and produced no usable document.

    Distinct from EngineUnavailable, and the distinction decides whether the engine stays in the
    comparison. An absent engine is omitted -- there is nothing to say about it. An engine that ran
    and failed has answered: it cannot do this job on this machine, which is a result, and omitting
    it would let a failing engine vanish from the ranking instead of scoring the zero it earned.
    Subclasses name how it failed, so the report can say which without inspecting strings.
    """


class EngineFailed(EngineAttempted):
    """The engine ran and errored -- a broken install, a CUDA failure, a crash mid-document."""


class EngineTimedOut(EngineAttempted):
    """The engine ran and overran its timeout, which is a cost result and cost is half the point."""


def parse_nvidia_smi(returncode: int, stdout: str):
    """Turn one ``nvidia-smi --query-gpu=memory.total`` result into a Hardware verdict.

    Split out from the subprocess call so the parsing is testable without a GPU. Anything other than
    a clean run with at least one numeric line reads as "no usable GPU": a driver that answers but
    errors, or answers with ``[N/A]``, cannot be relied on to hold a layout model.
    """
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
    from zotero_pdf_text._ollama_client import OllamaError, generate, probe
    from zotero_pdf_text.image_ocr import (
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
            # Mirror the two guards _run_ocr applies: a class with no prompt (skip) and a crop whose
            # PNG did not resolve both keep their placeholder rather than reaching the model.
            prompt = TASK_PROMPTS.get(plan.crop_class)
            if prompt is None or plan.ref.png_path is None:
                continue
            try:
                raw = generate(
                    settings.base_url, settings.model, prompt,
                    plan.ref.png_path, timeout=settings.per_image_timeout_seconds,
                )
            except OllamaError:
                # Production leaves the placeholder and carries on, so the benchmark must too: one
                # flaky crop out of forty aborting the run would void the whole comparison, and
                # crop-mvp runs first, so it would take the other engines down with it. The lost
                # crop then scores as recall the engine did not achieve, which is the honest result.
                continue
            replacements.append((
                plan.ref.span,
                render_replacement(plan.crop_class, sanitize_ocr_output(raw), plan.ref.markup),
            ))
        document = splice(body, replacements)
    return EngineRun(ENGINE_CROP_MVP, document, time.monotonic() - started, hardware_mode)


def run_marker(hardware_mode: str):
    """Re-extract the whole corpus with marker-pdf, in the same subprocess shape reconvert-math uses.

    The subprocess is not incidental: marker-pdf's torch/transformers import chain costs ~110s and
    is why the shipped code never imports it in-process. Benchmarking it any other way would measure
    a cost profile the pipeline does not have.
    """
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
            raise EngineTimedOut(f"marker-pdf did not finish within {MARKER_TIMEOUT_SECONDS}s") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            # Match the missing module by name. Testing for "marker" anywhere in stderr cannot work:
            # the subprocess IS zotero_pdf_text._extract_markdown_marker, so its own path appears in
            # every traceback it raises -- an installed marker-pdf whose CUDA chain fails to load
            # would be reported as not installed, advising the user to install what they already
            # have. Anything else falls through to the failure branch, which reports the real stderr.
            if "No module named 'marker" in stderr:
                raise EngineUnavailable(
                    "marker-pdf is not installed; install the reconvert extra to compare it"
                ) from exc
            # Present but broken is an ATTEMPT, not an absence: it stays in the comparison and
            # scores zero. Filing it under "unavailable" would drop a failing engine from the
            # ranking altogether, which flatters it.
            raise EngineFailed(f"marker-pdf failed: {stderr[-500:] or exc}") from exc
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


def _ocr_settings(config_path: str | None):
    """Resolve the crop path's OCR runtime the way the ``ocr-images`` command does.

    Benchmarking the crop engine against a default host and model when the user runs a different
    one would measure something they never use. An explicit ``--config`` that does not exist is an
    error rather than a fallback, for the same reason: the scores would be misattributed. Mirrors
    tools/score_recognition.py.
    """
    from zotero_pdf_text.config import ImageOcrSettings, load_config, resolve_config_path

    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise SystemExit(f"--config not found: {config_path}")
    else:
        path = resolve_config_path()
    return load_config(path).image_ocr if path.exists() else ImageOcrSettings()


def main(argv: list[str] | None = None) -> int:
    runners = {ENGINE_CROP_MVP: run_crop_mvp, ENGINE_MARKER: run_marker, ENGINE_MINERU: run_mineru}

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", action="append", choices=sorted(runners),
                        help="run only this engine (repeatable; default: all)")
    parser.add_argument("--config", help="path to the project config (default: standard resolution)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    hardware = detect_hardware()
    hardware_mode = "gpu" if hardware.gpu_available else "cpu"
    settings = _ocr_settings(args.config)

    runs: list = []
    unavailable: dict[str, str] = {}
    failed: dict[str, str] = {}
    for name in args.engine or sorted(runners):
        runner = runners[name]
        try:
            runs.append(
                runner(settings, hardware_mode) if name == ENGINE_CROP_MVP else runner(hardware_mode)
            )
        except EngineUnavailable as exc:
            unavailable[name] = str(exc)
        except EngineAttempted as exc:
            # It ran and delivered nothing, so it stays in the comparison with an empty document and
            # scores zero -- the outcome EngineRun documents for a crash. Dropping it here would let
            # a failing engine disappear from the ranking rather than place last in it.
            failed[name] = f"{type(exc).__name__}: {exc}"
            runs.append(EngineRun(name, "", 0.0, hardware_mode))

    comparison = compare(runs, corpus_expected_tokens())
    recommendation: str | None = None
    recommendation_blocked = ""
    try:
        recommendation = recommend_engine(comparison, hardware)
    except NotImplementedError as exc:
        recommendation_blocked = str(exc)
    except ValueError as exc:  # documented: an empty comparison has nothing to recommend from
        recommendation_blocked = str(exc)

    if args.json:
        print(json.dumps({
            "hardware": {"gpu_available": hardware.gpu_available, "gpu_vram_gb": hardware.gpu_vram_gb},
            "unavailable": unavailable,
            # Ran and produced nothing. These DO appear in "engines" below, scoring zero.
            "failed": failed,
            # False when any engine failed to locate an element, which inflates its neighbour's
            # recall by an unknown amount -- the scores below cannot be ranked against each other.
            "comparable": comparison.comparable,
            # Kept apart so a consumer never has to tell an engine name from an error message by
            # sniffing the string.
            "recommendation": recommendation,
            "recommendation_blocked": recommendation_blocked,
            "engines": [
                {"engine": s.engine, "macro_recall": s.macro_recall, "micro_recall": s.micro_recall,
                 "wall_seconds": s.wall_seconds, "seconds_per_element": s.seconds_per_element,
                 "hardware": s.hardware, "unanchored": list(s.unanchored),
                 "ambiguous": list(s.ambiguous)}
                for s in comparison.by_recall()
            ],
        }, indent=2))
    else:
        vram = f", {hardware.gpu_vram_gb:.1f} GB VRAM" if hardware.gpu_available else ""
        print(f"hardware: {hardware_mode}{vram}")
        for name, reason in sorted(unavailable.items()):
            print(f"skipped {name}: {reason}")
        for name, reason in sorted(failed.items()):
            print(f"failed {name} (scored zero, not omitted): {reason}")
        print(comparison.report())
        print(f"recommended: {recommendation or f'(none) {recommendation_blocked}'}")

    if not runs:
        # Nothing was measured. Exiting 0 here would hand a caller redirecting --json to a file an
        # artifact that looks valid and says nothing.
        print("no engine produced a document; nothing was compared", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
