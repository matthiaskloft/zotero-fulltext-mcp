from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .bibtex import DEFAULT_DEBUG_BRIDGE_ENDPOINT, execute_javascript
from .identity import normalize_text
from .ingestion import ImportCandidate, dedupe_candidates, load_candidates, load_existing_items


WRITE_OPERATIONS = {
    "create_item",
    "link_pdf",
    "create_item_with_linked_pdf",
    "create_item_and_find_pdf",
    "find_pdf_for_item",
    "update_metadata",
    "trash_item",
}
NO_OP_OPERATIONS = {"no_op"}
ALL_OPERATIONS = WRITE_OPERATIONS | NO_OP_OPERATIONS
RISK_LEVELS = {"low", "medium", "high", "destructive"}
PDF_STRATEGIES = {"link_local_pdf", "metadata_only", "find_available_pdf"}
METADATA_STRATEGIES = {"supplied_metadata", "zotero_identifier"}


@dataclass(frozen=True)
class WritePlanRecord:
    operation: str
    approval_status: str
    risk_level: str
    candidate: ImportCandidate = field(default_factory=ImportCandidate)
    target: dict[str, str] = field(default_factory=dict)
    dedupe: dict[str, str] = field(default_factory=dict)
    js_preview: str = ""
    pdf_strategy: str = "metadata_only"
    metadata_strategy: str = "supplied_metadata"
    zotmoov_expected: bool = False
    pdf_management_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidate"] = asdict(self.candidate)
        return data


