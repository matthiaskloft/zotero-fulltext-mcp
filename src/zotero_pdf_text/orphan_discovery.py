from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import ProjectConfig
from .identity import IdentityEvidence, classify_identity
from .lock import PipelineLockedError, pipeline_write_lock
from .orphan_candidates import (
    CONFIDENCE_HIGH,
    STATUS_RESOLVED,
    STATUS_SKIPPED,
    OrphanCandidate,
    append_master_candidates,
    find_candidate,
    mark_status,
    write_run_candidates,
)
from .pdf_probe import extract_early_text
from .zotero_db import ParentCandidateRecord, load_items_without_pdf_attachment

CANDIDATES_RELATIVE_PATH = Path("index") / "orphan_candidates.jsonl"

# Reported matches are built directly on top of classify_identity's own "verified" status (DOI
# exact match, or a strong title match corroborated by an author/year hit) -- not a second scoring
# algorithm layered on top. Early real-library smoke testing tried a looser fuzzy-title-score tier
# for classify_identity's "unverified" results too, but a short, generic Zotero item title
# ("Citations", "Index", "Preface" -- an individual chapter entry within an edited volume with no
# PDF of its own) gets a trivially high fuzz.partial_ratio against almost any academic PDF's text
# regardless of the threshold chosen, since the fuzzy-matching signal is nearly meaningless once
# the title is one or two common words. Trusting title_score at all when classify_identity itself
# left the verdict unverified was the problem, not the specific threshold value -- so only
# "verified" pairings are ever reported.

# Cap the number of candidate parents reported per orphan so a generically-titled PDF with many
# weak partial matches doesn't flood the report; the top matches are what a reviewer needs.
MAX_CANDIDATES_PER_ORPHAN = 5


