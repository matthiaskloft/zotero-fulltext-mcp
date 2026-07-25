"""Engine comparison harness -- offline, no model, no GPU, no network.

The comparison itself (benchmarks/engines.py) is pure: it takes each engine's output Markdown and
scores it. The part worth testing hard is the *anchoring*, because that is what makes output from
three unrelated engines commensurable: every element is scored inside a window located by its
CORPUSMARK token, so a token only counts where the element actually is.

The live runner that produces those documents (tools/compare_engines.py) needs a model and a GPU,
so it is not exercised here -- same split as the recognition harness.
"""

import importlib.util
import unittest
from pathlib import Path

from engines import (
    ENGINE_CROP_MVP,
    ENGINE_MARKER,
    Comparison,
    EngineRun,
    Hardware,
    anchor_pattern,
    anchor_windows,
    compare,
    recommend_engine,
    score_engine,
)
from recognition import corpus_expected_tokens

# Two corpus elements with disjoint expected tokens, used to prove windows are element-specific.
EQ_001 = "CORPUSMARK-EQ-001"  # ["sum", "w_", "frac"]
EQ_008 = "CORPUSMARK-EQ-008"  # ["alpha", "beta", "Gamma"]
FAITHFUL = {
    EQ_001: r"$$\bar{x}_w = \frac{\sum_{i=1}^{n} w_i x_i}{\sum_{i=1}^{n} w_i}$$",
    EQ_008: r"$$\alpha + \beta \in \Gamma$$",
}


def render_document(contents: dict[str, str]) -> str:
    """Stand in for an engine's output: each element's anchor, its content, then trailing prose."""
    return "\n\n".join(
        f"{element_id} An element introduced in prose:\n\n{body}\n\nSome trailing prose."
        for element_id, body in contents.items()
    )


class AnchorPatternTests(unittest.TestCase):
    def test_separators_may_be_rewritten_by_the_engine(self):
        # A layout model plus an OCR decoder routinely reflow the separators; the parts identify
        # the element, so any run of non-alphanumerics between them still anchors.
        pattern = anchor_pattern(EQ_001)
        for variant in ("CORPUSMARK-EQ-001", "CORPUSMARK EQ 001", "CORPUSMARK–EQ–001", "CORPUSMARKEQ001"):
            with self.subTest(variant=variant):
                self.assertIsNotNone(pattern.search(f"text {variant} more text"))

    def test_an_anchor_never_matches_a_longer_number(self):
        # Without the trailing guard, EQ-001 would anchor on EQ-0012 and window the wrong element.
        self.assertIsNone(anchor_pattern(EQ_001).search("CORPUSMARK-EQ-0012"))

    def test_a_different_element_does_not_match(self):
        self.assertIsNone(anchor_pattern(EQ_001).search("CORPUSMARK-EQ-002 something"))


class AnchorWindowTests(unittest.TestCase):
    def test_each_window_holds_only_its_own_element(self):
        windows, unanchored = anchor_windows(render_document(FAITHFUL), FAITHFUL)
        self.assertEqual(unanchored, ())
        self.assertIn(r"\frac", windows[EQ_001])
        self.assertNotIn(r"\frac", windows[EQ_008])
        self.assertIn(r"\Gamma", windows[EQ_008])
        self.assertNotIn(r"\Gamma", windows[EQ_001])

    def test_the_anchor_text_itself_is_not_part_of_the_window(self):
        # The window starts after the anchor: an element id that happened to contain an expected
        # token must not satisfy it.
        windows, _ = anchor_windows(render_document(FAITHFUL), FAITHFUL)
        self.assertNotIn("CORPUSMARK", windows[EQ_001])

    def test_an_element_with_no_anchor_is_reported(self):
        text = render_document({EQ_001: FAITHFUL[EQ_001]})
        windows, unanchored = anchor_windows(text, FAITHFUL)
        self.assertEqual(unanchored, (EQ_008,))
        self.assertNotIn(EQ_008, windows)

    def test_a_lost_anchor_widens_its_predecessor_rather_than_dropping_text(self):
        # The engine produced EQ-008's content but mangled its anchor. That text stays inside the
        # previous window instead of being discarded -- the forgiving direction, chosen because the
        # lost anchor is already reported and scored zero on its own.
        text = render_document(FAITHFUL).replace(EQ_008, "CORPUSMARK-XX-999")
        windows, unanchored = anchor_windows(text, FAITHFUL)
        self.assertEqual(unanchored, (EQ_008,))
        self.assertIn(r"\Gamma", windows[EQ_001])


