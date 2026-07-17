from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import ProjectConfig
from .ingestion import ImportCandidate
from .zotero_write import WritePlanRecord, write_plan

RESOLVED_CSV_FILENAME = "duplicate_groups.csv"
RESOLVED_JSONL_FILENAME = "duplicate_groups.jsonl"
AMBIGUOUS_CSV_FILENAME = "ambiguous_duplicate_groups.csv"
AMBIGUOUS_JSONL_FILENAME = "ambiguous_duplicate_groups.jsonl"
TRASH_PLAN_FILENAME = "duplicate_trash_plan.jsonl"

# Strips a Zotero-style disambiguating suffix from a filename stem (extension already removed): a
# trailing digit run glued directly onto the stem ("...Regularization2"), separated by a space
# ("...Regularization 1"), or OS/browser-style parenthesized ("...Regularization (1)"). Order
# matters: the parenthesized form is tried first since it's the most specific, then the spaced
# form, then the glued form.
#
# This is deliberately never trusted on its own to decide "this filename has a suffix" -- a
# legitimate title ending in a digit (e.g. "...Phase 2.pdf") would match just as well. Instead
# find_byte_identical_duplicates only accepts a stripped result when it exactly matches another
# file's full stem *within the same byte-identical group*, i.e. two filenames that otherwise agree
# and differ only by this trailing bit. That pairing is what makes the suffix reading trustworthy;
# a filename ending in a digit with no sibling matching its stripped base is left ambiguous instead
# of guessed.
_SUFFIX_RE = re.compile(r"^(?P<base>.+?)(?:\s*\(\d+\)|\s+\d+|\d+)$")


def _stem(filename: str) -> str:
    name = filename
    if name.lower().endswith(".pdf"):
        name = name[: -len(".pdf")]
    return name


def _strip_suffix(stem: str) -> str | None:
    match = _SUFFIX_RE.match(stem)
    if not match:
        return None
    base = match.group("base").rstrip()
    return base or None


@dataclass(frozen=True)
class DuplicateFile:
    attachment_key: str
    filename: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedDuplicateGroup:
    parent_key: str
    citation_key: str
    sha256: str
    keep_key: str
    keep_filename: str
    drop_files: tuple[DuplicateFile, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_key": self.parent_key,
            "citation_key": self.citation_key,
            "sha256": self.sha256,
            "keep_key": self.keep_key,
            "keep_filename": self.keep_filename,
            "drop_files": [f.to_dict() for f in self.drop_files],
        }


