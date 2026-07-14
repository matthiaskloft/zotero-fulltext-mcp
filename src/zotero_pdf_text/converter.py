from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import ProjectConfig

try:
    import pymupdf4llm
except Exception:  # pragma: no cover - exercised only if dependency is missing
    pymupdf4llm = None

PRIMARY_EXTRACTION_TOOL = "pymupdf4llm.to_markdown"
FALLBACK_EXTRACTION_TOOL = "pymupdf.get_text"
EXTRACTION_TOOL = PRIMARY_EXTRACTION_TOOL
SECONDS_PER_PAGE_TIMEOUT = 4


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
    run_dir = output_dir or config.output_root / "samples" / timestamp
    return _convert_verified_rows(
        mapping_report,
        run_dir,
        limit=limit,
        exist_ok=force,
        workers=workers,
        timeout_seconds=timeout_seconds,
        force=force,
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
    run_dir = output_dir or config.output_root / "verified" / timestamp
    return _convert_verified_rows(
        mapping_report,
        run_dir,
        limit=limit,
        exist_ok=resume or force,
        workers=workers,
        timeout_seconds=timeout_seconds,
        force=force,
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
) -> Path:
    if pymupdf4llm is None:
        raise RuntimeError("pymupdf4llm is not installed")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    if not mapping_report.exists():
        raise FileNotFoundError(mapping_report)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir or config.output_root / "unverified_review" / timestamp
    classifications = {"mapped_unverified"}
    if include_possible_mismatch:
        classifications.add("possible_mismatch")
    return _convert_mapping_rows(
        mapping_report,
        run_dir,
        limit=limit,
        exist_ok=resume or force,
        workers=workers,
        timeout_seconds=timeout_seconds,
        force=force,
        classifications=classifications,
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

    rows = _selected_rows(mapping_report, limit, classifications)
    indexed_rows = list(enumerate(rows, start=1))
    if workers == 1:
        results = [
            _convert_row(row, markdown_dir, images_root, index, timeout_seconds, force=force)
            for index, row in indexed_rows
        ]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(
                executor.map(
                    lambda item: _convert_row(item[1], markdown_dir, images_root, item[0], timeout_seconds, force=force),
                    indexed_rows,
                )
    )
    _write_manifest(run_dir / "manifest.csv", results)
    _write_jsonl(run_dir / "manifest.jsonl", results)
    _write_summary(run_dir / "summary.md", mapping_report, results, workers, timeout_seconds, force, classifications)
    return run_dir


def default_worker_count() -> int:
    return max(1, (os.cpu_count() or 1) - 4)


def _verified_rows(mapping_report: Path, limit: int | None) -> list[dict[str, str]]:
    return _selected_rows(mapping_report, limit, {"mapped_verified"})


def _selected_rows(mapping_report: Path, limit: int | None, classifications: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with mapping_report.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("classification") not in classifications:
                continue
            if not row.get("source_path"):
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
) -> ConversionResult:
    source_path = Path(row["source_path"])
    output_path = markdown_dir / f"{index:04d}_{_output_stem(row)}.md"
    raw_output_path = output_path.with_name(f"{output_path.stem}.raw.tmp")
    images_dir = images_root / output_path.stem
    math_sidecar_path = raw_output_path.with_suffix(".math.json")
    effective_timeout = _effective_timeout(row, timeout_seconds)
    try:
        if output_path.exists() and not force:
            extraction_tool = _existing_extraction_tool(output_path)
            has_math = _existing_has_math(output_path)
            body = _existing_markdown_body(output_path)
            output_path.write_text(
                _with_front_matter(row, body, extraction_tool, has_math=has_math), encoding="utf-8", newline="\n"
            )
            return _result(
                row,
                output_path,
                "skipped_existing",
                extraction_tool=extraction_tool,
                has_math=has_math,
            )
        if force:
            shutil.rmtree(images_dir, ignore_errors=True)
        extraction_tool, fallback_note = _extract_markdown(source_path, raw_output_path, images_dir, effective_timeout)
        markdown = raw_output_path.read_text(encoding="utf-8")
        has_math = _read_math_sidecar(math_sidecar_path)
        output_path.write_text(
            _with_front_matter(row, markdown, extraction_tool, has_math=has_math), encoding="utf-8", newline="\n"
        )
        return _result(row, output_path, "converted", extraction_tool=extraction_tool, has_math=has_math, error=fallback_note)
    except subprocess.TimeoutExpired:
        return _result(row, output_path, "error", f"TimeoutExpired: exceeded {effective_timeout} seconds")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        message = stderr[-1000:] if stderr else str(exc)
        return _result(row, output_path, "error", f"CalledProcessError: {message}")
    except Exception as exc:
        return _result(row, output_path, "error", f"{type(exc).__name__}: {exc}")
    finally:
        raw_output_path.unlink(missing_ok=True)
        math_sidecar_path.unlink(missing_ok=True)


def _effective_timeout(row: dict[str, str], timeout_seconds: int) -> int:
    try:
        page_count = int(row.get("page_count") or 0)
    except ValueError:
        page_count = 0
    return max(timeout_seconds, page_count * SECONDS_PER_PAGE_TIMEOUT)


def _read_math_sidecar(sidecar_path: Path) -> bool:
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        return bool(data.get("has_math", False))
    except (OSError, ValueError):
        return False


def _extract_markdown(source_path: Path, raw_output_path: Path, images_dir: Path, timeout_seconds: int) -> tuple[str, str]:
    try:
        _run_extractor(source_path, raw_output_path, PRIMARY_EXTRACTION_TOOL, timeout_seconds, image_dir=images_dir)
        return PRIMARY_EXTRACTION_TOOL, ""
    except subprocess.TimeoutExpired as exc:
        primary_error = f"TimeoutExpired: exceeded {timeout_seconds} seconds"
    except subprocess.CalledProcessError as exc:
        primary_error = f"CalledProcessError: {_stderr_tail(exc)}"

    raw_output_path.unlink(missing_ok=True)
    try:
        _run_extractor(source_path, raw_output_path, FALLBACK_EXTRACTION_TOOL, timeout_seconds)
        return FALLBACK_EXTRACTION_TOOL, f"Primary extractor failed; fallback used. Primary error: {primary_error}"
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Primary extractor failed ({primary_error}); fallback timed out after {timeout_seconds} seconds"
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Primary extractor failed ({primary_error}); fallback failed: {_stderr_tail(exc)}")


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
    stderr = (exc.stderr or "").strip()
    return stderr[-1000:] if stderr else str(exc)


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


def _result(
    row: dict[str, str],
    output_path: Path,
    status: str,
    error: str = "",
    extraction_tool: str = EXTRACTION_TOOL,
    has_math: bool = False,
) -> ConversionResult:
    return ConversionResult(
        status=status,
        extraction_tool=extraction_tool,
        has_math="true" if has_math else "false",
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
) -> None:
    converted = sum(1 for result in results if result.status == "converted")
    skipped = sum(1 for result in results if result.status == "skipped_existing")
    errors = sum(1 for result in results if result.status == "error")
    tool_counts = Counter(result.extraction_tool for result in results)
    lines = [
        "# Markdown Conversion Summary",
        "",
        f"- Run created: {datetime.now().isoformat(timespec='seconds')}",
        f"- Mapping report: `{mapping_report}`",
        f"- Requested rows: {len(results)}",
        f"- Converted: {converted}",
        f"- Skipped existing: {skipped}",
        f"- Errors: {errors}",
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
