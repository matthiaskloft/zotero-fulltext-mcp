"""Audit-driven, selective repair of the published index.

``audit-library`` reports where the published generation, the files on disk and Zotero disagree,
but resolving a finding used to mean rebuilding the index from one run's manifest -- which drops
every record that run did not produce. This module resolves findings one record at a time, and
only where the fix is provably safe:

- :func:`build_plan` is read-only. It runs the audit's own joins and rules
  (:func:`library.build_observations` + :func:`library.classify_item`), so a plan's findings are
  the audit's findings for the same inputs, and sorts every indexed attachment with a finding into
  exactly one group:

  - ``safe`` -- only ``metadata_changed``, on a verified identity whose Markdown still matches the
    indexed hash and whose parent item and linked PDF are unchanged. The fix copies Zotero's current title, DOI and
    citation key onto the existing record and changes nothing else: text, Markdown path and hash,
    source hash and extraction tool (and therefore any enrichment) are kept.
  - ``reconvert`` -- ``stale_markdown`` or ``source_changed`` on a record that
    :func:`provenance_reconvert.reconversion_bucket` accepts (verified identity, same parent, same
    PDF path, PDF present, one unambiguous mapping row). The fix re-extracts the PDF into this
    plan's run directory and replaces the record through the validated replacement path.
  - ``review`` -- everything else: missing PDF or Markdown, orphaned or duplicate index rows,
    ambiguous or unverified identity, a relinked attachment, an unreadable Zotero database. These
    are never applied automatically. The one review decision this module can carry out is removing
    an ``orphaned_index`` record, and only for keys the operator names explicitly.

- :func:`apply_plan` holds the pipeline write lock, re-validates each selected row against the
  *current* generation and Zotero, and publishes one new generation through
  :func:`artifacts.stage_and_publish`. Every record it does not repair is copied verbatim, so
  records from any number of earlier runs survive. An interruption before the pointer swap leaves
  the previous generation current. Rows already repaired report ``already_resolved``, so a rerun
  after success publishes nothing.

Nothing here writes to Zotero (its database is read through a snapshot copy), and no Markdown or
PDF file is moved, rewritten or deleted: a removal drops an index record only.
"""

from __future__ import annotations

import csv
import json
import secrets
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import (
    current_generation_jsonl,
    recover_pending_publication,
    stage_and_publish,
    write_jsonl_applying_repairs,
)
from .config import ProjectConfig
from .converter import convert_planned_rows
from .indexer import TextIndexRecord, _record_from_manifest_row, _sha256
from .library import (
    ALL_STATUSES,
    METADATA_KEYS,
    STATUS_CURRENT,
    STATUS_DUPLICATE_KEY,
    STATUS_MAPPING_AMBIGUOUS,
    STATUS_MEMBERSHIP_UNCHECKED,
    STATUS_METADATA_CHANGED,
    STATUS_MISSING_MARKDOWN,
    STATUS_MISSING_SOURCE,
    STATUS_ORPHANED_INDEX,
    STATUS_SOURCE_CHANGED,
    STATUS_SOURCE_UNCHECKED,
    STATUS_STALE_MARKDOWN,
    STATUS_UNVERIFIED_INDEXED,
    ItemObservation,
    build_observations,
    classify_item,
    load_attachment_inventory,
    load_index_records,
    load_mapping_snapshot,
)
from .lock import pipeline_write_lock
from .provenance_reconvert import (
    BUCKET_ELIGIBLE,
    CONVERSION_FIELDS,
    OUTCOME_ALREADY_RESOLVED,
    OUTCOME_MISSING_PDF,
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_NOT_IN_PLAN,
    OUTCOME_PLAN_STALE,
    OUTCOME_PUBLISHED,
    OUTCOME_ZOTERO_CHANGED,
    claim_run_dir,
    current_source_path,
    is_within,
    judge_conversion,
    read_conversion_manifest,
    read_inventory,
    reconversion_bucket,
    same_path,
    write_csv,
    zotero_change,
)
from .zotero_db import AttachmentRecord

PLAN_VERSION = 1
PLAN_FILENAME = "plan.json"
PLAN_CSV_FILENAME = "plan.csv"
APPLY_LOG_FILENAME = "apply_log.jsonl"
PLANS_DIRNAME = "index-repair"
ORDINAL_FIELD = "plan_ordinal"
COMMAND = "apply-index-repair"
# Bounded diagnostics: the plan summary names at most this many example keys per reason. The
# full list is always in plan.json / plan.csv.
EXAMPLES_PER_REASON = 5

GROUP_SAFE = "safe"
GROUP_RECONVERT = "reconvert"
GROUP_REVIEW = "review"
GROUPS: tuple[str, ...] = (GROUP_SAFE, GROUP_RECONVERT, GROUP_REVIEW)
APPLICABLE_GROUPS: tuple[str, ...] = (GROUP_SAFE, GROUP_RECONVERT)

ACTION_REFRESH_METADATA = "refresh_metadata"
ACTION_RECONVERT = "reconvert"

