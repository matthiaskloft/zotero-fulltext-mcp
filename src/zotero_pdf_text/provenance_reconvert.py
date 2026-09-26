"""Targeted reconversion of indexed records whose source provenance is unknown.

Records converted before the pipeline recorded ``source_sha256`` -- or reused through
``skipped_existing`` -- carry no hash of the PDF their text came from. ``library_status`` reports
them as ``source_provenance_unknown``. Rebuilding the index cannot repair that: the Markdown was
extracted from whatever the PDF was back then, and hashing today's file would pair old text with
a new hash, which is false assurance rather than provenance.

The only honest repair is to extract the text again and record the hash measured around that
extraction. This module does it in two steps:

- :func:`build_plan` is read-only. It joins the published generation's provenance-unknown records
  to Zotero's attachment inventory and the dry-run mapping snapshot, sorts every record into one
  bucket (``eligible``, ``missing_pdf``, ``identity_uncertain``, ``not_in_zotero``,
  ``membership_unchecked``) with a reason, and writes a stable plan with counts and a size/page
  estimate. Only a ``mapped_verified`` identity whose PDF is still the one the record was indexed
  from is ``eligible``.
- :func:`apply_plan` reconverts a selection of eligible rows into a dedicated run directory with
  the ordinary converter -- checkpointed, so an interrupted invocation resumes -- and publishes
  only the successful, verified, hash-bearing results through the validated replacement path
  (:func:`artifacts.replacement_rejection` + :func:`artifacts.write_jsonl_replacing_manifest`).
  A row that fails, or whose identity or source can no longer be confirmed, leaves its old
  indexed record untouched.

Nothing here writes to Zotero; its database is only read, through a snapshot copy.
"""

from __future__ import annotations

import csv
import json
import secrets
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import (
    REPLACEABLE_IDENTITY_STATUSES,
    current_generation_jsonl,
    recover_pending_publication,
    replacement_rejection,
    stage_and_publish,
    write_jsonl_replacing_manifest,
)
from .config import ProjectConfig
from .converter import convert_planned_rows
from .library import (
    ELIGIBLE_CLASSIFICATION,
    ItemObservation,
    build_observations,
    inventory_source_path,
    load_attachment_inventory,
    load_index_records,
    load_mapping_snapshot,
)
from .lock import pipeline_write_lock
from .zotero_db import AttachmentRecord

PLAN_VERSION = 1
PLAN_FILENAME = "plan.json"
PLAN_CSV_FILENAME = "plan.csv"
APPLY_LOG_FILENAME = "apply_log.jsonl"
# Written into a run directory by the first apply of a plan. A run directory holds Markdown the
# published index may reference, so it belongs to exactly one plan and is never reused by another.
RUN_CLAIM_FILENAME = "provenance_plan.json"
PLANS_DIRNAME = "provenance-reconvert"
ORDINAL_FIELD = "plan_ordinal"
COMMAND = "apply-provenance-reconvert"

BUCKET_ELIGIBLE = "eligible"
BUCKET_MISSING_PDF = "missing_pdf"
BUCKET_IDENTITY_UNCERTAIN = "identity_uncertain"
BUCKET_NOT_IN_ZOTERO = "not_in_zotero"
BUCKET_MEMBERSHIP_UNCHECKED = "membership_unchecked"
BUCKETS: tuple[str, ...] = (
    BUCKET_ELIGIBLE,
    BUCKET_MISSING_PDF,
    BUCKET_IDENTITY_UNCERTAIN,
    BUCKET_NOT_IN_ZOTERO,
    BUCKET_MEMBERSHIP_UNCHECKED,
)

OUTCOME_PUBLISHED = "published"
OUTCOME_CONVERSION_FAILED = "conversion_failed"
OUTCOME_REJECTED = "rejected"
OUTCOME_ALREADY_RESOLVED = "already_resolved"
OUTCOME_PLAN_STALE = "plan_stale"
OUTCOME_MISSING_PDF = "missing_pdf"
OUTCOME_ZOTERO_CHANGED = "zotero_changed"
OUTCOME_NOT_ELIGIBLE = "not_eligible"
OUTCOME_NOT_IN_PLAN = "not_in_plan"