@dataclass(frozen=True)
class AmbiguousDuplicateGroup:
    parent_key: str
    citation_key: str
    sha256: str
    files: tuple[DuplicateFile, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_key": self.parent_key,
            "citation_key": self.citation_key,
            "sha256": self.sha256,
            "files": [f.to_dict() for f in self.files],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DuplicateDiscoveryResult:
    resolved: list[ResolvedDuplicateGroup]
    ambiguous: list[AmbiguousDuplicateGroup]


def find_byte_identical_duplicates(mapping_report_path: Path) -> DuplicateDiscoveryResult:
    """Group a dry-run's mapped attachments by (zotero_parent_key, sha256) to find byte-identical
    duplicates -- the same PDF bytes linked twice under the same Zotero item, usually saved under
    slightly different filenames (a trailing suffix, or a mirror/z-lib copy).

    Only rows with both a zotero_parent_key and zotero_attachment_key (i.e. the mapper actually
    matched the file to a Zotero item, unlike an orphan_pdf/unsupported row) are considered. A real
    Zotero library's mapping report routinely has two rows sharing the exact same
    zotero_attachment_key under one parent: one row is the attachment's real Zotero-linked path
    (`mapped_verified`), and the other is a stray file the mapper's metadata-candidate fallback
    merely guessed belongs to the same item (`mapped_unverified`/`possible_mismatch`), reusing that
    already-linked attachment's key rather than naming a second real attachment. Those rows are not
    two attachments to choose between -- there is only one real Zotero attachment in that case, so
    rows are first collapsed to one file per distinct zotero_attachment_key; a parent+hash group
    left with fewer than 2 distinct attachment keys after that has nothing to dedupe and is skipped
    entirely (not even reported as ambiguous).

    Within a genuine group of 2+ *distinct* attachments sharing the same parent and file hash: if
    exactly one member's filename has no other group member's stripped-suffix form matching it
    (see _strip_suffix), it is kept, and every other member is accepted as a drop only if stripping
    its own suffix yields exactly that filename; otherwise (zero or multiple candidates without a
    suffix, or a suffixed filename that doesn't actually extend the keep candidate -- e.g.
    genuinely different editions, or an unexpected naming scheme) the group is reported as
    ambiguous and left for manual review rather than guessed.
    """
    rows = _load_mapping_rows(mapping_report_path)
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        parent_key = row.get("zotero_parent_key", "")
        attachment_key = row.get("zotero_attachment_key", "")
        sha256 = row.get("sha256", "")
        if not parent_key or not attachment_key or not sha256:
            continue
        groups.setdefault((parent_key, sha256), []).append(row)

    resolved: list[ResolvedDuplicateGroup] = []
    ambiguous: list[AmbiguousDuplicateGroup] = []
    for (parent_key, sha256), members in groups.items():
        citation_key = members[0].get("citation_key", "")
        files_by_key: dict[str, DuplicateFile] = {}
        for member in members:
            key = member["zotero_attachment_key"]
            if key not in files_by_key:
                files_by_key[key] = DuplicateFile(
                    attachment_key=key,
                    filename=Path(member.get("source_name") or member.get("source_path", "")).name,
                )
        files = tuple(files_by_key.values())
        if len(files) < 2:
            continue
        stems = {f.attachment_key: _stem(f.filename) for f in files}
        no_suffix = [f for f in files if _strip_suffix(stems[f.attachment_key]) is None]
        if len(no_suffix) != 1:
            reason = (
                "no attachment without a numeric suffix"
                if not no_suffix
                else "multiple attachments without a numeric suffix"
            )
            ambiguous.append(
                AmbiguousDuplicateGroup(
                    parent_key=parent_key,
                    citation_key=citation_key,
                    sha256=sha256,
                    files=files,
                    reason=reason,
                )
            )
            continue

        keep = no_suffix[0]
        keep_stem = stems[keep.attachment_key]
        others = [f for f in files if f.attachment_key != keep.attachment_key]
        mismatched = [f for f in others if _strip_suffix(stems[f.attachment_key]) != keep_stem]
        if mismatched:
            ambiguous.append(
                AmbiguousDuplicateGroup(
                    parent_key=parent_key,
                    citation_key=citation_key,
                    sha256=sha256,
                    files=files,
                    reason="a suffixed filename does not extend the unsuffixed filename's name",
                )
            )
            continue

        resolved.append(
            ResolvedDuplicateGroup(
                parent_key=parent_key,
                citation_key=citation_key,
                sha256=sha256,
                keep_key=keep.attachment_key,
                keep_filename=keep.filename,
                drop_files=tuple(others),
            )
        )
    return DuplicateDiscoveryResult(resolved=resolved, ambiguous=ambiguous)


def build_trash_plan(groups: list[ResolvedDuplicateGroup]) -> list[WritePlanRecord]:
    """Build trash_item write-plan records for the "drop" side of each resolved duplicate group.

    Records are created with approval_status="pending" -- nothing is trashed in Zotero until a
    human runs the existing zotero-write approve/validate --require-approved/apply --approve gate
    on the plan this produces.
    """
    records: list[WritePlanRecord] = []
    for group in groups:
        for drop in group.drop_files:
            records.append(
                WritePlanRecord(
                    operation="trash_item",
                    approval_status="pending",
                    risk_level="destructive",
                    candidate=ImportCandidate(title=drop.filename, reason="byte-identical duplicate"),
                    target={"zotero_attachment_key": drop.attachment_key},
                    dedupe={
                        "action": "trash_duplicate_attachment",
                        "reason": f"byte-identical duplicate; keeping [{group.keep_key}] {group.keep_filename}",
                        "existing_zotero_parent_key": group.parent_key,
                    },
                    js_preview=(
                        f"Trash duplicate attachment {drop.attachment_key} ({drop.filename}) "
                        f"on item {group.parent_key} ({group.citation_key})"
                    ),
                )
            )
    return records


def run_duplicate_discovery(
    config: ProjectConfig,
    mapping_report_path: Path,
    *,
    output_dir: Path | None = None,
) -> Path:
    """Discover byte-identical duplicate attachments and write a trash write-plan plus a report.

    Never touches Zotero. The generated `duplicate_trash_plan.jsonl` feeds into the same
    `zotero-write validate` / `approve` / `apply --approve` gate as any other write plan.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir or (config.output_root / "duplicate_attachments" / timestamp)
    run_dir.mkdir(parents=True, exist_ok=True)

    result = find_byte_identical_duplicates(mapping_report_path)
    _write_resolved(run_dir, result.resolved)
    _write_ambiguous(run_dir, result.ambiguous)
    write_plan(run_dir / TRASH_PLAN_FILENAME, build_trash_plan(result.resolved))
    return run_dir


def _load_mapping_rows(mapping_report_path: Path) -> list[dict[str, str]]:
    with mapping_report_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_resolved(run_dir: Path, groups: list[ResolvedDuplicateGroup]) -> None:
    csv_path = run_dir / RESOLVED_CSV_FILENAME
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["parent_key", "citation_key", "sha256", "keep_key", "keep_filename", "drop_key", "drop_filename"]
        )
        for group in groups:
            for drop in group.drop_files:
                writer.writerow(
                    [
                        group.parent_key,
                        group.citation_key,
                        group.sha256,
                        group.keep_key,
                        group.keep_filename,
                        drop.attachment_key,
                        drop.filename,
                    ]
                )

    jsonl_path = run_dir / RESOLVED_JSONL_FILENAME
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for group in groups:
            handle.write(_json_dumps(group.to_dict()) + "\n")


def _write_ambiguous(run_dir: Path, groups: list[AmbiguousDuplicateGroup]) -> None:
    csv_path = run_dir / AMBIGUOUS_CSV_FILENAME
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["parent_key", "citation_key", "sha256", "attachment_key", "filename", "reason"])
        for group in groups:
            for file in group.files:
                writer.writerow(
                    [group.parent_key, group.citation_key, group.sha256, file.attachment_key, file.filename, group.reason]
                )

    jsonl_path = run_dir / AMBIGUOUS_JSONL_FILENAME
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for group in groups:
            handle.write(_json_dumps(group.to_dict()) + "\n")


def _json_dumps(data: object) -> str:
    return json.dumps(data, ensure_ascii=False)
