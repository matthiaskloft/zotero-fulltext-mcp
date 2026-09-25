from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import ProjectConfig
from .indexer import load_indexed_keys
from ._atomic import atomic_write_text
from .timeout_candidates import (
    TimeoutCandidate,
    append_master_candidates,
    suggested_next_timeout,
    write_run_candidates,
)

try:
    import pymupdf4llm
except Exception:  # pragma: no cover - exercised only if dependency is missing
    pymupdf4llm = None

try:
    import pymupdf as fitz
except Exception:  # pragma: no cover - exercised only if dependency is missing
    fitz = None

PRIMARY_EXTRACTION_TOOL = "pymupdf4llm.to_markdown"
FALLBACK_EXTRACTION_TOOL = "pymupdf.get_text"
EXTRACTION_TOOL = PRIMARY_EXTRACTION_TOOL
SECONDS_PER_PAGE_TIMEOUT = 4

# pymupdf4llm's layout parser walks every vector path to reconstruct structure, so
# pages dense with vector drawings (statistical plots, diagrams) cost far more than
# plain text pages. A bounded page sample keeps this pre-scan cheap (same cost class
# as math_detection's font/text sampling) while still catching density outliers.
DRAWING_SAMPLE_PAGES = 40
DRAWING_DENSITY_DIVISOR = 10.0
MAX_DRAWING_TIMEOUT_MULTIPLIER = 5.0

SKIP_LIST_FILENAME = "timeout_skip_list.json"
NATIVE_CRASH_EXIT_STATUS = 0xC000070A


class PrimaryExtractorTimeoutError(RuntimeError):
    """Both the primary extractor and its fallback failed, and the primary failure was a timeout.

    Distinct from a plain RuntimeError (primary crashed rather than timed out) so _convert_row can
    tell a genuine timeout-driven failure apart from a crash when deciding whether to report a
    timeout candidate.
    """


@dataclass
class ConversionResult:
    status: str
    extraction_tool: str
    zotero_parent_key: str
    zotero_attachment_key: str
    item_type: str
    title: str
    creators: str
    year: str
    doi: str
    citation_key: str
    source_path: str
    output_path: str
    page_count: str
    classification: str
    identity_status: str
    identity_rule: str
    has_math: str = "false"
    error: str = ""
    # SHA-256 of the source PDF this text was actually extracted from, hashed at conversion time.
    # Empty means "not known", which is not the same as "unchanged": a `skipped_existing` row
    # reused Markdown converted from whatever the PDF was back then, and hashing the file now
    # would record confident provenance for text that may predate it. An audit reports an empty
    # hash as unverifiable rather than treating the source as current.
    source_sha256: str = ""


def convert_sample(
    config: ProjectConfig,
    mapping_report: Path,
    *,
    limit: int = 10,
    output_dir: Path | None = None,
    workers: int | None = None,
    timeout_seconds: int = 600,
    force: bool = False,
) -> Path:
    if pymupdf4llm is None:
        raise RuntimeError("pymupdf4llm is not installed")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not mapping_report.exists():
        raise FileNotFoundError(mapping_report)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir or config.output_root / "conversion-runs" / "samples" / timestamp
    return _convert_verified_rows(
        mapping_report,
        run_dir,
        limit=limit,
        exist_ok=force,
        workers=workers,
        timeout_seconds=timeout_seconds,
        force=force,
        output_root=config.output_root,
    )


def convert_verified(
    config: ProjectConfig,
    mapping_report: Path,
    *,
    limit: int | None = None,
    output_dir: Path | None = None,
    resume: bool = False,
    workers: int | None = None,
    timeout_seconds: int = 600,
    force: bool = False,
) -> Path:
    if pymupdf4llm is None:
        raise RuntimeError("pymupdf4llm is not installed")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    if not mapping_report.exists():
        raise FileNotFoundError(mapping_report)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir or config.output_root / "conversion-runs" / "verified" / timestamp
    return _convert_verified_rows(
        mapping_report,
        run_dir,
        limit=limit,
        exist_ok=resume or force,
        workers=workers,
        timeout_seconds=timeout_seconds,
        force=force,
        output_root=config.output_root,
    )


