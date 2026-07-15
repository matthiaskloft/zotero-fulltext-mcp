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
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# A paper's own DOI stamp, author byline, and abstract normally land within the first page or two
# of converted Markdown. Reference lists and cited works can run for many more pages after that,
# so scanning the *whole* document for DOI/author/year evidence lets a document that merely cites a
# different-DOI work, or a bibliography entry sharing an author's surname, masquerade as identity
# evidence about the document itself. Bounding the scan to a leading window keeps this evidence
# self-referential. Title matching is intentionally NOT bounded: `title_score` uses partial-ratio
# fuzzy matching, and a title can legitimately appear after a long author list or abstract.
EVIDENCE_WINDOW_CHARS = 6000


def strip_front_matter(markdown: str) -> str:
    """Remove a leading YAML front-matter block (between ``---`` fences), if present."""
    if not markdown.startswith("---\n"):
        return markdown.strip()
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return markdown.strip()
    return markdown[end + len("\n---\n") :].strip()


def _evidence_window(text: str) -> str:
    return text[:EVIDENCE_WINDOW_CHARS]


def strip_markdown_images(text: str) -> str:
    """Remove Markdown image syntax (``![alt](path)``) before text is used as evidence.

    `pymupdf4llm.to_markdown` embeds image references like `![](.../A-Descriptive-Filename.png)`
    inline in the body text. `normalize_text` strips all non-alphanumeric characters, so an image
    filename that happens to echo a candidate's title reads as plain body prose to `title_score`
    (and would otherwise leak into DOI/author/year matching too) -- flattening a false-positive
    match into what looks like real full-text evidence. Stripping the image syntax first keeps
    only prose that was actually written by the article.
    """
    if not text:
        return ""
    return MARKDOWN_IMAGE_RE.sub(" ", text)


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
    text = strip_markdown_images(text)
    evidence_text = _evidence_window(text)
    expected_doi = normalize_doi(doi)
    observed = extract_dois(evidence_text)
    observed_set = set(observed)
    score = title_score(title, text)
    normalized_evidence = normalize_text(evidence_text)
    author_hit = any(normalize_text(name) in normalized_evidence for name in author_surnames if name)
    year_hit = bool(year and year in evidence_text)

    if expected_doi and expected_doi in observed_set:
        return IdentityEvidence("verified", "doi_exact", score, author_hit, year_hit, observed)

    # A confidently-parsed DOI that conflicts with the expected one is strong disqualifying
    # evidence on its own -- e.g. two different books can share enough generic topic vocabulary
    # to push the title score past the title_author_or_year accept threshold below. This check
    # must therefore run before that accept rule (a conflicting DOI must never be silently
    # overridden by a merely-good title score) and must not be gated on a low title score.
    if expected_doi and observed and expected_doi not in observed_set:
        return IdentityEvidence("possible_mismatch", "conflicting_doi_low_title", score, author_hit, year_hit, observed)

    if score >= 86 and (author_hit or year_hit):
        return IdentityEvidence("verified", "title_author_or_year", score, author_hit, year_hit, observed)

    strict_types = {"journalArticle", "conferencePaper", "preprint", "report"}
    if (item_type or "") in strict_types and title and score < 35 and not author_hit and not year_hit:
        return IdentityEvidence("unverified", "weak_text_evidence", score, author_hit, year_hit, observed)

    return IdentityEvidence("unverified", "insufficient_evidence", score, author_hit, year_hit, observed)
