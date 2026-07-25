"""Compare whole extraction engines on the same document, and pick one for a machine.

``scoring.py`` scores classification (which prompt a crop routes to) and ``recognition.py`` scores
recognition (did the notation survive one crop's round trip). Both judge pieces of the crop-OCR
pipeline. This module asks the question above them: **of the engines that could produce this
document, which one should run here?**

The candidates differ in kind, not degree:

  - ``crop-mvp`` -- pymupdf4llm crops plus a small local OCR model on each crop. Runs on CPU.
  - ``marker``   -- marker-pdf re-renders the whole document. Wants a GPU; observed ~70-90s/page.
  - ``mineru``   -- a third whole-document engine, not wired up here; the harness accepts it as
                    soon as a runner can produce its output text.

Comparing them needs one axis they all land on, and there is exactly one that matters: **the final
Markdown a reader searches**. A crop engine gets there by splicing OCR back into the placeholder; a
whole-document engine gets there by regenerating the page. So every engine is judged on its output
document, never on an intermediate -- which is also the only interface a future engine has to
satisfy.

Scoring per element then needs a way to find, inside a foreign engine's output, the text that stands
where a known element used to be. The synthetic corpus already provides it: every element is
preceded in the prose by its ``CORPUSMARK-<KIND>-<NNN>`` token (see ``tests/fixtures/ocr_corpus/
corpus.tex``). Any engine that reads the page at all carries those tokens through, so they anchor a
window per element and the existing token-recall metric scores it unchanged.

Two properties of that windowing are deliberate and worth knowing before reading a number here:

  1. A window runs from its anchor to the *next anchor found*, so it also contains the element's
     trailing prose. Absolute recall is therefore a slight upper bound -- prose could in principle
     contain an expected token. It is the same bound for every engine, so comparisons stay fair.
  2. An anchor the engine failed to reproduce is reported separately AND scores zero recall. That is
     not a technicality: losing the sentence "CORPUSMARK-EQ-007 Set-theoretic and logical
     operators:" means the engine dropped running prose, which is a real extraction defect. Keeping
     the count visible is what stops a high recall over three surviving anchors from reading like a
     win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from recognition import RecognitionReport, score

# Engine identifiers. Free-form strings elsewhere would drift ('marker' vs 'marker-pdf'), and the
# selection policy compares against these names.
ENGINE_CROP_MVP = "crop-mvp"
ENGINE_MARKER = "marker"
ENGINE_MINERU = "mineru"

# The smallest GPU marker-pdf has been observed to complete a real paper on, from the measurement
# recorded at math_ocr.DEFAULT_TIMEOUT_SECONDS. Below this it either falls back to CPU (far slower
# than the crop path) or exhausts memory mid-document.
MARKER_MIN_VRAM_GB = 6.0


def anchor_pattern(element_id: str) -> re.Pattern[str]:
    """A tolerant matcher for one element's ``CORPUSMARK`` token in arbitrary engine output.

    An exact-string search is too brittle here. The anchor survives a round trip through a layout
    model and an OCR decoder, which routinely rewrite the separators -- ``CORPUSMARK-EQ-001`` comes
    back as ``CORPUSMARK EQ 001``, with an en dash, or run together. The *parts* are what identify
    the element, so any run of non-alphanumerics may sit between them. The trailing guard keeps a
    three-digit id from matching a longer number, so ``EQ-001`` can never anchor on ``EQ-0012``.
    """
    parts = element_id.split("-")
    return re.compile(r"[^A-Za-z0-9]*".join(re.escape(part) for part in parts) + r"(?![0-9])")


def anchor_windows(text: str, element_ids) -> tuple[dict[str, str], tuple[str, ...]]:
    """Slice an engine's output document into one text window per element.

    Returns ``(windows, unanchored)``. Each window spans from the end of the element's anchor to the
    start of the next anchor *in document order among the anchors actually found* -- so a lost
    anchor widens its predecessor's window rather than dropping the text on the floor. That is the
    forgiving direction on purpose: the alternative silently hides content the engine did produce,
    and the lost anchor is already reported and scored zero in its own right.
    """
    located: list[tuple[int, int, str]] = []
    unanchored: list[str] = []
    for element_id in sorted(element_ids):
        match = anchor_pattern(element_id).search(text)
        if match is None:
            unanchored.append(element_id)
        else:
            located.append((match.start(), match.end(), element_id))

    located.sort()
    windows: dict[str, str] = {}
    for position, (_, window_start, element_id) in enumerate(located):
        next_anchor = located[position + 1][0] if position + 1 < len(located) else len(text)
        windows[element_id] = text[window_start:next_anchor]
    return windows, tuple(unanchored)


@dataclass(frozen=True)
class EngineRun:
    """What one engine produced for one document, plus what the run cost.

    ``document_text`` is the engine's final Markdown. An engine that failed outright contributes an
    empty string rather than being omitted, so a crash scores zero instead of vanishing from the
    comparison.
    """

    engine: str
    document_text: str
    wall_seconds: float
    # 'gpu' or 'cpu': the acceleration the run had AVAILABLE, not a measurement of what it used.
    # Neither Ollama nor marker-pdf reports its device through the interfaces this harness has, so
    # this records the machine, and the timing beside it is what actually shows whether the engine
    # took advantage of it.
    hardware: str


@dataclass(frozen=True)
class EngineScore:
    """One engine's recognition quality and cost on a document, ready to compare."""

    engine: str
    recognition: RecognitionReport
    unanchored: tuple[str, ...]
    wall_seconds: float
    hardware: str

    @property
    def micro_recall(self) -> float:
        return self.recognition.micro_recall

    @property
    def macro_recall(self) -> float:
        return self.recognition.macro_recall

    @property
    def seconds_per_element(self) -> float:
        """Cost normalised by workload, so runs over different corpora stay comparable."""
        scored = len(self.recognition.results)
        return self.wall_seconds / scored if scored else 0.0


