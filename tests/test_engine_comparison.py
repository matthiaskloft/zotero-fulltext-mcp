"""Engine comparison harness -- offline, no model, no GPU, no network.

The comparison itself (benchmarks/engines.py) is pure: it takes each engine's output Markdown and
scores it. The part worth testing hard is the *anchoring*, because that is what makes output from
three unrelated engines commensurable: every element is scored inside a window located by its
CORPUSMARK token, so a token only counts where the element actually is.

The runner (tools/compare_engines.py) needs a model, marker-pdf and a GPU to run an engine, but its
hardware verdict and its "is this engine even here" classification are pure decisions on strings --
and that classification is the most failure-prone code in the harness, so it is tested here with a
faked subprocess rather than left to a live run.
"""

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

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
from recognition import corpus_element_ids, corpus_expected_tokens

# Two corpus elements with disjoint expected tokens, used to prove windows are element-specific.
EQ_001 = "CORPUSMARK-EQ-001"  # ["sum", "w_", "frac"]
EQ_008 = "CORPUSMARK-EQ-008"  # ["alpha", "beta", "Gamma"]
FAITHFUL = {
    EQ_001: r"$$\bar{x}_w = \frac{\sum_{i=1}^{n} w_i x_i}{\sum_{i=1}^{n} w_i}$$",
    EQ_008: r"$$\alpha + \beta \in \Gamma$$",
}


def _expected() -> dict[str, list[str]]:
    """Ground truth restricted to the two elements the synthetic documents below contain."""
    return {element_id: corpus_expected_tokens()[element_id] for element_id in FAITHFUL}


def render_document(contents: dict[str, str]) -> str:
    """Stand in for an engine's output: each element's anchor, its content, then trailing prose."""
    return "\n\n".join(
        f"{element_id} An element introduced in prose:\n\n{body}\n\nSome trailing prose."
        for element_id, body in contents.items()
    )


def _score(document: str, **kwargs) -> object:
    """Score one document as an engine run, anchoring only on the two elements it contains."""
    run = EngineRun(ENGINE_MARKER, document, kwargs.pop("wall_seconds", 120.0), "gpu")
    return score_engine(run, _expected(), anchor_ids=FAITHFUL, **kwargs)


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
        anchored = anchor_windows(render_document(FAITHFUL), FAITHFUL)
        self.assertEqual(anchored.unanchored, ())
        self.assertEqual(anchored.ambiguous, ())
        self.assertIn(r"\frac", anchored.windows[EQ_001])
        self.assertNotIn(r"\frac", anchored.windows[EQ_008])
        self.assertIn(r"\Gamma", anchored.windows[EQ_008])
        self.assertNotIn(r"\Gamma", anchored.windows[EQ_001])

    def test_the_anchor_text_itself_is_not_part_of_the_window(self):
        # The window starts after the anchor: an element id that happened to contain an expected
        # token must not satisfy it.
        anchored = anchor_windows(render_document(FAITHFUL), FAITHFUL)
        self.assertNotIn("CORPUSMARK", anchored.windows[EQ_001])

    def test_an_element_with_no_anchor_is_reported(self):
        text = render_document({EQ_001: FAITHFUL[EQ_001]})
        anchored = anchor_windows(text, FAITHFUL)
        self.assertEqual(anchored.unanchored, (EQ_008,))
        self.assertNotIn(EQ_008, anchored.windows)

    def test_a_lost_anchor_widens_its_predecessor_rather_than_dropping_text(self):
        # The engine produced EQ-008's content but mangled its anchor. That text stays inside the
        # previous window instead of being discarded -- the forgiving direction, chosen because the
        # lost anchor is already reported and scored zero on its own. This widening is also why
        # micro recall is only an upper bound; see test_losing_an_anchor_inflates_micro_recall.
        text = render_document(FAITHFUL).replace(EQ_008, "CORPUSMARK-XX-999")
        anchored = anchor_windows(text, FAITHFUL)
        self.assertEqual(anchored.unanchored, (EQ_008,))
        self.assertIn(r"\Gamma", anchored.windows[EQ_001])

    def test_a_repeated_anchor_is_ambiguous_rather_than_guessed_at(self):
        """A mark emitted twice must not be silently anchored on its first occurrence.

        A contents block, a repeated running header or a duplicated text layer all produce this. If
        the first occurrence wins, the window is located before the real content and can collapse to
        the empty string, which then reads as a recognition failure the engine never committed.
        """
        contents = f"Contents: {EQ_001}, {EQ_008}\n\n" + render_document(FAITHFUL)
        anchored = anchor_windows(contents, FAITHFUL)
        self.assertEqual(anchored.ambiguous, (EQ_001, EQ_008))
        self.assertEqual(anchored.windows, {})

    def test_every_element_anchors_not_only_the_token_bearing_ones(self):
        """Elements with nothing to recognise still bound their neighbours' windows.

        Dropping them is what let one corpus window grow to twelve times another's, which matters
        because macro recall weights every element equally regardless of how much text it was
        scored against.
        """
        figure = "CORPUSMARK-FIG-001"
        self.assertNotIn(figure, corpus_expected_tokens())  # carries no tokens
        self.assertIn(figure, corpus_element_ids())  # but is still an anchor
        document = render_document({
            EQ_001: FAITHFUL[EQ_001],
            figure: "![](image_p1_0.png)",
            EQ_008: FAITHFUL[EQ_008],
        })
        anchored = anchor_windows(document, [EQ_001, figure, EQ_008])
        # Without the figure as an anchor, EQ-001's window would swallow the figure block.
        self.assertNotIn("image_p1_0.png", anchored.windows[EQ_001])


