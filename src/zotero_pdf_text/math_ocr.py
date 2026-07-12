from __future__ import annotations

import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .converter import _with_front_matter
from .fts import build_fts_index, get_item_context
from .indexer import TextIndexRecord, _sha256, _strip_front_matter, replace_text_index_record
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
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ReconvertResult:
    context = get_item_context(db_path, attachment_key=attachment_key)
    records = context.get("records", [])
    if not records:
        return _error_result(attachment_key, f"No indexed record found for attachment key {attachment_key}")
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
    raw_output_path = markdown_path.with_name(f"{markdown_path.stem}.marker.raw.tmp")

    try:
        _run_marker_extractor(source_path, raw_output_path, images_dir, timeout_seconds)
        new_text = raw_output_path.read_text(encoding="utf-8")
    except subprocess.TimeoutExpired:
        return _error_result(
            attachment_key,
            f"marker-pdf extraction timed out after {timeout_seconds} seconds",
            previous_extraction_tool=previous_extraction_tool,
            previous_char_count=previous_char_count,
            markdown_path=str(markdown_path),
            source_path=str(source_path),
        )
    except subprocess.CalledProcessError as exc:
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
    finally:
        raw_output_path.unlink(missing_ok=True)

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
    markdown_path.write_text(new_markdown, encoding="utf-8", newline="\n")

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
    # Every other writer of jsonl_path/fts_db_path (build-index, append-index, convert-*) takes
    # this same lock; without it here, a reconvert-math run racing another write command could
    # have its update silently discarded by "last atomic replace wins" -- no exception, no data
    # corruption, just a lost update. Held only around the shared JSONL/FTS writes, not the
    # preceding GPU-bound marker extraction or the attachment-specific Markdown write above, so
    # it doesn't unnecessarily block other commands for minutes.
    try:
        with pipeline_write_lock(jsonl_path.parent, command="reconvert-math"):
            replace_text_index_record(jsonl_path, attachment_key, new_record)
            build_fts_index(jsonl_path, fts_db_path)
    except PipelineLockedError as exc:
        return _error_result(
            attachment_key,
            str(exc),
            previous_extraction_tool=previous_extraction_tool,
            previous_char_count=previous_char_count,
            markdown_path=str(markdown_path),
            source_path=str(source_path),
        )

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