# Mapping-row fields the converter reads. Everything else in a mapping row is evidence for the
# dry-run's identity decision and is not needed to reconvert.
CONVERSION_FIELDS: tuple[str, ...] = (
    "classification",
    "source_path",
    "safe_folder_id",
    "zotero_parent_key",
    "zotero_attachment_key",
    "item_type",
    "title",
    "creators",
    "year",
    "doi",
    "citation_key",
    "page_count",
    "identity_status",
    "identity_rule",
)

_BUCKET_ADVICE = {
    BUCKET_ELIGIBLE: "Reconvert with apply-provenance-reconvert.",
    BUCKET_MISSING_PDF: "Restore or relink the PDF in Zotero, re-run dry-run, then plan again.",
    BUCKET_IDENTITY_UNCERTAIN: (
        "Resolve the identity first (verify-unverified / apply-verification, duplicate cleanup, "
        "or a fresh dry-run after a relink); these records are never reconverted automatically."
    ),
    BUCKET_NOT_IN_ZOTERO: "Zotero no longer lists this attachment; nothing to reconvert (see audit-library orphaned_index).",
    BUCKET_MEMBERSHIP_UNCHECKED: "Zotero's database could not be read; close Zotero or wait for sync, then plan again.",
}


class ProvenancePlanError(RuntimeError):
    """A plan cannot be built, read, or applied as given."""


# --------------------------------------------------------------------------------------
# Plan (read-only)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRow:
    attachment_key: str
    bucket: str
    reason: str
    # 1-based position among eligible rows, stable for the life of the plan. It also numbers
    # the row's Markdown in the run directory, so a row keeps one output path -- and one
    # checkpoint entry -- whichever selection it is converted in. None for other buckets.
    ordinal: int | None
    zotero_parent_key: str
    title: str
    source_path: str
    source_exists: bool | None
    source_bytes: int | None
    page_count: int | None
    indexed_markdown_path: str
    indexed_markdown_sha256: str
    indexed_at: str
    # The mapping row the converter receives. Only present for eligible rows.
    conversion_row: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PlanRow:
        conversion = data.get("conversion_row")
        ordinal = data.get("ordinal")
        source_exists = data.get("source_exists")
        return cls(
            attachment_key=str(data["attachment_key"]),
            bucket=str(data["bucket"]),
            reason=str(data.get("reason") or ""),
            ordinal=int(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool) else None,
            zotero_parent_key=str(data.get("zotero_parent_key") or ""),
            title=str(data.get("title") or ""),
            source_path=str(data.get("source_path") or ""),
            source_exists=source_exists if isinstance(source_exists, bool) else None,
            source_bytes=_optional_int(data.get("source_bytes")),
            page_count=_optional_int(data.get("page_count")),
            indexed_markdown_path=str(data.get("indexed_markdown_path") or ""),
            indexed_markdown_sha256=str(data.get("indexed_markdown_sha256") or ""),
            indexed_at=str(data.get("indexed_at") or ""),
            conversion_row=(
                {str(k): str(v) for k, v in conversion.items()} if isinstance(conversion, dict) else None
            ),
        )


@dataclass(frozen=True)
class ProvenancePlan:
    plan_id: str
    created_at: str
    mapping_report: str
    generation_id: str | None
    inventory_available: bool
    inventory_error: str | None
    run_dir: str
    rows: tuple[PlanRow, ...]
    plan_version: int = PLAN_VERSION

    @property
    def counts(self) -> dict[str, int]:
        counts = Counter(row.bucket for row in self.rows)
        return {bucket: counts.get(bucket, 0) for bucket in BUCKETS}

    @property
    def estimate(self) -> dict[str, int]:
        eligible = [row for row in self.rows if row.bucket == BUCKET_ELIGIBLE]
        return {
            "eligible_rows": len(eligible),
            "source_bytes": sum(row.source_bytes or 0 for row in eligible),
            "pages": sum(row.page_count or 0 for row in eligible),
            "rows_without_page_count": sum(1 for row in eligible if not row.page_count),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_version": self.plan_version,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "mapping_report": self.mapping_report,
            "generation_id": self.generation_id,
            "inventory_available": self.inventory_available,
            "inventory_error": self.inventory_error,
            "run_dir": self.run_dir,
            "source_provenance_unknown": len(self.rows),
            "counts": self.counts,
            "estimate": self.estimate,
            "advice": {bucket: _BUCKET_ADVICE[bucket] for bucket in BUCKETS if self.counts[bucket]},
            "rows": [asdict(row) for row in self.rows],
        }


