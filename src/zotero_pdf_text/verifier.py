from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import ProjectConfig
from .converter import ConversionResult, convert_unverified
from .identity import classify_identity, normalize_doi, strip_front_matter


@dataclass
class VerificationReview:
    decision: str
    confidence: float
    review_rule: str
    reason: str
    matched_fields: list[str]
    evidence_snippets: list[dict[str, str]]
    evidence_status: str
    evidence_rule: str
    title_score: int
    author_evidence: bool
    year_evidence: bool
    observed_dois: list[str]
    conversion_status: str
    conversion_error: str
    zotero_parent_key: str
    zotero_attachment_key: str
    item_type: str
    title: str
    creators: str
    year: str
    doi: str
    citation_key: str
    source_path: str
    markdown_path: str
    extraction_tool: str
    page_count: str
    classification: str
    identity_status: str
    identity_rule: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def verify_unverified(
    config: ProjectConfig,
    mapping_report: Path,
    *,
    limit: int | None = None,
    output_dir: Path | None = None,
    resume: bool = False,
    workers: int | None = None,
    timeout_seconds: int = 600,
    force: bool = False,
    include_possible_mismatch: bool = False,
    agent_batch_size: int = 25,
    index_jsonl: Path | None = None,
) -> Path:
    run_dir = convert_unverified(
        config,
        mapping_report,
        limit=limit,
        output_dir=output_dir,
        resume=resume,
        workers=workers,
        timeout_seconds=timeout_seconds,
        force=force,
        include_possible_mismatch=include_possible_mismatch,
        index_jsonl=index_jsonl,
    )
    review_unverified_manifest(run_dir / "manifest.csv", run_dir=run_dir, agent_batch_size=agent_batch_size)
    return run_dir


def review_unverified_manifest(
    manifest: Path,
    *,
    run_dir: Path | None = None,
    agent_batch_size: int = 25,
) -> list[VerificationReview]:
    if not manifest.exists():
        raise FileNotFoundError(manifest)
    if agent_batch_size < 0:
        raise ValueError("agent_batch_size must be at least 0")
    run_dir = run_dir or manifest.parent
    reviews = [_review_manifest_row(row) for row in _read_csv(manifest)]
    _write_review_jsonl(run_dir / "review.jsonl", reviews)
    _write_review_csv(run_dir / "review.csv", reviews)
    _write_verification_summary(run_dir / "verification_summary.md", manifest, reviews)
    if agent_batch_size:
        _write_agent_batches(run_dir, reviews, agent_batch_size)
    return reviews


def apply_verification(
    review_path: Path,
    output_manifest: Path,
    *,
    base_manifest: Path | None = None,
    min_confidence: float = 0.92,
) -> dict[str, object]:
    if not 0 <= min_confidence <= 1:
        raise ValueError("min_confidence must be between 0 and 1")
    if not review_path.exists():
        raise FileNotFoundError(review_path)

    base_rows = _read_csv(base_manifest) if base_manifest else []
    duplicate_index = _duplicate_index(base_rows)
    accepted_rows: list[dict[str, str]] = []
    skipped_low_confidence = 0
    skipped_duplicate = 0
    skipped_duplicate_by_reason: dict[str, int] = {}
    skipped_non_accept = 0

    for review in _read_review_rows(review_path):
        decision = str(review.get("decision", ""))
        confidence = _float(review.get("confidence", 0.0))
        if decision != "accept":
            skipped_non_accept += 1
            continue
        if confidence < min_confidence:
            skipped_low_confidence += 1
            continue
        duplicate_reason = _duplicate_reason(review, duplicate_index)
        if duplicate_reason:
            skipped_duplicate += 1
            skipped_duplicate_by_reason[duplicate_reason] = skipped_duplicate_by_reason.get(duplicate_reason, 0) + 1
            continue
        accepted_row = _accepted_manifest_row(review)
        accepted_rows.append(accepted_row)
        _add_to_duplicate_index(duplicate_index, accepted_row)

    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = _manifest_fieldnames(base_rows, accepted_rows)
    with output_manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in base_rows:
            writer.writerow(row)
        for row in accepted_rows:
            writer.writerow(row)

    summary = {
        "review": str(review_path),
        "base_manifest": str(base_manifest) if base_manifest else "",
        "output_manifest": str(output_manifest),
        "min_confidence": min_confidence,
        "base_rows": len(base_rows),
        "promoted_rows": len(accepted_rows),
        "skipped_non_accept": skipped_non_accept,
        "skipped_low_confidence": skipped_low_confidence,
        "skipped_duplicate": skipped_duplicate,
        "skipped_duplicate_by_reason": skipped_duplicate_by_reason,
        "total_rows": len(base_rows) + len(accepted_rows),
    }
    _write_apply_summary(output_manifest.with_suffix(".summary.md"), summary)
    return summary


