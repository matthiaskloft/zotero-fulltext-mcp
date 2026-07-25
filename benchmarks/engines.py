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

Three properties of that windowing are load-bearing, and the first is a real limitation rather than
a footnote:

  1. **A window is only as tight as the next anchor the engine reproduced, so a lost anchor inflates
     its predecessor's score.** A window contains its element's trailing prose, and when the
     *following* anchor is missing it swallows that element's content too. An engine that mangles an
     anchor therefore scores HIGHER on the preceding element than an engine that did not, on
     byte-identical content. Neither aggregate is immune: when the lost anchor belongs to an element
     with no expected tokens (a figure), losing it deducts nothing while still widening its
     neighbour, so **macro and micro recall both invert** -- measured, 66.7% for the faithful engine
     against 100.0% for the one that dropped a figure's anchor. There is no scoring fix available
     here: without ground-truth offsets the harness cannot know where a missing anchor's element
     began. So the rule is structural instead -- **a comparison in which any engine has unlocated
     elements is not directly comparable.** ``EngineScore.trustworthy`` and
     ``Comparison.comparable`` say so, ``report()`` prints it, and the anchor loss has to be
     resolved before the numbers mean anything.
  2. Anchors come from the *whole* element set, not just the elements carrying expected tokens. The
     figures and no-crop elements have nothing to recognise, but they occupy page space: leaving
     them out made windows span 136 to 4638 characters on this corpus, so one element was scored
     against twelve times more text than another under a macro average that weights them equally.
  3. A lost or repeated anchor is reported separately AND scores zero recall. That is not a
     technicality: losing the sentence "CORPUSMARK-EQ-007 Set-theoretic and logical operators:"
     means the engine dropped running prose, which is a real extraction defect. Keeping the count
     visible is what stops a high recall over three surviving anchors from reading like a win.

What this harness does NOT detect: an engine that reproduces every anchor with its own content
attached but emits the elements in the wrong document order scores full recall. Order is a real
extraction property and this metric is blind to it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from recognition import RecognitionReport, corpus_element_ids, score

# What acceleration a run had available. Enforced by the type rather than by a comment, so a typo
# ('GPU', 'cuda') is caught where it is written instead of quietly reaching the selection policy.
HardwareMode = Literal["gpu", "cpu"]

# Engine identifiers. Free-form strings elsewhere would drift ('marker' vs 'marker-pdf'), and the
# selection policy compares against these names.
ENGINE_CROP_MVP = "crop-mvp"
ENGINE_MARKER = "marker"
ENGINE_MINERU = "mineru"

# The only GPU marker-pdf has been measured on here -- 6GB, from the timing recorded at
# math_ocr.DEFAULT_TIMEOUT_SECONDS. It is NOT a measured floor: nobody has run it on less, so
# whether a smaller card falls back to CPU or exhausts memory mid-document is unknown. Treat this as
# "known to work at" rather than "known to fail below".
MARKER_MEASURED_VRAM_GB = 6.0


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


@dataclass(frozen=True)
class AnchorMap:
    """Where each element's text was found in an engine's output, and what could not be located.

    ``unanchored`` and ``ambiguous`` are separate because they are different defects with the same
    consequence (zero recall): the first means the engine lost the prose, the second means it
    emitted the same mark more than once -- a duplicated text layer, a repeated running header, a
    table of contents -- so which occurrence is the element is no longer knowable.
    """

    windows: dict[str, str]
    unanchored: tuple[str, ...]
    ambiguous: tuple[str, ...]


def anchor_windows(text: str, element_ids: Iterable[str]) -> AnchorMap:
    """Slice an engine's output document into one text window per element.

    Each window spans from the end of its anchor to the start of the next anchor *in document order
    among the anchors found* -- so a lost anchor widens its predecessor's window rather than dropping
    the text on the floor. That is the forgiving direction on purpose (the alternative silently hides
    content the engine did produce), and it is why recall inflates with lost anchors; see the module
    docstring. Pass every element id, not just the token-bearing ones, or the gaps get large.

    An element whose mark appears more than once is reported as ``ambiguous`` rather than anchored on
    its first occurrence. Guessing would be worse than declining: a mark repeated in a contents block
    puts the window at the wrong place, where it can collapse to the empty string and read as a
    recognition failure that the engine never committed.
    """
    located: list[tuple[int, int, str]] = []
    unanchored: list[str] = []
    ambiguous: list[str] = []
    for element_id in sorted(element_ids):
        matches = list(anchor_pattern(element_id).finditer(text))
        if not matches:
            unanchored.append(element_id)
        elif len(matches) > 1:
            ambiguous.append(element_id)
        else:
            located.append((matches[0].start(), matches[0].end(), element_id))

    located.sort()
    windows: dict[str, str] = {}
    for position, (_, window_start, element_id) in enumerate(located):
        next_anchor = located[position + 1][0] if position + 1 < len(located) else len(text)
        windows[element_id] = text[window_start:next_anchor]
    return AnchorMap(windows, tuple(unanchored), tuple(ambiguous))


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
    # The acceleration the run had AVAILABLE, not a measurement of what it used. Neither Ollama nor
    # marker-pdf reports its device through the interfaces this harness has, so this records the
    # machine, and the timing beside it is what shows whether the engine took advantage of it.
    hardware: HardwareMode