def build_plan(
    config: ProjectConfig,
    mapping_report: Path,
    *,
    index_root: Path | None = None,
    plan_id: str | None = None,
) -> ProvenancePlan:
    """Bucket every published record that lacks a source hash. Reads only; writes nothing.

    The same joins as ``audit_library`` (one read of the index pointer, a snapshot copy of the
    Zotero database, the mapping snapshot), so the plan's total always equals the audit's
    ``source_provenance_unknown`` for the same inputs.
    """
    root = index_root if index_root is not None else config.output_root / "index"
    # Timestamp for readability, random suffix so two plans made in the same second can never
    # share a run directory.
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
    )
    rows: list[PlanRow] = []
    ordinal = 0
    for obs in observations:
        if not obs.in_index or obs.indexed_source_sha256:
            continue
        indexed = index_rows[obs.attachment_key][0]
        indexed_source = _text(indexed, "source_path")
        source_file = Path(indexed_source) if indexed_source else None
        source_exists = source_file.is_file() if source_file else None
        bucket, reason, mapping = _bucket(
            obs,
            indexed,
            mapping_rows.get(obs.attachment_key, []),
            source_exists,
            current_source=_current_source(inventory.get(obs.attachment_key), config),
        )
        page_count = _optional_int(_text(mapping, "page_count") or _text(indexed, "page_count"))
        conversion_row = None
        row_ordinal = None
        if bucket == BUCKET_ELIGIBLE and mapping is not None:
            ordinal += 1
            row_ordinal = ordinal
            conversion_row = {name: _text(mapping, name) for name in CONVERSION_FIELDS}
            # Reconvert exactly the file the old record was indexed from: the replacement path
            # only accepts a conversion of the same source path as the record it replaces.
            conversion_row["source_path"] = indexed_source
        rows.append(
            PlanRow(
                attachment_key=obs.attachment_key,
                bucket=bucket,
                reason=reason,
                ordinal=row_ordinal,
                zotero_parent_key=_text(indexed, "zotero_parent_key"),
                title=_text(indexed, "title") or obs.title,
                source_path=indexed_source,
                source_exists=source_exists,
                source_bytes=_file_size(source_file) if source_exists and source_file else None,
                page_count=page_count,
                indexed_markdown_path=_text(indexed, "markdown_path"),
                indexed_markdown_sha256=_text(indexed, "markdown_sha256"),
                indexed_at=_text(indexed, "indexed_at"),
                conversion_row=conversion_row,
            )
        )
    return ProvenancePlan(
        plan_id=plan_id,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        mapping_report=str(mapping_report),
        generation_id=generation_id,
        inventory_available=inventory_available,
        inventory_error=inventory_error,
        run_dir=str(config.output_root / "conversion-runs" / PLANS_DIRNAME / plan_id),
        rows=tuple(rows),
    )


