from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ._atomic import replace_with_retry
from .converter import _with_front_matter
from .fts import build_fts_index, get_item_context
from .indexer import (
    TextIndexRecord,
    _sha256,
    _strip_front_matter,
    load_indexed_keys,
    replace_text_index_record,
)
from .lock import PipelineLockedError, pipeline_write_lock

MARKER_TOOL = "marker"
# Empirically ~70-90s/page for equation/figure-dense papers on a 6GB GPU (measured directly:
# 3 pages in 218.8s with no resource contention) -- 1800s was too tight for a ~36-page paper.
DEFAULT_TIMEOUT_SECONDS = 5400


@dataclass
class ReconvertResult:
    ok: bool
    attachment_key: str
    previous_extraction_tool: str
    new_extraction_tool: str
    previous_char_count: int
    new_char_count: int
    markdown_path: str
    source_path: str
    reconverted_at: str
    error: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def reconvert_with_marker(
    attachment_key: str,
    *,
    db_path: Path,
    jsonl_path: Path,
    fts_db_path: Path,
    lock_root: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ReconvertResult:
    context = get_item_context(db_path, attachment_key=attachment_key)
    records = context.get("records", [])
    if not records:
        return _error_result(attachment_key, f"No indexed record found for attachment key {attachment_key}")
    if attachment_key not in load_indexed_keys(jsonl_path):
        return _error_result(attachment_key, f"No text-sidecar record found for attachment key {attachment_key}")
    record = records[0]

    source_path = Path(record["source_path"])
    markdown_path = Path(record["markdown_path"])
    previous_extraction_tool = record["extraction_tool"]
    previous_char_count = int(record.get("char_count") or 0)

    if not source_path.exists():
        return _error_result(
            attachment_key,
            f"Source PDF no longer exists at {source_path}",
            previous_extraction_tool=previous_extraction_tool,
            previous_char_count=previous_char_count,
            markdown_path=str(markdown_path),
            source_path=str(source_path),
        )

    images_dir = markdown_path.parent.parent / "images" / markdown_path.stem
    images_root = images_dir.parent
    images_root_was_present = images_root.exists()
    images_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=f".{markdown_path.stem}.marker-", dir=images_root))
    preserve_staging = False
    staged_images_dir = staging_root / "images"
    staged_images_dir.mkdir()
    raw_output_path = staging_root / "output.md"

    def cleanup_staging() -> None:
        if not preserve_staging:
            shutil.rmtree(staging_root, ignore_errors=True)
        if not images_root_was_present:
            with contextlib.suppress(OSError):
                images_root.rmdir()

    try:
        _run_marker_extractor(source_path, raw_output_path, staged_images_dir, timeout_seconds)
        new_text = raw_output_path.read_text(encoding="utf-8")
    except subprocess.TimeoutExpired:
        cleanup_staging()
        return _error_result(
            attachment_key,
            f"marker-pdf extraction timed out after {timeout_seconds} seconds",
            previous_extraction_tool=previous_extraction_tool,
            previous_char_count=previous_char_count,
            markdown_path=str(markdown_path),
            source_path=str(source_path),
        )
    except subprocess.CalledProcessError as exc:
        cleanup_staging()
        stderr = (exc.stderr or "").strip()
        message = stderr[-1000:] if stderr else str(exc)
        return _error_result(
            attachment_key,
            f"marker-pdf extraction failed: {message}",
            previous_extraction_tool=previous_extraction_tool,
            previous_char_count=previous_char_count,
            markdown_path=str(markdown_path),
            source_path=str(source_path),
        )
    except Exception:
        cleanup_staging()
        raise

    has_math = bool(record.get("has_math", False))
    reconverted_at = datetime.now().isoformat(timespec="seconds")
    new_markdown = _with_front_matter(
        record,
        new_text,
        MARKER_TOOL,
        has_math=has_math,
        extra_fields={
            "previous_extraction_tool": previous_extraction_tool,
            "reconverted_at": reconverted_at,
        },
    )
    staged_markdown_path = staging_root / markdown_path.name
    try:
        staged_markdown_path.write_text(new_markdown, encoding="utf-8", newline="\n")
    except Exception:
        cleanup_staging()
        raise

    # Every other writer of jsonl_path/fts_db_path (build-index, append-index, convert-*) takes
    # a lock; without one here, a reconvert-math run racing another write command could have its
    # update silently discarded by "last atomic replace wins" -- no exception, no data corruption,
    # just a lost update. `lock_root` must be the same canonical root the caller's other pipeline
    # commands lock (e.g. convert-new's config.output_root) -- locking jsonl_path.parent here
    # would use a different lock file than convert-new's, defeating mutual exclusion between them
    # even though both write the same index files. The Markdown and sidecar writes happen inside
    # the lock, and their previous state is restored if a later commit step fails. Writing Markdown
    # before acquiring the lock would leave it updated while the index still describes the old
    # extraction if the lock were contested. The lock is held only around these writes, not the
    # preceding GPU-bound extraction.
    try:
        with pipeline_write_lock(lock_root, command="reconvert-math"):
            if attachment_key not in load_indexed_keys(jsonl_path):
                return _error_result(
                    attachment_key,
                    f"No text-sidecar record found for attachment key {attachment_key}",
                    previous_extraction_tool=previous_extraction_tool,
                    previous_char_count=previous_char_count,
                    markdown_path=str(markdown_path),
                    source_path=str(source_path),
                )
            previous_markdown = markdown_path.read_bytes() if markdown_path.exists() else None
            previous_jsonl = jsonl_path.read_bytes()
            previous_images_path = staging_root / "previous-images"
            images_promoted = False
            try:
                if images_dir.exists():
                    replace_with_retry(images_dir, previous_images_path)
                replace_with_retry(staged_images_dir, images_dir)
                images_promoted = True
                replace_with_retry(staged_markdown_path, markdown_path)
                new_text_for_index = _strip_front_matter(new_markdown)
                new_record = TextIndexRecord(
                    zotero_parent_key=record["zotero_parent_key"],
                    zotero_attachment_key=record["zotero_attachment_key"],
                    title=record["title"],
                    creators=record["creators"],
                    year=record["year"],
                    doi=record["doi"],
                    citation_key=record["citation_key"],
                    source_path=record["source_path"],
                    markdown_path=str(markdown_path),
                    markdown_sha256=_sha256(markdown_path),
                    extraction_tool=MARKER_TOOL,
                    char_count=len(new_text_for_index),
                    word_count=len(new_text_for_index.split()),
                    page_count=record["page_count"],
                    classification=record["classification"],
                    identity_status=record["identity_status"],
                    identity_rule=record["identity_rule"],
                    has_math=has_math,
                    text=new_text_for_index,
                )
                replace_text_index_record(jsonl_path, attachment_key, new_record)
                build_fts_index(jsonl_path, fts_db_path)
            except Exception:
                image_rollback_error: OSError | None = None
                try:
                    if images_promoted and images_dir.exists():
                        replace_with_retry(images_dir, staging_root / "failed-images")
                    if previous_images_path.exists():
                        replace_with_retry(previous_images_path, images_dir)
                except OSError as exc:
                    preserve_staging = True
                    image_rollback_error = exc
                if previous_markdown is None:
                    markdown_path.unlink(missing_ok=True)
                else:
                    _restore_bytes(markdown_path, previous_markdown)
                _restore_bytes(jsonl_path, previous_jsonl)
                build_fts_index(jsonl_path, fts_db_path)
                if image_rollback_error is not None:
                    raise RuntimeError("Math reconversion could not restore prior image assets.") from image_rollback_error
                raise
    except PipelineLockedError as exc:
        return _error_result(
            attachment_key,
            str(exc),
            previous_extraction_tool=previous_extraction_tool,
            previous_char_count=previous_char_count,
            markdown_path=str(markdown_path),
            source_path=str(source_path),
        )
    finally:
        cleanup_staging()

    return ReconvertResult(
        ok=True,
        attachment_key=attachment_key,
        previous_extraction_tool=previous_extraction_tool,
        new_extraction_tool=MARKER_TOOL,
        previous_char_count=previous_char_count,
        new_char_count=len(new_text_for_index),
        markdown_path=str(markdown_path),
        source_path=str(source_path),
        reconverted_at=reconverted_at,
        error="",
    )


def _restore_bytes(path: Path, content: bytes) -> None:
    """Atomically restore a file while the caller holds the pipeline write lock."""
    tmp_path = path.with_name(f".{path.name}.rollback-{os.getpid()}")
    try:
        tmp_path.write_bytes(content)
        replace_with_retry(tmp_path, path)
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def _run_marker_extractor(source_path: Path, raw_output_path: Path, images_dir: Path, timeout_seconds: int) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "zotero_pdf_text._extract_markdown_marker",
            str(source_path),
            str(raw_output_path),
            "--image-dir",
            str(images_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _error_result(
    attachment_key: str,
    error: str,
    *,
    previous_extraction_tool: str = "",
    previous_char_count: int = 0,
    markdown_path: str = "",
    source_path: str = "",
) -> ReconvertResult:
    return ReconvertResult(
        ok=False,
        attachment_key=attachment_key,
        previous_extraction_tool=previous_extraction_tool,
        new_extraction_tool="",
        previous_char_count=previous_char_count,
        new_char_count=0,
        markdown_path=markdown_path,
        source_path=source_path,
        reconverted_at="",
        error=error,
    )
