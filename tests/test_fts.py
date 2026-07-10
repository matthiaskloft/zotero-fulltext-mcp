import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.fts import build_fts_index, coverage_report, get_fulltext, get_item_context, search_fts


class FtsTests(unittest.TestCase):
    def test_build_search_and_fetch_fulltext(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)

            summary = build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)

            self.assertEqual(summary.records, 2)
            self.assertGreater(summary.chunks, 1)
            results = search_fts(sqlite_db, "cultural consensus", limit=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].zotero_attachment_key, "ATTACH1")
            self.assertEqual(results[0].citation_key, "smithConsensus2024")
            self.assertIn("cultural", results[0].snippet.casefold())
            self.assertIs(results[0].has_math, True)

            fulltext = get_fulltext(sqlite_db, attachment_key="ATTACH1", max_chars=50)
            self.assertEqual(fulltext.zotero_parent_key, "PARENT1")
            self.assertEqual(fulltext.citation_key, "smithConsensus2024")
            self.assertLessEqual(len(fulltext.text), 50)
            self.assertIs(fulltext.has_math, True)

            fulltext_no_math = get_fulltext(sqlite_db, attachment_key="ATTACH2", max_chars=50)
            self.assertIs(fulltext_no_math.has_math, False)

            context = get_item_context(sqlite_db, parent_key="PARENT1")
            self.assertEqual(context["records"][0]["zotero_attachment_key"], "ATTACH1")
            self.assertEqual(context["records"][0]["citation_key"], "smithConsensus2024")
            self.assertIs(context["records"][0]["has_math"], True)

            citation_results = search_fts(sqlite_db, "smithConsensus2024", limit=1)
            self.assertEqual(citation_results[0].zotero_attachment_key, "ATTACH1")

            old_record_context = get_item_context(sqlite_db, parent_key="PARENT2")
            self.assertEqual(old_record_context["records"][0]["citation_key"], "")
            self.assertIs(old_record_context["records"][0]["has_math"], False)

            coverage = coverage_report(sqlite_db)
            self.assertEqual(coverage["records"], 2)
            self.assertEqual(coverage["by_extraction_tool"]["pymupdf4llm.to_markdown"], 2)
            self.assertEqual(coverage["by_has_math"], {True: 1, False: 1})

    def test_search_rejects_empty_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, sqlite_db)

            with self.assertRaises(ValueError):
                search_fts(sqlite_db, " / ")


def _write_jsonl(path: Path) -> None:
    records = [
        {
            "zotero_parent_key": "PARENT1",
            "zotero_attachment_key": "ATTACH1",
            "title": "Cultural consensus theory",
            "creators": "Jane Smith",
            "year": "2024",
            "doi": "10.1000/one",
            "citation_key": "smithConsensus2024",
            "source_path": "one.pdf",
            "markdown_path": "one.md",
            "markdown_sha256": "abc",
            "extraction_tool": "pymupdf4llm.to_markdown",
            "char_count": 80,
            "word_count": 10,
            "page_count": "2",
            "classification": "mapped_verified",
            "identity_status": "verified",
            "identity_rule": "doi_exact",
            "has_math": True,
            "text": "Cultural consensus theory models shared knowledge. Bayesian models can extend it.",
        },
        {
            "zotero_parent_key": "PARENT2",
            "zotero_attachment_key": "ATTACH2",
            "title": "Response time models",
            "creators": "John Doe",
            "year": "2020",
            "doi": "10.1000/two",
            "source_path": "two.pdf",
            "markdown_path": "two.md",
            "markdown_sha256": "def",
            "extraction_tool": "pymupdf4llm.to_markdown",
            "char_count": 50,
            "word_count": 8,
            "page_count": "1",
            "classification": "mapped_verified",
            "identity_status": "verified",
            "identity_rule": "title_author_or_year",
            "text": "Response time models are useful for psychometric data.",
        },
    ]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    unittest.main()