@dataclass(frozen=True)
class EngineScore:
    """One engine's recognition quality and cost on a document, ready to compare."""

    engine: str
    recognition: RecognitionReport
    unanchored: tuple[str, ...]
    ambiguous: tuple[str, ...]
    wall_seconds: float
    hardware: HardwareMode

    @property
    def micro_recall(self) -> float:
        """Pooled-token recall. Meaningful only while ``trustworthy`` -- see the module docstring."""
        return self.recognition.micro_recall

    @property
    def macro_recall(self) -> float:
        """Mean per-element recall. Meaningful only while ``trustworthy``: a lost anchor can inflate
        this one too, so it is not the safe alternative to micro."""
        return self.recognition.macro_recall

    @property
    def unlocated(self) -> tuple[str, ...]:
        """Every element that could not be scored in place, whichever way it failed."""
        return tuple(sorted(self.unanchored + self.ambiguous))

    @property
    def trustworthy(self) -> bool:
        """True when every element was located, which is when this score can be compared at all.

        An unlocated element widens its predecessor's window, so its neighbour's recall -- and both
        aggregates -- are inflated by an unknown amount. The score is still worth printing (it says
        the engine lost prose), it just cannot be ranked against another engine's.
        """
        return not self.unlocated

    @property
    def seconds_per_element(self) -> float:
        """Cost normalised by workload, so runs over different corpora stay comparable."""
        scored = len(self.recognition.results)
        return self.wall_seconds / scored if scored else 0.0


def score_engine(
    run: EngineRun,
    expected_tokens: dict[str, list[str]],
    *,
    anchor_ids: Iterable[str] | None = None,
) -> EngineScore:
    """Score one engine run: window its output at the anchors, then apply the recognition metric.

    ``anchor_ids`` is the full element set to locate, defaulting to the corpus's. It is deliberately
    wider than ``expected_tokens``: elements with nothing to recognise still bound their neighbours'
    windows, and dropping them is what let one window grow twelve times another's.
    """
    anchored = anchor_windows(run.document_text, anchor_ids if anchor_ids is not None else corpus_element_ids())
    # score() covers every token-bearing element and treats an absent one as zero recall, so an
    # element that could not be located is already penalised by being missing from `windows`.
    return EngineScore(
        engine=run.engine,
        recognition=score(anchored.windows, expected_tokens),
        unanchored=anchored.unanchored,
        ambiguous=anchored.ambiguous,
        wall_seconds=run.wall_seconds,
        hardware=run.hardware,
    )


@dataclass(frozen=True)
class Comparison:
    """Several engines scored on the same document and the same ground truth."""

    scores: tuple[EngineScore, ...]

    def by_recall(self) -> tuple[EngineScore, ...]:
        """Best macro recall first; ties broken by the cheaper run, so the order is total.

        This is a presentation order, not a verdict. When ``comparable`` is false the ordering is
        meaningless -- an engine that lost an anchor can outrank the engine it lost against.
        """
        return tuple(sorted(self.scores, key=lambda s: (-s.macro_recall, s.seconds_per_element)))

    @property
    def comparable(self) -> bool:
        """True when every engine located every element, so the scores can be ranked against each
        other. See the module docstring: a lost anchor inflates its neighbour by an unknown amount."""
        return all(s.trustworthy for s in self.scores)

    def best(self) -> EngineScore | None:
        """The top-ranked score, or None when nothing was measured.

        Deliberately does NOT refuse when ``comparable`` is false: the caller (a selection policy)
        must be able to see the ranking and decide, and hiding it would just push the same judgement
        somewhere with less information. Check ``comparable`` before trusting this.
        """
        ranked = self.by_recall()
        return ranked[0] if ranked else None

    def report(self) -> str:
        ranked = self.by_recall()
        width = max((len(s.engine) for s in ranked), default=6)
        lines = [
            f"{'engine':{width}s} {'macro':>7s} {'micro':>7s} {'s/elem':>8s}  hardware  unlocated"
        ]
        for s in ranked:
            lines.append(
                f"{s.engine:{width}s} {s.macro_recall:6.1%} {s.micro_recall:6.1%} "
                f"{s.seconds_per_element:8.1f}  {s.hardware:8s}  {len(s.unlocated)}"
            )
        for s in ranked:
            if s.unanchored:
                lines.append(f"  {s.engine}: no anchor found for {list(s.unanchored)}")
            if s.ambiguous:
                lines.append(f"  {s.engine}: anchor appears more than once for {list(s.ambiguous)}")
        if ranked and not self.comparable:
            lines.append(
                "  NOT COMPARABLE: an unlocated element widens its neighbour's window, inflating "
                "that neighbour's recall and both aggregates by an unknown amount. Resolve the "
                "unlocated elements above before ranking these engines against each other."
            )
        return "\n".join(lines)


def compare(
    runs: Iterable[EngineRun],
    expected_tokens: dict[str, list[str]],
    *,
    anchor_ids: Iterable[str] | None = None,
) -> Comparison:
    """Score every engine run against one ground truth."""
    ids = tuple(anchor_ids) if anchor_ids is not None else None
    return Comparison(tuple(score_engine(run, expected_tokens, anchor_ids=ids) for run in runs))


@dataclass(frozen=True)
class Hardware:
    """The machine a recommendation is being made for.

    The two fields must agree. An available GPU with 0 GB would read to the selection policy as a
    card too small for any whole-document engine, which is a different claim from "no GPU" and would
    silently steer the recommendation for a machine that does not exist.
    """

    gpu_available: bool
    gpu_vram_gb: float = 0.0

    def __post_init__(self) -> None:
        if self.gpu_available and self.gpu_vram_gb <= 0:
            raise ValueError("an available GPU must report its VRAM")
        if not self.gpu_available and self.gpu_vram_gb:
            raise ValueError("VRAM reported for a machine with no available GPU")


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
        needs a GPU, and on CPU becomes far slower than the crop path. Recommending it on a GPU-less
        machine turns a working pipeline into an overnight job. ``MARKER_MEASURED_VRAM_GB`` is the
        one card it has been run on, not a floor -- deciding what to do between "smaller than that"
        and "no GPU at all" is part of this judgement, on no measurement.
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
