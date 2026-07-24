"""Recognition-quality metric and end-to-end search recovery -- offline, no model, no network.

Two things are proven here without ever calling the OCR model:

  1. The recognition metric itself (benchmarks/recognition.py) behaves as claimed on canned strings:
     normalization erases meaningless LaTeX spelling differences, matching stays case-sensitive, and
     recall aggregates correctly. The live model run (tests/test_ocr_corpus.py, opt-in) trusts this
     metric, so the metric must be tested where the model is not.

  2. Recovery is searchable end to end. This is the whole point of the feature: a term that lived
     ONLY inside an equation image is invisible to full-text search until OCR splices it back. We
     drive canned OCR text through the real render_replacement -> splice -> index -> search path and
     assert the term is unfindable before enrichment and findable after.
"""

import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.fts import build_fts_index, search_fts
from zotero_pdf_text.identity import MARKDOWN_IMAGE_RE, strip_front_matter
from zotero_pdf_text.image_ocr import CLASS_FORMULA, render_replacement, splice

from recognition import (
    RecognitionReport,
    corpus_expected_tokens,
    normalize,
    score,
    score_element,
    token_found,
)


class NormalizationTests(unittest.TestCase):
    def test_backslashes_are_removed_so_bare_tokens_match_commands(self):
        self.assertEqual(normalize(r"\frac{a}{b}"), "frac{a}{b}")
        self.assertTrue(token_found("frac", normalize(r"\frac{a}{b}")))
        self.assertTrue(token_found("sum", normalize(r"\sum_{j=1}^{n} w_j")))

    def test_whitespace_runs_collapse_but_word_tokens_stay_distinct(self):
        self.assertEqual(normalize("for   all\n x"), "for all x")
        self.assertTrue(token_found("for all", normalize(r"\text{for  all}")))

    def test_matching_is_case_sensitive_because_greek_case_is_meaningful(self):
        # \Gamma and \gamma are different symbols; a metric that lowercased would reward the wrong
        # transcription. Gamma must not be found in an output that only produced gamma.
        self.assertTrue(token_found("Gamma", normalize(r"\Gamma(x)")))
        self.assertFalse(token_found("Gamma", normalize(r"\gamma(x)")))


class RecallMathTests(unittest.TestCase):
    def test_recall_counts_the_fraction_of_expected_tokens_present(self):
        result = score_element("EQ", r"\sum_{j} w_j", ["sum", "w_", "frac"])
        self.assertEqual(result.found, ("sum", "w_"))
        self.assertEqual(result.missing, ("frac",))
        self.assertAlmostEqual(result.recall, 2 / 3)

    def test_a_tokenless_element_is_vacuously_fully_recognised(self):
        # Figures carry no expected tokens; scoring one must not divide by zero or count as a miss.
        self.assertEqual(score_element("FIG", "a scatter plot of two variables", []).recall, 1.0)

    def test_micro_weights_by_token_volume_macro_weights_by_element(self):
        report = RecognitionReport(
            (
                score_element("BIG", r"\frac \sum \int", ["frac", "sum", "int", "partial"]),  # 3/4
                score_element("SMALL", r"\alpha", ["alpha"]),                                  # 1/1
            )
        )
        # micro pools tokens: (3 + 1) / (4 + 1) = 0.8; macro averages elements: (0.75 + 1.0)/2 = 0.875
        self.assertAlmostEqual(report.micro_recall, 0.8)
        self.assertAlmostEqual(report.macro_recall, 0.875)

    def test_report_lists_only_the_elements_that_lost_notation(self):
        report = RecognitionReport(
            (
                score_element("GOOD", r"\alpha", ["alpha"]),
                score_element("BAD", r"\alpha", ["alpha", "beta"]),
            )
        )
        text = report.report()
        self.assertIn("BAD", text)
        self.assertNotIn("GOOD ", text)  # trailing space: the perfectly-scored element has no miss line