def run_orphan_discovery(
    config: ProjectConfig,
    mapping_report_path: Path,
    *,
    output_dir: Path | None = None,
    limit: int | None = None,
) -> Path:
    """Discover plausible Zotero parents for orphan PDFs, scored on PDF content, not filename.

    For every `orphan_pdf` row in a prior dry-run's mapping report, extracts early-page text (the
    same window the rest of the pipeline already uses) and scores it with classify_identity
    against every Zotero item that has no *working* PDF attachment of its own -- either no PDF
    attachment row at all, or one whose recorded path no longer resolves to a real file (see
    `zotero_db.load_items_without_pdf_attachment`) -- the only items an orphan PDF could plausibly
    belong to. Only high-confidence pairings (classify_identity's own "verified" status) are
    reported (see the module-level comment above). Never opens the Zotero SQLite database for
    anything but reads, never converts PDFs to Markdown, and never writes to Zotero.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir or (config.output_root / "orphan_discovery" / timestamp)
    run_dir.mkdir(parents=True, exist_ok=True)

    orphan_rows = _load_orphan_rows(mapping_report_path)
    if limit is not None:
        orphan_rows = orphan_rows[:limit]

    parent_candidates = load_items_without_pdf_attachment(config.zotero_sqlite, config.linked_attachments)
    logging.info(
        "Orphan discovery: %s orphan PDFs, %s candidate parent items without a working PDF attachment",
        len(orphan_rows),
        len(parent_candidates),
    )

    all_candidates: list[OrphanCandidate] = []
    for row in orphan_rows:
        source_path = Path(row.get("source_path", ""))
        try:
            text, page_count = extract_early_text(
                source_path,
                pages=config.early_pages,
                max_page_chars=config.max_page_chars,
            )
        except Exception as exc:
            logging.warning("Skipping unreadable orphan PDF %s: %s", source_path, exc)
            continue

        matches = score_candidates(text, parent_candidates)
        detected_at = datetime.now().isoformat(timespec="seconds")
        for candidate, evidence, tier in matches:
            all_candidates.append(
                OrphanCandidate(
                    orphan_source_path=str(source_path),
                    orphan_sha256=row.get("sha256", ""),
                    orphan_safe_folder_id=row.get("safe_folder_id", ""),
                    orphan_page_count=page_count,
                    candidate_parent_key=candidate.parent_key,
                    candidate_item_type=candidate.item_type or "",
                    candidate_title=candidate.title,
                    candidate_creators="; ".join(candidate.creators),
                    candidate_year=candidate.year,
                    candidate_doi=candidate.doi,
                    candidate_citation_key=candidate.citation_key,
                    candidate_had_stale_attachment=candidate.had_stale_attachment,
                    title_score=evidence.title_score,
                    author_evidence=evidence.author_evidence,
                    year_evidence=evidence.year_evidence,
                    observed_dois=";".join(evidence.observed_dois),
                    confidence_tier=tier,
                    identity_rule=evidence.rule,
                    detected_at=detected_at,
                )
            )

    write_run_candidates(run_dir, all_candidates)
    master_path = config.output_root / CANDIDATES_RELATIVE_PATH
    append_master_candidates(master_path, all_candidates)
    return run_dir


def score_candidates(
    text: str,
    parent_candidates: list[ParentCandidateRecord],
) -> list[tuple[ParentCandidateRecord, IdentityEvidence, str]]:
    """Score one orphan PDF's early text against every no-PDF Zotero item, reusing classify_identity.

    Only "high"-confidence matches are returned (classify_identity's own "verified" status);
    anything else -- including a merely plausible but unverified title match -- is dropped.
    Results are sorted by title_score, capped to MAX_CANDIDATES_PER_ORPHAN.
    """
    scored: list[tuple[ParentCandidateRecord, IdentityEvidence, str]] = []
    for candidate in parent_candidates:
        evidence = classify_identity(
            title=candidate.title,
            doi=candidate.doi,
            year=candidate.year,
            author_surnames=candidate.creator_surnames,
            item_type=candidate.item_type,
            text=text,
        )
        if evidence.status != "verified":
            continue
        scored.append((candidate, evidence, CONFIDENCE_HIGH))

    scored.sort(key=lambda item: -item[1].title_score)
    return scored[:MAX_CANDIDATES_PER_ORPHAN]


def _load_orphan_rows(mapping_report_path: Path) -> list[dict[str, str]]:
    with mapping_report_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [row for row in rows if row.get("classification") == "orphan_pdf"]


@dataclass
class OrphanResolutionResult:
    ok: bool
    action: str  # "skip" | "mark-resolved"
    orphan_sha256: str
    candidate_parent_key: str
    previous_status: str
    new_status: str
    error: str
    resolved_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def skip_orphan_candidate(
    config: ProjectConfig,
    orphan_sha256: str,
    candidate_parent_key: str,
    *,
    reason: str,
) -> OrphanResolutionResult:
    """Permanently dismiss one suggested (orphan PDF, candidate parent) pairing as not a match.

    Only touches this feature's own persisted master file -- never Zotero, Markdown, or the
    sidecar index.
    """
    match_key = f"{orphan_sha256}:{candidate_parent_key}"
    master_path = config.output_root / CANDIDATES_RELATIVE_PATH
    try:
        candidate = find_candidate(master_path, match_key)
    except KeyError as exc:
        return _resolution_error("skip", orphan_sha256, candidate_parent_key, str(exc))
    previous_status = str(candidate.get("status", "pending"))
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with pipeline_write_lock(config.output_root, command="orphan-candidate"):
            mark_status(
                master_path, match_key, status=STATUS_SKIPPED, extra_fields={"skip_reason": reason, "skipped_at": now}
            )
    except PipelineLockedError as exc:
        return _resolution_error("skip", orphan_sha256, candidate_parent_key, str(exc), previous_status=previous_status)
    return OrphanResolutionResult(
        ok=True,
        action="skip",
        orphan_sha256=orphan_sha256,
        candidate_parent_key=candidate_parent_key,
        previous_status=previous_status,
        new_status=STATUS_SKIPPED,
        error="",
        resolved_at=now,
    )


def mark_orphan_candidate_resolved(
    config: ProjectConfig,
    orphan_sha256: str,
    candidate_parent_key: str,
    *,
    note: str = "",
) -> OrphanResolutionResult:
    """Record that a suggested pairing was confirmed and separately attached via `link-pdf`.

    This is bookkeeping only -- it does not call `link_local_pdf` or otherwise touch Zotero.
    Run `link-pdf --key <candidate_parent_key> --file <orphan_source_path>` yourself first.
    """
    match_key = f"{orphan_sha256}:{candidate_parent_key}"
    master_path = config.output_root / CANDIDATES_RELATIVE_PATH
    try:
        candidate = find_candidate(master_path, match_key)
    except KeyError as exc:
        return _resolution_error("mark-resolved", orphan_sha256, candidate_parent_key, str(exc))
    previous_status = str(candidate.get("status", "pending"))
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with pipeline_write_lock(config.output_root, command="orphan-candidate"):
            mark_status(
                master_path,
                match_key,
                status=STATUS_RESOLVED,
                extra_fields={"resolved_via": "link-pdf", "resolved_note": note, "resolved_at": now},
            )
    except PipelineLockedError as exc:
        return _resolution_error(
            "mark-resolved", orphan_sha256, candidate_parent_key, str(exc), previous_status=previous_status
        )
    return OrphanResolutionResult(
        ok=True,
        action="mark-resolved",
        orphan_sha256=orphan_sha256,
        candidate_parent_key=candidate_parent_key,
        previous_status=previous_status,
        new_status=STATUS_RESOLVED,
        error="",
        resolved_at=now,
    )


def _resolution_error(
    action: str, orphan_sha256: str, candidate_parent_key: str, error: str, *, previous_status: str = ""
) -> OrphanResolutionResult:
    return OrphanResolutionResult(
        ok=False,
        action=action,
        orphan_sha256=orphan_sha256,
        candidate_parent_key=candidate_parent_key,
        previous_status=previous_status,
        new_status=previous_status,
        error=error,
        resolved_at="",
    )