class EngineScoringTests(unittest.TestCase):
    def test_a_faithful_document_scores_full_recall_on_the_elements_it_contains(self):
        result = _score(render_document(FAITHFUL))
        self.assertEqual(result.unanchored, ())
        self.assertEqual(result.macro_recall, 1.0)

    def test_swapping_two_elements_contents_destroys_recall(self):
        """The load-bearing test: scoring is windowed, not document-wide.

        Both elements' notation is still present in the document -- only in each other's place. A
        harness that searched the whole document would score this a perfect 100% and would rank an
        engine that swapped the page's content above one that got it right.
        """
        swapped = {EQ_001: FAITHFUL[EQ_008], EQ_008: FAITHFUL[EQ_001]}
        result = _score(render_document(swapped))
        self.assertEqual(result.unanchored, ())
        self.assertEqual(result.macro_recall, 0.0)

    def test_losing_a_tokenless_anchor_inverts_both_aggregates(self):
        """Pins the harness's central limitation so it cannot be forgotten or quietly regressed.

        Two engines, identical mathematics. The second merely mangled the anchor of a FIGURE -- an
        element with no expected tokens. Losing it therefore deducts nothing, while still widening
        the preceding element's window to swallow the figure block. The strictly worse engine scores
        a perfect 100% against the faithful engine's 66.7%, on BOTH aggregates.

        This is why there is no "safe" figure to rank on and why the harness reports comparability
        structurally instead: without ground-truth offsets it cannot know where the missing
        element began, so it can only refuse to pretend the numbers are commensurable.
        """
        figure = "CORPUSMARK-FIG-001"
        self.assertNotIn(figure, corpus_expected_tokens())  # the element that costs nothing to lose
        expected = {EQ_001: corpus_expected_tokens()[EQ_001]}  # ["sum", "w_", "frac"]
        # EQ-001's own content is missing \frac; the figure block that follows contains one.
        keeps_anchors = (
            f"{EQ_001} intro:\n\n" + r"$$ \sum w_i x_i $$" + "\n\n"
            f"{figure} Figure 1:\n\n" + r"![](p1.png) caption mentioning \frac here" + "\n"
        )
        loses_anchor = keeps_anchors.replace(figure, "CORPUSMARK-XX-999")

        def score_of(document):
            run = EngineRun(ENGINE_MARKER, document, 10.0, "gpu")
            return score_engine(run, expected, anchor_ids=[EQ_001, figure])

        faithful, worse = score_of(keeps_anchors), score_of(loses_anchor)

        self.assertEqual(faithful.unlocated, ())
        self.assertEqual(worse.unlocated, (figure,))
        self.assertGreater(worse.macro_recall, faithful.macro_recall)  # the inversion, pinned
        self.assertGreater(worse.micro_recall, faithful.micro_recall)
        # Which is exactly why the faithful score is trustworthy and the inflated one is not.
        self.assertTrue(faithful.trustworthy)
        self.assertFalse(worse.trustworthy)

    def test_a_comparison_is_not_comparable_when_any_engine_lost_an_element(self):
        keeps = render_document(FAITHFUL)
        loses = keeps.replace(EQ_008, "CORPUSMARK-XX-999")
        self.assertTrue(compare([EngineRun(ENGINE_MARKER, keeps, 10.0, "gpu")],
                                _expected(), anchor_ids=FAITHFUL).comparable)
        mixed = compare(
            [EngineRun(ENGINE_CROP_MVP, loses, 10.0, "cpu"),
             EngineRun(ENGINE_MARKER, keeps, 10.0, "gpu")],
            _expected(), anchor_ids=FAITHFUL,
        )
        self.assertFalse(mixed.comparable)
        # best() still answers -- a selection policy needs to see the ranking and decide.
        self.assertIsNotNone(mixed.best())

    def test_an_engine_that_produced_nothing_scores_zero_over_the_whole_corpus(self):
        result = score_engine(EngineRun(ENGINE_MARKER, "", 5.0, "gpu"), corpus_expected_tokens())
        self.assertEqual(result.macro_recall, 0.0)
        self.assertEqual(result.micro_recall, 0.0)
        self.assertEqual(len(result.unanchored), len(corpus_element_ids()))

    def test_cost_is_normalised_by_the_number_of_scored_elements(self):
        self.assertAlmostEqual(_score(render_document(FAITHFUL), ).seconds_per_element, 60.0)

    def test_unlocated_merges_both_ways_an_element_can_go_missing(self):
        text = render_document(FAITHFUL).replace(EQ_008, "CORPUSMARK-XX-999")
        self.assertEqual(_score(text).unlocated, (EQ_008,))