@dataclass(frozen=True)
class WriteValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    records: int
    write_records: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WriteApplyResult:
    ok: bool
    script_path: str
    auto_run_attempted: bool
    auto_run_available: bool
    instructions: str
    records: int
    write_records: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_write_plan(candidates_path: Path, zotero_sqlite: Path, output: Path) -> list[WritePlanRecord]:
    candidates = load_candidates(candidates_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    db_snapshot = output.with_name(output.stem + "_zotero.sqlite")
    shutil.copy2(zotero_sqlite, db_snapshot)
    existing = load_existing_items(db_snapshot)
    decisions = dedupe_candidates(candidates, existing)
    records: list[WritePlanRecord] = []
    for decision in decisions:
        candidate = decision.candidate
        pdf_strategy = _candidate_pdf_strategy(candidate)
        metadata_strategy = _candidate_metadata_strategy(candidate)
        dedupe = {
            "action": decision.action,
            "reason": decision.reason,
            "existing_zotero_parent_key": decision.existing_zotero_parent_key,
        }
        if pdf_strategy == "find_available_pdf" and candidate.zotero_parent_key:
            records.append(
                WritePlanRecord(
                    operation="find_pdf_for_item",
                    approval_status="pending",
                    risk_level="medium",
                    candidate=candidate,
                    target={"zotero_parent_key": candidate.zotero_parent_key},
                    dedupe=dedupe,
                    js_preview=f"Find available PDF for Zotero item: {candidate.zotero_parent_key}",
                    pdf_strategy=pdf_strategy,
                    metadata_strategy=metadata_strategy,
                    zotmoov_expected=candidate.zotmoov_expected,
                    pdf_management_note=_pdf_management_note(candidate, pdf_strategy),
                )
            )
            continue
        if decision.action == "add_candidate":
            if pdf_strategy == "find_available_pdf":
                operation = "create_item_and_find_pdf"
                risk_level = "medium"
                preview = f"Create Zotero item and find available PDF: {candidate.title or candidate.doi}"
            elif pdf_strategy == "link_local_pdf":
                operation = "create_item_with_linked_pdf"
                risk_level = "medium"
                preview = f"Create Zotero item and link PDF: {candidate.title or candidate.doi}"
            else:
                operation = "create_item"
                risk_level = "low"
                preview = f"Create Zotero item: {candidate.title or candidate.doi}"
            records.append(
                WritePlanRecord(
                    operation=operation,
                    approval_status="pending",
                    risk_level=risk_level,
                    candidate=candidate,
                    target={"planned_id": _planned_id(candidate)},
                    dedupe=dedupe,
                    js_preview=preview,
                    pdf_strategy=pdf_strategy,
                    metadata_strategy=metadata_strategy,
                    zotmoov_expected=candidate.zotmoov_expected,
                    pdf_management_note=_pdf_management_note(candidate, pdf_strategy),
                )
            )
            continue
        records.append(
            WritePlanRecord(
                operation="no_op",
                approval_status="not_required",
                risk_level="low",
                candidate=candidate,
                target={"zotero_parent_key": decision.existing_zotero_parent_key}
                if decision.existing_zotero_parent_key
                else {},
                dedupe=dedupe,
                js_preview=f"No Zotero write: {decision.action} ({decision.reason})",
                pdf_strategy=pdf_strategy,
                metadata_strategy=metadata_strategy,
                zotmoov_expected=candidate.zotmoov_expected,
                pdf_management_note=_pdf_management_note(candidate, pdf_strategy),
            )
        )
    write_plan(output, records)
    return records


def load_write_plan(path: Path) -> list[WritePlanRecord]:
    if not path.exists():
        raise FileNotFoundError(path)
    records: list[WritePlanRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        first = handle.read(1)
        handle.seek(0)
        if first == "[":
            raw_items = json.load(handle)
            records.extend(_record_from_dict(item) for item in raw_items)
        else:
            for line in handle:
                if line.strip():
                    records.append(_record_from_dict(json.loads(line)))
    return records


def write_plan(path: Path, records: list[WritePlanRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def validate_write_plan(path: Path, *, require_approved: bool = False) -> WriteValidationResult:
    records = load_write_plan(path)
    errors: list[str] = []
    warnings: list[str] = []
    for index, record in enumerate(records, start=1):
        prefix = f"record {index}"
        if record.operation not in ALL_OPERATIONS:
            errors.append(f"{prefix}: unsupported operation {record.operation!r}")
            continue
        if record.risk_level not in RISK_LEVELS:
            errors.append(f"{prefix}: unsupported risk_level {record.risk_level!r}")
        if record.pdf_strategy not in PDF_STRATEGIES:
            errors.append(f"{prefix}: unsupported pdf_strategy {record.pdf_strategy!r}")
        if record.metadata_strategy not in METADATA_STRATEGIES:
            errors.append(f"{prefix}: unsupported metadata_strategy {record.metadata_strategy!r}")
        if record.operation in WRITE_OPERATIONS:
            _validate_write_record(prefix, record, errors, warnings, require_approved=require_approved)
        elif record.approval_status == "approved":
            warnings.append(f"{prefix}: no_op record is approved but will not write to Zotero")
    return WriteValidationResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        records=len(records),
        write_records=sum(1 for record in records if record.operation in WRITE_OPERATIONS),
    )


def approve_write_plan_rows(path: Path, rows: list[int]) -> dict[str, Any]:
    records = load_write_plan(path)
    if not rows:
        raise ValueError("At least one row number is required.")
    selected = sorted(set(rows))
    invalid = [row for row in selected if row < 1 or row > len(records)]
    if invalid:
        raise ValueError(f"Invalid row number(s): {', '.join(str(row) for row in invalid)}")
    approved_rows: list[int] = []
    for row_number in selected:
        record = records[row_number - 1]
        if record.operation not in WRITE_OPERATIONS:
            raise ValueError(f"Row {row_number} is {record.operation!r} and cannot be approved.")
        records[row_number - 1] = WritePlanRecord(
            operation=record.operation,
            approval_status="approved",
            risk_level=record.risk_level,
            candidate=record.candidate,
            target=record.target,
            dedupe=record.dedupe,
            js_preview=record.js_preview,
            pdf_strategy=record.pdf_strategy,
            metadata_strategy=record.metadata_strategy,
            zotmoov_expected=record.zotmoov_expected,
            pdf_management_note=record.pdf_management_note,
        )
        approved_rows.append(row_number)
    write_plan(path, records)
    return {
        "plan": str(path),
        "approved_rows": approved_rows,
        "status": write_plan_status(path),
    }


def apply_write_plan(
    path: Path,
    out_script: Path,
    *,
    approve: bool = False,
    auto_run: bool = True,
    bbt_endpoint: str = DEFAULT_DEBUG_BRIDGE_ENDPOINT,
) -> WriteApplyResult:
    if not approve:
        raise PermissionError("zotero-write apply requires --approve")
    validation = validate_write_plan(path, require_approved=True)
    if not validation.ok:
        raise ValueError("\n".join(validation.errors))
    records = load_write_plan(path)
    write_records = [record for record in records if record.operation in WRITE_OPERATIONS]
    script = generate_zotero_javascript(write_records)
    out_script.parent.mkdir(parents=True, exist_ok=True)
    out_script.write_text(script, encoding="utf-8", newline="\n")

    auto_run_available = False
    instructions = (
        f"Open Zotero, then run the generated script via "
        f"Tools -> Developer -> Run JavaScript. Keep 'Run as async function' enabled. "
        f"Script: {out_script}"
    )

    if auto_run:
        js_result = execute_javascript(script, endpoint=bbt_endpoint)
        if js_result.ok:
            auto_run_available = True
            instructions = (
                f"Script executed automatically via debug-bridge. "
                f"Result: {js_result.result}"
            )
        else:
            instructions = (
                f"debug-bridge auto-run failed ({js_result.error}). "
                f"Open Zotero, then run the script manually via "
                f"Tools -> Developer -> Run JavaScript. Keep 'Run as async function' enabled. "
                f"Script: {out_script}"
            )

    return WriteApplyResult(
        ok=True,
        script_path=str(out_script),
        auto_run_attempted=auto_run,
        auto_run_available=auto_run_available,
        instructions=instructions,
        records=len(records),
        write_records=len(write_records),
    )


def write_plan_status(path: Path) -> dict[str, Any]:
    records = load_write_plan(path)
    by_operation: dict[str, int] = {}
    by_approval: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    by_pdf_strategy: dict[str, int] = {}
    by_metadata_strategy: dict[str, int] = {}
    zotmoov_expected_count = 0
    for record in records:
        by_operation[record.operation] = by_operation.get(record.operation, 0) + 1
        by_approval[record.approval_status] = by_approval.get(record.approval_status, 0) + 1
        by_risk[record.risk_level] = by_risk.get(record.risk_level, 0) + 1
        by_pdf_strategy[record.pdf_strategy] = by_pdf_strategy.get(record.pdf_strategy, 0) + 1
        by_metadata_strategy[record.metadata_strategy] = by_metadata_strategy.get(record.metadata_strategy, 0) + 1
        if record.zotmoov_expected:
            zotmoov_expected_count += 1
    return {
        "records": len(records),
        "write_records": sum(1 for record in records if record.operation in WRITE_OPERATIONS),
        "by_operation": by_operation,
        "by_approval_status": by_approval,
        "by_risk_level": by_risk,
        "by_pdf_strategy": by_pdf_strategy,
        "by_metadata_strategy": by_metadata_strategy,
        "zotmoov_expected_count": zotmoov_expected_count,
    }


def generate_zotero_javascript(records: list[WritePlanRecord]) -> str:
    payload = json.dumps([_js_spec(record) for record in records], ensure_ascii=False, indent=2)
    return f"""/*
Generated by zotero-pdf-text zotero-write apply.

Run inside Zotero:
Tools -> Developer -> Run JavaScript
Keep "Run as async function" enabled.

The script is intentionally idempotent where Zotero metadata allows it: it skips
existing items with the same DOI/title and existing linked child attachments
with the same path or title.
*/

const operations = {payload};
const libraryID = Zotero.Libraries.userLibraryID;
const results = [];

function norm(value) {{
  return String(value || "").trim().toLowerCase();
}}

function splitCreators(authors) {{
  return String(authors || "")
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part.length > 0)
    .map((name) => {{
      const bits = name.split(/\\s+/);
      if (bits.length === 1) {{
        return {{ creatorType: "author", lastName: bits[0], fieldMode: 1 }};
      }}
      return {{ creatorType: "author", firstName: bits.slice(0, -1).join(" "), lastName: bits[bits.length - 1] }};
    }});
}}

async function findItemByKey(key) {{
  if (!key) return null;
  return await Zotero.Items.getByLibraryAndKeyAsync(libraryID, key);
}}

async function findDuplicate(metadata) {{
  const targetDoi = norm(metadata.DOI);
  const targetTitle = norm(metadata.title);
  const items = await Zotero.Items.getAll(libraryID, true);
  for (const item of items) {{
    if (!item || item.isAttachment()) continue;
    const doi = norm(item.getField("DOI"));
    const title = norm(item.getField("title"));
    if (targetDoi && doi === targetDoi) return item;
    if (targetTitle && title === targetTitle) return item;
  }}
  return null;
}}

async function createItem(metadata) {{
  const duplicate = await findDuplicate(metadata);
  if (duplicate) {{
    return {{ item: duplicate, created: false, message: `SKIP existing item ${{duplicate.key}}` }};
  }}
  if (metadata.metadata_strategy === "zotero_identifier" && metadata.DOI) {{
    const imported = await createItemFromIdentifier(metadata.DOI);
    if (imported) {{
      return {{ item: imported, created: true, message: `CREATED item ${{imported.key}} from Zotero identifier lookup` }};
    }}
    results.push(`WARN identifier lookup unavailable or failed for ${{metadata.DOI}}; falling back to supplied metadata`);
  }}
  const item = new Zotero.Item(metadata.itemType || "journalArticle");
  item.libraryID = libraryID;
  item.setField("title", metadata.title || "");
  item.setField("DOI", metadata.DOI || "");
  item.setField("date", metadata.date || "");
  item.setField("publicationTitle", metadata.publicationTitle || "");
  item.setField("url", metadata.url || "");
  for (const creator of splitCreators(metadata.authors)) {{
    item.setCreator(item.numCreators(), creator);
  }}
  await item.saveTx();
  return {{ item, created: true, message: `CREATED item ${{item.key}}` }};
}}

async function createItemFromIdentifier(identifier) {{
  const internal = Zotero.Utilities && Zotero.Utilities.Internal;
  if (!internal || typeof internal.createItemsFromIdentifier !== "function") {{
    return null;
  }}
  try {{
    const created = await internal.createItemsFromIdentifier(identifier);
    const items = Array.isArray(created) ? created : [created];
    return items.find((item) => item && !item.isAttachment()) || null;
  }} catch (error) {{
    results.push(`ERROR identifier lookup for ${{identifier}}: ${{error && error.message ? error.message : error}}`);
    return null;
  }}
}}

async function linkPdf(parent, spec) {{
  const children = await parent.getAttachments();
  for (const childID of children) {{
    const child = Zotero.Items.get(childID);
    if (!child || !child.isAttachment()) continue;
    const existingPath = child.getFilePath && child.getFilePath();
    if (existingPath === spec.pdf_path || child.getField("title") === spec.attachment_title) {{
      return `SKIP existing attachment ${{parent.key}} -> ${{spec.attachment_title}}`;
    }}
  }}
  await Zotero.Attachments.linkFromFile({{
    file: spec.pdf_path,
    parentItemID: parent.id,
    contentType: "application/pdf",
    title: spec.attachment_title,
  }});
  return `LINKED ${{parent.key}} -> ${{spec.attachment_title}}`;
}}

async function findAvailablePdf(parent, spec) {{
  if (!Zotero.Attachments || typeof Zotero.Attachments.addAvailablePDF !== "function") {{
    return `ERROR find_available_pdf unavailable for ${{parent.key}}: Zotero.Attachments.addAvailablePDF is not available`;
  }}
  const before = new Set(await parent.getAttachments());
  const attachment = await Zotero.Attachments.addAvailablePDF(parent);
  const after = new Set(await parent.getAttachments());
  const added = [...after].filter((id) => !before.has(id));
  const zotmoovNote = spec.zotmoov_expected ? " ZotMoov may move/rename the attachment afterward." : "";
  if (attachment) {{
    const key = attachment.key || "(unknown attachment key)";
    return `FOUND available PDF for ${{parent.key}} -> ${{key}}.${{zotmoovNote}}`;
  }}
  if (added.length) {{
    return `FOUND available PDF for ${{parent.key}} -> ${{added.length}} new attachment(s).${{zotmoovNote}}`;
  }}
  return `NO available PDF found for ${{parent.key}}`;
}}

for (const op of operations) {{
  try {{
    if (
      op.operation === "create_item"
      || op.operation === "create_item_with_linked_pdf"
      || op.operation === "create_item_and_find_pdf"
    ) {{
      const created = await createItem(op.metadata);
      results.push(`${{op.operation}}: ${{created.message}}`);
      if (op.operation === "create_item_with_linked_pdf") {{
        results.push(await linkPdf(created.item, op));
      }}
      if (op.operation === "create_item_and_find_pdf") {{
        results.push(await findAvailablePdf(created.item, op));
      }}
      continue;
    }}
    if (op.operation === "find_pdf_for_item") {{
      const parent = await findItemByKey(op.target.zotero_parent_key);
      if (!parent) {{
        results.push(`MISSING parent ${{op.target.zotero_parent_key}} for find_available_pdf`);
        continue;
      }}
      results.push(await findAvailablePdf(parent, op));
      continue;
    }}
    if (op.operation === "link_pdf") {{
      const parent = await findItemByKey(op.target.zotero_parent_key);
      if (!parent) {{
        results.push(`MISSING parent ${{op.target.zotero_parent_key}} for ${{op.attachment_title}}`);
        continue;
      }}
      results.push(await linkPdf(parent, op));
      continue;
    }}
    if (op.operation === "update_metadata") {{
      const item = await findItemByKey(op.target.zotero_parent_key);
      if (!item) {{
        results.push(`MISSING item ${{op.target.zotero_parent_key}} for update`);
        continue;
      }}
      for (const [field, value] of Object.entries(op.metadata)) {{
        item.setField(field, value || "");
      }}
      await item.saveTx();
      results.push(`UPDATED ${{item.key}}`);
      continue;
    }}
    if (op.operation === "trash_item") {{
      const item = await findItemByKey(op.target.zotero_parent_key || op.target.zotero_attachment_key);
      if (!item) {{
        results.push(`MISSING item for trash: ${{JSON.stringify(op.target)}}`);
        continue;
      }}
      item.deleted = true;
      await item.saveTx();
      results.push(`TRASHED ${{item.key}}`);
      continue;
    }}
    results.push(`SKIP unsupported operation ${{op.operation}}`);
  }} catch (error) {{
    results.push(`ERROR ${{op.operation}}: ${{error && error.message ? error.message : error}}`);
  }}
}}

return results.join("\\n");
"""


def _validate_write_record(
    prefix: str,
    record: WritePlanRecord,
    errors: list[str],
    warnings: list[str],
    *,
    require_approved: bool,
) -> None:
    if require_approved and record.approval_status != "approved":
        errors.append(f"{prefix}: write operation requires approval_status='approved'")
    if record.operation in {"create_item", "create_item_with_linked_pdf", "create_item_and_find_pdf"}:
        if not record.candidate.title and not record.candidate.doi:
            errors.append(f"{prefix}: create operation requires candidate title or DOI")
    if record.operation in {"link_pdf", "create_item_with_linked_pdf"}:
        if not record.candidate.pdf_path:
            errors.append(f"{prefix}: {record.operation} requires candidate.pdf_path")
        elif not Path(record.candidate.pdf_path).exists():
            errors.append(f"{prefix}: PDF path does not exist: {record.candidate.pdf_path}")
    if record.operation in {"link_pdf", "update_metadata"} and not record.target.get("zotero_parent_key"):
        errors.append(f"{prefix}: {record.operation} requires target.zotero_parent_key")
    if record.operation == "create_item_and_find_pdf" and not (record.candidate.doi or record.candidate.title):
        errors.append(f"{prefix}: create_item_and_find_pdf requires candidate DOI or title")
    if record.operation == "find_pdf_for_item" and not record.target.get("zotero_parent_key"):
        errors.append(f"{prefix}: find_pdf_for_item requires target.zotero_parent_key")
    if record.operation in {"create_item_and_find_pdf", "find_pdf_for_item"} and record.pdf_strategy != "find_available_pdf":
        errors.append(f"{prefix}: {record.operation} requires pdf_strategy='find_available_pdf'")
    if record.metadata_strategy == "zotero_identifier" and not record.candidate.doi:
        errors.append(f"{prefix}: metadata_strategy='zotero_identifier' requires candidate DOI")
    if record.operation == "trash_item":
        if record.risk_level != "destructive":
            errors.append(f"{prefix}: trash_item requires risk_level='destructive'")
        if not (record.target.get("zotero_parent_key") or record.target.get("zotero_attachment_key")):
            errors.append(f"{prefix}: trash_item requires an exact Zotero key in target")
    if not record.dedupe:
        warnings.append(f"{prefix}: missing dedupe evidence")


def _record_from_dict(data: dict[str, Any]) -> WritePlanRecord:
    candidate_data = data.get("candidate") or {}
    if not isinstance(candidate_data, dict):
        candidate_data = {}
    candidate = _candidate_from_mapping(candidate_data)
    return WritePlanRecord(
        operation=str(data.get("operation", "") or ""),
        approval_status=str(data.get("approval_status", "") or ""),
        risk_level=str(data.get("risk_level", "") or ""),
        candidate=candidate,
        target=_string_dict(data.get("target")),
        dedupe=_string_dict(data.get("dedupe")),
        js_preview=str(data.get("js_preview", "") or ""),
        pdf_strategy=str(data.get("pdf_strategy", "") or "") or _candidate_pdf_strategy(candidate),
        metadata_strategy=str(data.get("metadata_strategy", "") or "") or _candidate_metadata_strategy(candidate),
        zotmoov_expected=bool(data.get("zotmoov_expected", candidate_data.get("zotmoov_expected", False))),
        pdf_management_note=str(data.get("pdf_management_note", "") or ""),
    )


def _candidate_from_mapping(candidate_data: dict[str, object]) -> ImportCandidate:
    return ImportCandidate(
        doi=str(candidate_data.get("doi", "") or ""),
        title=str(candidate_data.get("title", "") or ""),
        authors=str(candidate_data.get("authors", candidate_data.get("creators", "")) or ""),
        year=str(candidate_data.get("year", "") or ""),
        venue=str(candidate_data.get("venue", "") or ""),
        url=str(candidate_data.get("url", "") or ""),
        pdf_url=str(candidate_data.get("pdf_url", "") or ""),
        pdf_path=str(candidate_data.get("pdf_path", "") or ""),
        pdf_strategy=str(candidate_data.get("pdf_strategy", "") or ""),
        metadata_strategy=str(candidate_data.get("metadata_strategy", "") or ""),
        zotmoov_expected=bool(candidate_data.get("zotmoov_expected", False)),
        pdf_management_note=str(candidate_data.get("pdf_management_note", "") or ""),
        zotero_parent_key=str(candidate_data.get("zotero_parent_key", "") or ""),
        source_query=str(candidate_data.get("source_query", "") or ""),
        reason=str(candidate_data.get("reason", "") or ""),
    )


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item or "") for key, item in value.items()}


def _planned_id(candidate: ImportCandidate) -> str:
    base = candidate.doi or candidate.title or candidate.url or "candidate"
    normalized = normalize_text(base)
    return normalized[:80] or "candidate"


def _candidate_pdf_strategy(candidate: ImportCandidate) -> str:
    strategy = (candidate.pdf_strategy or "").strip()
    if strategy:
        return strategy
    if candidate.pdf_path:
        return "link_local_pdf"
    return "metadata_only"


def _candidate_metadata_strategy(candidate: ImportCandidate) -> str:
    strategy = (candidate.metadata_strategy or "").strip()
    if strategy:
        return strategy
    return "zotero_identifier" if candidate.doi else "supplied_metadata"


def _pdf_management_note(candidate: ImportCandidate, pdf_strategy: str) -> str:
    if candidate.pdf_management_note:
        return candidate.pdf_management_note
    if pdf_strategy == "find_available_pdf":
        if candidate.zotmoov_expected:
            return "Zotero may create a stored PDF; ZotMoov is expected to move/link it afterward."
        return "Zotero may create a stored PDF attachment."
    if pdf_strategy == "link_local_pdf":
        return "Link existing local PDF path without moving or renaming it."
    return "No PDF handling requested."


def _js_spec(record: WritePlanRecord) -> dict[str, Any]:
    candidate = record.candidate
    metadata = {
        "itemType": "journalArticle",
        "title": candidate.title,
        "DOI": candidate.doi,
        "date": candidate.year,
        "publicationTitle": candidate.venue,
        "url": candidate.url,
        "authors": candidate.authors,
        "metadata_strategy": record.metadata_strategy,
    }
    attachment_title = Path(candidate.pdf_path).name if candidate.pdf_path else candidate.title or candidate.doi
    return {
        "operation": record.operation,
        "target": record.target,
        "metadata": metadata,
        "pdf_path": candidate.pdf_path,
        "pdf_strategy": record.pdf_strategy,
        "metadata_strategy": record.metadata_strategy,
        "zotmoov_expected": record.zotmoov_expected,
        "pdf_management_note": record.pdf_management_note,
        "attachment_title": attachment_title,
    }
