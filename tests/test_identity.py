import unittest

from zotero_pdf_text.identity import classify_identity, normalize_doi, safe_folder_id, strip_markdown_images, title_score


class IdentityTests(unittest.TestCase):
    def test_normalize_doi_removes_common_prefixes(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1000/ABC."), "10.1000/abc")
        self.assertEqual(normalize_doi("doi: 10.1000/ABC"), "10.1000/abc")

    def test_safe_folder_id_removes_path_separators(self):
        self.assertEqual(safe_folder_id("doi:10.1000/foo(bar)"), "doi_10.1000_foo_bar")

    def test_identity_verifies_exact_doi(self):
        evidence = classify_identity(
            title="A Good Article",
            doi="10.1000/example",
            year="2024",
            author_surnames=["Smith"],
            item_type="journalArticle",
            text="This PDF mentions https://doi.org/10.1000/example in the header.",
        )
        self.assertEqual(evidence.status, "verified")
        self.assertEqual(evidence.rule, "doi_exact")

    def test_strip_markdown_images_removes_image_syntax_only(self):
        text = "![](../images/A-New-Auto-Exposure-System.png)\n\nReal article prose follows."
        stripped = strip_markdown_images(text)
        self.assertNotIn("A-New-Auto-Exposure-System", stripped)
        self.assertIn("Real article prose follows.", stripped)

    def test_title_score_ignores_title_found_only_in_image_filename(self):
        title = "A New Auto Exposure System for High Dynamic Range"
        # The title only ever appears inside an embedded image filename, never in real prose.
        text = (
            "![](../images/A-New-Auto-Exposure-System-for-High-Dynamic-Range.png)\n\n"
            "This unrelated article discusses camera sensor calibration and lens design."
        )
        raw_score = title_score(title, text)
        stripped_score = title_score(title, strip_markdown_images(text))
        self.assertGreaterEqual(raw_score, 86)
        self.assertLess(stripped_score, 86)

    def test_classify_identity_does_not_verify_from_title_in_image_filename(self):
        title = "A New Auto Exposure System for High Dynamic Range"
        text = (
            "![](../images/A-New-Auto-Exposure-System-for-High-Dynamic-Range.png)\n\n"
            "This unrelated article discusses camera sensor calibration and lens design by "
            "Smith in 2020."
        )
        evidence = classify_identity(
            title=title,
            doi=None,
            year="2020",
            author_surnames=["Smith"],
            item_type="journalArticle",
            text=text,
        )
        # Without stripping the image filename, the inflated title_score plus the genuine
        # author/year hits in the unrelated prose would wrongly verify this as a match.
        self.assertNotEqual(evidence.status, "verified")
        self.assertLess(evidence.title_score, 86)

    def test_conflicting_doi_overrides_high_title_score_regardless_of_score(self):
        # A real, differently-parsed DOI in the text must disqualify the mapping even when
        # generic topic-vocabulary overlap pushes the title score above the accept threshold.
        title = "Process Management in Distributed Systems"
        text = (
            "Process Management in Distributed Systems: a survey of process management "
            "techniques. DOI: 10.1007/978-3-662-49851-4. Smith 2016."
        )
        evidence = classify_identity(
            title=title,
            doi="10.1007/978-3-642-33010-0",
            year="2016",
            author_surnames=["Smith"],
            item_type="book",
            text=text,
        )
        self.assertGreaterEqual(evidence.title_score, 86)
        self.assertEqual(evidence.status, "possible_mismatch")
        self.assertEqual(evidence.rule, "conflicting_doi_low_title")

    def test_conflicting_doi_with_low_title_score_is_still_a_mismatch(self):
        evidence = classify_identity(
            title="Bayesian Psychometrics for Longitudinal Response Processes",
            doi="10.1000/right",
            year="2024",
            author_surnames=["Smith"],
            item_type="journalArticle",
            text="Completely different article. DOI: 10.2000/wrong.",
        )
        self.assertEqual(evidence.status, "possible_mismatch")
        self.assertEqual(evidence.rule, "conflicting_doi_low_title")


if __name__ == "__main__":
    unittest.main()