class ComparisonTests(unittest.TestCase):
    def _comparison(self) -> Comparison:
        partial = dict(FAITHFUL, **{EQ_008: r"$$\alpha + b \in G$$"})  # loses beta and Gamma
        return compare(
            [
                EngineRun(ENGINE_CROP_MVP, render_document(partial), 40.0, "cpu"),
                EngineRun(ENGINE_MARKER, render_document(FAITHFUL), 200.0, "gpu"),
            ],
            _expected(), anchor_ids=FAITHFUL,
        )

    def test_higher_recall_ranks_first_even_when_it_is_slower(self):
        # Ranking is on quality; cost is reported for the policy to weigh, not used to rank.
        comparison = self._comparison()
        self.assertEqual(comparison.by_recall()[0].engine, ENGINE_MARKER)
        self.assertEqual(comparison.best().engine, ENGINE_MARKER)

    def test_a_recall_tie_is_broken_by_the_cheaper_run(self):
        document = render_document(FAITHFUL)
        comparison = compare(
            [
                EngineRun(ENGINE_MARKER, document, 200.0, "gpu"),
                EngineRun(ENGINE_CROP_MVP, document, 40.0, "cpu"),
            ],
            _expected(), anchor_ids=FAITHFUL,
        )
        self.assertEqual(comparison.by_recall()[0].engine, ENGINE_CROP_MVP)

    def test_the_report_names_every_engine_and_surfaces_lost_anchors(self):
        text = render_document(FAITHFUL).replace(EQ_008, "CORPUSMARK-XX-999")
        report = compare(
            [EngineRun(ENGINE_MARKER, text, 10.0, "gpu")], _expected(), anchor_ids=FAITHFUL
        ).report()
        self.assertIn(ENGINE_MARKER, report)
        self.assertIn(EQ_008, report)  # a lost anchor must be legible, not just counted
        self.assertIn("NOT COMPARABLE", report)  # and the inflation must be stated, not implied

    def test_the_report_distinguishes_a_repeated_anchor_from_a_missing_one(self):
        text = f"Contents: {EQ_001}\n\n" + render_document(FAITHFUL)
        report = compare(
            [EngineRun(ENGINE_MARKER, text, 10.0, "gpu")], _expected(), anchor_ids=FAITHFUL
        ).report()
        self.assertIn("more than once", report)

    def test_an_empty_comparison_has_no_best_engine(self):
        self.assertIsNone(Comparison(()).best())


class HardwareTests(unittest.TestCase):
    def test_an_available_gpu_must_report_its_vram(self):
        # 0 GB would read to the selection policy as a card too small for any whole-document
        # engine, which is a different claim from "no GPU at all".
        with self.assertRaises(ValueError):
            Hardware(gpu_available=True)

    def test_vram_without_a_gpu_is_contradictory(self):
        with self.assertRaises(ValueError):
            Hardware(gpu_available=False, gpu_vram_gb=8.0)


