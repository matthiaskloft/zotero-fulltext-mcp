"""Recognition-quality scoring for the OCR benchmark.

Classification (``benchmarks/scoring.py``) asks *which prompt* a crop is routed to. Recognition asks
the next question: given the model's output for that crop, did the notation actually survive? The
synthetic corpus seeds this -- every formula/table in ``tests/fixtures/ocr_corpus/expected.json``
carries ``expected_tokens``, the LaTeX fragments a faithful transcription must contain.

Exact LaTeX comparison is the wrong bar: ``\tfrac`` and ``\frac``, ``x^{2}`` and ``x^2``, brace and
spacing style all render the same mathematics, so pinning one spelling would fail a *correct*
answer. Instead we score TOKEN RECALL -- what fraction of the expected fragments appear in the
output -- after a normalization that erases the differences that do not change meaning.

This module is model-free and pure: it scores strings. The live model that produces those strings
runs opt-in (``tests/test_ocr_corpus.py::CorpusRecognitionTests``, gated on ``ZOTERO_PDF_TEXT_LIVE_OCR``)
or via a pressure-test harness. The CI tests here validate the *metric* on canned strings, so the
scorer stays trustworthy without a GPU, a model or a network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_EXPECTED = REPO_ROOT / "tests" / "fixtures" / "ocr_corpus" / "expected.json"


def normalize(text: str) -> str:
    """Erase LaTeX spelling differences that do not change meaning, so matching is robust.

    Backslashes become spaces (not nothing) so a bare token like ``frac`` matches ``\\frac`` while
    adjacent commands stay separate words -- deleting them would glue ``\\hat\\beta`` into
    ``hatbeta``, and the word-boundary matcher would then find *neither* token in a perfectly
    faithful output. Whitespace runs collapse to a single space so ``for all`` matches ``for  all``
    while word tokens stay distinct. Case is deliberately preserved -- ``\\Gamma`` and ``\\gamma``
    are different symbols, and a recognition benchmark that treats them as equal would reward a
    wrong transcription.
    """
    return re.sub(r"\s+", " ", text.replace("\\", " ")).strip()


def token_found(token: str, normalized_text: str) -> bool:
    """True if ``token`` appears in already-normalized OCR text as a bounded fragment.

    This is the one policy lever of the whole harness: how forgiving a match counts as "recovered".
    Normalized matching is lenient enough that ``sum`` matches ``\\sum_{j}`` and stays strict on
    case -- but a bare substring lets a short token be satisfied by unrelated text: ``in`` lurks
    inside ``begin{...}`` and a lone ``X`` inside any word, inflating recall past what the model
    actually preserved. So an *alphanumeric* edge of the token must fall on a word boundary; a
    structural edge (``_``, ``^``, ``(``, ``/`` ...) is already its own boundary and needs none.
    Loosen this (fuzzy distance for OCR slips) or tighten it further here; every caller routes
    through this one function.
    """
    needle = normalize(token)
    if not needle:
        return False
    left = r"(?<![A-Za-z0-9])" if needle[0].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if needle[-1].isalnum() else ""
    return re.search(left + re.escape(needle) + right, normalized_text) is not None


@dataclass(frozen=True)
class RecognitionResult:
    """Per-element recognition outcome: which expected fragments survived the OCR round trip."""

    element_id: str
    expected: tuple[str, ...]
    found: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def recall(self) -> float:
        # An element with no expected tokens (a figure) is vacuously fully recognised.
        return len(self.found) / len(self.expected) if self.expected else 1.0


def score_element(element_id: str, ocr_text: str, expected_tokens: list[str]) -> RecognitionResult:
    """Score one crop's OCR output against the notation it was supposed to preserve."""
    normalized = normalize(ocr_text)
    found = tuple(tok for tok in expected_tokens if token_found(tok, normalized))
    missing = tuple(tok for tok in expected_tokens if tok not in found)
    return RecognitionResult(element_id, tuple(expected_tokens), found, missing)


@dataclass(frozen=True)
class RecognitionReport:
    """Aggregate recognition quality across a set of scored elements."""

    results: tuple[RecognitionResult, ...]

    @property
    def micro_recall(self) -> float:
        """Recall over the pooled token bag -- weights elements by how much notation they carry."""
        expected = sum(len(r.expected) for r in self.results)
        found = sum(len(r.found) for r in self.results)
        return found / expected if expected else 1.0

    @property
    def macro_recall(self) -> float:
        """Mean of per-element recall -- weights every element equally, small ones included."""
        scored = [r for r in self.results if r.expected]
        return sum(r.recall for r in scored) / len(scored) if scored else 1.0

    def report(self) -> str:
        lines = [
            f"recognition: micro {self.micro_recall:.1%} "
            f"({sum(len(r.found) for r in self.results)}/"
            f"{sum(len(r.expected) for r in self.results)} tokens), "
            f"macro {self.macro_recall:.1%} over {len([r for r in self.results if r.expected])} elements"
        ]
        for r in sorted(self.results, key=lambda r: r.recall):
            if r.missing:
                lines.append(f"  {r.element_id:24s} {len(r.found)}/{len(r.expected)}  missing {list(r.missing)}")
        return "\n".join(lines)


def score(results_by_element: dict[str, str], expected_tokens: dict[str, list[str]]) -> RecognitionReport:
    """Score a mapping of ``element_id -> OCR text`` against the corpus's expected tokens.

    *Every* token-bearing element is scored. An element with no supplied output scores zero recall
    -- never silent exclusion. Excluding it would let a dropped crop vanish from the average (and an
    empty result set score a vacuous 100%), so an extraction regression that sheds hard crops could
    pass or even *raise* the recognition score. The corpus's token-bearing elements are exactly the
    ones a faithful run must recover, so a missing one is a real failure and must weigh as zero.
    """
    results = tuple(
        score_element(eid, results_by_element.get(eid, ""), tokens)
        for eid, tokens in sorted(expected_tokens.items())
        if tokens
    )
    return RecognitionReport(results)


def corpus_expected_tokens() -> dict[str, list[str]]:
    """Load the ``expected_tokens`` of every corpus element that has notation to recognise."""
    elements = json.loads(CORPUS_EXPECTED.read_text(encoding="utf-8"))["elements"]
    return {eid: spec["expected_tokens"] for eid, spec in elements.items() if spec.get("expected_tokens")}