OUTCOME_METADATA_REFRESHED = "metadata_refreshed"
OUTCOME_REMOVED = "removed"
OUTCOME_SOURCE_CHANGED = "source_changed"

# Statuses that put an indexed record in `review`, in the order their reason is reported. Each is
# a question only a person (or a fresh dry-run) can settle: acting on any of them automatically
# would either guess at identity or drop a record Zotero may still represent.
_REVIEW_REASONS: dict[str, str] = {
    STATUS_MEMBERSHIP_UNCHECKED: "Zotero's attachment inventory could not be read; close Zotero or wait for sync, then plan again.",
    STATUS_ORPHANED_INDEX: (
        "Zotero no longer lists this attachment. Its index record can be removed only by naming "
        "the key in apply-index-repair --remove-keys."
    ),
    STATUS_DUPLICATE_KEY: "The published index holds more than one record for this attachment; rebuild or inspect it.",
    STATUS_MAPPING_AMBIGUOUS: "The mapping snapshot matches this attachment more than once; resolve the duplicate first.",
    STATUS_UNVERIFIED_INDEXED: "The indexed identity was never verified; use verify-unverified / apply-verification first.",
    STATUS_MISSING_SOURCE: "The PDF is not on disk; restore or relink it in Zotero, re-run dry-run, then plan again.",
    STATUS_MISSING_MARKDOWN: (
        "The indexed Markdown file is gone (search still serves the indexed text); restore it, "
        "or reconvert deliberately."
    ),
    STATUS_SOURCE_UNCHECKED: (
        "The source PDF could not be compared with the indexed hash (relinked, or not in the "
        "mapping snapshot); re-run dry-run or plan with --full."
    ),
}
_RECONVERT_STATUSES: tuple[str, ...] = (STATUS_STALE_MARKDOWN, STATUS_SOURCE_CHANGED)
REASON_RECONVERT_BLOCKED = "reconvert_blocked"
REASON_PARENT_CHANGED = "parent_changed"
REASON_RELINKED = "relinked"
REASON_SOURCE_UNREADABLE = "source_unreadable"
REASON_METADATA_VALUE_REMOVED = "metadata_value_removed"
REASON_MARKDOWN_UNVERIFIED = "markdown_unverified"

_GROUP_ADVICE = {
    GROUP_SAFE: "Refresh metadata in place with apply-index-repair --group safe.",
    GROUP_RECONVERT: "Reconvert and replace with apply-index-repair --group reconvert (optionally --limit).",
    GROUP_REVIEW: "Needs a decision; never applied automatically. See each row's reason.",
}


class IndexRepairError(RuntimeError):
    """A repair plan cannot be built, read, or applied as given."""


# --------------------------------------------------------------------------------------
# Plan (read-only)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairRow:
    attachment_key: str
    group: str
    # The audit statuses of this attachment, exactly as audit-library reports them.
    statuses: tuple[str, ...]
    # A stable code to group rows by (a status name or one of the REASON_* codes) and its text.
    reason_code: str
    reason: str
    action: str
    # Only ever True for an `orphaned_index` review row with a single index record: the one
    # review decision apply-index-repair can carry out, and only when named in --remove-keys.
    removable: bool
    # 1-based position among reconvert rows; numbers the row's output in the run directory.
    ordinal: int | None
    zotero_parent_key: str
    title: str
    source_path: str
    source_bytes: int | None
    page_count: int | None
    indexed_markdown_path: str
    indexed_markdown_sha256: str
    indexed_source_sha256: str
    indexed_metadata: dict[str, str] = field(default_factory=dict)
    # Zotero's current values for METADATA_KEYS; the safe fix writes exactly these.
    target_metadata: dict[str, str] | None = None
    # The mapping row the converter receives (reconvert rows only).
    conversion_row: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RepairRow:
        def _str_dict(value: object) -> dict[str, str] | None:
            return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else None

        ordinal = data.get("ordinal")
        statuses = data.get("statuses")
        return cls(
            attachment_key=str(data["attachment_key"]),
            group=str(data["group"]),
            statuses=tuple(str(s) for s in statuses) if isinstance(statuses, (list, tuple)) else (),
            reason_code=str(data.get("reason_code") or ""),
            reason=str(data.get("reason") or ""),
            action=str(data.get("action") or ""),
            removable=data.get("removable") is True,
            ordinal=ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else None,
            zotero_parent_key=str(data.get("zotero_parent_key") or ""),
            title=str(data.get("title") or ""),
            source_path=str(data.get("source_path") or ""),
            source_bytes=_optional_int(data.get("source_bytes")),
            page_count=_optional_int(data.get("page_count")),
            indexed_markdown_path=str(data.get("indexed_markdown_path") or ""),
            indexed_markdown_sha256=str(data.get("indexed_markdown_sha256") or ""),
            indexed_source_sha256=str(data.get("indexed_source_sha256") or ""),
            indexed_metadata=_str_dict(data.get("indexed_metadata")) or {},
            target_metadata=_str_dict(data.get("target_metadata")),
            conversion_row=_str_dict(data.get("conversion_row")),
        )