def _bucket(
    obs: ItemObservation,
    indexed: dict[str, object],
    candidates: list[dict[str, object]],
    source_exists: bool | None,
    *,
    current_source: str,
) -> tuple[str, str, dict[str, object] | None]:
    """First matching bucket, in order of what must be settled before anything else can be.

    ``current_source`` is where Zotero's own record says the PDF is now, resolved from the
    inventory alone -- never the snapshot's or index's path, which only say where it *was*.
    """
    if not obs.in_zotero and not obs.inventory_available:
        return BUCKET_MEMBERSHIP_UNCHECKED, "Zotero's attachment inventory could not be read.", None
    if not obs.in_zotero:
        return BUCKET_NOT_IN_ZOTERO, "Zotero no longer lists this attachment.", None
    if obs.index_row_count > 1:
        return (
            BUCKET_IDENTITY_UNCERTAIN,
            f"The published index holds {obs.index_row_count} records for this attachment.",
            None,
        )
    indexed_source = _text(indexed, "source_path")
    if not indexed_source:
        return BUCKET_IDENTITY_UNCERTAIN, "The indexed record names no source PDF.", None
    if not current_source:
        return (
            BUCKET_IDENTITY_UNCERTAIN,
            "Zotero's current path for this attachment does not resolve to a linked file (for "
            "example a stored `storage:` attachment), so nothing confirms it is still the indexed PDF.",
            None,
        )
    if not _same_path(current_source, indexed_source):
        return (
            BUCKET_IDENTITY_UNCERTAIN,
            "Zotero now links this attachment to a different PDF than the one the record was "
            "indexed from; reconverting either file would not establish what the old text came from.",
            None,
        )
    if not source_exists:
        return BUCKET_MISSING_PDF, "The PDF the record was indexed from is not on disk.", None
    matches = [row for row in candidates if _same_path(_text(row, "source_path"), indexed_source)]
    if not matches:
        return (
            BUCKET_IDENTITY_UNCERTAIN,
            "The mapping snapshot has no row for this attachment's indexed PDF; re-run dry-run.",
            None,
        )
    if len(matches) > 1:
        return BUCKET_IDENTITY_UNCERTAIN, "The mapping snapshot matches this PDF more than once.", None
    mapping = matches[0]
    classification = _text(mapping, "classification")
    identity_status = _text(mapping, "identity_status")
    if classification != ELIGIBLE_CLASSIFICATION or identity_status not in REPLACEABLE_IDENTITY_STATUSES:
        return (
            BUCKET_IDENTITY_UNCERTAIN,
            f"Mapping identity is {classification or 'unknown'}/{identity_status or 'unknown'}; only "
            f"{ELIGIBLE_CLASSIFICATION} with {'/'.join(sorted(REPLACEABLE_IDENTITY_STATUSES))} may replace a record.",
            None,
        )
    if _text(mapping, "zotero_parent_key") != _text(indexed, "zotero_parent_key"):
        return BUCKET_IDENTITY_UNCERTAIN, "The mapping and the indexed record name different parent items.", None
    return BUCKET_ELIGIBLE, "", mapping


