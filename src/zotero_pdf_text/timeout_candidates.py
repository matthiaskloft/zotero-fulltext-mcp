from __future__ import annotations

import contextlib
import csv
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ._atomic import replace_with_retry

CANDIDATE_JSONL_FILENAME = "timeout_candidates.jsonl"
CANDIDATE_CSV_FILENAME = "timeout_candidates.csv"
SKIP_LIST_FILENAME = "timeout_skip_list.json"

STATUS_PENDING = "pending"
STATUS_SKIPPED = "skipped"
STATUS_RESOLVED = "resolved"

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
            occurrence_count = int(existing.get("occurrence_count") or 1) + 1
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
    return sorted(values, key=lambda record: record.get("last_detected_at", ""), reverse=True)


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
    _atomic_write_text(skip_list_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _load_skip_list(skip_list_path: Path) -> dict[str, object]:
    try:
        data = json.loads(skip_list_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            return {"version": 1, "entries": {}}
        return data
    except (OSError, ValueError):
        return {"version": 1, "entries": {}}


def _load_master_records(master_jsonl_path: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    if not master_jsonl_path.exists():
        return records
    with master_jsonl_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            key = record.get("zotero_attachment_key", "")
            if key:
                records[key] = record
    return records


def _write_master_records(master_jsonl_path: Path, records: dict[str, dict[str, object]]) -> None:
    master_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records.values())
    _atomic_write_text(master_jsonl_path, content)


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8", newline="\n")
        replace_with_retry(tmp_path, path)
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