@dataclass(frozen=True)
class RepairPlan:
    plan_id: str
    created_at: str
    mapping_report: str
    generation_id: str | None
    full_audit: bool
    inventory_available: bool
    inventory_error: str | None
    run_dir: str
    rows: tuple[RepairRow, ...]
    # Findings on attachments with no index record (e.g. `unindexed`). Not index repairs: they
    # are counted so the plan accounts for every audit finding, and pointed at convert-new.
    not_indexed: dict[str, int] = field(default_factory=dict)
    plan_version: int = PLAN_VERSION

    @property
    def counts(self) -> dict[str, int]:
        counts = Counter(row.group for row in self.rows)
        return {group: counts.get(group, 0) for group in GROUPS}

    @property
    def diagnostics(self) -> dict[str, dict[str, object]]:
        """Per group: counts by reason code, each with at most EXAMPLES_PER_REASON example keys."""
        result: dict[str, dict[str, object]] = {}
        for group in GROUPS:
            by_reason: dict[str, dict[str, object]] = {}
            for row in self.rows:
                if row.group != group:
                    continue
                entry = by_reason.setdefault(row.reason_code, {"count": 0, "examples": []})
                entry["count"] = int(str(entry["count"])) + 1
                examples = entry["examples"]
                if isinstance(examples, list) and len(examples) < EXAMPLES_PER_REASON:
                    examples.append(row.attachment_key)
            result[group] = {"count": self.counts[group], "by_reason": by_reason}
        return result

    @property
    def estimate(self) -> dict[str, int]:
        rows = [row for row in self.rows if row.group == GROUP_RECONVERT]
        return {
            "reconvert_rows": len(rows),
            "source_bytes": sum(row.source_bytes or 0 for row in rows),
            "pages": sum(row.page_count or 0 for row in rows),
            "rows_without_page_count": sum(1 for row in rows if not row.page_count),
        }

    def summary(self) -> dict[str, object]:
        return {
            "plan_version": self.plan_version,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "mapping_report": self.mapping_report,
            "generation_id": self.generation_id,
            "full_audit": self.full_audit,
            "inventory_available": self.inventory_available,
            "inventory_error": self.inventory_error,
            "run_dir": self.run_dir,
            "counts": self.counts,
            "removable": sum(1 for row in self.rows if row.removable),
            "diagnostics": self.diagnostics,
            "estimate": self.estimate,
            "not_indexed": dict(self.not_indexed),
            "advice": {group: _GROUP_ADVICE[group] for group in GROUPS if self.counts[group]},
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.summary(), "rows": [asdict(row) for row in self.rows]}


def build_plan(
    config: ProjectConfig,
    mapping_report: Path,
    *,
    index_root: Path | None = None,
    plan_id: str | None = None,
    full_audit: bool = False,
) -> RepairPlan:
    """Group every indexed attachment with an audit finding. Reads only; writes nothing."""
    root = index_root if index_root is not None else config.output_root / "index"
    # Timestamp for readability, random suffix so two plans can never share a run directory.
    plan_id = plan_id or f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
    mapping_rows = load_mapping_snapshot(mapping_report)
    generation_id, _published_at, index_rows = load_index_records(root)
    inventory: dict[str, AttachmentRecord] = {}
    inventory_available = False
    inventory_error: str | None = None
    try:
        inventory = load_attachment_inventory(config.zotero_sqlite)
        inventory_available = True
    except Exception as exc:  # same degradation as audit_library: report, never guess
        inventory_error = f"{type(exc).__name__}: {exc}"

    observations = build_observations(
        config,
        mapping_rows=mapping_rows,
        index_rows=index_rows,
        inventory=inventory,
        inventory_available=inventory_available,
        full_audit=full_audit,
    )
    rows: list[RepairRow] = []
    not_indexed: Counter[str] = Counter()
    ordinal = 0
    for obs in observations:
        statuses = classify_item(obs)
        findings = statuses - {STATUS_CURRENT}
        if not findings:
            continue
        if not obs.in_index:
            not_indexed.update(findings)
            continue
        ordered = tuple(status for status in ALL_STATUSES if status in statuses)
        indexed = index_rows[obs.attachment_key][0]
        attachment = inventory.get(obs.attachment_key)
        row = _plan_row(
            obs,
            ordered,
            indexed,
            mapping_rows.get(obs.attachment_key, []),
            attachment,
            current_source=current_source_path(attachment, config),
        )
        if row.group == GROUP_RECONVERT:
            ordinal += 1
            row = replace(row, ordinal=ordinal)
        rows.append(row)
    return RepairPlan(
        plan_id=plan_id,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        mapping_report=str(mapping_report),
        generation_id=generation_id,
        full_audit=full_audit,
        inventory_available=inventory_available,
        inventory_error=inventory_error,
        run_dir=str(config.output_root / "conversion-runs" / PLANS_DIRNAME / plan_id),
        rows=tuple(rows),
        not_indexed={status: not_indexed[status] for status in ALL_STATUSES if not_indexed[status]},
    )