def write_plan(plan: ProvenancePlan, plan_dir: Path) -> Path:
    """Write ``plan.json`` and a reviewer-friendly ``plan.csv``; refuses to overwrite a plan."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / PLAN_FILENAME
    if plan_path.exists():
        raise ProvenancePlanError(f"A plan already exists at {plan_path}; choose another directory.")
    if Path(plan.run_dir).exists():
        raise ProvenancePlanError(
            f"Run directory {plan.run_dir} already exists and may belong to another plan; plan again."
        )
    csv_fields = ("bucket", "attachment_key", "ordinal", "reason", "title", "source_path", "source_bytes", "page_count")
    with (plan_dir / PLAN_CSV_FILENAME).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for row in plan.rows:
            data = asdict(row)
            writer.writerow({name: "" if data[name] is None else data[name] for name in csv_fields})
    plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return plan_path


def default_plan_dir(config: ProjectConfig, plan_id: str) -> Path:
    return config.output_root / PLANS_DIRNAME / plan_id


def load_plan(path: Path) -> tuple[ProvenancePlan, Path]:
    """Read a plan from ``plan.json`` or the directory holding it; returns it with its directory."""
    plan_path = path / PLAN_FILENAME if path.is_dir() else path
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenancePlanError(f"Cannot read plan {plan_path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("plan_version") != PLAN_VERSION:
        raise ProvenancePlanError(f"{plan_path} is not a version-{PLAN_VERSION} provenance reconversion plan.")
    rows = tuple(PlanRow.from_dict(row) for row in data.get("rows", []) if isinstance(row, dict))
    plan = ProvenancePlan(
        plan_id=str(data["plan_id"]),
        created_at=str(data.get("created_at") or ""),
        mapping_report=str(data.get("mapping_report") or ""),
        generation_id=str(data["generation_id"]) if data.get("generation_id") else None,
        inventory_available=bool(data.get("inventory_available")),
        inventory_error=str(data["inventory_error"]) if data.get("inventory_error") else None,
        run_dir=str(data["run_dir"]),
        rows=rows,
    )
    return plan, plan_path.parent


# --------------------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RowOutcome:
    attachment_key: str
    outcome: str
    reason: str = ""
    markdown_path: str = ""
    source_sha256: str = ""


@dataclass
class ApplyReport:
    plan_id: str
    run_dir: str
    previous_generation_id: str | None
    generation_id: str | None
    selected: int
    remaining_eligible: int
    rows: list[RowOutcome] = field(default_factory=list)

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
            "remaining_eligible": self.remaining_eligible,
            "counts": self.counts,
            "rows": [asdict(row) for row in self.rows],
        }


def apply_plan(
    config: ProjectConfig,
    plan_path: Path,
    *,
    keys: list[str] | None = None,
    limit: int | None = None,
    workers: int | None = None,
    timeout_seconds: int = 600,
) -> ApplyReport:
    """Reconvert selected eligible rows and publish only validated, hash-bearing replacements.

    Holds the pipeline write lock throughout. The selection is the plan's eligible rows, in plan
    order, that still lack provenance in the *current* generation and still match the record
    the plan was made from; ``keys`` narrows it and ``limit`` caps it. Conversion runs in the
    plan's dedicated run directory with the ordinary checkpoint, so re-running after an
    interruption reuses completed extractions instead of repeating them.

    Publication is all-or-nothing per invocation but per-row in effect: rows that failed to
    convert, or that the replacement validation rejects, are left out, and their old records
    are carried into the new generation verbatim.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    plan, plan_dir = load_plan(plan_path)
    run_dir = Path(plan.run_dir)
    if not _is_within(run_dir, config.output_root) or not _is_within(plan_dir, config.output_root):
        raise ProvenancePlanError(
            f"Plan {plan.plan_id} lives outside the configured output_root ({config.output_root}); "
            "the pipeline lock only covers writes inside output_root."
        )
    index_root = config.output_root / "index"
    with pipeline_write_lock(config.output_root, command=COMMAND):
        # Finish or roll back an interrupted publication first. Otherwise this would read the
        # old generation, stage_and_publish would then complete the pending one, and the
        # successor built from the stale JSONL would silently drop that publication's records.
        recover_pending_publication(index_root)
        previous_generation, _published_at, index_rows = load_index_records(index_root)
        current_jsonl = current_generation_jsonl(index_root)
        if previous_generation is None or current_jsonl is None:
            raise ProvenancePlanError("No managed index generation is published; run rebuild-index first.")

        outcomes: list[RowOutcome] = []
        by_key = {row.attachment_key: row for row in plan.rows}
        candidates = [row for row in plan.rows if row.bucket == BUCKET_ELIGIBLE]
        if keys:
            wanted = list(dict.fromkeys(keys))
            for key in wanted:
                if key not in by_key:
                    outcomes.append(RowOutcome(key, OUTCOME_NOT_IN_PLAN, "This plan has no provenance-unknown record for this key."))
                elif by_key[key].bucket != BUCKET_ELIGIBLE:
                    row = by_key[key]
                    outcomes.append(RowOutcome(key, OUTCOME_NOT_ELIGIBLE, f"{row.bucket}: {row.reason}"))
            candidates = [row for row in candidates if row.attachment_key in set(wanted)]
        candidates.sort(key=lambda row: row.ordinal or 0)

        inventory = _read_inventory(config) if candidates else {}
        selectable: list[PlanRow] = []
        for row in candidates:
            skip = _revalidate(row, index_rows.get(row.attachment_key, [])) or _zotero_change(
                row, inventory.get(row.attachment_key), config
            )
            if skip is not None:
                outcomes.append(skip)
            else:
                selectable.append(row)
        selected = selectable if limit is None else selectable[:limit]
        report = ApplyReport(
            plan_id=plan.plan_id,
            run_dir=str(run_dir),
            previous_generation_id=previous_generation,
            generation_id=None,
            selected=len(selected),
            remaining_eligible=len(selectable) - len(selected),
        )
        if not selected:
            report.rows = outcomes
            _append_log(plan_dir, report)
            return report

        _claim_run_dir(run_dir, plan, plan_dir, config.output_root)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        selection_csv = plan_dir / "selections" / f"{stamp}_selection.csv"
        _write_csv(
            selection_csv,
            [{**(row.conversion_row or {}), ORDINAL_FIELD: str(row.ordinal)} for row in selected],
            fieldnames=[*CONVERSION_FIELDS, ORDINAL_FIELD],
        )
        convert_planned_rows(
            config, selection_csv, run_dir, ordinal_field=ORDINAL_FIELD, workers=workers, timeout_seconds=timeout_seconds
        )
        results = _read_manifest(run_dir / "manifest.csv")
        # Extraction can take hours; Zotero is consulted again so an attachment deleted or
        # relinked meanwhile is not published as the current one.
        inventory = _read_inventory(config)

        accepted: list[dict[str, str]] = []
        for row in selected:
            result = results.get(row.attachment_key)
            old = index_rows[row.attachment_key][0]
            outcome = _zotero_change(row, inventory.get(row.attachment_key), config) or _judge(
                row.attachment_key, result, old
            )
            if outcome.outcome == OUTCOME_PUBLISHED and result is not None:
                accepted.append(result)
            outcomes.append(outcome)

        if accepted:
            publish_csv = plan_dir / "selections" / f"{stamp}_publish.csv"
            _write_csv(publish_csv, accepted, fieldnames=list(accepted[0].keys()))
            writer, added, replaced, _skipped = write_jsonl_replacing_manifest(current_jsonl, publish_csv)
            if added or replaced != len(accepted):
                raise ProvenancePlanError(
                    f"Replacement would add {added} and replace {replaced} records; expected to replace "
                    f"exactly {len(accepted)}. Nothing was published."
                )
            info = stage_and_publish(index_root, writer, command=COMMAND)
            report.generation_id = info.generation_id
        report.rows = outcomes
        _append_log(plan_dir, report)
        return report