def convert_unverified(
    config: ProjectConfig,
    mapping_report: Path,
    *,
    limit: int | None = None,
    output_dir: Path | None = None,
    resume: bool = False,
    workers: int | None = None,
    timeout_seconds: int = 600,
    force: bool = False,
    include_possible_mismatch: bool = False,
    index_jsonl: Path | None = None,
) -> Path:
    if pymupdf4llm is None:
        raise RuntimeError("pymupdf4llm is not installed")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    if not mapping_report.exists():
        raise FileNotFoundError(mapping_report)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir or config.output_root / "conversion-runs" / "unverified-review" / timestamp
    classifications = {"mapped_unverified"}
    if include_possible_mismatch:
        classifications.add("possible_mismatch")
    resolved_index_jsonl = index_jsonl or (config.output_root / "index" / "zotero_text_index.jsonl")
    already_indexed_keys = _load_indexed_keys_fail_open(resolved_index_jsonl)
    return _convert_mapping_rows(
        mapping_report,
        run_dir,
        limit=limit,
        exist_ok=resume or force,
        workers=workers,
        timeout_seconds=timeout_seconds,
        force=force,
        classifications=classifications,
        output_root=config.output_root,
        skip_attachment_keys=already_indexed_keys,
    )


def _convert_verified_rows(
    mapping_report: Path,
    run_dir: Path,
    *,
    limit: int | None,
    exist_ok: bool,
    workers: int | None,
    timeout_seconds: int,
    force: bool,
    output_root: Path,
) -> Path:
    return _convert_mapping_rows(
        mapping_report,
        run_dir,
        limit=limit,
        exist_ok=exist_ok,
        workers=workers,
        timeout_seconds=timeout_seconds,
        force=force,
        classifications={"mapped_verified"},
        output_root=output_root,
    )


def _convert_mapping_rows(
    mapping_report: Path,
    run_dir: Path,
    *,
    limit: int | None,
    exist_ok: bool,
    workers: int | None,
    timeout_seconds: int,
    force: bool,
    classifications: set[str],
    output_root: Path,
    skip_attachment_keys: frozenset[str] = frozenset(),
) -> Path:
    if workers is None:
        workers = default_worker_count()
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be at least 1")
    markdown_dir = run_dir / "markdown"
    markdown_dir.mkdir(parents=True, exist_ok=exist_ok)
    images_root = run_dir / "images"
    skip_keys = _load_persisted_skip_keys(output_root)

    rows = _selected_rows(mapping_report, limit, classifications, skip_attachment_keys=skip_attachment_keys)
    indexed_rows = list(enumerate(rows, start=1))
    if workers == 1:
        row_outcomes = [
            _convert_row(row, markdown_dir, images_root, index, timeout_seconds, force=force, skip_keys=skip_keys)
            for index, row in indexed_rows
        ]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            row_outcomes = list(
                executor.map(
                    lambda item: _convert_row(
                        item[1], markdown_dir, images_root, item[0], timeout_seconds, force=force, skip_keys=skip_keys
                    ),
                    indexed_rows,
                )
    )
    retry_workers = min(2, workers - 1)
    retry_indexes = [
        index for index, (result, _) in enumerate(row_outcomes)
        if retry_workers > 0 and _has_native_crash(result.error)
    ]
    retry_counts = Counter()
    if retry_indexes:
        def retry(index: int) -> tuple[ConversionResult, TimeoutCandidate | None]:
            row_number, row = indexed_rows[index]
            return _convert_row(
                row, markdown_dir, images_root, row_number, timeout_seconds,
                force=True, skip_keys=skip_keys, retry=True,
            )

        with ThreadPoolExecutor(max_workers=retry_workers) as executor:
            retried = list(executor.map(retry, retry_indexes))
        for index, retry_outcome in zip(retry_indexes, retried):
            initial_result, initial_candidate = row_outcomes[index]
            retry_result, retry_candidate = retry_outcome
            if retry_result.status == "converted" and retry_result.extraction_tool == PRIMARY_EXTRACTION_TOOL:
                retry_counts["recovered"] += 1
                row_outcomes[index] = retry_result, retry_candidate
            elif retry_result.status == "converted":
                retry_counts["fallback_only"] += 1
                if initial_result.status == "error":
                    row_outcomes[index] = retry_result, retry_candidate
            else:
                retry_counts["fallback_only" if initial_result.status == "converted" else "still_failed"] += 1
            if row_outcomes[index][0] is initial_result:
                initial_result.error += "; native crash retry: " + _retry_diagnostic(retry_result)
    results = [result for result, _candidate in row_outcomes]
    candidates = [candidate for _result, candidate in row_outcomes if candidate is not None]
    _write_manifest(run_dir / "manifest.csv", results)
    _write_jsonl(run_dir / "manifest.jsonl", results)
    _write_summary(
        run_dir / "summary.md", mapping_report, results, workers, timeout_seconds,
        force, classifications, len(retry_indexes), retry_workers, retry_counts,
    )
    write_run_candidates(run_dir, candidates)
    append_master_candidates(output_root / "index" / "timeout_candidates.jsonl", candidates)
    return run_dir