def _plan_row(
    obs: ItemObservation,
    statuses: tuple[str, ...],
    indexed: dict[str, object],
    candidates: list[dict[str, object]],
    attachment: AttachmentRecord | None,
    *,
    current_source: str,
) -> RepairRow:
    indexed_source = _text(indexed, "source_path")
    source_file = Path(indexed_source) if indexed_source else None
    source_exists = source_file.is_file() if source_file else False

    def row(
        group: str,
        reason_code: str,
        reason: str,
        *,
        action: str = "",
        removable: bool = False,
        mapping: dict[str, object] | None = None,
        target: dict[str, str] | None = None,
        conversion_row: dict[str, str] | None = None,
    ) -> RepairRow:
        return RepairRow(
            attachment_key=obs.attachment_key,
            group=group,
            statuses=statuses,
            reason_code=reason_code,
            reason=reason,
            action=action,
            removable=removable,
            ordinal=None,
            zotero_parent_key=_text(indexed, "zotero_parent_key"),
            title=_text(indexed, "title") or obs.title,
            source_path=indexed_source,
            source_bytes=_file_size(source_file) if source_exists and source_file else None,
            page_count=_optional_int(_text(mapping, "page_count") or _text(indexed, "page_count")),
            indexed_markdown_path=_text(indexed, "markdown_path"),
            indexed_markdown_sha256=_text(indexed, "markdown_sha256"),
            indexed_source_sha256=_text(indexed, "source_sha256"),
            indexed_metadata=dict(obs.indexed_metadata),
            target_metadata=target,
            conversion_row=conversion_row,
        )

    for status, reason in _REVIEW_REASONS.items():
        if status in statuses:
            removable = status == STATUS_ORPHANED_INDEX and STATUS_DUPLICATE_KEY not in statuses
            return row(GROUP_REVIEW, status, reason, removable=removable)

    triggers = [status for status in _RECONVERT_STATUSES if status in statuses]
    measured_note = ""
    indexed_source_hash = _text(indexed, "source_sha256")
    if not triggers and STATUS_METADATA_CHANGED in statuses and indexed_source_hash and source_file and source_exists:
        # The audit compared the snapshot's PDF hash, which predates any in-place edit made after
        # the dry-run. A metadata-only refresh keeps the text and its source hash, so it must be
        # sure the PDF is still the one that text came from: measure it now.
        current_hash = _sha256_or_none(source_file)
        if current_hash is None:
            return row(GROUP_REVIEW, REASON_SOURCE_UNREADABLE, "The PDF could not be read to confirm it is unchanged.")
        if current_hash != indexed_source_hash:
            triggers = [STATUS_SOURCE_CHANGED]
            measured_note = " (measured now: the PDF changed after the mapping snapshot)"
    if triggers:
        bucket, bucket_reason, mapping = reconversion_bucket(
            obs, indexed, candidates, source_exists, current_source=current_source
        )
        if bucket != BUCKET_ELIGIBLE or mapping is None:
            return row(GROUP_REVIEW, REASON_RECONVERT_BLOCKED, f"{bucket}: {bucket_reason}{measured_note}")
        removed = _removed_values(obs)
        if removed:
            return row(GROUP_REVIEW, REASON_METADATA_VALUE_REMOVED, _removed_reason(removed))
        conversion_row = {name: _text(mapping, name) for name in CONVERSION_FIELDS}
        # Reconvert exactly the file the old record was indexed from (the replacement path only
        # accepts the same source path), carrying Zotero's current citation metadata -- all of
        # it, as Zotero holds it -- so a reconverted record neither reintroduces the drift the
        # audit reported nor revives a value from the older snapshot.
        conversion_row["source_path"] = indexed_source
        conversion_row.update(obs.zotero_metadata)
        return row(
            GROUP_RECONVERT,
            triggers[0],
            f"{' and '.join(triggers)}{measured_note}: re-extract the PDF and replace the record.",
            action=ACTION_RECONVERT,
            mapping=mapping,
            conversion_row=conversion_row,
        )

    if STATUS_METADATA_CHANGED in statuses:
        target = dict(obs.zotero_metadata)
        if attachment is None or not obs.in_zotero:
            # Unreachable while membership statuses are reviewed first; kept so a change there
            # cannot turn a snapshot's historical metadata into a "current" refresh.
            return row(GROUP_REVIEW, STATUS_MEMBERSHIP_UNCHECKED, _REVIEW_REASONS[STATUS_MEMBERSHIP_UNCHECKED])
        if (attachment.parent_key or "") != _text(indexed, "zotero_parent_key"):
            return row(
                GROUP_REVIEW,
                REASON_PARENT_CHANGED,
                "Zotero now files this attachment under a different parent item; the record's "
                "identity changed, not just its metadata.",
            )
        if not same_path(current_source, indexed_source):
            return row(
                GROUP_REVIEW,
                REASON_RELINKED,
                "Zotero's current path for this attachment is not the PDF the record was indexed "
                "from; a metadata refresh would describe a different file.",
            )
        removed = _removed_values(obs)
        if removed:
            return row(GROUP_REVIEW, REASON_METADATA_VALUE_REMOVED, _removed_reason(removed))
        if obs.markdown_exists is not True or obs.markdown_sha256_current != obs.indexed_markdown_sha256:
            return row(GROUP_REVIEW, REASON_MARKDOWN_UNVERIFIED, "The indexed Markdown could not be verified against its hash.")
        return row(
            GROUP_SAFE,
            STATUS_METADATA_CHANGED,
            "Zotero's title/DOI/citation key differ from the indexed record; refresh them in place.",
            action=ACTION_REFRESH_METADATA,
            target=target,
        )

    # No rule above claimed it. Unreachable for today's statuses; reported rather than dropped.
    return row(GROUP_REVIEW, "unclassified", f"No repair rule covers {', '.join(statuses)}.")