class EngineScoringTests(unittest.TestCase):
    def test_a_faithful_document_scores_full_recall_on_the_elements_it_contains(self):
        run = EngineRun(ENGINE_MARKER, render_document(FAITHFUL), wall_seconds=120.0, hardware="gpu")
        result = score_engine(run, {k: corpus_expected_tokens()[k] for k in FAITHFUL})
        self.assertEqual(result.unanchored, ())
        self.assertEqual(result.macro_recall, 1.0)

    def test_swapping_two_elements_contents_destroys_recall(self):
        """The load-bearing test: scoring is windowed, not document-wide.

        Both elements' notation is still present in the document -- only in each other's place. A
        harness that searched the whole document would score this a perfect 100% and would rank an
        engine that scrambled the page above one that got it right.
        """
        swapped = {EQ_001: FAITHFUL[EQ_008], EQ_008: FAITHFUL[EQ_001]}
        expected = {k: corpus_expected_tokens()[k] for k in FAITHFUL}
        run = EngineRun(ENGINE_MARKER, render_document(swapped), wall_seconds=120.0, hardware="gpu")
        result = score_engine(run, expected)
        self.assertEqual(result.unanchored, ())
        self.assertEqual(result.macro_recall, 0.0)

    def test_an_engine_that_produced_nothing_scores_zero_over_the_whole_corpus(self):
        expected = corpus_expected_tokens()
        result = score_engine(EngineRun(ENGINE_MARKER, "", 5.0, "gpu"), expected)
        self.assertEqual(result.macro_recall, 0.0)
        self.assertEqual(result.micro_recall, 0.0)
        self.assertEqual(len(result.unanchored), len(expected))

    def test_cost_is_normalised_by_the_number_of_scored_elements(self):
        run = EngineRun(ENGINE_MARKER, render_document(FAITHFUL), wall_seconds=60.0, hardware="gpu")
        result = score_engine(run, {k: corpus_expected_tokens()[k] for k in FAITHFUL})
        self.assertAlmostEqual(result.seconds_per_element, 30.0)


class ComparisonTests(unittest.TestCase):
    def _comparison(self) -> Comparison:
        expected = {k: corpus_expected_tokens()[k] for k in FAITHFUL}
        partial = dict(FAITHFUL, **{EQ_008: r"$$\alpha + b \in G$$"})  # loses beta and Gamma
        return compare(
            [
                EngineRun(ENGINE_CROP_MVP, render_document(partial), 40.0, "cpu"),
                EngineRun(ENGINE_MARKER, render_document(FAITHFUL), 200.0, "gpu"),
            ],
            expected,
        )

    def test_higher_recall_ranks_first_even_when_it_is_slower(self):
        # Ranking is on quality; cost is reported for the policy to weigh, not used to rank.
        ranked = self._comparison().by_recall()
        self.assertEqual(ranked[0].engine, ENGINE_MARKER)
        self.assertEqual(self._comparison().best().engine, ENGINE_MARKER)

    def test_a_recall_tie_is_broken_by_the_cheaper_run(self):
        expected = {k: corpus_expected_tokens()[k] for k in FAITHFUL}
        document = render_document(FAITHFUL)
        comparison = compare(
            [
                EngineRun(ENGINE_MARKER, document, 200.0, "gpu"),
                EngineRun(ENGINE_CROP_MVP, document, 40.0, "cpu"),
            ],
            expected,
        )
        self.assertEqual(comparison.by_recall()[0].engine, ENGINE_CROP_MVP)

    def test_the_report_names_every_engine_and_surfaces_lost_anchors(self):
        expected = {k: corpus_expected_tokens()[k] for k in FAITHFUL}
        text = render_document(FAITHFUL).replace(EQ_008, "CORPUSMARK-XX-999")
        text_report = compare([EngineRun(ENGINE_MARKER, text, 10.0, "gpu")], expected).report()
        self.assertIn(ENGINE_MARKER, text_report)
        self.assertIn(EQ_008, text_report)  # a lost anchor must be legible, not just counted

    def test_an_empty_comparison_has_no_best_engine(self):
        self.assertIsNone(Comparison(()).best())


