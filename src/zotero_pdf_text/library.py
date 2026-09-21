"""Canonical library paths, drift observation, and read-only audit reporting.

This module implements the read-only half of the hardening plan's Package 3 (roadmap rank 8):
`audit-library` and `library_status`. It compares three independent views of the same library --
the mapper snapshot (what Zotero says exists), the filesystem (what is actually on disk), and the
published index generation's JSONL metadata (what search can currently return) -- and reports
where they disagree.

A fourth view, the canonical layout, is recorded per item as `canonical_markdown_exists` but is
deliberately not classified: nothing writes to `library/` yet, so every canonical file is absent
and a status derived from that would fire on the entire library while meaning nothing. It is
evidence held ready for the migration half of Package 3, not a finding.

The index is read through its generation JSONL only; this module never opens the SQLite index, so
a divergence between the two within one generation is out of scope here.

Nothing here moves, renames, or rewrites anything. Every function reads. The migration half of
Package 3 (`migrate-library-layout`) is gated separately and is deliberately not implemented here;
this audit is the evidence that decides whether it is worth doing at all.

The canonical path helpers are defined here in *report-only* form: they say where a converted file
would live under the canonical layout so the audit can report whether it does, and they are the
single definition converters and migration must reuse if the canonical layout is ever adopted.
Deriving an image directory from a Markdown filename anywhere else is what these helpers exist to
prevent.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import (
    ManagedIndexMissingError,
    current_generation_jsonl,
    read_current_pointer,
)
from .config import ProjectConfig
from .identity import resolve_attachment_paths
from .zotero_db import AttachmentRecord, load_attachment_records

# --------------------------------------------------------------------------------------
# Status vocabulary
# --------------------------------------------------------------------------------------
# These are not mutually exclusive. A single attachment routinely satisfies several at once --
# a PDF that moved *and* whose metadata changed *and* whose Markdown predates the current source
# hits three. Each audited item therefore carries a *set* of statuses, and the audit's counts
# overlap by construction: they do not sum to the library size. `LibraryAudit.total_items` is the
# only number that answers "how many attachments are there".
#
# This was a deliberate choice over a single precedence-ordered label. The audit's purpose is to
# supply evidence for the go/no-go on the risky migration half of Package 3, and an audit that
# drops secondary findings to keep its columns tidy is not evidence -- it is a summary that needs
# several fix-and-rerun cycles before it tells the truth.

STATUS_CURRENT = "current"
STATUS_UNINDEXED = "unindexed"
STATUS_STALE_MARKDOWN = "stale_markdown"
STATUS_SOURCE_CHANGED = "source_changed"
STATUS_METADATA_CHANGED = "metadata_changed"
STATUS_MISSING_SOURCE = "missing_source"
STATUS_MISSING_MARKDOWN = "missing_markdown"
STATUS_ORPHANED_INDEX = "orphaned_index"
STATUS_DUPLICATE_KEY = "duplicate_key"
# Not one of the nine the plan lists. The plan wrote that vocabulary before the index recorded
# enough provenance to detect this condition, and it names something serious enough not to leave
# as a footnote: an attachment Zotero represents but whose identity was never verified, which is
# nonetheless in the published generation and therefore being returned by search right now.
# Distinct from `orphaned_index`, which is an index row Zotero does not represent at all.
STATUS_UNVERIFIED_INDEXED = "unverified_indexed"

ALL_STATUSES: tuple[str, ...] = (
    STATUS_CURRENT,
    STATUS_UNINDEXED,
    STATUS_STALE_MARKDOWN,
    STATUS_SOURCE_CHANGED,
    STATUS_METADATA_CHANGED,
    STATUS_MISSING_SOURCE,
    STATUS_MISSING_MARKDOWN,
    STATUS_ORPHANED_INDEX,
    STATUS_UNVERIFIED_INDEXED,
    STATUS_DUPLICATE_KEY,
)

# Identity states that may publish to the canonical library. Defined once here (plan step 3) so
# migration, reconciliation, rebuild-index, update-index and reconvert-math share one predicate
# rather than each re-deriving eligibility slightly differently.
ELIGIBLE_CLASSIFICATION = "mapped_verified"
ELIGIBLE_IDENTITY_STATUSES = frozenset({"verified", "manual_accepted", "fulltext_verified"})

MAPPING_REPORT_JSONL = "mapping_report.jsonl"
LIBRARY_DIRNAME = "library"
MARKDOWN_DIRNAME = "markdown"
IMAGES_DIRNAME = "images"

_ATTACHMENT_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


class LibraryAuditError(RuntimeError):
    """Raised when an audit cannot be performed at all (bad snapshot, unreadable index)."""


# --------------------------------------------------------------------------------------
# Canonical path derivation (report-only)
# --------------------------------------------------------------------------------------


def canonical_library_root(config: ProjectConfig) -> Path:
    return config.output_root / LIBRARY_DIRNAME


def canonical_markdown_path(config: ProjectConfig, attachment_key: str, title: str = "") -> Path:
    """Where attachment `attachment_key`'s converted Markdown lives under the canonical layout.

    Identity comes from the attachment key alone; the title slug is cosmetic and never
    participates in matching. That asymmetry matters -- a retitled Zotero item must not change
    where its converted text lives, or every locator and index row pointing at it silently breaks.
    """
    key = validate_attachment_key(attachment_key)
    slug = slugify_title(title)
    stem = f"{key}--{slug}" if slug else key
    return canonical_library_root(config) / MARKDOWN_DIRNAME / f"{stem}.md"


def canonical_image_dir(config: ProjectConfig, attachment_key: str) -> Path:
    """Where attachment `attachment_key`'s extracted images live under the canonical layout.

    Keyed on the attachment key only -- deliberately *not* derived from the Markdown filename, so
    a title change cannot orphan a whole image directory.
    """
    key = validate_attachment_key(attachment_key)
    return canonical_library_root(config) / IMAGES_DIRNAME / key


def validate_attachment_key(attachment_key: str) -> str:
    """Reject anything that is not a Zotero attachment key before it reaches a path join."""
    key = (attachment_key or "").strip()
    if not _ATTACHMENT_KEY_RE.match(key):
        raise ValueError(
            f"Not a valid Zotero attachment key: {attachment_key!r}. "
            "Canonical paths are derived from validated keys only."
        )
    return key


def slugify_title(title: str, *, max_length: int = 60) -> str:
    """Cosmetic, ASCII-only, never load-bearing for identity."""
    if not title:
        return ""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP_RE.sub("-", ascii_only).strip("-")
    return slug[:max_length].rstrip("-")


def is_canonical_eligible(record: object) -> bool:
    """Whether an index or mapping record may publish to the canonical library.

    Applied in report-only form by this audit: a non-eligible row is reported as quarantine or
    unverified rather than treated as library content.
    """
    classification = _field(record, "classification")
    identity_status = _field(record, "identity_status")
    return (
        classification == ELIGIBLE_CLASSIFICATION
        and identity_status in ELIGIBLE_IDENTITY_STATUSES
    )


# --------------------------------------------------------------------------------------
# Observation: the facts, gathered before any judgement is applied
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemObservation:
    """Everything the audit knows about one attachment key, with no interpretation applied.

    Separating observation from classification is what makes the status rules testable: a test
    constructs an observation literal and asserts on the returned status set, without needing a
    Zotero database, a filesystem or a published index generation.

    Fields are three-valued where absence is meaningful: `None` means "not known / not checked",
    which is distinct from `False` or `""`. `source_sha256_current` in particular is `None`
    unless the audit ran in full mode, because hashing every source PDF is expensive.
    """

    attachment_key: str

    # --- Zotero's own attachment inventory: what Zotero represents ---
    # Distinct from `in_mapping` and load-bearing. A mapping snapshot is built by walking files
    # on disk, so an attachment whose PDF is already gone produces no mapping row at all. Reading
    # membership off the snapshot alone would make `missing_source` nearly unreachable and would
    # mislabel a previously-indexed attachment with a vanished PDF as `orphaned_index`, asserting
    # Zotero no longer represents something it still does.
    in_zotero: bool = False

    # Whether `in_zotero` is an answer or a shrug. False means the inventory could not be read,
    # so `in_zotero=False` carries no information and membership must fall back to the snapshot.
    # Without this flag the two cases are indistinguishable and the fallback can never be
    # switched off, which is what let a deleted-from-Zotero item keep its mapping-row membership.
    inventory_available: bool = False

    # --- mapper snapshot: what the last dry-run matched on disk ---
    in_mapping: bool = False
    classification: str = ""
    identity_status: str = ""
    identity_rule: str = ""
    parent_key: str = ""
    title: str = ""
    mapping_metadata: dict[str, str] = field(default_factory=dict)
    # The path the audit actually checked and hashed: Zotero's current one when the inventory
    # could supply it, otherwise the snapshot's or the index's. Not necessarily the path the
    # indexed text came from -- `indexed_source_path` keeps that, and the two differ after a
    # relink.
    source_path: str = ""
    source_sha256_mapping: str = ""

    # --- filesystem: what is actually on disk ---
    source_exists: bool | None = None
    source_sha256_current: str | None = None
    markdown_exists: bool | None = None
    markdown_sha256_current: str | None = None
    canonical_markdown_exists: bool | None = None

    # --- published index generation: what search can currently return ---
    in_index: bool = False
    index_row_count: int = 0
    indexed_markdown_path: str = ""
    indexed_markdown_sha256: str = ""
    indexed_source_path: str = ""
    indexed_source_sha256: str = ""
    indexed_metadata: dict[str, str] = field(default_factory=dict)
    indexed_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AuditItem:
    """One audited attachment: its observation plus every status that applies to it."""

    attachment_key: str
    statuses: tuple[str, ...]
    canonical_eligible: bool
    observation: ItemObservation

    def to_dict(self) -> dict[str, object]:
        return {
            "attachment_key": self.attachment_key,
            "statuses": list(self.statuses),
            "canonical_eligible": self.canonical_eligible,
            "evidence": self.observation.to_dict(),
        }

    def has(self, status: str) -> bool:
        return status in self.statuses


@dataclass(frozen=True)
class LibraryAudit:
    """A whole-library audit result.

    `status_counts` overlaps: an item contributing to `source_changed` may also contribute to
    `stale_markdown`. Only `total_items` is a partition of the library. Any presentation layer
    that renders these counts must say so, or it will read as a set of mutually exclusive buckets
    that happen not to add up.
    """

    snapshot_time: str
    generation_id: str | None
    mapping_report: str
    full_audit: bool
    total_items: int
    status_counts: dict[str, int]
    items: tuple[AuditItem, ...]
    # Two plain numbers that explain the status counts rather than adding to them.
    #
    # `ineligible_items` is why a large share of rows can carry no status at all: a quarantine
    # item correctly absent from the index has nothing to report, and without this number that
    # silence is indistinguishable from a broken rule.
    #
    # `source_provenance_unknown` is why `source_changed` can read near zero on a library that
    # has genuinely drifted: records converted before the provenance field existed carry no
    # source hash, so nothing can be compared for them. It states how much of the audit is
    # actually informative yet.
    ineligible_items: int = 0
    source_provenance_unknown: int = 0
    # Whether Zotero's attachment inventory could be read. When it could not, membership falls
    # back to the mapping snapshot, and an attachment whose PDF is missing is invisible rather
    # than reported -- so missing_source and orphaned_index are both understated. Reported
    # rather than raised: a partial audit that says which question it could not answer beats
    # refusing to run because Zotero happened to be mid-sync.
    inventory_available: bool = False

    def items_with(self, status: str) -> tuple[AuditItem, ...]:
        return tuple(item for item in self.items if item.has(status))

    def to_dict(self, *, include_items: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "snapshot_time": self.snapshot_time,
            "generation_id": self.generation_id,
            "mapping_report": self.mapping_report,
            "full_audit": self.full_audit,
            "total_items": self.total_items,
            "status_counts": dict(self.status_counts),
            "counts_overlap": True,
            "inventory_available": self.inventory_available,
            "ineligible_items": self.ineligible_items,
            "source_provenance_unknown": self.source_provenance_unknown,
        }
        if include_items:
            payload["items"] = [item.to_dict() for item in self.items]
        return payload


# --------------------------------------------------------------------------------------
# Status resolution -- the rules that turn facts into findings
# --------------------------------------------------------------------------------------


def classify_item(observation: ItemObservation) -> frozenset[str]:
    """Return every status that applies to `observation`.

    The rules are grouped by what kind of statement they make. Structural facts come first and
    hold regardless of whether an item belongs in the library at all; eligibility-dependent
    judgements come afterwards. If the eligibility predicate ever changes, only the third group
    moves.

    An empty result is a normal, expected outcome, not a gap: an ineligible item that is
    correctly absent from the index has nothing to report. `LibraryAudit.ineligible_items` exists
    so a reader can tell that silence apart from a broken rule.
    """
    obs = observation
    statuses: set[str] = set()
    eligible = is_canonical_eligible(obs)
    # Who decides whether the library still contains this attachment.
    #
    # When the live inventory was read, it is the authority outright: it is current, whereas a
    # mapping row only proves membership as of the last dry-run. Unioning the two would let a
    # stale row vouch for an attachment the user has since deleted from Zotero, reporting it
    # `current` when the honest answer is `orphaned_index`.
    #
    # When the inventory could not be read, `in_zotero` is False for everything and means
    # nothing, so the snapshot is all there is. The fallback is weaker in the other direction --
    # an attachment whose PDF vanished never reached the snapshot -- which is why the audit
    # reports `inventory_available` rather than quietly picking one.
    #
    # Written so that positive evidence is never discarded: only the *fallback* is switched off
    # by a successful read, never `in_zotero` itself. A plain conditional would answer "not
    # represented" for the contradictory state `in_zotero=True, inventory_available=False`,
    # which production cannot produce but a hand-built observation can.
    represented = obs.in_zotero or (obs.in_mapping and not obs.inventory_available)

    # 1. Structural facts. Reported for quarantine rows too -- a duplicate key or a vanished PDF
    #    is a fact about the library, not a judgement about whether the item belongs in it.
    if obs.index_row_count > 1:
        statuses.add(STATUS_DUPLICATE_KEY)
    if obs.in_index and not represented:
        statuses.add(STATUS_ORPHANED_INDEX)
    # `is False`, not a falsy test. Both fields are None when never checked, and an unindexed
    # item has no indexed markdown_path to check at all. Treating None as False here would report
    # every unindexed item as missing its Markdown as well, doubling the count for no information.
    if obs.source_exists is False:
        statuses.add(STATUS_MISSING_SOURCE)
    if obs.markdown_exists is False:
        statuses.add(STATUS_MISSING_MARKDOWN)

    # 2. Comparisons. Absence is not inequality -- see `_differs`.
    if _differs(obs.markdown_sha256_current, obs.indexed_markdown_sha256):
        statuses.add(STATUS_STALE_MARKDOWN)
    # Prefer the hash this audit just computed; fall back to the one the mapper already computed
    # during dry-run. The mapper hashes every source PDF it sees, so a snapshot carries usable
    # provenance for free and `source_changed` is detectable without --full. The fallback is
    # weaker only in age -- it answers "has the PDF changed since the snapshot" rather than
    # "since this instant" -- which is a real caveat but a far smaller one than not looking.
    if _differs(obs.source_sha256_current or obs.source_sha256_mapping, obs.indexed_source_sha256):
        statuses.add(STATUS_SOURCE_CHANGED)
    if obs.in_mapping and obs.in_index and obs.mapping_metadata != obs.indexed_metadata:
        statuses.add(STATUS_METADATA_CHANGED)

    # 3. Eligibility-dependent judgements.
    #    `unverified_indexed` requires `in_mapping`, which is what separates it from
    #    `orphaned_index`: the two name different repairs -- verify the identity, versus drop a
    #    row for an attachment Zotero no longer represents.
    if represented and obs.in_index and not eligible:
        statuses.add(STATUS_UNVERIFIED_INDEXED)
    #    An ineligible item is never `unindexed`. It is correctly absent from the library, and
    #    reporting it as a gap would manufacture a backlog that should not be worked -- the exact
    #    false signal that would wrongly push the gated migration forward.
    if represented and eligible and not obs.in_index:
        statuses.add(STATUS_UNINDEXED)

    # 4. `current` last, defined as the absence of every other finding rather than by restating
    #    the conditions. A status added later then narrows `current` automatically, instead of
    #    leaving a second definition to remember to update.
    if represented and eligible and obs.in_index and not statuses:
        statuses.add(STATUS_CURRENT)

    return frozenset(statuses)


def _differs(left: str | None, right: str | None) -> bool:
    """Whether two hashes are both known and disagree.

    Absence never counts as a difference. `indexed_source_sha256` is "" for any record converted
    before the provenance field existed or reused through `skipped_existing`, and a computed
    source hash is None outside a full audit. An unguarded `!=` would report
    the entire pre-upgrade library as changed -- a library-wide false positive on the one status
    whose job is to justify a high-risk migration.
    """
    return bool(left) and bool(right) and left != right


# --------------------------------------------------------------------------------------
# Audit orchestration
# --------------------------------------------------------------------------------------


def audit_library(
    config: ProjectConfig,
    mapping_report: Path,
    *,
    full_audit: bool = False,
    index_root: Path | None = None,
) -> LibraryAudit:
    """Compare the mapper snapshot, the filesystem, and the published index generation.

    Read-only: nothing here moves, renames, rewrites or locks anything.

    `full_audit` re-hashes every source PDF from disk. It is not what makes `source_changed`
    detectable -- the mapper already hashed each PDF during dry-run and the snapshot carries
    those hashes -- it is what makes the comparison current, answering "has the PDF changed as of
    now" rather than "as of the snapshot". It also covers index-only rows, which no snapshot
    describes. It is the expensive mode; the default compares recorded hashes only.

    The pointer and the generation JSONL are read in two steps without a lock, which is correct
    for a read-only command but means a publication landing between them yields a generation id
    that does not describe the rows reported. Re-run if that matters.
    """
    snapshot_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    root = index_root if index_root is not None else config.output_root / "index"

    mapping_rows = load_mapping_snapshot(mapping_report)
    generation_id, index_rows = load_index_records(root)

    # Zotero is the authority on which attachments exist. A snapshot cannot be, because the
    # mapper only sees attachments whose files it found. Read-only, and never fatal: an audit
    # that refuses to run because Zotero is mid-sync is less useful than one that runs and says
    # which question it could not answer.
    inventory: dict[str, AttachmentRecord] = {}
    inventory_available = False
    try:
        inventory = load_attachment_inventory(config.zotero_sqlite)
        inventory_available = True
    except Exception:
        inventory = {}

    observations = build_observations(
        config,
        mapping_rows=mapping_rows,
        index_rows=index_rows,
        inventory=inventory,
        inventory_available=inventory_available,
        full_audit=full_audit,
    )

    items: list[AuditItem] = []
    counts = {status: 0 for status in ALL_STATUSES}
    for observation in observations:
        statuses = classify_item(observation)
        unknown = set(statuses) - set(ALL_STATUSES)
        if unknown:
            raise LibraryAuditError(
                f"classify_item returned unknown status(es) {sorted(unknown)} for "
                f"{observation.attachment_key}; add them to ALL_STATUSES or fix the rule."
            )
        ordered = tuple(status for status in ALL_STATUSES if status in statuses)
        for status in ordered:
            counts[status] += 1
        items.append(
            AuditItem(
                attachment_key=observation.attachment_key,
                statuses=ordered,
                canonical_eligible=is_canonical_eligible(observation),
                observation=observation,
            )
        )

    items.sort(key=lambda item: item.attachment_key)
    return LibraryAudit(
        snapshot_time=snapshot_time,
        generation_id=generation_id,
        mapping_report=str(mapping_report),
        full_audit=full_audit,
        total_items=len(items),
        status_counts=counts,
        items=tuple(items),
        inventory_available=inventory_available,
        ineligible_items=sum(1 for item in items if not item.canonical_eligible),
        source_provenance_unknown=sum(
            1
            for item in items
            if item.observation.in_index and not item.observation.indexed_source_sha256
        ),
    )


def build_observations(
    config: ProjectConfig,
    *,
    mapping_rows: dict[str, dict[str, object]],
    index_rows: dict[str, list[dict[str, object]]],
    inventory: dict[str, AttachmentRecord] | None = None,
    inventory_available: bool = False,
    full_audit: bool = False,
) -> list[ItemObservation]:
    """Join the views on attachment key, hashing only what the audit mode asks for.

    The key set is the *union* of all three sources, never any one of them. Each contributes a
    case the others cannot see: the index alone holds a row for an attachment Zotero dropped
    (`orphaned_index`); Zotero alone holds an attachment whose PDF is gone, which never reaches
    the file-walking mapper at all (`missing_source`); the snapshot alone carries the identity
    decisions the other two do not record.
    """
    inventory = inventory or {}
    observations: list[ItemObservation] = []
    for key in sorted(set(mapping_rows) | set(index_rows) | set(inventory)):
        mapping = mapping_rows.get(key)
        rows = index_rows.get(key, [])
        indexed = rows[0] if rows else None
        attachment = inventory.get(key)

        # Where the snapshot and the index believe the PDF lives. Kept as provenance: it is
        # what the indexed text was extracted from, and it is what `indexed_source_path`
        # reports even after Zotero has been pointed somewhere else.
        recorded_source_path = _text(mapping, "source_path") or _text(indexed, "source_path")
        # Where Zotero says it lives *now*. This wins when Zotero can tell us, because an
        # attachment relinked to a different PDF must be audited at its current location: the
        # old path answers a question nobody asked. Auditing the stale path misses the source
        # change outright when the old file is still there, and invents `missing_source` when
        # it is not, while the new PDF sits on disk perfectly intact.
        current_source_path = (
            _inventory_source_path(attachment, config.linked_attachments)
            if attachment is not None
            else ""
        )
        source_path = current_source_path or recorded_source_path
        source_file = Path(source_path) if source_path else None
        source_exists = source_file.is_file() if source_file else None
        # The snapshot hash stands in for a fresh one only when it describes the same file.
        # After a relink it describes a different PDF entirely, and letting it match
        # `indexed_source_sha256` would report the item clean precisely when it changed most.
        # Dropping it degrades the default mode to "unknown", which `source_provenance_unknown`
        # already counts, rather than to a confident wrong answer.
        snapshot_hash = _text(mapping, "sha256")
        if recorded_source_path and source_path != recorded_source_path:
            snapshot_hash = ""

        markdown_path = _text(indexed, "markdown_path")
        markdown_file = Path(markdown_path) if markdown_path else None
        markdown_exists = markdown_file.is_file() if markdown_file else None

        try:
            canonical_exists: bool | None = canonical_markdown_path(
                config, key, _text(mapping, "title")
            ).is_file()
        except ValueError:
            canonical_exists = None

        observations.append(
            ItemObservation(
                attachment_key=key,
                in_zotero=attachment is not None,
                inventory_available=inventory_available,
                in_mapping=mapping is not None,
                classification=_text(mapping, "classification") or _text(indexed, "classification"),
                identity_status=_text(mapping, "identity_status") or _text(indexed, "identity_status"),
                identity_rule=_text(mapping, "identity_rule") or _text(indexed, "identity_rule"),
                parent_key=(
                    _text(mapping, "zotero_parent_key") or _text(indexed, "zotero_parent_key")
                ),
                title=_text(mapping, "title") or _text(indexed, "title") or (attachment.title if attachment else ""),
                mapping_metadata=_metadata(mapping),
                source_path=source_path,
                source_sha256_mapping=snapshot_hash,
                source_exists=source_exists,
                source_sha256_current=(
                    _sha256_file(source_file)
                    if full_audit and source_file and source_exists
                    else None
                ),
                markdown_exists=markdown_exists,
                markdown_sha256_current=(
                    _sha256_file(markdown_file) if markdown_file and markdown_exists else None
                ),
                canonical_markdown_exists=canonical_exists,
                in_index=indexed is not None,
                index_row_count=len(rows),
                indexed_markdown_path=markdown_path,
                indexed_markdown_sha256=_text(indexed, "markdown_sha256"),
                indexed_source_path=_text(indexed, "source_path"),
                indexed_source_sha256=_text(indexed, "source_sha256"),
                indexed_metadata=_metadata(indexed),
                indexed_at=_text(indexed, "indexed_at"),
            )
        )
    return observations


def library_status(
    config: ProjectConfig,
    mapping_report: Path,
    *,
    full_audit: bool = False,
    index_root: Path | None = None,
) -> dict[str, object]:
    """A truthful library-health summary for CLI and MCP use.

    Deliberately reports health categories and the last successful publication rather than
    calling index row counts "library coverage" -- the overstatement this function exists to
    replace. Index row counts describe the indexed *snapshot*; they say nothing about whether the
    source library still matches it, which is precisely what the audit measures.
    """
    audit = audit_library(config, mapping_report, full_audit=full_audit, index_root=index_root)
    root = index_root if index_root is not None else config.output_root / "index"
    pointer = read_current_pointer(root) or {}
    return {
        "snapshot_time": audit.snapshot_time,
        "generation_id": audit.generation_id,
        "last_published_at": pointer.get("published_at"),
        "mapping_report": audit.mapping_report,
        "full_audit": audit.full_audit,
        "total_items": audit.total_items,
        "health": dict(audit.status_counts),
        "inventory_available": audit.inventory_available,
        "ineligible_items": audit.ineligible_items,
        "source_provenance_unknown": audit.source_provenance_unknown,
        "counts_overlap": True,
        "counts_overlap_note": (
            "An attachment can hold several statuses at once; these counts overlap and do not "
            "sum to total_items."
        ),
    }


# --------------------------------------------------------------------------------------
# Snapshot loading
# --------------------------------------------------------------------------------------


def load_mapping_snapshot(mapping_report: Path) -> dict[str, dict[str, object]]:
    """Load a mapper `mapping_report.jsonl` snapshot, keyed by attachment key.

    Accepts either the JSONL file itself or the run directory containing it. Rows without an
    attachment key are skipped: they are orphan or unsupported sources, which this audit does not
    key on (the existing `orphan_candidates` reporting covers them).
    """
    path = mapping_report
    if path.is_dir():
        path = path / MAPPING_REPORT_JSONL
    if not path.is_file():
        raise LibraryAuditError(
            f"No mapping snapshot at {path}. Run `zotero-pdf-text dry-run` first, or pass "
            "--mapping-report pointing at an existing run directory."
        )

    rows: dict[str, dict[str, object]] = {}
    for row in _read_jsonl(path):
        key = str(row.get("zotero_attachment_key") or "").strip()
        if key:
            rows[key] = row
    return rows


def load_attachment_inventory(zotero_sqlite: Path) -> dict[str, AttachmentRecord]:
    """Every PDF attachment Zotero represents, keyed by attachment key.

    Read-only, and the authority on membership. The mapping snapshot cannot serve that role: the
    mapper walks source files on disk and emits a row per *file*, so an attachment whose PDF has
    been moved or deleted simply never appears in it. Inferring membership from the snapshot
    would make `missing_source` almost unreachable and would report a previously-indexed
    attachment with a vanished PDF as `orphaned_index` -- claiming Zotero dropped it when Zotero
    still lists it and the file is what went missing.
    """
    # Check before connecting: sqlite3.connect on a path that does not exist *creates* it, and
    # for this argument that means writing a file into someone's Zotero data directory. A
    # missing database is "inventory unavailable", not something to hand to sqlite.
    if not Path(zotero_sqlite).is_file():
        raise LibraryAuditError(f"No Zotero database at {zotero_sqlite}")
    # read_only is not optional here. This is the *live* database, and a read-write connection
    # lets SQLite checkpoint or recover on open and on close -- rewriting the main file and
    # discarding the WAL -- even when every statement issued is a SELECT. The audit promises to
    # touch nothing; that promise has to live in the connection, not in the queries.
    records = load_attachment_records(zotero_sqlite, read_only=True)
    inventory: dict[str, AttachmentRecord] = {}
    for record in records:
        key = (record.attachment_key or "").strip()
        if key and _is_pdf_attachment(record):
            inventory[key] = record
    return inventory


def _is_pdf_attachment(record: AttachmentRecord) -> bool:
    if (record.content_type or "").casefold() == "application/pdf":
        return True
    zotero_path = record.zotero_path or ""
    name = Path(zotero_path.split(":", 1)[1]).name if ":" in zotero_path else Path(zotero_path).name
    return name.casefold().endswith(".pdf")


def _inventory_source_path(record: AttachmentRecord, linked_root: Path) -> str:
    """Where Zotero says this attachment's PDF should be, whether or not it is there."""
    paths = resolve_attachment_paths(record.zotero_path or "", linked_root)
    return str(paths[0]) if paths else ""