def _removed_values(obs: ItemObservation) -> list[str]:
    """Citation fields the record has and Zotero's current record does not.

    Applied to both applicable groups: neither a refresh nor a reconversion drops a value from
    the index on the strength of one blank field, and neither revives it from an older snapshot.
    """
    if not obs.in_zotero:
        return []
    return sorted(key for key in METADATA_KEYS if obs.indexed_metadata.get(key) and not obs.zotero_metadata.get(key))


def _removed_reason(removed: list[str]) -> str:
    return (
        f"Zotero's record no longer has {', '.join(removed)}; confirm the edit in Zotero before the "
        "index drops a value."
    )


def _sha256_or_none(path: Path) -> str | None:
    try:
        return _sha256(path)
    except OSError:
        return None


def _source_drift(row: RepairRow) -> RepairOutcome | None:
    """Refuse a safe row whose PDF no longer has the hash its indexed text was extracted from.

    With no indexed hash there is nothing to compare: the metadata refresh leaves the text's
    (unknown) provenance exactly as it was, so it is still allowed.
    """
    if not row.indexed_source_sha256:
        return None
    current = _sha256_or_none(Path(row.source_path))
    if current is None:
        return RepairOutcome(row.attachment_key, GROUP_SAFE, OUTCOME_MISSING_PDF, "The PDF can no longer be read.")
    if current != row.indexed_source_sha256:
        return RepairOutcome(
            row.attachment_key,
            GROUP_SAFE,
            OUTCOME_SOURCE_CHANGED,
            "The PDF changed since its text was indexed; plan again so it is reconverted instead.",
        )
    return None


