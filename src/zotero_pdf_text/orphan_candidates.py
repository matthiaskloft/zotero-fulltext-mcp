from __future__ import annotations

import contextlib
import csv
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ._atomic import replace_with_retry

CANDIDATE_JSONL_FILENAME = "orphan_candidates.jsonl"
CANDIDATE_CSV_FILENAME = "orphan_candidates.csv"

STATUS_PENDING = "pending"
STATUS_SKIPPED = "skipped"
STATUS_RESOLVED = "resolved"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


@dataclass
class OrphanCandidate:
    orphan_source_path: str
    orphan_sha256: str
    orphan_safe_folder_id: str
    orphan_page_count: int
    candidate_parent_key: str
    candidate_item_type: str
    candidate_title: str
    candidate_creators: str
    candidate_year: str
    candidate_doi: str
    candidate_citation_key: str
    candidate_had_stale_attachment: bool
    title_score: int
    author_evidence: bool
    year_evidence: bool
    observed_dois: str
    confidence_tier: str  # "high" | "medium" | "low"
    identity_rule: str
    detected_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def match_key(self) -> str:
        return f"{self.orphan_sha256}:{self.candidate_parent_key}"


def write_run_candidates(run_dir: Path, candidates: list[OrphanCandidate]) -> None:
    """Write this run's orphan candidates as CSV/JSONL, mirroring the timeout-candidate pattern.

    Always writes both files (header-only when empty) so a run directory has a consistent,
    predictable set of artifacts regardless of whether any candidate was found.
    """
    fieldnames = list(OrphanCandidate.__dataclass_fields__)
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


def append_master_candidates(master_jsonl_path: Path, candidates: list[OrphanCandidate]) -> None:
    """Merge newly discovered candidates into the persistent master file, deduped by match_key.

    A new (orphan, candidate-parent) pairing becomes a pending entry with occurrence_count=1. An
    existing pending entry has its scoring fields refreshed and occurrence_count incremented, but
    keeps first_detected_at. An existing skipped/resolved entry is left untouched -- a later
    automatic discovery run must never silently reopen a human decision.
    """
    if not candidates:
        return
    records = _load_master_records(master_jsonl_path)
    for candidate in candidates:
        key = candidate.match_key
        existing = records.get(key)
        if existing is None:
            record = candidate.to_dict()
            record["status"] = STATUS_PENDING
            record["occurrence_count"] = 1
            record["first_detected_at"] = candidate.detected_at
            record["last_detected_at"] = candidate.detected_at
            records[key] = record
        elif existing.get("status") == STATUS_PENDING:
            first_detected_at = existing.get("first_detected_at", existing.get("detected_at", candidate.detected_at))
            occurrence_count = int(existing.get("occurrence_count") or 1) + 1
            record = candidate.to_dict()
            record["status"] = STATUS_PENDING
            record["occurrence_count"] = occurrence_count
            record["first_detected_at"] = first_detected_at
            record["last_detected_at"] = candidate.detected_at
            records[key] = record
        # skipped/resolved entries: left untouched on purpose.
    _write_master_records(master_jsonl_path, records)


def find_candidate(master_jsonl_path: Path, match_key: str) -> dict[str, object]:
    records = _load_master_records(master_jsonl_path)
    if match_key not in records:
        raise KeyError(f"No orphan candidate found for match key {match_key}")
    return records[match_key]


def list_candidates(master_jsonl_path: Path, *, status: str | None = STATUS_PENDING) -> list[dict[str, object]]:
    records = _load_master_records(master_jsonl_path)
    values = list(records.values())
    if status is not None:
        values = [record for record in values if record.get("status") == status]
    return sorted(values, key=lambda record: record.get("last_detected_at", ""), reverse=True)


def mark_status(master_jsonl_path: Path, match_key: str, *, status: str, extra_fields: dict[str, object]) -> None:
    records = _load_master_records(master_jsonl_path)
    if match_key not in records:
        raise KeyError(f"No orphan candidate found for match key {match_key}")
    records[match_key]["status"] = status
    records[match_key].update(extra_fields)
    _write_master_records(master_jsonl_path, records)


def _load_master_records(master_jsonl_path: Path) -> dict[str, dict[str, object]]:
    """Read the master candidates file, skipping any malformed line rather than aborting the read.

    This file is user-editable (mirroring timeout_candidates.jsonl), and it is rewritten by every
    discovery run -- a single truncated/hand-edited line must not permanently break every future
    run's append_master_candidates call or the read-only list_orphan_candidates MCP tool.
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
            sha = record.get("orphan_sha256", "")
            parent_key = record.get("candidate_parent_key", "")
            if not sha or not parent_key:
                continue
            records[f"{sha}:{parent_key}"] = record
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