class RecommendEngineTests(unittest.TestCase):
    """Scores the selection policy as soon as it has one; skips while it is a documented stub."""

    def _comparison(self) -> Comparison:
        return compare(
            [
                EngineRun(ENGINE_CROP_MVP, render_document(FAITHFUL), 40.0, "cpu"),
                EngineRun(ENGINE_MARKER, render_document(FAITHFUL), 200.0, "gpu"),
            ],
            _expected(), anchor_ids=FAITHFUL,
        )

    def _recommend(self, comparison, hardware, **kwargs):
        try:
            return recommend_engine(comparison, hardware, **kwargs)
        except NotImplementedError:
            self.skipTest(
                "recommend_engine() has no policy yet; these tests score it against the "
                "documented contract as soon as it does."
            )

    def test_it_recommends_an_engine_that_was_actually_measured(self):
        choice = self._recommend(self._comparison(), Hardware(gpu_available=True, gpu_vram_gb=8.0))
        self.assertIn(choice, {ENGINE_CROP_MVP, ENGINE_MARKER})

    def test_it_never_recommends_marker_without_a_usable_gpu(self):
        # marker on CPU is far slower than the crop path, so recommending it on a GPU-less machine
        # turns a working pipeline into an overnight job.
        self.assertNotEqual(
            self._recommend(self._comparison(), Hardware(gpu_available=False)), ENGINE_MARKER
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


class RunnerTests(unittest.TestCase):
    """tools/compare_engines.py: the hardware verdict and the availability classification."""

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

    def _marker_failing_with(self, stderr: str):
        error = subprocess.CalledProcessError(1, ["marker"], stderr=stderr)
        return patch.object(self.runner.subprocess, "run", side_effect=error)

    def test_a_missing_marker_pdf_is_reported_as_not_installed(self):
        stderr = (
            'File "zotero_pdf_text/_extract_markdown_marker.py", line 20, in main\n'
            "    from marker.converters.pdf import PdfConverter\n"
            "ModuleNotFoundError: No module named 'marker'\n"
        )
        with self._marker_failing_with(stderr):
            with self.assertRaises(self.runner.EngineUnavailable) as caught:
                self.runner.run_marker("gpu")
        self.assertIn("not installed", str(caught.exception))

    def test_an_installed_marker_with_a_broken_import_is_not_called_uninstalled(self):
        """The bug this guards is subtle and was real: the subprocess IS
        zotero_pdf_text._extract_markdown_marker, so its own module path -- containing the substring
        'marker' -- appears in every traceback it raises. Classifying on that substring told a user
        whose CUDA chain failed to load to install a package they already had, which is both wrong
        and unactionable. The real stderr must reach them instead.
        """
        stderr = (
            'File "zotero_pdf_text/_extract_markdown_marker.py", line 20, in main\n'
            "    from marker.converters.pdf import PdfConverter\n"
            "ImportError: DLL load failed while importing _C: The specified module could not be found.\n"
        )
        with self._marker_failing_with(stderr):
            with self.assertRaises(self.runner.EngineUnavailable) as caught:
                self.runner.run_marker("gpu")
        message = str(caught.exception)
        self.assertNotIn("not installed", message)
        self.assertIn("DLL load failed", message)  # the actual cause survives

    def test_a_timeout_is_not_laundered_into_unavailable(self):
        # A timeout is a cost result, and cost is half of what this harness measures. Filing it
        # under "unavailable" would report an installed, working engine as absent.
        timeout = subprocess.TimeoutExpired(["marker"], self.runner.MARKER_TIMEOUT_SECONDS)
        with patch.object(self.runner.subprocess, "run", side_effect=timeout):
            with self.assertRaises(self.runner.EngineTimedOut):
                self.runner.run_marker("gpu")

    def test_an_explicit_missing_config_is_an_error_not_a_silent_default(self):
        # Benchmarking the default host/model when the user pointed at a specific config would
        # attribute the scores to a runtime they never use.
        with self.assertRaises(SystemExit):
            self.runner._ocr_settings("no/such/config.json")

    def test_nothing_measured_exits_non_zero(self):
        # An empty comparison redirected to a file would otherwise look like a valid artifact.
        self.assertEqual(self.runner.main(["--engine", "mineru"]), 1)


if __name__ == "__main__":
    unittest.main()