def write_plan(plan: RepairPlan, plan_dir: Path) -> Path:
    """Write ``plan.json`` and a reviewer-friendly ``plan.csv``; refuses to overwrite a plan."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / PLAN_FILENAME
    if plan_path.exists():
        raise IndexRepairError(f"A plan already exists at {plan_path}; choose another directory.")
    if Path(plan.run_dir).exists():
        raise IndexRepairError(f"Run directory {plan.run_dir} already exists and may belong to another plan; plan again.")
    csv_fields = ("group", "attachment_key", "reason_code", "statuses", "removable", "ordinal", "reason", "title", "source_path")
    with (plan_dir / PLAN_CSV_FILENAME).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for row in plan.rows:
            data = asdict(row)
            data["statuses"] = ";".join(row.statuses)
            writer.writerow({name: "" if data[name] is None else data[name] for name in csv_fields})
    plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return plan_path


def default_plan_dir(config: ProjectConfig, plan_id: str) -> Path:
    return config.output_root / PLANS_DIRNAME / plan_id


def load_plan(path: Path) -> tuple[RepairPlan, Path]:
    """Read a plan from ``plan.json`` or the directory holding it; returns it with its directory."""
    plan_path = path / PLAN_FILENAME if path.is_dir() else path
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IndexRepairError(f"Cannot read plan {plan_path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("plan_version") != PLAN_VERSION or "counts" not in data:
        raise IndexRepairError(f"{plan_path} is not a version-{PLAN_VERSION} index repair plan.")
    rows = tuple(RepairRow.from_dict(row) for row in data.get("rows", []) if isinstance(row, dict))
    if any(row.group not in GROUPS for row in rows):
        raise IndexRepairError(f"{plan_path} names an unknown repair group.")
    not_indexed = data.get("not_indexed")
    plan = RepairPlan(
        plan_id=str(data["plan_id"]),
        created_at=str(data.get("created_at") or ""),
        mapping_report=str(data.get("mapping_report") or ""),
        generation_id=str(data["generation_id"]) if data.get("generation_id") else None,
        full_audit=bool(data.get("full_audit")),
        inventory_available=bool(data.get("inventory_available")),
        inventory_error=str(data["inventory_error"]) if data.get("inventory_error") else None,
        run_dir=str(data["run_dir"]),
        rows=rows,
        not_indexed={str(k): int(v) for k, v in not_indexed.items()} if isinstance(not_indexed, dict) else {},
    )
    return plan, plan_path.parent


# --------------------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairOutcome:
    attachment_key: str
    group: str
    outcome: str
    reason: str = ""


@dataclass
class RepairReport:
    plan_id: str
    run_dir: str
    previous_generation_id: str | None
    generation_id: str | None
    selected: int
    remaining: int
    rows: list[RepairOutcome] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(row.outcome for row in self.rows))

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "run_dir": self.run_dir,
            "previous_generation_id": self.previous_generation_id,
            "generation_id": self.generation_id,
            "published": self.generation_id is not None,
            "selected": self.selected,
            "remaining": self.remaining,
            "counts": self.counts,
            "rows": [asdict(row) for row in self.rows],
        }


def apply_plan(
    config: ProjectConfig,
    plan_path: Path,
    *,
    groups: list[str] | None = None,
    keys: list[str] | None = None,
    remove_keys: list[str] | None = None,
    limit: int | None = None,
    workers: int | None = None,
    timeout_seconds: int = 600,
) -> RepairReport:
    """Apply a selection of a plan's safe/reconvert rows and approved removals as one generation.

    Nothing is applied without a selection: ``groups`` (``safe``/``reconvert``), ``keys``, or
    ``remove_keys``. ``keys`` narrows the groups; ``limit`` caps the safe/reconvert rows in plan
    order. ``remove_keys`` names ``orphaned_index`` review rows whose index record the operator has
    decided to drop.

    Under the pipeline write lock: an interrupted earlier publication is finished or rolled back
    first, then each row is re-validated against the current generation (a record changed since
    planning is ``plan_stale``, one already repaired is ``already_resolved``) and against a fresh
    read of Zotero (``zotero_changed``). Zotero is read again after any reconversion, immediately
    before publishing, and must be readable: nothing is applied on membership or metadata nobody
    could confirm. Everything not repaired is carried into the new generation verbatim.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    if groups is not None and any(group not in APPLICABLE_GROUPS for group in groups):
        raise ValueError(f"groups must be among {', '.join(APPLICABLE_GROUPS)}; review rows are never applied.")
    if not groups and not keys and not remove_keys:
        raise IndexRepairError("Nothing selected: pass --group, --keys, or --remove-keys.")
    plan, plan_dir = load_plan(plan_path)
    run_dir = Path(plan.run_dir)
    if not is_within(run_dir, config.output_root) or not is_within(plan_dir, config.output_root):
        raise IndexRepairError(
            f"Plan {plan.plan_id} lives outside the configured output_root ({config.output_root}); "
            "the pipeline lock only covers writes inside output_root."
        )
    index_root = config.output_root / "index"
    with pipeline_write_lock(config.output_root, command=COMMAND):
        # Finish or roll back an interrupted publication before reading the current generation,
        # so this successor is never built from a JSONL that a pending publication supersedes.
        recover_pending_publication(index_root)
        previous_generation, _published_at, index_rows = load_index_records(index_root)
        current_jsonl = current_generation_jsonl(index_root)
        if previous_generation is None or current_jsonl is None:
            raise IndexRepairError("No managed index generation is published; run rebuild-index first.")

        outcomes: list[RepairOutcome] = []
        candidates = _select(plan, groups=groups, keys=keys, outcomes=outcomes)
        removal_rows = _select_removals(plan, remove_keys or [], outcomes)
        inventory = read_inventory(config) if candidates or removal_rows else {}

        selectable: list[RepairRow] = []
        for row in candidates:
            current = index_rows.get(row.attachment_key, [])
            if row.group == GROUP_SAFE:
                skip = _revalidate_safe(row, current)
            else:
                skip = _revalidate_reconvert(row, current, run_dir)
            skip = skip or _zotero_recheck(row, inventory, config)
            if skip is not None:
                outcomes.append(skip)
            else:
                selectable.append(row)
        selected = selectable if limit is None else selectable[:limit]
        for row in removal_rows:
            skip = _revalidate_removal(row, index_rows.get(row.attachment_key, [])) or _zotero_recheck(
                row, inventory, config
            )
            if skip is not None:
                outcomes.append(skip)
            else:
                selected.append(row)

        report = RepairReport(
            plan_id=plan.plan_id,
            run_dir=str(run_dir),
            previous_generation_id=previous_generation,
            generation_id=None,
            selected=len(selected),
            remaining=len(selectable) - len([row for row in selected if row.group != GROUP_REVIEW]),
        )

        reconvert_rows = [row for row in selected if row.group == GROUP_RECONVERT]
        results: dict[str, dict[str, str]] = {}
        if reconvert_rows:
            claim_run_dir(run_dir, plan, plan_dir, config.output_root)
            results = _convert(config, reconvert_rows, plan_dir=plan_dir, run_dir=run_dir, workers=workers, timeout_seconds=timeout_seconds)
            # Extraction can take hours: consult Zotero again so an attachment deleted, relinked
            # or re-parented meanwhile is not published as the current one.
            inventory = read_inventory(config)

        replacements: dict[str, TextIndexRecord] = {}
        metadata_updates: dict[str, dict[str, str]] = {}
        removals: set[str] = set()
        for row in selected:
            key = row.attachment_key
            change = _zotero_recheck(row, inventory, config)
            if change is None and row.group == GROUP_SAFE:
                # Measured again at publication: reconversions in this invocation can take hours.
                change = _source_drift(row)
            if change is not None:
                outcomes.append(change)
            elif row.group == GROUP_SAFE and row.target_metadata is not None:
                metadata_updates[key] = dict(row.target_metadata)
                outcomes.append(RepairOutcome(key, GROUP_SAFE, OUTCOME_METADATA_REFRESHED))
            elif row.group == GROUP_RECONVERT:
                result = results.get(key)
                judged = judge_conversion(key, result, index_rows[key][0])
                if judged.outcome == OUTCOME_PUBLISHED and result is not None:
                    replacements[key] = _record_from_manifest_row(result)
                outcomes.append(RepairOutcome(key, GROUP_RECONVERT, judged.outcome, judged.reason))
            elif row.group == GROUP_REVIEW:
                removals.add(key)
                outcomes.append(RepairOutcome(key, GROUP_REVIEW, OUTCOME_REMOVED))

        if metadata_updates or replacements or removals:
            writer = write_jsonl_applying_repairs(
                current_jsonl, replacements=replacements, metadata_updates=metadata_updates, removals=removals
            )
            info = stage_and_publish(index_root, writer, command=COMMAND)
            report.generation_id = info.generation_id
        report.rows = outcomes
        _append_log(plan_dir, report)
        return report


