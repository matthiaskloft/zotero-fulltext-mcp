from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import ProjectConfig
from .converter import convert_verified
from .fts import build_fts_index
from .indexer import _record_from_manifest_row, append_text_index, load_indexed_keys, replace_text_index_record
from .lock import PipelineLockedError, pipeline_write_lock
from .timeout_candidates import (
    STATUS_PENDING,
    STATUS_RESOLVED,
    STATUS_SKIPPED,
    add_to_skip_list,
    find_candidate,
    mark_status,
)

# Explicit CLI/MCP overrides are hard-capped here regardless of the candidate's own
# suggested_next_timeout_seconds (itself capped at 6h) -- nothing should be able to start a
# multi-day accidental run, even by request.
MAX_RETRY_TIMEOUT_SECONDS = 86400

CANDIDATES_RELATIVE_PATH = Path("index") / "timeout_candidates.jsonl"
SKIP_LIST_RELATIVE_PATH = Path("timeout_skip_list.json")


@dataclass
class RetryTimeoutResult:
    ok: bool
    action: str  # "skip" | "retry"
    attachment_key: str
    previous_status: str
    new_status: str
    timeout_seconds_used: int | None
    extraction_tool: str
    markdown_path: str
    error: str
    resolved_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def skip_timeout_candidate(
    attachment_key: str,
    *,
    config: ProjectConfig,
    reason: str,
) -> RetryTimeoutResult:
    """Permanently skip the primary extractor for a recorded timeout candidate.

    Never touches Markdown, the sidecar index, or Zotero -- only the persisted skip list and the
    candidate's own status.
    """
    candidates_jsonl = config.output_root / CANDIDATES_RELATIVE_PATH
    skip_list_path = config.output_root / SKIP_LIST_RELATIVE_PATH
    try:
        candidate = find_candidate(candidates_jsonl, attachment_key)
    except KeyError as exc:
        return _error_result("skip", attachment_key, str(exc))
    previous_status = str(candidate.get("status", STATUS_PENDING))

    now = datetime.now().isoformat(timespec="seconds")
    try:
        with pipeline_write_lock(config.output_root, command="retry-timeout"):
            add_to_skip_list(
                skip_list_path,
                attachment_key,
                reason=reason,
                title=str(candidate.get("title", "")),
                citation_key=str(candidate.get("citation_key", "")),
            )
            mark_status(
                candidates_jsonl,
                attachment_key,
                status=STATUS_SKIPPED,
                extra_fields={"skip_reason": reason, "skipped_at": now},
            )
    except PipelineLockedError as exc:
        return _error_result("skip", attachment_key, str(exc), previous_status=previous_status)

    return RetryTimeoutResult(
        ok=True,
        action="skip",
        attachment_key=attachment_key,
        previous_status=previous_status,
        new_status=STATUS_SKIPPED,
        timeout_seconds_used=None,
        extraction_tool="",
        markdown_path="",
        error="",
        resolved_at=now,
    )


