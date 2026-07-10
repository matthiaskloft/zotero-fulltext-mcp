from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import fitz

MATH_FONT_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "cmmi",
        "cmsy",
        "cmex",
        "cmbsy",
        "msam",
        "msbm",
        "mnsymbol",
        "stix",
        "stixmath",
        "asana",
        "lmmath",
        "latinmodern-math",
        "cambria math",
        "xits math",
    }
)

# Unicode ranges that only show up in meaningful density when a document contains real
# mathematical notation (operators, blackboard-bold letters, math italic/bold alphanumerics).
MATH_UNICODE_RANGES: tuple[tuple[int, int], ...] = (
    (0x2200, 0x22FF),  # Mathematical Operators
    (0x2100, 0x214F),  # Letterlike Symbols (∀, ℝ, ℤ, ...)
    (0x27C0, 0x27EF),  # Misc Mathematical Symbols-A
    (0x2980, 0x29FF),  # Misc Mathematical Symbols-B
    (0x2A00, 0x2AFF),  # Supplemental Mathematical Operators
    (0x1D400, 0x1D7FF),  # Mathematical Alphanumeric Symbols
)

DEFAULT_DENSITY_THRESHOLD = 0.002
DEFAULT_MAX_TEXT_SAMPLE_PAGES = 25


@dataclass(frozen=True)
class MathDetectionResult:
    has_math: bool
    font_signals: list[str]
    unicode_math_char_count: int
    unicode_math_density: float
    pages_sampled: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _has_math_font(font_names: list[str]) -> tuple[bool, list[str]]:
    """Check font basenames for known math-font substrings (case-insensitive)."""
    signals: set[str] = set()
    for name in font_names:
        lowered = name.lower()
        for substring in MATH_FONT_SUBSTRINGS:
            if substring in lowered:
                signals.add(substring)
    return bool(signals), sorted(signals)


def _is_math_char(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in MATH_UNICODE_RANGES)


def _unicode_math_density(text: str) -> tuple[int, float]:
    """Return (math_char_count, density) for the given text sample."""
    if not text:
        return 0, 0.0
    math_char_count = sum(1 for ch in text if _is_math_char(ord(ch)))
    return math_char_count, math_char_count / len(text)


def detect_math(
    source: Path,
    *,
    max_text_sample_pages: int = DEFAULT_MAX_TEXT_SAMPLE_PAGES,
    threshold: float = DEFAULT_DENSITY_THRESHOLD,
) -> MathDetectionResult:
    """Detect whether a PDF likely contains mathematical notation.

    Combines two independent, cheap signals: math-specific embedded font names (checked
    across every page, since font-metadata lookup doesn't require text extraction) and
    Unicode math-symbol density in a capped sample of extracted text (math can appear
    anywhere in a paper, not just early pages, so the cap is generous rather than reusing
    the smaller early_pages convention used for identity checks elsewhere in this project).
    """
    font_names: list[str] = []
    text_parts: list[str] = []
    with fitz.open(source) as document:
        for page_index, page in enumerate(document):
            for font in page.get_fonts(full=True):
                font_names.append(font[3])
            if page_index < max_text_sample_pages:
                text_parts.append(page.get_text("text"))

    has_font_signal, font_signals = _has_math_font(font_names)
    sampled_text = "".join(text_parts)
    math_char_count, density = _unicode_math_density(sampled_text)
    has_math = has_font_signal or density > threshold

    return MathDetectionResult(
        has_math=has_math,
        font_signals=font_signals,
        unicode_math_char_count=math_char_count,
        unicode_math_density=density,
        pages_sampled=len(text_parts),
    )