def _duplicate_index(rows: list[dict[str, str]]) -> dict[str, set[str]]:
    index = {
        "attachment": set(),
        "parent": set(),
        "doi": set(),
        "citation_key": set(),
    }
    for row in rows:
        _add_to_duplicate_index(index, row)
    return index


def _add_to_duplicate_index(index: dict[str, set[str]], row: dict[str, str]) -> None:
    attachment_key = str(row.get("zotero_attachment_key", "")).strip()
    parent_key = str(row.get("zotero_parent_key", "")).strip()
    doi = normalize_doi(str(row.get("doi", "")))
    citation_key = str(row.get("citation_key", "")).strip().lower()
    if attachment_key:
        index["attachment"].add(attachment_key)
    if parent_key:
        index["parent"].add(parent_key)
    if doi:
        index["doi"].add(doi)
    if citation_key:
        index["citation_key"].add(citation_key)


def _duplicate_reason(row: dict[str, object], index: dict[str, set[str]]) -> str:
    attachment_key = str(row.get("zotero_attachment_key", "")).strip()
    parent_key = str(row.get("zotero_parent_key", "")).strip()
    doi = normalize_doi(str(row.get("doi", "")))
    citation_key = str(row.get("citation_key", "")).strip().lower()
    if attachment_key and attachment_key in index["attachment"]:
        return "attachment"
    if parent_key and parent_key in index["parent"]:
        return "parent"
    if doi and doi in index["doi"]:
        return "doi"
    if citation_key and citation_key in index["citation_key"]:
        return "citation_key"
    return ""