class CorpusScoringTests(unittest.TestCase):
    """The metric wired to the real corpus expected_tokens, still with canned OCR strings."""

    def test_a_faithful_transcription_of_eq_001_scores_full_recall(self):
        tokens = corpus_expected_tokens()["CORPUSMARK-EQ-001"]  # ["sum", "w_", "frac"]
        faithful = r"\bar{x}_w = \frac{\sum_{j=1}^{n} w_j x_j}{\sum_{j=1}^{n} w_j}"
        result = score_element("CORPUSMARK-EQ-001", faithful, tokens)
        self.assertEqual(result.missing, ())
        self.assertEqual(result.recall, 1.0)

    def test_scoring_only_touches_elements_present_in_both_maps(self):
        expected = corpus_expected_tokens()
        outputs = {"CORPUSMARK-EQ-001": r"\frac{\sum w_j x_j}{\sum w_j}"}
        report = score(outputs, expected)
        # Only the one supplied output is scored, even though the corpus lists many elements.
        self.assertEqual([r.element_id for r in report.results], ["CORPUSMARK-EQ-001"])
        self.assertEqual(report.micro_recall, 1.0)


class SearchRecoveryTests(unittest.TestCase):
    """End to end: a term that exists only inside an equation image becomes searchable after OCR."""

    # The equation the crop stands in for. 'OWAAC' appears nowhere in the surrounding prose -- it is
    # notation the extractor lost into the image, exactly the content this feature recovers.
    # The placeholder is named positionally (like pymupdf4llm's real crop names), NOT after its
    # content -- otherwise the term would leak through the searchable filename and the "unfindable
    # before" premise would be quietly false.
    BODY = (
        "The ordered weighted averaging aggregation operator is defined in the following way:\n\n"
        "![](image_p1_0.png)\n\n"
        "where the weights sum to one and each score is bounded."
    )
    OCR_TEXT = r"\mathrm{OWAAC}(x_1,\dots,x_n) = \sum_{j=1}^{n} w_j K_j"
    ONLY_IN_EQUATION = "OWAAC"

    def _index_and_search(self, text: str, term: str) -> int:
        """Build a one-record FTS index over ``text`` and return the number of hits for ``term``."""
        record = {
            "zotero_parent_key": "PARENT1",
            "zotero_attachment_key": "ATTACH1",
            "title": "Aggregation operators",
            "creators": "Jane Smith",
            "year": "2024",
            "citation_key": "smith2024",
            "source_path": "one.pdf",
            "markdown_path": "one.md",
            "markdown_sha256": "abc",
            "extraction_tool": "pymupdf4llm.to_markdown+glm-ocr",
            "char_count": len(text),
            "word_count": len(text.split()),
            "page_count": "1",
            "classification": "mapped_verified",
            "identity_status": "verified",
            "identity_rule": "doi_exact",
            "has_math": True,
            "text": strip_front_matter(text),
        }
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "index.jsonl"
            db = Path(tmp) / "index.sqlite"
            jsonl.write_text(json.dumps(record) + "\n", encoding="utf-8")
            build_fts_index(jsonl, db, chunk_chars=400, overlap_chars=40)
            return len(search_fts(db, term, limit=5))

    def _enrich(self) -> str:
        """Splice the canned OCR text in at the placeholder using the real recovery primitives."""
        match = MARKDOWN_IMAGE_RE.search(self.BODY)
        replacement = render_replacement(CLASS_FORMULA, self.OCR_TEXT, match.group(0))
        return splice(self.BODY, [(match.span(), replacement)])

    def test_the_term_is_unfindable_while_it_lives_only_in_the_image(self):
        self.assertEqual(self._index_and_search(self.BODY, self.ONLY_IN_EQUATION), 0)

    def test_ocr_recovery_makes_the_term_searchable(self):
        enriched = self._enrich()
        self.assertIn(self.ONLY_IN_EQUATION, enriched)  # splice actually happened
        self.assertEqual(self._index_and_search(enriched, self.ONLY_IN_EQUATION), 1)

    def test_recovery_does_not_disturb_prose_already_searchable(self):
        # Enrichment must add the equation's terms without dropping the words that were always there.
        enriched = self._enrich()
        self.assertEqual(self._index_and_search(enriched, "aggregation operator"), 1)


if __name__ == "__main__":
    unittest.main()
