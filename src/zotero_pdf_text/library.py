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
    GENERATION_JSONL_FILENAME,
    CurrentPointerError,
    ManagedIndexMissingError,
    read_current_pointer,
    resolve_generation_dir,
)
from .config import ProjectConfig
from .identity import resolve_attachment_paths
from .zotero_db import AttachmentRecord, load_attachment_records, snapshot_for_reading

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
# Not "the source changed" but "nobody could tell". The index records a source hash, yet this
# audit has nothing to compare it against -- the attachment was relinked, so the snapshot's hash
# describes the previous file, or it never reached the mapper and no snapshot hash exists. It is
# a separate status rather than silence because silence here reads as `current`, which is the one
# answer known to be wrong. `--full` resolves it by hashing the file the audit can actually see.
STATUS_SOURCE_UNCHECKED = "source_unchecked"
STATUS_METADATA_CHANGED = "metadata_changed"
STATUS_MISSING_SOURCE = "missing_source"
STATUS_MISSING_MARKDOWN = "missing_markdown"
STATUS_ORPHANED_INDEX = "orphaned_index"
# The index holds a row and this audit cannot say whether Zotero still represents it, because
# the inventory could not be read. Separate from `orphaned_index` because the two carry opposite
# advice: one says the row can go, this one says do not act until Zotero can be consulted. The
# mapping snapshot cannot settle it -- an attachment whose PDF is missing never reaches it.
STATUS_MEMBERSHIP_UNCHECKED = "membership_unchecked"
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
    STATUS_SOURCE_UNCHECKED,
    STATUS_METADATA_CHANGED,
    STATUS_MISSING_SOURCE,
    STATUS_MISSING_MARKDOWN,
    STATUS_ORPHANED_INDEX,
    STATUS_MEMBERSHIP_UNCHECKED,
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