def load_index_records(index_root: Path) -> tuple[str | None, dict[str, list[dict[str, object]]]]:
    """Load the published generation's metadata rows, keyed by attachment key.

    Returns a *list* per key rather than one row, because detecting `duplicate_key` requires
    seeing the duplicates. The FTS builder rejects duplicates at build time, so more than one row
    here means the index predates that check or was written out of band -- exactly the kind of
    drift worth reporting rather than silently collapsing.

    A missing or unpublished index is not an error: an unbuilt library audits fine and reports
    everything as `unindexed`.
    """
    pointer = read_current_pointer(index_root)
    if not pointer:
        return None, {}
    generation_id = str(pointer.get("current_generation") or "") or None

    rows: dict[str, list[dict[str, object]]] = {}
    try:
        jsonl_path = current_generation_jsonl(index_root)
    except ManagedIndexMissingError:
        jsonl_path = None
    if jsonl_path and jsonl_path.is_file():
        for row in _read_jsonl(jsonl_path):
            key = str(row.get("zotero_attachment_key") or "").strip()
            if key:
                rows.setdefault(key, []).append(row)
    return generation_id, rows


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------

# Metadata keys compared between the mapping snapshot and the index. Kept narrow on purpose:
# every key added here is a key that can fire `metadata_changed` across the whole library at once.
#
# Deliberately excludes `creators` and `year`. Both change during ordinary Zotero tidying -- fixing
# a creator list, filling in a missing year -- which would light up a large slice of the library as
# "changed" and bury the drift actually worth acting on. What remains are the keys that change
# identity or citation, where a hit almost certainly means something real.
METADATA_KEYS: tuple[str, ...] = ("title", "doi", "citation_key")


def _metadata(row: dict[str, object] | None) -> dict[str, str]:
    if not row:
        return {}
    return {key: str(row.get(key) or "") for key in METADATA_KEYS}


def _text(row: dict[str, object] | None, key: str) -> str:
    if not row:
        return ""
    return str(row.get(key) or "")


def _field(record: object, name: str) -> str:
    if isinstance(record, dict):
        return str(record.get(name) or "")
    return str(getattr(record, name, "") or "")


def _read_jsonl(path: Path) -> Iterator[dict[str, object]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise LibraryAuditError(f"{path}:{line_number}: not valid JSON ({exc})") from exc
            if isinstance(row, dict):
                yield row


def _sha256_file(path: Path) -> str | None:
    """Hash a file, returning None if it cannot be read.

    A read failure is reported as "not known" rather than raised: an audit that aborts on one
    unreadable file tells you nothing about the other several thousand.
    """
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None
