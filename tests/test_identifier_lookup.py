"""DOI and ISBN lookup against the local index (#77 F)."""

import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.artifacts import stage_and_publish, write_jsonl_from_existing
from zotero_pdf_text.fts import build_fts_index, lookup_identifier, parse_identifier
from zotero_pdf_text.identity import extract_isbns, normalize_isbn
from zotero_pdf_text.mcp_contract import PublicMcpError, create_server

from test_citation_key_lookup import _record
from test_mcp_server import FakeFastMCP, assert_no_local_path
from test_metadata_quality import LegacyIndexTests

# Real, checksum-valid book numbers (ISBN-10 and its ISBN-13 form).
BOOK_10 = "0-306-40615-2"
BOOK_13 = "978-0-306-40615-7"
OTHER_BOOK_13 = "9780804429573"  # ISBN-10 080442957X


def _build(root: Path) -> Path:
    records = [
        {**_record("PARENTA", "ATTACHA1", "smith2024", "Article text."), "doi": "10.1000/ABC.123"},
        {**_record("PARENTA", "ATTACHA2", "smith2024", "Supplement."), "doi": "https://doi.org/10.1000/abc.123"},
        {**_record("PARENTB", "ATTACHB1", "other2020", "Other article."), "doi": "10.1000/abc.1234"},
        # Two chapters of one book: same ISBN, different parents.
        {**_record("PARENTC", "ATTACHC1", "chap1", "Chapter one."), "isbn": f"{BOOK_13} {OTHER_BOOK_13}"},
        {**_record("PARENTD", "ATTACHD1", "chap2", "Chapter two."), "isbn": "0306406152"},
        {**_record("PARENTE", "ATTACHE1", "nothing", "No identifiers.")},
    ]
    jsonl = root / "index.jsonl"
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    sqlite_path = root / "index.sqlite"
    build_fts_index(jsonl, sqlite_path)
    # The MCP surface only reads a managed, published generation.
    stage_and_publish(root, write_jsonl_from_existing(jsonl), command="test")
    return sqlite_path


def _attachments(result) -> list[str]:
    return [r["zotero_attachment_key"] for r in result["records"]]


class IsbnNormalizationTests(unittest.TestCase):
    def test_isbn_10_and_13_spellings_normalize_to_one_isbn_13(self):
        for spelling in (BOOK_10, BOOK_13, "ISBN 0306406152", "ISBN-13: 978 0 306 40615 7", "isbn:9780306406157"):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_isbn(spelling), "9780306406157")
        self.assertEqual(normalize_isbn("080442957x"), OTHER_BOOK_13)

    def test_wrong_check_digits_and_non_isbns_are_rejected(self):
        for value in ("0-306-40615-3", "978-0-306-40615-8", "12345", "", None, "9790306406157X"):
            with self.subTest(value=value):
                self.assertEqual(normalize_isbn(value), "")

    def test_a_stored_field_may_list_several_isbns(self):
        self.assertEqual(extract_isbns(f"{BOOK_13} 080442957X"), sorted(["9780306406157", OTHER_BOOK_13]))
        self.assertEqual(extract_isbns(""), [])


class IsbnEdgeCaseTests(unittest.TestCase):
    def test_label_and_qualifier_variants(self):
        for spelling in ("ISBN 13: 978-0-306-40615-7", "isbn10 0306406152", f"{BOOK_13}.", f"{BOOK_13} (pbk.)"):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_isbn(spelling), "9780306406157")

    def test_stored_fields_are_validated_and_may_group_digits_with_spaces(self):
        # A wrong ISBN-10 check digit must not match the book its first nine digits name.
        self.assertEqual(extract_isbns("0306406159"), [])
        self.assertEqual(extract_isbns("ISBN 978 0 306 40615 7"), ["9780306406157"])
        self.assertEqual(
            extract_isbns(f"{BOOK_13} {BOOK_10}, 080442957X (pbk.)"), sorted(["9780306406157", OTHER_BOOK_13])
        )


