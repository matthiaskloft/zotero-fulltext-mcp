from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ._atomic import atomic_write_text

CANDIDATE_JSONL_FILENAME = "timeout_candidates.jsonl"
CANDIDATE_CSV_FILENAME = "timeout_candidates.csv"
SKIP_LIST_FILENAME = "timeout_skip_list.json"

STATUS_PENDING = "pending"
STATUS_SKIPPED = "skipped"
STATUS_RESOLVED = "resolved"

# What the *current* published index holds for a candidate's attachment, derived at read time.
# A candidate's own fields (conversion_status, fallback_outcome, ...) describe the historical
# timeout attempt; these describe the record a search would return today.
CURRENT_STATE_STRUCTURED = "structured_extraction"
CURRENT_STATE_FALLBACK = "fallback_extraction"
CURRENT_STATE_NOT_INDEXED = "not_indexed"
CURRENT_STATE_UNKNOWN = "unknown"
RESOLVED_VIA_CURRENT_INDEX = "current_index"

# Mirrors converter.FALLBACK_EXTRACTION_TOOL (asserted equal in tests). Not imported: converter
# imports this module, and a read-only candidate listing should not pull in the PDF extractors.
_FALLBACK_EXTRACTION_TOOL = "pymupdf.get_text"

# Anchored to the one confirmed pathological case (ran past 13540s / ~3.75h without finishing):
# a 2x-uncapped suggestion could reach a full day+ for a similarly dense long book. 21600s (6h,
# 2x that already-impractical figure) gives genuinely slow-but-finishable documents real headroom
# while making it obvious in reporting when a document is "at the ceiling" -- a signal to skip
# rather than retry further.
MAX_SUGGESTED_TIMEOUT_SECONDS = 21600


@dataclass
class TimeoutCandidate:
    zotero_parent_key: str
    zotero_attachment_key: str
    item_type: str
    title: str
    creators: str
    year: str
    doi: str
    citation_key: str
    source_path: str
    page_count: str
    classification: str
    identity_status: str
    identity_rule: str
    safe_folder_id: str
    drawing_density: float
    attempted_timeout_seconds: int
    suggested_next_timeout_seconds: int
    fallback_outcome: str  # "fallback_used" | "fallback_failed"
    conversion_status: str  # "converted" | "error"
    detected_at: str
    # SHA-256 of the PDF when the timed-out attempt started; "" in records written before it existed.
    source_sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def suggested_next_timeout(attempted_timeout_seconds: int) -> int:
    return min(attempted_timeout_seconds * 2, MAX_SUGGESTED_TIMEOUT_SECONDS)


def write_run_candidates(run_dir: Path, candidates: list[TimeoutCandidate]) -> None:
    """Write this run's timeout candidates as CSV/JSONL, mirroring manifest.csv/.jsonl.

    Always writes both files (header-only when empty) so a run directory has a consistent,
    predictable set of artifacts regardless of whether any candidate was detected.
    """
    fieldnames = list(TimeoutCandidate.__dataclass_fields__)
    csv_path = run_dir / CANDIDATE_CSV_FILENAME
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate.to_dict())

    jsonl_path = run_dir / CANDIDATE_JSONL_FILENAME
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for candidate in candidates:
            handle.write(json.dumps(candidate.to_dict(), ensure_ascii=False) + "\n")


def append_master_candidates(master_jsonl_path: Path, candidates: list[TimeoutCandidate]) -> None:
    """Merge newly detected candidates into the persistent master file, deduped by attachment key.

    A new key becomes a pending entry with occurrence_count=1. An existing pending entry has its
    attempt fields refreshed and occurrence_count incremented, but keeps first_detected_at. An
    existing skipped/resolved entry is left untouched -- an automatic re-run must never silently
    reopen a human decision.
    """
    if not candidates:
        return
    records = _load_master_records(master_jsonl_path)
    now = datetime.now().isoformat(timespec="seconds")
    for candidate in candidates:
        key = candidate.zotero_attachment_key
        existing = records.get(key)
        if existing is None:
            record = candidate.to_dict()
            record["status"] = STATUS_PENDING
            record["occurrence_count"] = 1
            record["first_detected_at"] = candidate.detected_at
            record["last_detected_at"] = candidate.detected_at
            records[key] = record
        elif existing.get("status") == STATUS_PENDING:
            first_detected_at = existing.get("first_detected_at", existing.get("detected_at", now))
            occurrence_count = int(cast(Any, existing.get("occurrence_count")) or 1) + 1
            record = candidate.to_dict()
            record["status"] = STATUS_PENDING
            record["occurrence_count"] = occurrence_count
            record["first_detected_at"] = first_detected_at
            record["last_detected_at"] = candidate.detected_at
            records[key] = record
        # skipped/resolved entries: left untouched on purpose.
    _write_master_records(master_jsonl_path, records)


def find_candidate(master_jsonl_path: Path, attachment_key: str) -> dict[str, object]:
    records = _load_master_records(master_jsonl_path)
    if attachment_key not in records:
        raise KeyError(f"No timeout candidate found for attachment key {attachment_key}")
    return records[attachment_key]


def list_candidates(master_jsonl_path: Path, *, status: str | None = STATUS_PENDING) -> list[dict[str, object]]:
    records = _load_master_records(master_jsonl_path)
    values = list(records.values())
    if status is not None:
        values = [record for record in values if record.get("status") == status]
    return sorted(values, key=lambda record: cast(Any, record.get("last_detected_at", "")), reverse=True)