def default_worker_count() -> int:
    workers = max(1, (os.cpu_count() or 1) - 4)
    # PDF extraction runs in separate native-code processes. On Windows, high
    # concurrency has produced STATUS_THREADPOOL_HANDLE_EXCEPTION child crashes;
    # keep the automatic setting conservative while allowing explicit overrides.
    return min(workers, 2) if sys.platform == "win32" else workers


def _has_native_crash(error: str) -> bool:
    return f"NativeExtractorCrash: exit status {NATIVE_CRASH_EXIT_STATUS}" in error


def _retry_diagnostic(result: ConversionResult) -> str:
    if result.status == "converted":
        return "fallback only"
    if _has_native_crash(result.error):
        return f"native exit status {NATIVE_CRASH_EXIT_STATUS}"
    if "timed out" in result.error.lower() or "TimeoutExpired" in result.error:
        return "timeout"
    return "extractor error"


def _verified_rows(mapping_report: Path, limit: int | None) -> list[dict[str, str]]:
    return _selected_rows(mapping_report, limit, {"mapped_verified"})


def _selected_rows(
    mapping_report: Path,
    limit: int | None,
    classifications: set[str],
    *,
    skip_attachment_keys: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with mapping_report.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("classification") not in classifications:
                continue
            if not row.get("source_path"):
                continue
            attachment_key = row.get("zotero_attachment_key")
            if attachment_key and attachment_key in skip_attachment_keys:
                continue
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
    return rows


def _convert_row(
    row: dict[str, str],
    markdown_dir: Path,
    images_root: Path,
    index: int,
    timeout_seconds: int,
    *,
    force: bool,
    skip_keys: frozenset[str] = frozenset(),
    retry: bool = False,
) -> tuple[ConversionResult, TimeoutCandidate | None]:
    source_path = Path(row["source_path"])
    output_path = markdown_dir / f"{index:04d}_{_output_stem(row)}.md"
    raw_output_path = output_path.with_name(f"{output_path.stem}.raw.tmp")
    images_dir = images_root / output_path.stem
    staged_images_root: Path | None = None
    extraction_images_dir = images_dir
    math_sidecar_path = raw_output_path.with_suffix(".math.json")
    effective_timeout = _effective_timeout(row, timeout_seconds, source_path)
    try:
        if output_path.exists() and not force:
            extraction_tool = _existing_extraction_tool(output_path)
            has_math = _existing_has_math(output_path)
            body = _existing_markdown_body(output_path)
            atomic_write_text(output_path, _with_front_matter(row, body, extraction_tool, has_math=has_math))
            return (
                _result(
                    row,
                    output_path,
                    "skipped_existing",
                    extraction_tool=extraction_tool,
                    has_math=has_math,
                ),
                None,
            )
        if force and output_path.exists():
            images_root.mkdir(parents=True, exist_ok=True)
            staged_images_root = Path(tempfile.mkdtemp(prefix=".conversion-", dir=images_root))
            extraction_images_dir = staged_images_root / output_path.stem
        elif force:
            shutil.rmtree(images_dir, ignore_errors=True)
        skip_primary = row.get("zotero_attachment_key") in skip_keys
        # Hash before and after extraction, not only after. The extractor reads the PDF at
        # `source_path` over a long window; if the file is replaced while it runs, hashing only
        # afterwards pairs PDF A's text with PDF B's hash. That is worse than no provenance,
        # because `source_changed` then reads as clean and no audit can ever surface the drift.
        # This detects the race rather than preventing it -- a replace-and-restore inside the
        # window still slips through -- but it narrows "silently wrong forever" to a
        # pathological write pattern.
        source_sha256_before = _source_sha256(source_path)
        extraction_tool, fallback_note, primary_timed_out = _extract_markdown(
            source_path, raw_output_path, extraction_images_dir, effective_timeout, skip_primary=skip_primary
        )
        source_sha256 = _source_sha256(source_path)
        if source_sha256 != source_sha256_before:
            # Return before writing output_path. An error row is retried; a written Markdown
            # file is accepted, so publishing text whose source is unknown is the worse failure.
            # The enclosing `finally` removes the raw and sidecar temporaries either way.
            return (
                _result(
                    row,
                    output_path,
                    "error",
                    f"The source PDF changed while {extraction_tool} was extracting it; "
                    "no Markdown was written. Re-run the conversion for this attachment.",
                ),
                None,
            )
        if output_path.exists() and (_has_native_crash(fallback_note) or (retry and fallback_note)):
            # A native primary crash must not replace an already usable conversion
            # with fallback text while the lower-concurrency retry is pending.
            return _result(row, output_path, "error", fallback_note + "; existing Markdown retained"), None
        markdown = raw_output_path.read_text(encoding="utf-8")
        if staged_images_root is not None and extraction_tool == PRIMARY_EXTRACTION_TOOL:
            markdown = markdown.replace(extraction_images_dir.as_posix(), images_dir.as_posix())
            markdown = markdown.replace(str(extraction_images_dir), str(images_dir))
        has_math = _read_math_sidecar(math_sidecar_path)
        if staged_images_root is not None:
            staged_markdown = staged_images_root / output_path.name
            staged_markdown.write_text(
                _with_front_matter(row, markdown, extraction_tool, has_math=has_math), encoding="utf-8", newline="\n"
            )
            backup_images = staged_images_root / "previous-images"
            if images_dir.exists():
                images_dir.rename(backup_images)
            try:
                if extraction_tool == PRIMARY_EXTRACTION_TOOL and extraction_images_dir.exists():
                    extraction_images_dir.rename(images_dir)
                os.replace(staged_markdown, output_path)
            except Exception:
                if images_dir.exists():
                    shutil.rmtree(images_dir)
                if backup_images.exists():
                    backup_images.rename(images_dir)
                raise
        else:
            atomic_write_text(output_path, _with_front_matter(row, markdown, extraction_tool, has_math=has_math))
        result = _result(
            row,
            output_path,
            "converted",
            extraction_tool=extraction_tool,
            has_math=has_math,
            error=fallback_note,
            source_sha256=source_sha256,
        )
        candidate = (
            _build_timeout_candidate(row, source_path, effective_timeout, "fallback_used", "converted")
            if primary_timed_out
            else None
        )
        return result, candidate
    except PrimaryExtractorTimeoutError as exc:
        result = _result(row, output_path, "error", str(exc))
        candidate = _build_timeout_candidate(row, source_path, effective_timeout, "fallback_failed", "error")
        return result, candidate
    except subprocess.TimeoutExpired:
        return _result(row, output_path, "error", f"TimeoutExpired: exceeded {effective_timeout} seconds"), None
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        message = stderr[-1000:] if stderr else f"exit status {exc.returncode}"
        return _result(row, output_path, "error", f"CalledProcessError: {message}"), None
    except Exception as exc:
        return _result(row, output_path, "error", f"{type(exc).__name__}: {exc}"), None
    finally:
        raw_output_path.unlink(missing_ok=True)
        math_sidecar_path.unlink(missing_ok=True)
        if staged_images_root is not None:
            shutil.rmtree(staged_images_root, ignore_errors=True)


def _build_timeout_candidate(
    row: dict[str, str],
    source_path: Path,
    effective_timeout: int,
    fallback_outcome: str,
    conversion_status: str,
) -> TimeoutCandidate:
    density = _sample_drawing_density(source_path)
    return TimeoutCandidate(
        zotero_parent_key=row.get("zotero_parent_key", ""),
        zotero_attachment_key=row.get("zotero_attachment_key", ""),
        item_type=row.get("item_type", ""),
        title=row.get("title", ""),
        creators=row.get("creators", ""),
        year=row.get("year", ""),
        doi=row.get("doi", ""),
        citation_key=row.get("citation_key", ""),
        source_path=row.get("source_path", ""),
        page_count=row.get("page_count", ""),
        classification=row.get("classification", ""),
        identity_status=row.get("identity_status", ""),
        identity_rule=row.get("identity_rule", ""),
        safe_folder_id=row.get("safe_folder_id", ""),
        drawing_density=density,
        attempted_timeout_seconds=effective_timeout,
        suggested_next_timeout_seconds=suggested_next_timeout(effective_timeout),
        fallback_outcome=fallback_outcome,
        conversion_status=conversion_status,
        detected_at=datetime.now().isoformat(timespec="seconds"),
    )


def _effective_timeout(row: dict[str, str], timeout_seconds: int, source_path: Path) -> int:
    try:
        page_count = int(row.get("page_count") or 0)
    except ValueError:
        page_count = 0
    density = _sample_drawing_density(source_path)
    multiplier = 1.0 + min(density / DRAWING_DENSITY_DIVISOR, MAX_DRAWING_TIMEOUT_MULTIPLIER - 1.0)
    seconds_per_page = SECONDS_PER_PAGE_TIMEOUT * multiplier
    return max(timeout_seconds, int(page_count * seconds_per_page))


def _sample_drawing_density(source_path: Path) -> float:
    """Average vector-drawing count per page, from a bounded page sample.

    A best-effort signal like math_detection's font/text sampling: any failure (missing
    fitz, unreadable PDF) must not block conversion, so it just falls back to 0 density,
    i.e. the plain page-count timeout.
    """
    if fitz is None:
        return 0.0
    try:
        with fitz.open(source_path) as document:
            sample_size = min(len(document), DRAWING_SAMPLE_PAGES)
            if sample_size == 0:
                return 0.0
            total_drawings = sum(len(document[i].get_drawings()) for i in range(sample_size))
        return total_drawings / sample_size
    except Exception:
        return 0.0


def _load_persisted_skip_keys(output_root: Path) -> frozenset[str]:
    """Attachment keys that skip straight to the fallback extractor, per timeout_skip_list.json.

    Replaces a prior hardcoded frozenset: a document confirmed to exceed even the
    drawing-density-scaled timeout gets added here (see `docs/data-dictionary.md`) instead of
    requiring a source change. Best-effort like the drawing-density scan: a missing or corrupt
    file just means no skip entries, not a conversion failure.
    """
    skip_list_path = output_root / SKIP_LIST_FILENAME
    try:
        data = json.loads(skip_list_path.read_text(encoding="utf-8"))
        return frozenset(data.get("entries", {}).keys())
    except (OSError, ValueError):
        return frozenset()


def _load_indexed_keys_fail_open(jsonl_path: Path) -> frozenset[str]:
    """Attachment keys already present in the sidecar full-text index (zotero_text_index.jsonl).

    An attachment reaches the index either as an original `mapped_verified` conversion or via a
    later `apply-verification` promotion; either way it is already resolved, so `verify-unverified`
    should not reconvert or rescore it again on a future run. Fail-open like
    `_load_persisted_skip_keys`: a missing or corrupt index just means nothing is skipped.
    """
    try:
        return frozenset(load_indexed_keys(jsonl_path))
    except (OSError, ValueError):
        return frozenset()


def _read_math_sidecar(sidecar_path: Path) -> bool:
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        return bool(data.get("has_math", False))
    except (OSError, ValueError):
        return False


def _extract_markdown(
    source_path: Path, raw_output_path: Path, images_dir: Path, timeout_seconds: int, *, skip_primary: bool = False
) -> tuple[str, str, bool]:
    primary_timed_out = False
    if skip_primary:
        primary_error = "primary extractor skipped: known to exceed the drawing-density timeout cap"
    else:
        try:
            _run_extractor(source_path, raw_output_path, PRIMARY_EXTRACTION_TOOL, timeout_seconds, image_dir=images_dir)
            return PRIMARY_EXTRACTION_TOOL, "", False
        except subprocess.TimeoutExpired:
            primary_error = f"TimeoutExpired: exceeded {timeout_seconds} seconds"
            primary_timed_out = True
        except subprocess.CalledProcessError as exc:
            primary_error = f"CalledProcessError: {_stderr_tail(exc)}"

    raw_output_path.unlink(missing_ok=True)
    try:
        _run_extractor(source_path, raw_output_path, FALLBACK_EXTRACTION_TOOL, timeout_seconds)
        return (
            FALLBACK_EXTRACTION_TOOL,
            f"Primary extractor failed; fallback used. Primary error: {primary_error}",
            primary_timed_out,
        )
    except subprocess.TimeoutExpired:
        message = f"Primary extractor failed ({primary_error}); fallback timed out after {timeout_seconds} seconds"
        if primary_timed_out:
            raise PrimaryExtractorTimeoutError(message)
        raise RuntimeError(message)
    except subprocess.CalledProcessError as exc:
        message = f"Primary extractor failed ({primary_error}); fallback failed: {_stderr_tail(exc)}"
        if primary_timed_out:
            raise PrimaryExtractorTimeoutError(message)
        raise RuntimeError(message)


def _run_extractor(
    source_path: Path,
    raw_output_path: Path,
    extraction_tool: str,
    timeout_seconds: int,
    *,
    image_dir: Path | None = None,
) -> None:
    argv = [
        sys.executable,
        "-m",
        "zotero_pdf_text._extract_markdown",
        str(source_path),
        str(raw_output_path),
        "--tool",
        extraction_tool,
    ]
    if image_dir is not None:
        argv += ["--image-dir", str(image_dir)]
    subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _stderr_tail(exc: subprocess.CalledProcessError) -> str:
    if exc.returncode & 0xFFFFFFFF == NATIVE_CRASH_EXIT_STATUS:
        return f"NativeExtractorCrash: exit status {NATIVE_CRASH_EXIT_STATUS}"
    stderr = (exc.stderr or "").strip()
    if stderr:
        return stderr[-1000:].replace("NativeExtractorCrash:", "extractor stderr:")
    return f"exit status {exc.returncode}"


def _with_front_matter(
    row: dict[str, str],
    markdown: str,
    extraction_tool: str = EXTRACTION_TOOL,
    *,
    has_math: bool = False,
    extra_fields: dict[str, str] | None = None,
) -> str:
    lines = [
        "---",
        f'zotero_parent_key: "{_yaml_escape(row.get("zotero_parent_key", ""))}"',
        f'zotero_attachment_key: "{_yaml_escape(row.get("zotero_attachment_key", ""))}"',
        f'title: "{_yaml_escape(row.get("title", ""))}"',
        f'creators: "{_yaml_escape(row.get("creators", ""))}"',
        f'year: "{_yaml_escape(row.get("year", ""))}"',
        f'doi: "{_yaml_escape(row.get("doi", ""))}"',
        f'citation_key: "{_yaml_escape(row.get("citation_key", ""))}"',
        f'source_path: "{_yaml_escape(row.get("source_path", ""))}"',
        f'extraction_tool: "{_yaml_escape(extraction_tool)}"',
        f'has_math: {"true" if has_math else "false"}',
    ]
    for key, value in (extra_fields or {}).items():
        lines.append(f'{key}: "{_yaml_escape(value)}"')
    lines += ["---", ""]
    return "\n".join(lines) + markdown.rstrip() + "\n"


def _existing_extraction_tool(output_path: Path) -> str:
    try:
        for line in output_path.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
            if line.startswith("extraction_tool:"):
                return line.split(":", 1)[1].strip().strip('"')
    except OSError:
        pass
    return EXTRACTION_TOOL


def _existing_has_math(output_path: Path) -> bool:
    try:
        for line in output_path.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
            if line.startswith("has_math:"):
                return line.split(":", 1)[1].strip().strip('"').lower() == "true"
    except OSError:
        pass
    return False


def _existing_markdown_body(output_path: Path) -> str:
    markdown = output_path.read_text(encoding="utf-8", errors="replace")
    if not markdown.startswith("---\n"):
        return markdown.rstrip()
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return markdown.rstrip()
    return markdown[end + len("\n---\n") :].lstrip("\n").rstrip()


def _source_sha256(source_path: Path) -> str:
    """Hash the source PDF, returning "" if it cannot be read.

    A hash failure must not fail a conversion that otherwise succeeded, so this degrades to
    "not known" rather than raising. Callers treat "" as unverifiable, never as unchanged.
    """
    try:
        digest = hashlib.sha256()
        with source_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return ""


def _result(
    row: dict[str, str],
    output_path: Path,
    status: str,
    error: str = "",
    extraction_tool: str = EXTRACTION_TOOL,
    has_math: bool = False,
    source_sha256: str = "",
) -> ConversionResult:
    return ConversionResult(
        status=status,
        extraction_tool=extraction_tool,
        has_math="true" if has_math else "false",
        source_sha256=source_sha256,
        zotero_parent_key=row.get("zotero_parent_key", ""),
        zotero_attachment_key=row.get("zotero_attachment_key", ""),
        item_type=row.get("item_type", ""),
        title=row.get("title", ""),
        creators=row.get("creators", ""),
        year=row.get("year", ""),
        doi=row.get("doi", ""),
        citation_key=row.get("citation_key", ""),
        source_path=row.get("source_path", ""),
        output_path=str(output_path) if status in {"converted", "skipped_existing"} else "",
        page_count=row.get("page_count", ""),
        classification=row.get("classification", ""),
        identity_status=row.get("identity_status", ""),
        identity_rule=row.get("identity_rule", ""),
        error=error,
    )


def _output_stem(row: dict[str, str]) -> str:
    base = row.get("safe_folder_id") or row.get("zotero_attachment_key") or Path(row.get("source_path", "pdf")).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")[:120] or "pdf"


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _write_manifest(path: Path, results: list[ConversionResult]) -> None:
    fieldnames = list(asdict(results[0]).keys()) if results else list(ConversionResult.__dataclass_fields__)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


def _write_jsonl(path: Path, results: list[ConversionResult]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for result in results:
            handle.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")


def _write_summary(
    path: Path,
    mapping_report: Path,
    results: list[ConversionResult],
    workers: int,
    timeout_seconds: int,
    force: bool,
    classifications: set[str],
    retry_attempted: int = 0,
    retry_workers: int = 0,
    retry_counts: Counter[str] | None = None,
) -> None:
    retry_counts = retry_counts or Counter()
    converted = sum(1 for result in results if result.status == "converted")
    skipped = sum(1 for result in results if result.status == "skipped_existing")
    errors = sum(1 for result in results if result.status == "error")
    tool_counts = Counter(result.extraction_tool for result in results)
    exit_counts = Counter(
        code for result in results for code in set(re.findall(r"\bexit status (\d+)\b", result.error))
    )
    unresolved_native = sum(result.status == "error" and _has_native_crash(result.error) for result in results)
    timeout_errors = sum(
        result.status == "error" and not _has_native_crash(result.error)
        and ("TimeoutExpired" in result.error or "timed out" in result.error.lower())
        for result in results
    )
    lines = [
        "# Markdown Conversion Summary",
        "",
        f"- Run created: {datetime.now().isoformat(timespec='seconds')}",
        f"- Mapping report: `{mapping_report}`",
        f"- Requested rows: {len(results)}",
        f"- Converted: {converted}",
        f"- Skipped existing: {skipped}",
        f"- Errors: {errors}",
        f"- Unresolved native crash errors: {unresolved_native}",
        f"- Timeout errors: {timeout_errors}",
        f"- Other errors: {errors - unresolved_native - timeout_errors}",
        f"- Native crash retries attempted: {retry_attempted} (workers: {retry_workers})",
        f"- Native crash retries recovered: {retry_counts['recovered']}",
        f"- Native crash retries fallback-only: {retry_counts['fallback_only']}",
        f"- Native crash retries still-failed: {retry_counts['still_failed']}",
        *[f"- Child process exit status {code}: {count} attachments" for code, count in sorted(exit_counts.items())],
        "- Extraction tools:",
        *[f"  - `{tool}`: {count}" for tool, count in sorted(tool_counts.items())],
        f"- Workers: {workers}",
        f"- Per-PDF timeout seconds: {timeout_seconds}",
        f"- Force reconversion: {force}",
        f"- Source classifications: {', '.join(sorted(classifications))}",
        f"- Markdown folder: `{path.parent / 'markdown'}`",
        "",
        "## Outputs",
        "",
        "- `manifest.csv`: spreadsheet-friendly conversion manifest",
        "- `manifest.jsonl`: line-delimited manifest for tools",
        "- `markdown/`: converted Markdown files with Zotero front matter",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
