from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - exercised when dependency is absent
    fuzz = None


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I)
    value = value.strip().rstrip(".,;)")
    return value.casefold()


def extract_year(value: str | None) -> str:
    if not value:
        return ""
    match = YEAR_RE.search(value)
    return match.group(0) if match else ""


def extract_dois(text: str) -> list[str]:
    found = {normalize_doi(match.group(0)) for match in DOI_RE.finditer(text or "")}
    return sorted(doi for doi in found if doi)


def safe_folder_id(logical_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", logical_id)
    safe = re.sub(r"_+", "_", safe).strip("._")
    return safe[:160] or "unknown"


def title_score(title: str | None, text: str) -> int:
    title_norm = normalize_text(title)
    text_norm = normalize_text(text)
    if not title_norm or not text_norm:
        return 0
    if fuzz is not None:
        return int(fuzz.partial_ratio(title_norm, text_norm))
    # Dependency-free fallback for tests or emergency runs.
    title_words = set(title_norm.split())
    text_words = set(text_norm.split())
    if not title_words:
        return 0
    return int(100 * len(title_words & text_words) / len(title_words))


@dataclass(frozen=True)
class IdentityEvidence:
    status: str
    rule: str
    title_score: int
    author_evidence: bool
    year_evidence: bool
    observed_dois: list[str]


def classify_identity(
    *,
    title: str | None,
    doi: str | None,
    year: str | None,
    author_surnames: list[str],
    item_type: str | None,
    text: str,
) -> IdentityEvidence:
    expected_doi = normalize_doi(doi)
    observed = extract_dois(text)
    observed_set = set(observed)
    score = title_score(title, text)
    normalized_body = normalize_text(text)
    author_hit = any(normalize_text(name) in normalized_body for name in author_surnames if name)
    year_hit = bool(year and year in text)

    if expected_doi and expected_doi in observed_set:
        return IdentityEvidence("verified", "doi_exact", score, author_hit, year_hit, observed)

    if score >= 86 and (author_hit or year_hit):
        return IdentityEvidence("verified", "title_author_or_year", score, author_hit, year_hit, observed)

    strict_types = {"journalArticle", "conferencePaper", "preprint", "report"}
    if expected_doi and observed and expected_doi not in observed_set and score < 50:
        return IdentityEvidence("possible_mismatch", "conflicting_doi_low_title", score, author_hit, year_hit, observed)

    if (item_type or "") in strict_types and title and score < 35 and not author_hit and not year_hit:
        return IdentityEvidence("unverified", "weak_text_evidence", score, author_hit, year_hit, observed)

    return IdentityEvidence("unverified", "insufficient_evidence", score, author_hit, year_hit, observed)