def _select(
    plan: RepairPlan, *, groups: list[str] | None, keys: list[str] | None, outcomes: list[RepairOutcome]
) -> list[RepairRow]:
    wanted_groups = set(groups) if groups else set(APPLICABLE_GROUPS)
    by_key = {row.attachment_key: row for row in plan.rows}
    rows = [row for row in plan.rows if row.group in wanted_groups]
    if keys:
        wanted = list(dict.fromkeys(keys))
        for key in wanted:
            row = by_key.get(key)
            if row is None:
                outcomes.append(RepairOutcome(key, "", OUTCOME_NOT_IN_PLAN, "This plan has no finding for this key."))
            elif row.group == GROUP_REVIEW:
                hint = " Name it in --remove-keys to drop its index record." if row.removable else ""
                outcomes.append(RepairOutcome(key, row.group, OUTCOME_NOT_ELIGIBLE, f"{row.reason_code}: {row.reason}{hint}"))
            elif row.group not in wanted_groups:
                outcomes.append(RepairOutcome(key, row.group, OUTCOME_NOT_ELIGIBLE, f"In group {row.group}, not selected by --group."))
        rows = [row for row in rows if row.attachment_key in set(wanted)]
    elif not groups:
        rows = []  # only --remove-keys was given
    return sorted(rows, key=lambda row: (APPLICABLE_GROUPS.index(row.group), row.ordinal or 0, row.attachment_key))


def _select_removals(plan: RepairPlan, remove_keys: list[str], outcomes: list[RepairOutcome]) -> list[RepairRow]:
    by_key = {row.attachment_key: row for row in plan.rows}
    selected: list[RepairRow] = []
    for key in dict.fromkeys(remove_keys):
        row = by_key.get(key)
        if row is None:
            outcomes.append(RepairOutcome(key, "", OUTCOME_NOT_IN_PLAN, "This plan has no finding for this key."))
        elif not row.removable:
            outcomes.append(
                RepairOutcome(
                    key,
                    row.group,
                    OUTCOME_NOT_ELIGIBLE,
                    "Only an orphaned_index record (Zotero no longer lists the attachment, one index "
                    f"record) can be removed; this row is {row.group}/{row.reason_code}.",
                )
            )
        else:
            selected.append(row)
    return selected


def _revalidate_safe(row: RepairRow, current: list[dict[str, object]]) -> RepairOutcome | None:
    """Why a safe row must not be applied now, judged against the current generation and disk."""
    key = row.attachment_key

    def stale(reason: str) -> RepairOutcome:
        return RepairOutcome(key, GROUP_SAFE, OUTCOME_PLAN_STALE, reason)

    if row.target_metadata is None:
        return stale("The plan row carries no target metadata.")
    if len(current) != 1:
        return stale(f"The current generation holds {len(current)} records for this key.")
    record = current[0]
    if all(_text(record, name) == value for name, value in row.target_metadata.items()):
        return RepairOutcome(key, GROUP_SAFE, OUTCOME_ALREADY_RESOLVED, "The record already carries Zotero's metadata.")
    if (
        _text(record, "markdown_sha256") != row.indexed_markdown_sha256
        or _text(record, "source_sha256") != row.indexed_source_sha256
        or _text(record, "zotero_parent_key") != row.zotero_parent_key
        or not same_path(_text(record, "source_path"), row.source_path)
        or any(_text(record, name) != value for name, value in row.indexed_metadata.items())
    ):
        return stale("The indexed record changed after the plan was made; plan again.")
    try:
        if _sha256(Path(row.indexed_markdown_path)) != row.indexed_markdown_sha256:
            return stale("The indexed Markdown changed on disk after the plan was made; plan again.")
    except OSError:
        return stale("The indexed Markdown can no longer be read; plan again.")
    return _source_drift(row)