def retry_timeout_candidate(
    attachment_key: str,
    *,
    config: ProjectConfig,
    jsonl_path: Path,
    fts_db_path: Path,
    timeout_seconds: int | None = None,
    multiplier: float | None = None,
) -> RetryTimeoutResult:
    """Reconvert one recorded timeout candidate with a longer budget.

    Always uses a fresh --output-dir, so the originally converted Markdown file (and the run
    directory/manifest that produced it) is never overwritten in place -- only a successful
    result gets promoted into the sidecar JSONL/FTS index. A failed retry leaves the index and
    the candidate's status untouched; the candidate's own occurrence_count/last_detected_at are
    refreshed automatically by the nested convert_verified() call if it times out again.
    """
    if timeout_seconds is not None and multiplier is not None:
        return _error_result("retry", attachment_key, "Supply at most one of timeout_seconds and multiplier.")

    candidates_jsonl = config.output_root / CANDIDATES_RELATIVE_PATH
    try:
        candidate = find_candidate(candidates_jsonl, attachment_key)
    except KeyError as exc:
        return _error_result("retry", attachment_key, str(exc))
    previous_status = str(candidate.get("status", STATUS_PENDING))

    attempted_timeout_seconds = int(candidate.get("attempted_timeout_seconds") or 0)
    if timeout_seconds is not None:
        if timeout_seconds > MAX_RETRY_TIMEOUT_SECONDS:
            return _error_result(
                "retry",
                attachment_key,
                f"timeout_seconds must be at most {MAX_RETRY_TIMEOUT_SECONDS}.",
                previous_status=previous_status,
            )
        next_timeout = timeout_seconds
    elif multiplier is not None:
        next_timeout = min(int(attempted_timeout_seconds * multiplier), MAX_RETRY_TIMEOUT_SECONDS)
    else:
        next_timeout = min(int(candidate.get("suggested_next_timeout_seconds") or attempted_timeout_seconds), MAX_RETRY_TIMEOUT_SECONDS)

    row = {
        "classification": str(candidate.get("classification", "mapped_verified")),
        "source_path": str(candidate.get("source_path", "")),
        "safe_folder_id": str(candidate.get("safe_folder_id", "")),
        "zotero_parent_key": str(candidate.get("zotero_parent_key", "")),
        "zotero_attachment_key": attachment_key,
        "item_type": str(candidate.get("item_type", "")),
        "title": str(candidate.get("title", "")),
        "creators": str(candidate.get("creators", "")),
        "year": str(candidate.get("year", "")),
        "doi": str(candidate.get("doi", "")),
        "citation_key": str(candidate.get("citation_key", "")),
        "page_count": str(candidate.get("page_count", "")),
        "identity_status": str(candidate.get("identity_status", "")),
        "identity_rule": str(candidate.get("identity_rule", "")),
    }

    now = datetime.now().isoformat(timespec="seconds")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        with pipeline_write_lock(config.output_root, command="retry-timeout"):
            mapping_report = config.output_root / "retry_timeout" / f"{timestamp}_{attachment_key}_mapping_report.csv"
            _write_single_row_mapping_report(mapping_report, row)
            run_dir = config.output_root / "retry_timeout" / f"{timestamp}_{attachment_key}"
            convert_verified(
                config,
                mapping_report,
                output_dir=run_dir,
                workers=1,
                timeout_seconds=next_timeout,
                force=True,
            )

            manifest_path = run_dir / "manifest.csv"
            with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
                manifest_rows = list(csv.DictReader(handle))
            if not manifest_rows or manifest_rows[0].get("status") != "converted":
                error = manifest_rows[0].get("error", "") if manifest_rows else "No conversion result was produced."
                return _error_result("retry", attachment_key, error, previous_status=previous_status, timeout_seconds_used=next_timeout)

            manifest_row = manifest_rows[0]
            new_record = _record_from_manifest_row(manifest_row)
            if attachment_key in load_indexed_keys(jsonl_path):
                replace_text_index_record(jsonl_path, attachment_key, new_record)
            else:
                append_text_index(manifest_path, jsonl_path, jsonl_path)
            build_fts_index(jsonl_path, fts_db_path)
            mark_status(
                candidates_jsonl,
                attachment_key,
                status=STATUS_RESOLVED,
                extra_fields={"resolved_at": now, "resolved_via": "retry"},
            )
    except PipelineLockedError as exc:
        return _error_result("retry", attachment_key, str(exc), previous_status=previous_status, timeout_seconds_used=next_timeout)

    return RetryTimeoutResult(
        ok=True,
        action="retry",
        attachment_key=attachment_key,
        previous_status=previous_status,
        new_status=STATUS_RESOLVED,
        timeout_seconds_used=next_timeout,
        extraction_tool=manifest_row["extraction_tool"],
        markdown_path=manifest_row["output_path"],
        error="",
        resolved_at=now,
    )


def _write_single_row_mapping_report(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(row.keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


def _error_result(
    action: str,
    attachment_key: str,
    error: str,
    *,
    previous_status: str = "",
    timeout_seconds_used: int | None = None,
) -> RetryTimeoutResult:
    return RetryTimeoutResult(
        ok=False,
        action=action,
        attachment_key=attachment_key,
        previous_status=previous_status,
        new_status=previous_status,
        timeout_seconds_used=timeout_seconds_used,
        extraction_tool="",
        markdown_path="",
        error=error,
        resolved_at="",
    )