def canonical_markdown_path(config: ProjectConfig, attachment_key: str) -> Path:
    """Where attachment `attachment_key`'s converted Markdown lives under the canonical layout.

    Keyed on the attachment key and nothing else, exactly like `canonical_image_dir`. A retitled
    Zotero item must resolve to the same file, or every locator and index row pointing at it
    silently breaks and the previous file is orphaned.

    This used to append a cosmetic title slug, which made the path a function of current
    metadata and contradicted the guarantee in this docstring: `Old Title` and `New Title`
    resolved to different files, so `canonical_markdown_exists` would look at the wrong one.
    Human-readable filenames are not worth a mutable identity; a browsable name belongs in a
    sidecar mapping if it is ever wanted, not in the path a lookup depends on.
    """
    key = validate_attachment_key(attachment_key)
    return canonical_library_root(config) / MARKDOWN_DIRNAME / f"{key}.md"


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

    # Title, DOI and citation key as Zotero holds them *now*. The snapshot's copy is only as
    # current as the last dry-run, so an edit made in Zotero afterwards is invisible to it --
    # and an attachment that never reached the mapper has no snapshot metadata at all.
    zotero_metadata: dict[str, str] = field(default_factory=dict)

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
    # Read from the same pointer as `generation_id`, never fetched again. `library_status` used
    # to read `current.json` a second time for it, which could pair one generation's id with
    # another's timestamp -- the same mixed-generation race as the rows themselves.
    published_at: str | None
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
    # Whether Zotero's attachment inventory could be read. When it could not, no membership
    # question can be answered at all: every item is reported `membership_unchecked` and the
    # membership conclusions (`current`, `unindexed`, `orphaned_index`) are withheld. Reported
    # rather than raised: a partial audit that says which question it could not answer beats
    # refusing to run because Zotero happened to be mid-sync.
    inventory_available: bool = False
    # Why it could not be read, in the failing call's own words. The flag alone tells a user
    # that something went wrong without telling them whether it is worth retrying: a database
    # that would not hold still resolves by closing Zotero, a permission or schema failure
    # never will. `SnapshotUnstableError` carries the only actionable recovery instruction the
    # audit has, and dropping it made that instruction unreachable.
    inventory_error: str | None = None

    def items_with(self, status: str) -> tuple[AuditItem, ...]:
        return tuple(item for item in self.items if item.has(status))

    def to_dict(self, *, include_items: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "snapshot_time": self.snapshot_time,
            "generation_id": self.generation_id,
            "published_at": self.published_at,
            "mapping_report": self.mapping_report,
            "full_audit": self.full_audit,
            "total_items": self.total_items,
            "status_counts": dict(self.status_counts),
            "counts_overlap": True,
            "inventory_available": self.inventory_available,
            "inventory_error": self.inventory_error,
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
    # Whether the library still contains this attachment -- three-valued, because "Zotero does
    # not list this" and "nobody could ask Zotero" call for opposite actions and a boolean
    # cannot hold them apart. This was a boolean, and collapsing the two let an unreadable
    # inventory fall through to a mapping row that only ever proved *historical* membership.
    #
    # Only Zotero can answer it. A mapping row proves the attachment existed at the last
    # dry-run, which says nothing about whether the user has deleted it since; treating the
    # snapshot as a fallback authority is what reported a deleted-but-indexed attachment as
    # `current`, and a deleted-but-unindexed one as a backlog item to go and convert.
    #
    # Positive evidence is never discarded, which is why `in_zotero` is tested before the flag:
    # a plain conditional would answer "absent" for the contradictory state
    # `in_zotero=True, inventory_available=False`, which production cannot produce but a
    # hand-built observation can.
    if obs.in_zotero:
        member: bool | None = True
    elif obs.inventory_available:
        member = False
    else:
        member = None
    # Used by every rule that must not fire against an attachment Zotero has retired, while
    # still firing when membership is merely unknown. Those findings -- a stale Markdown file, a
    # changed PDF -- are facts about files on disk, true regardless of who owns them.
    not_retired = member is not False

    # 1. Structural facts. Reported for quarantine rows too -- a duplicate key or a vanished PDF
    #    is a fact about the library, not a judgement about whether the item belongs in it.
    if obs.index_row_count > 1:
        statuses.add(STATUS_DUPLICATE_KEY)
    if obs.in_index and member is False:
        statuses.add(STATUS_ORPHANED_INDEX)
    if member is None:
        # Applies to every item in the audit, not just index-only rows, and that is the point:
        # when the count equals `total_items` it says plainly that this run could not check
        # membership for anything. Reported rather than inferred, because both directions of
        # guess are wrong -- calling a row orphaned recommends dropping one Zotero may still
        # list, and calling it current vouches for one the user may have deleted.
        statuses.add(STATUS_MEMBERSHIP_UNCHECKED)
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
    source_comparand = obs.source_sha256_current or obs.source_sha256_mapping
    if _differs(source_comparand, obs.indexed_source_sha256):
        statuses.add(STATUS_SOURCE_CHANGED)
    elif (
        not_retired
        and obs.in_index
        and obs.indexed_source_sha256
        and not source_comparand
        and obs.source_exists is not False
    ):
        # The index knows what it extracted, and this audit cannot check it. Without a status
        # the item falls through to `current`, asserting a clean bill of health that was never
        # examined -- the failure mode that made a known relink read as `current`. Suppressed
        # when the PDF is already reported missing, which says the same thing more precisely.
        statuses.add(STATUS_SOURCE_UNCHECKED)
    # Zotero's own record wins when it could be read: the snapshot's copy is only as current as
    # the last dry-run, so an edit made afterwards is invisible to it, and an attachment that
    # never reached the mapper carries no snapshot metadata at all. Requires non-empty current
    # metadata so that "we know nothing" never masquerades as "everything differs".
    current_metadata = obs.zotero_metadata if obs.in_zotero else obs.mapping_metadata
    if (
        not_retired
        and obs.in_index
        and current_metadata
        and current_metadata != obs.indexed_metadata
    ):
        statuses.add(STATUS_METADATA_CHANGED)

    # 3. Eligibility-dependent judgements.
    #    `unverified_indexed` requires `in_mapping`, which is what separates it from
    #    `orphaned_index`: the two name different repairs -- verify the identity, versus drop a
    #    row for an attachment Zotero no longer represents.
    if not_retired and obs.in_index and not eligible:
        statuses.add(STATUS_UNVERIFIED_INDEXED)
    #    An ineligible item is never `unindexed`. It is correctly absent from the library, and
    #    reporting it as a gap would manufacture a backlog that should not be worked -- the exact
    #    false signal that would wrongly push the gated migration forward.
    #
    #    `member is True`, not `not_retired`, for the same reason: `unindexed` is a membership
    #    conclusion. It asserts the attachment belongs in the library and is missing from it,
    #    and the first half is exactly what an unreadable inventory leaves unknown. Work queued
    #    from an unchecked membership converts PDFs for attachments the user may have deleted.
    #    `membership_unchecked` carries those items instead, so they are named rather than
    #    silently dropped.
    if member is True and eligible and not obs.in_index:
        statuses.add(STATUS_UNINDEXED)

    # 4. `current` last, defined as the absence of every other finding rather than by restating
    #    the conditions. A status added later then narrows `current` automatically, instead of
    #    leaving a second definition to remember to update.
    #    `member is True` is redundant today -- `membership_unchecked` already makes `statuses`
    #    non-empty -- but `current` is the one answer that must never be reached by accident,
    #    and stating the precondition keeps it out of reach if that status is ever narrowed.
    if member is True and eligible and obs.in_index and not statuses:
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

    The pointer is read once and everything else resolved from it, so the reported generation
    id, publication time and rows always describe the same generation even if a publication
    lands mid-audit. The report is then a moment old, which is what a snapshot is.
    """
    snapshot_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    root = index_root if index_root is not None else config.output_root / "index"

    mapping_rows = load_mapping_snapshot(mapping_report)
    generation_id, published_at, index_rows = load_index_records(root)

    # Zotero is the authority on which attachments exist. A snapshot cannot be, because the
    # mapper only sees attachments whose files it found. Read-only, and never fatal: an audit
    # that refuses to run because Zotero is mid-sync is less useful than one that runs and says
    # which question it could not answer.
    inventory: dict[str, AttachmentRecord] = {}
    inventory_available = False
    inventory_error: str | None = None
    try:
        inventory = load_attachment_inventory(config.zotero_sqlite)
        inventory_available = True
    except Exception as exc:
        # Broad on purpose -- every failure here degrades to the same partial audit -- but the
        # reason is kept rather than swallowed. The exception type is included because the
        # message alone does not always say whether retrying is worth anything.
        inventory = {}
        inventory_error = f"{type(exc).__name__}: {exc}"

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
        published_at=published_at,
        mapping_report=str(mapping_report),
        full_audit=full_audit,
        total_items=len(items),
        status_counts=counts,
        items=tuple(items),
        inventory_available=inventory_available,
        inventory_error=inventory_error,
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
            canonical_exists: bool | None = canonical_markdown_path(config, key).is_file()
        except ValueError:
            canonical_exists = None

        observations.append(
            ItemObservation(
                attachment_key=key,
                in_zotero=attachment is not None,
                zotero_metadata=_attachment_metadata(attachment),
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
    return {
        "snapshot_time": audit.snapshot_time,
        "generation_id": audit.generation_id,
        "last_published_at": audit.published_at,
        "mapping_report": audit.mapping_report,
        "full_audit": audit.full_audit,
        "total_items": audit.total_items,
        "health": dict(audit.status_counts),
        "inventory_available": audit.inventory_available,
        "inventory_error": audit.inventory_error,
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
    # Read a copy, never the original. `mode=ro` is not enough: it forbids writes to the
    # database but still creates the `-shm` any reader of a WAL database needs, which is a new
    # file inside the user's Zotero folder. `immutable=1` creates nothing but cannot see the
    # WAL, and fails outright when the rows live in an uncheckpointed one. Copying is the only
    # option that is both complete and genuinely non-writing.
    with snapshot_for_reading(Path(zotero_sqlite)) as snapshot:
        records = load_attachment_records(snapshot)
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


def load_index_records(
    index_root: Path,
) -> tuple[str | None, str | None, dict[str, list[dict[str, object]]]]:
    """Load the published generation's id, publication time and metadata rows.

    All three come from a single read of `current.json`, so they always describe one generation.

    Returns a *list* per key rather than one row, because detecting `duplicate_key` requires
    seeing the duplicates. The FTS builder rejects duplicates at build time, so more than one row
    here means the index predates that check or was written out of band -- exactly the kind of
    drift worth reporting rather than silently collapsing.

    A missing or unpublished index is not an error: an unbuilt library audits fine and reports
    everything as `unindexed`. A pointer that names a generation whose JSONL is absent *is* an
    error -- see below.
    """
    pointer = read_current_pointer(index_root)
    if not pointer:
        return None, None, {}
    generation_id = str(pointer.get("current_generation") or "") or None
    raw_published = pointer.get("published_at")
    published_at = str(raw_published) if raw_published is not None else None
    if not generation_id:
        return None, published_at, {}

    # Resolve everything from the pointer this function already read, rather than calling
    # `current_generation_jsonl`, which would read `current.json` a second time. A publish
    # landing between the two reads would pair generation A's id with generation B's rows and
    # the audit would report that false provenance as fact. Generation directories are
    # immutable once published, so one pointer read plus a direct resolve cannot mix identities
    # and contents -- the report is then merely a moment old, which is what a snapshot is.
    #
    # `published_at` comes from the same read for the same reason: `library_status` used to
    # fetch it separately and could pair one generation's id with another's timestamp.
    try:
        generation_dir = resolve_generation_dir(index_root, generation_id)
    except ManagedIndexMissingError:
        generation_dir = None
    jsonl_path = (generation_dir / GENERATION_JSONL_FILENAME) if generation_dir else None
    # Past the pointer, absence is corruption rather than an unbuilt library.
    # `resolve_generation_dir` does not require the directory to exist, so treating a missing
    # JSONL as "no rows" would return the generation id with an empty index and report every
    # eligible attachment as `unindexed` -- a broken publication rendered as a routine backlog,
    # which is the reading most likely to send someone re-converting a library that is fine.
    if jsonl_path is None or not jsonl_path.is_file():
        raise CurrentPointerError(
            "current.json names generation '%s' but its JSONL file is missing. The published "
            "index is incomplete. Re-publish with 'zotero-pdf-text rebuild-index'."
            % generation_id
        )

    rows: dict[str, list[dict[str, object]]] = {}
    for row in _read_jsonl(jsonl_path):
        key = str(row.get("zotero_attachment_key") or "").strip()
        if key:
            rows.setdefault(key, []).append(row)
    return generation_id, published_at, rows


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


def _attachment_metadata(record: AttachmentRecord | None) -> dict[str, str]:
    """Project a live Zotero attachment onto the same keys the index stores.

    Safe to compare against `indexed_metadata` because the mapper derives its own row from this
    very dataclass, so both sides of the comparison come from one definition of each field --
    a `citation_key` computed differently on each side would fire across the whole library.
    """
    if record is None:
        return {}
    return {key: str(getattr(record, key, "") or "") for key in METADATA_KEYS}


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
