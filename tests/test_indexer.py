import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.indexer import _record_from_manifest_row, load_indexed_keys


class RecordFromManifestRowTests(unittest.TestCase):
    def test_strips_front_matter_and_fills_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "paper.md"
            markdown.write_text("---\ntitle: Test\n---\n# Heading\n\nBody text", encoding="utf-8")
            row = _manifest_row(markdown)

            record = _record_from_manifest_row(row)

            self.assertEqual(record.zotero_parent_key, "PARENT")
            self.assertEqual(record.zotero_attachment_key, "ATTACH")
            self.assertEqual(record.citation_key, "smithTitle2024")
            self.assertEqual(record.extraction_tool, "pymupdf4llm.to_markdown")
            self.assertEqual(record.text, "# Heading\n\nBody text")
            self.assertGreater(record.char_count, 0)
            self.assertTrue(record.markdown_sha256)
            self.assertIs(record.has_math, False)

    def test_has_math_true_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "paper.md"
            markdown.write_text("---\ntitle: Test\n---\nBody", encoding="utf-8")
            row = _manifest_row(markdown, has_math="true")
            self.assertIs(_record_from_manifest_row(row).has_math, True)


class LoadIndexedKeysTests(unittest.TestCase):
    def test_returns_keys_from_valid_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "index.jsonl"
            jsonl_path.write_text(
                json.dumps({"zotero_attachment_key": "ATTACH1"}) + "\n"
                + json.dumps({"zotero_attachment_key": "ATTACH2"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(load_indexed_keys(jsonl_path), {"ATTACH1", "ATTACH2"})

    def test_skips_valid_non_object_json_lines_instead_of_crashing(self):
        # "[]", "null", and "42" are all syntactically valid JSON but not objects -- calling
        # .get() on them would raise AttributeError rather than the ValueError/OSError callers
        # expect to catch for fail-open handling.
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "index.jsonl"
            jsonl_path.write_text(
                "[]\nnull\n42\n" + json.dumps({"zotero_attachment_key": "ATTACH1"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(load_indexed_keys(jsonl_path), {"ATTACH1"})

    def test_returns_empty_set_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_indexed_keys(Path(tmp) / "missing.jsonl"), set())


def _manifest_row(markdown: Path, *, has_math: str = "") -> dict[str, str]:
    return {
        "status": "converted",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "zotero_parent_key": "PARENT",
        "zotero_attachment_key": "ATTACH",
        "title": "Title",
        "creators": "Jane Smith",
        "year": "2024",
        "doi": "10.1000/test",
        "citation_key": "smithTitle2024",
        "source_path": "paper.pdf",
        "output_path": str(markdown),
        "page_count": "2",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": has_math,
        "error": "",
    }


if __name__ == "__main__":
    unittest.main()
