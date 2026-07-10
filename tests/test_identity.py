import unittest

from zotero_pdf_text.identity import classify_identity, normalize_doi, safe_folder_id


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


if __name__ == "__main__":
    unittest.main()