def _revalidate_reconvert(row: RepairRow, current: list[dict[str, object]], run_dir: Path) -> RepairOutcome | None:
    """Why a reconvert row must not be converted now, judged against the current generation."""
    key = row.attachment_key

    def stale(reason: str) -> RepairOutcome:
        return RepairOutcome(key, GROUP_RECONVERT, OUTCOME_PLAN_STALE, reason)

    if row.conversion_row is None or row.ordinal is None:
        return stale("The plan row carries no conversion input.")
    if len(current) != 1:
        return stale(f"The current generation holds {len(current)} records for this key.")
    record = current[0]
    if (
        _text(record, "markdown_sha256") != row.indexed_markdown_sha256
        or _text(record, "source_sha256") != row.indexed_source_sha256
        or _text(record, "zotero_parent_key") != row.zotero_parent_key
        or not same_path(_text(record, "source_path"), row.source_path)
    ):
        markdown_path = _text(record, "markdown_path")
        if markdown_path and _text(record, "source_sha256") and is_within(Path(markdown_path), run_dir):
            return RepairOutcome(key, GROUP_RECONVERT, OUTCOME_ALREADY_RESOLVED, "Already replaced by this plan's reconversion.")
        return stale("The indexed record changed after the plan was made; plan again.")
    if not Path(row.source_path).is_file():
        return RepairOutcome(key, GROUP_RECONVERT, OUTCOME_MISSING_PDF, "The PDF is no longer on disk.")
    return None


def _revalidate_removal(row: RepairRow, current: list[dict[str, object]]) -> RepairOutcome | None:
    key = row.attachment_key
    if not current:
        return RepairOutcome(key, GROUP_REVIEW, OUTCOME_ALREADY_RESOLVED, "The current generation holds no record for this key.")
    if len(current) != 1 or _text(current[0], "markdown_sha256") != row.indexed_markdown_sha256:
        return RepairOutcome(key, GROUP_REVIEW, OUTCOME_PLAN_STALE, "The indexed record changed after the plan was made; plan again.")
    return None


def _zotero_recheck(
    row: RepairRow, inventory: dict[str, AttachmentRecord], config: ProjectConfig
) -> RepairOutcome | None:
    """Why Zotero, as read now, no longer supports applying this row -- or None."""
    key = row.attachment_key
    record = inventory.get(key)
    if row.group == GROUP_REVIEW:
        if record is not None:
            return RepairOutcome(key, GROUP_REVIEW, OUTCOME_ZOTERO_CHANGED, "Zotero lists this attachment again; its record is kept.")
        return None
    change = zotero_change(row, record, config)
    if change is not None:
        return RepairOutcome(key, row.group, change.outcome, change.reason)
    # The citation metadata this row will publish -- the safe refresh's target, or what the
    # reconversion was given -- must still be Zotero's, or the new generation would re-report
    # `metadata_changed` the moment it is published. Rejected rather than patched: replan.
    planned = row.target_metadata if row.group == GROUP_SAFE else row.conversion_row
    if record is not None and planned is not None:
        expected = {name: planned.get(name, "") for name in METADATA_KEYS}
        current = {name: str(getattr(record, name, "") or "") for name in METADATA_KEYS}
        if current != expected:
            return RepairOutcome(key, row.group, OUTCOME_ZOTERO_CHANGED, "Zotero's metadata changed after the plan was made; plan again.")
    return None


def _convert(
    config: ProjectConfig,
    rows: list[RepairRow],
    *,
    plan_dir: Path,
    run_dir: Path,
    workers: int | None,
    timeout_seconds: int,
) -> dict[str, dict[str, str]]:
    """Convert the rows into the plan's claimed run directory; return its manifest rows by key."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    selection_csv = plan_dir / "selections" / f"{stamp}_selection.csv"
    write_csv(
        selection_csv,
        [{**(row.conversion_row or {}), ORDINAL_FIELD: str(row.ordinal)} for row in rows],
        fieldnames=[*CONVERSION_FIELDS, ORDINAL_FIELD],
    )
    convert_planned_rows(config, selection_csv, run_dir, ordinal_field=ORDINAL_FIELD, workers=workers, timeout_seconds=timeout_seconds)
    return read_conversion_manifest(run_dir / "manifest.csv")


def _append_log(plan_dir: Path, report: RepairReport) -> None:
    entry = {"logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **report.to_dict()}
    plan_dir.mkdir(parents=True, exist_ok=True)
    with (plan_dir / APPLY_LOG_FILENAME).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _text(row: dict[str, object] | None, key: str) -> str:
    if not row:
        return ""
    value = row.get(key)
    return "" if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None