def _revalidate(row: PlanRow, current: list[dict[str, object]]) -> RowOutcome | None:
    """Why an eligible plan row must not be converted now, judged against the current generation."""
    key = row.attachment_key
    if row.conversion_row is None or row.ordinal is None:
        return RowOutcome(key, OUTCOME_PLAN_STALE, "The plan row carries no conversion input.")
    if len(current) != 1:
        return RowOutcome(key, OUTCOME_PLAN_STALE, f"The current generation holds {len(current)} records for this key.")
    record = current[0]
    if _text(record, "source_sha256"):
        return RowOutcome(key, OUTCOME_ALREADY_RESOLVED, "The current record already carries a source hash.")
    if (
        _text(record, "markdown_sha256") != row.indexed_markdown_sha256
        or _text(record, "zotero_parent_key") != row.zotero_parent_key
        or not _same_path(_text(record, "source_path"), row.source_path)
    ):
        return RowOutcome(key, OUTCOME_PLAN_STALE, "The indexed record changed after the plan was made; plan again.")
    if not Path(row.source_path).is_file():
        return RowOutcome(key, OUTCOME_MISSING_PDF, "The PDF is no longer on disk.")
    return None


def _read_inventory(config: ProjectConfig) -> dict[str, AttachmentRecord]:
    """Zotero's attachment inventory from a temporary copy; refuses to proceed without it."""
    try:
        return load_attachment_inventory(config.zotero_sqlite)
    except Exception as exc:
        raise ProvenancePlanError(
            f"Zotero's attachment inventory could not be read ({type(exc).__name__}: {exc}); "
            "nothing is reconverted or published without confirming each attachment. Close "
            "Zotero or wait for sync, then run this again."
        ) from exc