def _review_manifest_row(row: dict[str, str]) -> VerificationReview:
    if row.get("status") not in {"converted", "skipped_existing"} or not row.get("output_path"):
        return _error_review(row, "conversion_error", row.get("error", "No converted Markdown available"))

    markdown_path = Path(row["output_path"])
    try:
        text = strip_front_matter(markdown_path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return _error_review(row, "markdown_read_error", f"{type(exc).__name__}: {exc}")

    evidence = classify_identity(
        title=row.get("title", ""),
        doi=row.get("doi", ""),
        year=row.get("year", ""),
        author_surnames=_creator_surnames(row.get("creators", "")),
        item_type=row.get("item_type", ""),
        text=text,
    )
    decision, confidence, review_rule, reason = _decision_from_evidence(evidence)
    matched_fields = _matched_fields(row, evidence)
    return VerificationReview(
        decision=decision,
        confidence=confidence,
        review_rule=review_rule,
        reason=reason,
        matched_fields=matched_fields,
        evidence_snippets=_evidence_snippets(row, text, evidence.observed_dois),
        evidence_status=evidence.status,
        evidence_rule=evidence.rule,
        title_score=evidence.title_score,
        author_evidence=evidence.author_evidence,
        year_evidence=evidence.year_evidence,
        observed_dois=evidence.observed_dois,
        conversion_status=row.get("status", ""),
        conversion_error=row.get("error", ""),
        zotero_parent_key=row.get("zotero_parent_key", ""),
        zotero_attachment_key=row.get("zotero_attachment_key", ""),
        item_type=row.get("item_type", ""),
        title=row.get("title", ""),
        creators=row.get("creators", ""),
        year=row.get("year", ""),
        doi=row.get("doi", ""),
        citation_key=row.get("citation_key", ""),
        source_path=row.get("source_path", ""),
        markdown_path=row.get("output_path", ""),
        extraction_tool=row.get("extraction_tool", ""),
        page_count=row.get("page_count", ""),
        classification=row.get("classification", ""),
        identity_status=row.get("identity_status", ""),
        identity_rule=row.get("identity_rule", ""),
    )


def _error_review(row: dict[str, str], rule: str, reason: str) -> VerificationReview:
    return VerificationReview(
        decision="manual_review",
        confidence=0.0,
        review_rule=rule,
        reason=reason,
        matched_fields=[],
        evidence_snippets=[],
        evidence_status="error",
        evidence_rule=rule,
        title_score=0,
        author_evidence=False,
        year_evidence=False,
        observed_dois=[],
        conversion_status=row.get("status", ""),
        conversion_error=row.get("error", ""),
        zotero_parent_key=row.get("zotero_parent_key", ""),
        zotero_attachment_key=row.get("zotero_attachment_key", ""),
        item_type=row.get("item_type", ""),
        title=row.get("title", ""),
        creators=row.get("creators", ""),
        year=row.get("year", ""),
        doi=row.get("doi", ""),
        citation_key=row.get("citation_key", ""),
        source_path=row.get("source_path", ""),
        markdown_path=row.get("output_path", ""),
        extraction_tool=row.get("extraction_tool", ""),
        page_count=row.get("page_count", ""),
        classification=row.get("classification", ""),
        identity_status=row.get("identity_status", ""),
        identity_rule=row.get("identity_rule", ""),
    )


def _decision_from_evidence(evidence) -> tuple[str, float, str, str]:
    if evidence.rule == "doi_exact":
        return "accept", 0.99, "auto_accept_doi_exact", "Expected DOI is present in converted full text."
    if evidence.status == "possible_mismatch":
        return (
            "reject",
            0.95,
            "auto_reject_conflicting_doi_low_title",
            "Converted text contains a different, confidently-parsed DOI than the expected one.",
        )
    if evidence.title_score >= 95 and evidence.author_evidence and evidence.year_evidence:
        return (
            "accept",
            0.97,
            "auto_accept_title_author_year",
            "Title, author surname, and year all match strongly in converted full text.",
        )
    if evidence.title_score >= 94 and evidence.author_evidence:
        return (
            "accept",
            0.94,
            "auto_accept_title_author",
            "Title and author surname match strongly in converted full text.",
        )
    if evidence.title_score >= 94 and evidence.year_evidence:
        return (
            "accept",
            0.93,
            "auto_accept_title_year",
            "Title and year match strongly in converted full text.",
        )
    if evidence.title_score >= 90 and evidence.author_evidence and evidence.year_evidence:
        return (
            "accept",
            0.93,
            "auto_accept_title_author_year_moderate",
            "Title, author surname, and year provide combined support.",
        )
    confidence = min(0.89, max(0.25, evidence.title_score / 100))
    if evidence.author_evidence:
        confidence = min(0.89, confidence + 0.05)
    if evidence.year_evidence:
        confidence = min(0.89, confidence + 0.03)
    return (
        "manual_review",
        round(confidence, 2),
        "needs_agent_or_manual_review",
        "Full-text evidence is not strong enough for automatic promotion.",
    )


def _matched_fields(row: dict[str, str], evidence) -> list[str]:
    fields: list[str] = []
    expected_doi = normalize_doi(row.get("doi", ""))
    if expected_doi and expected_doi in set(evidence.observed_dois):
        fields.append("doi")
    elif expected_doi and evidence.observed_dois:
        fields.append("conflicting_doi")
    if evidence.title_score >= 86:
        fields.append("title")
    if evidence.author_evidence:
        fields.append("author")
    if evidence.year_evidence:
        fields.append("year")
    return fields


def _evidence_snippets(row: dict[str, str], text: str, observed_dois: list[str]) -> list[dict[str, str]]:
    snippets: list[dict[str, str]] = []
    first_lines = _first_nonempty_lines(text, limit=5)
    if first_lines:
        snippets.append({"field": "title_area", "text": _clean_snippet(" ".join(first_lines), limit=360)})

    doi = normalize_doi(row.get("doi", ""))
    if doi:
        snippet = _snippet_for_term(text, doi)
        if snippet:
            snippets.append({"field": "doi", "text": snippet})
    for observed in observed_dois[:3]:
        snippet = _snippet_for_term(text, observed)
        if snippet:
            snippets.append({"field": "observed_doi", "text": snippet})

    for surname in _creator_surnames(row.get("creators", ""))[:3]:
        snippet = _snippet_for_term(text, surname)
        if snippet:
            snippets.append({"field": "author", "text": snippet})
            break
    if row.get("year"):
        snippet = _snippet_for_term(text, row["year"])
        if snippet:
            snippets.append({"field": "year", "text": snippet})

    title_words = [word for word in re.findall(r"[A-Za-z0-9]{5,}", row.get("title", "")) if word.casefold() not in _STOPWORDS]
    for word in sorted(title_words, key=len, reverse=True)[:3]:
        snippet = _snippet_for_term(text, word)
        if snippet:
            snippets.append({"field": "title_term", "text": snippet})
            break
    return snippets[:6]


def _accepted_manifest_row(review: dict[str, object]) -> dict[str, str]:
    review_rule = str(review.get("review_rule", "accepted"))
    return {
        "status": "skipped_existing",
        "extraction_tool": str(review.get("extraction_tool", "")),
        "zotero_parent_key": str(review.get("zotero_parent_key", "")),
        "zotero_attachment_key": str(review.get("zotero_attachment_key", "")),
        "item_type": str(review.get("item_type", "")),
        "title": str(review.get("title", "")),
        "creators": str(review.get("creators", "")),
        "year": str(review.get("year", "")),
        "doi": str(review.get("doi", "")),
        "citation_key": str(review.get("citation_key", "")),
        "source_path": str(review.get("source_path", "")),
        "output_path": str(review.get("markdown_path", "")),
        "page_count": str(review.get("page_count", "")),
        "classification": "mapped_verified",
        "identity_status": "fulltext_verified",
        "identity_rule": f"fulltext_review:{review_rule}",
        "error": "",
    }


def _manifest_fieldnames(base_rows: list[dict[str, str]], accepted_rows: list[dict[str, str]]) -> list[str]:
    if base_rows:
        fieldnames = list(base_rows[0].keys())
    else:
        fieldnames = list(ConversionResult.__dataclass_fields__)
    for row in accepted_rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    return fieldnames


def _read_csv(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_review_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                row["confidence"] = _float(row.get("confidence", 0.0))
                rows.append(row)
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _write_review_jsonl(path: Path, reviews: list[VerificationReview]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for review in reviews:
            handle.write(json.dumps(review.to_dict(), ensure_ascii=False) + "\n")


def _write_review_csv(path: Path, reviews: list[VerificationReview]) -> None:
    fieldnames = list(VerificationReview.__dataclass_fields__)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for review in reviews:
            row = review.to_dict()
            row["matched_fields"] = ";".join(review.matched_fields)
            row["observed_dois"] = ";".join(review.observed_dois)
            row["evidence_snippets"] = json.dumps(review.evidence_snippets, ensure_ascii=False)
            writer.writerow(row)


def _write_verification_summary(path: Path, manifest: Path, reviews: list[VerificationReview]) -> None:
    counts: dict[str, int] = {}
    rules: dict[str, int] = {}
    for review in reviews:
        counts[review.decision] = counts.get(review.decision, 0) + 1
        rules[review.review_rule] = rules.get(review.review_rule, 0) + 1
    lines = [
        "# Unverified Full-Text Review Summary",
        "",
        f"- Created: {datetime.now().isoformat(timespec='seconds')}",
        f"- Manifest: `{manifest}`",
        f"- Reviewed rows: {len(reviews)}",
        "",
        "## Decisions",
        "",
        *[f"- `{key}`: {counts[key]}" for key in sorted(counts)],
        "",
        "## Review Rules",
        "",
        *[f"- `{key}`: {rules[key]}" for key in sorted(rules)],
        "",
        "## Artifacts",
        "",
        "- `review.jsonl`: machine-readable decisions and evidence",
        "- `review.csv`: spreadsheet-friendly decisions and evidence",
        "- `agent_batches/`: bounded JSONL batches for cheap LLM review of ambiguous rows",
        "- `agent_review_prompt.md`: instructions and strict output schema for subagents",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_agent_batches(run_dir: Path, reviews: list[VerificationReview], batch_size: int) -> None:
    candidates = [review for review in reviews if review.decision != "accept"]
    batch_dir = run_dir / "agent_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    for old in batch_dir.glob("*.jsonl"):
        old.unlink()
    for batch_index in range(math.ceil(len(candidates) / batch_size)):
        batch = candidates[batch_index * batch_size : (batch_index + 1) * batch_size]
        path = batch_dir / f"batch_{batch_index + 1:04d}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for review in batch:
                handle.write(json.dumps(review.to_dict(), ensure_ascii=False) + "\n")
    _write_agent_prompt(run_dir / "agent_review_prompt.md", batch_dir)


def _write_agent_prompt(path: Path, batch_dir: Path) -> None:
    lines = [
        "# Agent Review Prompt",
        "",
        "Review one `agent_batches/*.jsonl` file. Each row is a Zotero attachment whose mapped PDF was not trusted by the early-page mapper.",
        "",
        "Use the row metadata, evidence snippets, and `markdown_path` when more context is needed. Do not modify Zotero, PDFs, Markdown, or mapping reports.",
        "",
        "Return strict JSONL with one object per input row using these fields:",
        "",
        "- `zotero_attachment_key`",
        "- `decision`: `accept`, `reject`, or `manual_review`",
        "- `confidence`: number between 0 and 1",
        "- `matched_fields`: array of strings such as `doi`, `title`, `author`, `year`, or `conflicting_doi`",
        "- `evidence_snippets`: short bounded excerpts only",
        "- `reason`: one sentence",
        "- `reviewer`: model or agent name",
        "",
        "Use `accept` only for exact DOI evidence or strong title plus author/year evidence. Use `manual_review` whenever the evidence is plausible but incomplete.",
        "",
        f"Batch folder: `{batch_dir}`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_apply_summary(path: Path, summary: dict[str, object]) -> None:
    lines = [
        "# Verification Apply Summary",
        "",
        f"- Created: {datetime.now().isoformat(timespec='seconds')}",
        f"- Review: `{summary['review']}`",
        f"- Base manifest: `{summary['base_manifest']}`",
        f"- Output manifest: `{summary['output_manifest']}`",
        f"- Min confidence: {summary['min_confidence']}",
        f"- Base rows: {summary['base_rows']}",
        f"- Promoted rows: {summary['promoted_rows']}",
        f"- Skipped non-accept rows: {summary['skipped_non_accept']}",
        f"- Skipped low-confidence rows: {summary['skipped_low_confidence']}",
        f"- Skipped duplicate rows: {summary['skipped_duplicate']}",
        f"- Total rows: {summary['total_rows']}",
    ]
    duplicate_reasons = summary.get("skipped_duplicate_by_reason")
    if isinstance(duplicate_reasons, dict) and duplicate_reasons:
        lines.extend(["", "## Duplicate Reasons", ""])
        for reason, count in sorted(duplicate_reasons.items()):
            lines.append(f"- `{reason}`: {count}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _creator_surnames(creators: str) -> list[str]:
    surnames: list[str] = []
    for creator in creators.split(";"):
        cleaned = re.sub(r"\s+", " ", creator).strip()
        if not cleaned:
            continue
        if "," in cleaned:
            surname = cleaned.split(",", 1)[0].strip()
        else:
            surname = cleaned.split()[-1]
        surname = re.sub(r"[^A-Za-z0-9-]+", "", surname)
        if len(surname) >= 2:
            surnames.append(surname)
    return surnames


def _first_nonempty_lines(text: str, limit: int) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if len(cleaned) < 8:
            continue
        lines.append(cleaned)
        if len(lines) >= limit:
            break
    return lines


def _snippet_for_term(text: str, term: str, *, window: int = 140) -> str:
    if not term:
        return ""
    match = re.search(re.escape(term), text, flags=re.IGNORECASE)
    if not match:
        return ""
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    return _clean_snippet(text[start:end])


def _clean_snippet(value: str, *, limit: int = 300) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_STOPWORDS = {
    "about",
    "after",
    "among",
    "based",
    "between",
    "effects",
    "model",
    "models",
    "study",
    "using",
    "within",
}