class ParseIdentifierTests(unittest.TestCase):
    def test_dois_in_common_spellings(self):
        for spelling in (
            "10.1000/ABC.123", "doi:10.1000/abc.123", "https://doi.org/10.1000/abc.123",
            " http://dx.doi.org/10.1000/abc.123 ", "doi.org/10.1000/abc.123", "https://www.doi.org/10.1000/abc.123",
            "DOI 10.1000/abc.123", "info:doi/10.1000/abc.123", "10.1000%2Fabc.123",
        ):
            with self.subTest(spelling=spelling):
                self.assertEqual(parse_identifier(spelling), ("doi", "10.1000/abc.123"))

    def test_isbns_and_invalid_values(self):
        self.assertEqual(parse_identifier(BOOK_10), ("isbn", "9780306406157"))
        for bad in ("", "   ", "smith2024", "10.1000", "0-306-40615-3", "x" * 300):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_identifier(bad)


class LookupIdentifierFtsTests(unittest.TestCase):
    def test_doi_matches_case_and_url_variants_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(Path(tmp))
            result = lookup_identifier(db, "DOI:10.1000/abc.123")
            self.assertEqual(_attachments(result), ["ATTACHA1", "ATTACHA2"])
            self.assertEqual((result["identifier_type"], result["normalized"]), ("doi", "10.1000/abc.123"))
            self.assertEqual(result["parent_keys"], ["PARENTA"])
            # A DOI that only starts with a stored one is a different DOI.
            self.assertEqual(_attachments(lookup_identifier(db, "10.1000/abc.12")), [])

    def test_isbn_matches_either_form_and_any_listed_isbn(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(Path(tmp))
            for spelling in (BOOK_10, BOOK_13):
                with self.subTest(spelling=spelling):
                    result = lookup_identifier(db, spelling)
                    self.assertEqual(_attachments(result), ["ATTACHC1", "ATTACHD1"])
                    self.assertEqual(result["parent_keys"], ["PARENTC", "PARENTD"])
            self.assertEqual(_attachments(lookup_identifier(db, "080442957X")), ["ATTACHC1"])

    def test_limit_reports_truncation_but_keeps_every_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = lookup_identifier(_build(Path(tmp)), BOOK_13, limit=1)
        self.assertEqual(_attachments(result), ["ATTACHC1"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["parent_keys"], ["PARENTC", "PARENTD"])

    def test_an_index_without_the_isbn_column_matches_no_isbn(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = LegacyIndexTests._legacy_db(LegacyIndexTests(), tmp)
            self.assertEqual(lookup_identifier(db, BOOK_13)["records"], [])


class LookupIdentifierToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.server = create_server(_build(self.root), mcp_factory=FakeFastMCP)
        self.lookup = self.server.tools["lookup_identifier"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_found_doi_reports_type_normalized_value_and_records(self):
        result = self.lookup(identifier="https://doi.org/10.1000/ABC.123")
        self.assertEqual(result["identifier"], "https://doi.org/10.1000/ABC.123")
        self.assertEqual((result["identifier_type"], result["normalized"]), ("doi", "10.1000/abc.123"))
        self.assertTrue(result["found"])
        self.assertFalse(result["ambiguous"])
        self.assertEqual([r["attachment_key"] for r in result["records"]], ["ATTACHA1", "ATTACHA2"])
        assert_no_local_path(self, result, self.root)

    def test_shared_isbn_is_ambiguous_and_missing_one_is_not_found(self):
        shared = self.lookup(identifier=BOOK_10)
        self.assertTrue(shared["ambiguous"])
        self.assertEqual(shared["parent_keys"], ["PARENTC", "PARENTD"])
        missing = self.lookup(identifier="10.9999/absent")
        self.assertEqual(
            {k: missing[k] for k in ("found", "ambiguous", "truncated", "records", "parent_keys")},
            {"found": False, "ambiguous": False, "truncated": False, "records": [], "parent_keys": []},
        )

    def test_invalid_identifiers_are_public_errors(self):
        for bad in ("", "  ", None, 5, "smith2024", "0-306-40615-3", "x" * 300):
            with self.subTest(bad=bad):
                with self.assertRaises(PublicMcpError) as raised:
                    self.lookup(identifier=bad)
                self.assertEqual(raised.exception.code, "invalid_identifier")


if __name__ == "__main__":
    unittest.main()