def score_engine(run: EngineRun, expected_tokens: dict[str, list[str]]) -> EngineScore:
    """Score one engine run: window its output at the anchors, then apply the recognition metric."""
    windows, unanchored = anchor_windows(run.document_text, expected_tokens)
    # score() covers every token-bearing element and treats an absent one as zero recall, so the
    # unanchored elements are already penalised by being missing from `windows`.
    return EngineScore(
        engine=run.engine,
        recognition=score(windows, expected_tokens),
        unanchored=unanchored,
        wall_seconds=run.wall_seconds,
        hardware=run.hardware,
    )


@dataclass(frozen=True)
class Comparison:
    """Several engines scored on the same document and the same ground truth."""

    scores: tuple[EngineScore, ...]

    def by_recall(self) -> tuple[EngineScore, ...]:
        """Best macro recall first; ties broken by the cheaper run, so the order is total."""
        return tuple(sorted(self.scores, key=lambda s: (-s.macro_recall, s.seconds_per_element)))

    def best(self) -> EngineScore | None:
        ranked = self.by_recall()
        return ranked[0] if ranked else None

    def report(self) -> str:
        lines = [f"{'engine':12s} {'macro':>7s} {'micro':>7s} {'s/elem':>8s}  hardware  anchors lost"]
        for s in self.by_recall():
            lines.append(
                f"{s.engine:12s} {s.macro_recall:6.1%} {s.micro_recall:6.1%} "
                f"{s.seconds_per_element:8.1f}  {s.hardware:8s}  {len(s.unanchored)}"
            )
        for s in self.by_recall():
            if s.unanchored:
                lines.append(f"  {s.engine}: no anchor found for {list(s.unanchored)}")
        return "\n".join(lines)


def compare(runs, expected_tokens: dict[str, list[str]]) -> Comparison:
    """Score every engine run against one ground truth."""
    return Comparison(tuple(score_engine(run, expected_tokens) for run in runs))


@dataclass(frozen=True)
class Hardware:
    """The machine a recommendation is being made for."""

    gpu_available: bool
    gpu_vram_gb: float = 0.0


def recommend_engine(
    comparison: Comparison,
    hardware: Hardware,
    *,
    max_seconds_per_element: float | None = None,
) -> str:
    """Choose the engine to run on ``hardware``, given how the candidates actually scored.

    TODO(contributor): implement the selection policy.

    The measurements do not decide this on their own -- the trade-off is a judgement about the
    library and the machine it runs on:

      - ``marker`` tends to win on recall (it re-renders the page rather than reading a crop) but
        needs a GPU: below ``MARKER_MIN_VRAM_GB`` it falls back to CPU and becomes far slower than
        the crop path. Recommending it on a GPU-less machine turns a working pipeline into an
        overnight job.
      - ``crop-mvp`` runs anywhere and touches only the placeholders, so its cost scales with the
        number of crops rather than with page count -- but it can only recover what the extractor
        already isolated into a crop.
      - A recall win that busts ``max_seconds_per_element`` may still be the wrong pick for a
        library of thousands of papers, and the right pick for ten.

    Guarantees the caller relies on: return one of the ``ENGINE_*`` names present in
    ``comparison``, and raise ``ValueError`` if the comparison is empty -- inventing a default for
    "nothing was measured" would recommend an engine on no evidence at all.
    """
    raise NotImplementedError(
        "recommend_engine() has no policy yet; benchmarks/engines.py documents the trade-off"
    )