class RecommendEngineTests(unittest.TestCase):
    """Scores the selection policy as soon as it has one; skips while it is a documented stub."""

    def _recommend(self, comparison, hardware, **kwargs):
        try:
            return recommend_engine(comparison, hardware, **kwargs)
        except NotImplementedError:
            self.skipTest(
                "recommend_engine() has no policy yet; these tests score it against the "
                "documented contract as soon as it does."
            )

    def test_it_recommends_an_engine_that_was_actually_measured(self):
        comparison = compare(
            [
                EngineRun(ENGINE_CROP_MVP, render_document(FAITHFUL), 40.0, "cpu"),
                EngineRun(ENGINE_MARKER, render_document(FAITHFUL), 200.0, "gpu"),
            ],
            {k: corpus_expected_tokens()[k] for k in FAITHFUL},
        )
        choice = self._recommend(comparison, Hardware(gpu_available=True, gpu_vram_gb=8.0))
        self.assertIn(choice, {ENGINE_CROP_MVP, ENGINE_MARKER})

    def test_it_never_recommends_marker_without_a_usable_gpu(self):
        # marker on CPU is far slower than the crop path, so recommending it on a GPU-less machine
        # turns a working pipeline into an overnight job.
        comparison = compare(
            [
                EngineRun(ENGINE_CROP_MVP, render_document(FAITHFUL), 40.0, "cpu"),
                EngineRun(ENGINE_MARKER, render_document(FAITHFUL), 200.0, "gpu"),
            ],
            {k: corpus_expected_tokens()[k] for k in FAITHFUL},
        )
        self.assertNotEqual(
            self._recommend(comparison, Hardware(gpu_available=False)), ENGINE_MARKER
        )

    def test_nothing_measured_is_an_error_not_a_default(self):
        # Returning a default here would recommend an engine on no evidence at all.
        try:
            recommend_engine(Comparison(()), Hardware(gpu_available=True, gpu_vram_gb=8.0))
        except NotImplementedError:
            self.skipTest("recommend_engine() has no policy yet")
        except ValueError:
            return
        self.fail("an empty comparison must raise ValueError")


class HardwareDetectionTests(unittest.TestCase):
    """The runner's GPU probe (tools/compare_engines.py), tested without a GPU."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent.parent / "tools" / "compare_engines.py"
        spec = importlib.util.spec_from_file_location("compare_engines", path)
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def test_the_largest_gpu_is_what_one_run_can_use(self):
        hardware = self.runner.parse_nvidia_smi(0, "6144\n24576\n")
        self.assertTrue(hardware.gpu_available)
        self.assertAlmostEqual(hardware.gpu_vram_gb, 24.0)

    def test_a_driver_that_errors_reads_as_no_gpu(self):
        # A GPU whose driver cannot answer cannot be relied on to hold a layout model, so the
        # recommendation must treat this exactly like a CPU machine.
        self.assertFalse(self.runner.parse_nvidia_smi(9, "6144\n").gpu_available)

    def test_a_non_numeric_answer_reads_as_no_gpu(self):
        self.assertFalse(self.runner.parse_nvidia_smi(0, "[N/A]\n").gpu_available)

    def test_an_absent_engine_is_unavailable_not_a_zero_score(self):
        # MinerU has no runner yet. Scoring it zero would read as "MinerU is bad" rather than
        # "MinerU was never asked", which is the opposite of what the comparison is for.
        with self.assertRaises(self.runner.EngineUnavailable):
            self.runner.run_mineru("cpu")


if __name__ == "__main__":
    unittest.main()