def _current_source(record: AttachmentRecord | None, config: ProjectConfig) -> str:
    return inventory_source_path(record, config.linked_attachments) if record is not None else ""


def _zotero_change(row: PlanRow, record: AttachmentRecord | None, config: ProjectConfig) -> RowOutcome | None:
    """Why Zotero no longer confirms this row's attachment, parent and linked PDF, or None."""
    key = row.attachment_key
    if record is None:
        return RowOutcome(key, OUTCOME_ZOTERO_CHANGED, "Zotero no longer lists this attachment.")
    if (record.parent_key or "") != row.zotero_parent_key:
        return RowOutcome(key, OUTCOME_ZOTERO_CHANGED, "Zotero now files this attachment under a different parent item.")
    current = _current_source(record, config)
    if not current:
        return RowOutcome(key, OUTCOME_ZOTERO_CHANGED, "Zotero's current path for this attachment no longer resolves to a linked file.")
    if not _same_path(current, row.source_path):
        return RowOutcome(key, OUTCOME_ZOTERO_CHANGED, "Zotero now links this attachment to a different PDF.")
    return None


def _claim_run_dir(run_dir: Path, plan: ProvenancePlan, plan_dir: Path, output_root: Path) -> None:
    """Bind ``run_dir`` to this plan, refusing one that another plan (or anything else) uses.

    The claim names the plan directory relative to ``output_root``, so a copy of a plan in
    another directory -- even with the same plan id -- cannot convert into this run directory.
    """
    claim_path = run_dir / RUN_CLAIM_FILENAME
    claim = {
        "plan_id": plan.plan_id,
        "created_at": plan.created_at,
        "plan_dir": plan_dir.resolve().relative_to(output_root.resolve()).as_posix(),
    }
    if claim_path.exists():
        try:
            existing = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProvenancePlanError(f"Cannot read run-directory claim {claim_path}: {exc}") from exc
        if existing != claim:
            raise ProvenancePlanError(
                f"Run directory {run_dir} belongs to plan {existing.get('plan_id') if isinstance(existing, dict) else '?'}, "
                f"not {plan.plan_id}; plan again."
            )
        return
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ProvenancePlanError(f"Run directory {run_dir} is not empty and not claimed by plan {plan.plan_id}; plan again.")
    run_dir.mkdir(parents=True, exist_ok=True)
    claim_path.write_text(json.dumps(claim) + "\n", encoding="utf-8", newline="\n")


def _judge(key: str, result: dict[str, str] | None, old: dict[str, object]) -> RowOutcome:
    if result is None:
        return RowOutcome(key, OUTCOME_CONVERSION_FAILED, "The conversion produced no result for this row.")
    status = result.get("status", "")
    if status == "skipped_existing":
        return RowOutcome(
            key,
            OUTCOME_CONVERSION_FAILED,
            "Markdown already exists in the run directory without a checkpoint entry that vouches "
            f"for it, so its provenance is unknown. Remove {result.get('output_path', '')} to reconvert.",
        )
    if status != "converted":
        return RowOutcome(key, OUTCOME_CONVERSION_FAILED, result.get("error", "") or f"status {status}")
    rejection = replacement_rejection(result, old)
    if rejection:
        return RowOutcome(key, OUTCOME_REJECTED, rejection)
    return RowOutcome(
        key, OUTCOME_PUBLISHED, markdown_path=result.get("output_path", ""), source_sha256=result.get("source_sha256", "")
    )


def _append_log(plan_dir: Path, report: ApplyReport) -> None:
    entry = {"logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **report.to_dict()}
    plan_dir.mkdir(parents=True, exist_ok=True)
    with (plan_dir / APPLY_LOG_FILENAME).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_manifest(path: Path) -> dict[str, dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return {row.get("zotero_attachment_key", ""): row for row in csv.DictReader(handle)}
    except FileNotFoundError as exc:
        raise ProvenancePlanError(f"The conversion wrote no manifest at {path}.") from exc


def _write_csv(path: Path, rows: list[dict[str, str]], *, fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return left == right


def _is_within(path: Path, root: Path) -> bool:
    resolved, resolved_root = path.resolve(), root.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents

