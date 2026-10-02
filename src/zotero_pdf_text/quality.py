"""Per-record extraction-quality score and a small offline language heuristic.

Both are computed from the converted Markdown at index-build time. They are derived values: they
never touch Zotero or the source PDF, and a rebuild recomputes them from the stored text.

The score answers one question -- "is the indexed text of this paper usable?" -- so an absent
search hit can be told apart from an absent paper. It is deliberately cheap and explainable: three
bounded components multiplied together, with the raw signals stored beside the result.

    density   characters per page against a typical text page (a scanned PDF has almost none)
    wordlike  share of tokens that look like words (garbled glyph runs do not)
    clean     one minus a scaled rate of replacement/private-use characters

The thresholds are defaults chosen on synthetic fixtures, not tuned on a real library; they are
documented in docs/data-dictionary.md and live here as named constants so a later tuning pass has
one place to change. Per-page signals (share of near-empty pages) need the page map planned for
the chunking work and are not computed yet.
"""

from __future__ import annotations

import json
import re

QUALITY_GOOD = "good"
QUALITY_DEGRADED = "degraded"
QUALITY_UNUSABLE = "unusable"
QUALITY_LABELS = (QUALITY_GOOD, QUALITY_DEGRADED, QUALITY_UNUSABLE)

# A page of ordinary prose holds roughly 2000-3500 characters. At or above this many characters
# per page the density component is 1.0.
FULL_DENSITY_CHARS_PER_PAGE = 800
# With no usable page count, density is judged on the total length instead.
FULL_DENSITY_TOTAL_CHARS = 4000
# Share of word-like tokens at which that component saturates. Prose with equations, tables and
# reference lists sits well above this; garbled extraction sits far below it.
FULL_WORDLIKE_SHARE = 0.55
# A replacement/private-use rate of 1/GARBAGE_RATE_ZERO or more zeroes the clean component.
GARBAGE_RATE_ZERO = 0.05

GOOD_MIN_SCORE = 0.6
UNUSABLE_BELOW_SCORE = 0.15
# Fewer characters than this is "no text layer" whatever the page count says.
UNUSABLE_BELOW_CHARS = 200

_IMAGE_MARKUP = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_WORDLIKE = re.compile(r"^[^\W\d_]{2,}$")
_TOKEN_EDGE = re.compile(r"^[\W_]+|[\W_]+$")
# U+FFFD plus the Private Use Areas, where a failed font mapping leaves its glyph codes.
_GARBAGE_CHARS = re.compile("[\ufffd\ue000-\uf8ff\U000f0000-\U000ffffd]")


def quality_signals(text: str, page_count: str | int | None) -> dict[str, float]:
    """Measure the raw signals the score is built from. Pure and deterministic."""
    body = _IMAGE_MARKUP.sub(" ", text or "")
    chars = len(body.strip())
    pages = _parse_pages(page_count)
    tokens = body.split()
    wordlike = 0
    for token in tokens:
        if _WORDLIKE.match(_TOKEN_EDGE.sub("", token)):
            wordlike += 1
    garbage = len(_GARBAGE_CHARS.findall(body))
    return {
        "chars": float(chars),
        "pages": float(pages) if pages else 0.0,
        "chars_per_page": round(chars / pages, 1) if pages else 0.0,
        "tokens": float(len(tokens)),
        "wordlike_share": round(wordlike / len(tokens), 4) if tokens else 0.0,
        "garbage_rate": round(garbage / chars, 5) if chars else 0.0,
    }


def score_extraction(text: str, page_count: str | int | None) -> tuple[float, str, dict[str, float]]:
    """Return (score in 0..1, label, signals) for one record's converted text."""
    signals = quality_signals(text, page_count)
    chars = signals["chars"]
    if signals["pages"]:
        density = min(1.0, signals["chars_per_page"] / FULL_DENSITY_CHARS_PER_PAGE)
    else:
        density = min(1.0, chars / FULL_DENSITY_TOTAL_CHARS)
    wordlike = min(1.0, signals["wordlike_share"] / FULL_WORDLIKE_SHARE)
    clean = max(0.0, 1.0 - signals["garbage_rate"] / GARBAGE_RATE_ZERO)
    score = round(density * wordlike * clean, 3)
    if chars < UNUSABLE_BELOW_CHARS or score < UNUSABLE_BELOW_SCORE:
        label = QUALITY_UNUSABLE
    elif score < GOOD_MIN_SCORE:
        label = QUALITY_DEGRADED
    else:
        label = QUALITY_GOOD
    return score, label, signals


def quality_signals_json(signals: dict[str, float]) -> str:
    return json.dumps(signals, sort_keys=True, separators=(",", ":"))


def _parse_pages(page_count: str | int | None) -> int:
    try:
        pages = int(str(page_count).strip())
    except (TypeError, ValueError):
        return 0
    return pages if pages > 0 else 0


# ---------------------------------------------------------------------------
# Detected language
# ---------------------------------------------------------------------------

_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the of and to in is that for with as are was on by this be from or an at it which".split()),
    "de": frozenset("der die und das ist von mit den für nicht ein eine auf zu im dem des sich auch als bei".split()),
    "fr": frozenset("le la les des et est que pour dans une un du en qui au sur par pas ce avec sont".split()),
    "es": frozenset("el la los las de y que en un una es por con para del se su al lo como más".split()),
}
_LANGUAGE_SAMPLE_CHARS = 20000
_LANGUAGE_MIN_HITS = 25
_LANGUAGE_MARGIN = 1.5
_LANGUAGE_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect_language(text: str) -> str:
    """Best-effort ISO 639-1 code (en, de, fr, es) from stopword frequency, else "".

    A documented heuristic rather than a dependency: it separates the languages the library is
    likely to hold from each other and refuses to guess on short, mixed or non-Latin text. It is
    kept apart from Zotero's own `language` field and never written back.
    """
    words = [w.lower() for w in _LANGUAGE_WORD.findall((text or "")[:_LANGUAGE_SAMPLE_CHARS])]
    if not words:
        return ""
    hits = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOPWORDS.items()}
    ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
    best, runner_up = ranked[0], ranked[1]
    if best[1] < _LANGUAGE_MIN_HITS or best[1] < runner_up[1] * _LANGUAGE_MARGIN:
        return ""
    return best[0]