def current_index_state(extraction_tool: str | None) -> str:
    """Classify the current index record's extraction tool for one candidate attachment.

    ``None`` means the attachment has no record in the current generation. Composite labels such
    as ``pymupdf4llm.to_markdown+glm-ocr`` are classified by their base extractor.
    """
    if extraction_tool is None:
        return CURRENT_STATE_NOT_INDEXED
    base = extraction_tool.split("+", 1)[0]
    if not base or base == _FALLBACK_EXTRACTION_TOOL:
        return CURRENT_STATE_FALLBACK
    return CURRENT_STATE_STRUCTURED


def with_current_index_state(
    record: dict[str, object], index_states: Mapping[str, Mapping[str, str]] | None
) -> dict[str, object]:
    """Return a copy of a master record annotated with the current index state.

    ``index_states`` maps attachment keys to their record in the current published generation
    (``extraction_tool``, ``indexed_at``, ``source_sha256``), or is ``None`` when that index could
    not be read. The stored decision stays available as ``recorded_status``. A *pending*
    candidate is reported as resolved via the current index only when recovery is established:
    the attachment is indexed with structured (non-fallback) text, that record was indexed after
    the candidate's last timeout, and its source hash does not contradict the one recorded at the
    timeout. Structured text that predates the timeout (or cannot be dated) stays pending -- it may
    be stale -- but its state and tool are still reported. The master file is never rewritten here,
    skipped/resolved decisions are never changed, and fallback-only text stays pending.
    """
    annotated = dict(record)
    recorded_status = str(record.get("status", ""))
    annotated["recorded_status"] = recorded_status
    annotated["resolved_via"] = str(record.get("resolved_via", ""))
    if index_states is None:
        annotated["current_index_state"] = CURRENT_STATE_UNKNOWN
        annotated["current_extraction_tool"] = ""
        return annotated
    indexed = index_states.get(str(record.get("zotero_attachment_key", "")))
    state = current_index_state(indexed.get("extraction_tool", "") if indexed is not None else None)
    annotated["current_index_state"] = state
    annotated["current_extraction_tool"] = indexed.get("extraction_tool", "") if indexed is not None else ""
    if (
        recorded_status == STATUS_PENDING
        and state == CURRENT_STATE_STRUCTURED
        and indexed is not None
        and _indexed_after_last_timeout(record, indexed)
        and not _source_hash_contradicts(record, indexed)
    ):
        annotated["status"] = STATUS_RESOLVED
        annotated["resolved_via"] = RESOLVED_VIA_CURRENT_INDEX
    return annotated


def _indexed_after_last_timeout(record: Mapping[str, object], indexed: Mapping[str, str]) -> bool:
    indexed_at = _parse_timestamp(indexed.get("indexed_at", ""))
    last_timeout = _parse_timestamp(record.get("last_detected_at") or record.get("detected_at") or "")
    return indexed_at is not None and last_timeout is not None and indexed_at > last_timeout


def _source_hash_contradicts(record: Mapping[str, object], indexed: Mapping[str, str]) -> bool:
    candidate_hash = str(record.get("source_sha256") or "")
    indexed_hash = indexed.get("source_sha256", "")
    return bool(candidate_hash and indexed_hash and candidate_hash != indexed_hash)


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO timestamp into an aware datetime, or None when absent/unparseable.

    Candidate timestamps are written with ``datetime.now()`` (naive local time); index
    ``indexed_at`` values carry an explicit UTC offset. A naive value is therefore read as local
    time, which is how it was written, so the two formats compare on one timeline.
    """
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def mark_status(master_jsonl_path: Path, attachment_key: str, *, status: str, extra_fields: dict[str, object]) -> None:
    records = _load_master_records(master_jsonl_path)
    if attachment_key not in records:
        raise KeyError(f"No timeout candidate found for attachment key {attachment_key}")
    records[attachment_key]["status"] = status
    records[attachment_key].update(extra_fields)
    _write_master_records(master_jsonl_path, records)


def add_to_skip_list(skip_list_path: Path, attachment_key: str, *, reason: str, title: str = "", citation_key: str = "") -> None:
    """Atomically add/update one skip-list entry."""
    data = _load_skip_list(skip_list_path)
    data["entries"][attachment_key] = {
        "reason": reason,
        "added_at": datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "citation_key": citation_key,
    }
    atomic_write_text(skip_list_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _load_skip_list(skip_list_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(skip_list_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            return {"version": 1, "entries": {}}
        return data
    except (OSError, ValueError):
        return {"version": 1, "entries": {}}


def _load_master_records(master_jsonl_path: Path) -> dict[str, dict[str, object]]:
    """Read the master candidates file, skipping any malformed line rather than aborting the read.

    This file is documented as user-editable (see docs/data-dictionary.md), and it is rewritten by
    every conversion run -- a single truncated/hand-edited line must not permanently break every
    future run's append_master_candidates call or the read-only list_timeout_candidates MCP tool.
    """
    records: dict[str, dict[str, object]] = {}
    if not master_jsonl_path.exists():
        return records
    with master_jsonl_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            key = record.get("zotero_attachment_key", "")
            if key:
                records[key] = record
    return records


def _write_master_records(master_jsonl_path: Path, records: dict[str, dict[str, object]]) -> None:
    master_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records.values())
    atomic_write_text(master_jsonl_path, content)
