import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from zotero_pdf_text.indexer import TextIndexRecord, build_text_index, replace_text_index_record


class IndexerTests(unittest.TestCase):
    def test_build_text_index_writes_jsonl_with_text_without_front_matter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "paper.md"
            markdown.write_text("---\ntitle: Test\n---\n# Heading\n\nBody text", encoding="utf-8")
            manifest = root / "manifest.csv"
            _write_manifest(manifest, markdown)
            output = root / "index" / "zotero_text_index.jsonl"

            build_text_index(manifest, output)

            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["zotero_parent_key"], "PARENT")
            self.assertEqual(record["zotero_attachment_key"], "ATTACH")
            self.assertEqual(record["citation_key"], "smithTitle2024")
            self.assertEqual(record["extraction_tool"], "pymupdf4llm.to_markdown")
            self.assertEqual(record["text"], "# Heading\n\nBody text")
            self.assertGreater(record["char_count"], 0)
            self.assertTrue(record["markdown_sha256"])
            self.assertTrue(output.with_suffix(".summary.md").exists())
            self.assertIs(record["has_math"], False)

    def test_has_math_true_in_manifest_round_trips_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "paper.md"
            markdown.write_text("---\ntitle: Test\n---\n# Heading\n\nBody text", encoding="utf-8")
            manifest = root / "manifest.csv"
            _write_manifest(manifest, markdown, has_math="true")
            output = root / "index" / "zotero_text_index.jsonl"

            build_text_index(manifest, output)

            record = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            self.assertIs(record["has_math"], True)


class ReplaceTextIndexRecordTests(unittest.TestCase):
    def test_replaces_matching_line_and_preserves_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            first = _make_record("ATTACH1", text="Old text one")
            second = _make_record("ATTACH2", text="Untouched text two")
            with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(first.__dict__, ensure_ascii=False) + "\n")
                handle.write(json.dumps(second.__dict__, ensure_ascii=False) + "\n")

            updated = replace(first, extraction_tool="marker", text="New marker text", has_math=True)
            replace_text_index_record(jsonl, "ATTACH1", updated)

            lines = jsonl.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            first_record = json.loads(lines[0])
            second_record = json.loads(lines[1])
            self.assertEqual(first_record["extraction_tool"], "marker")
            self.assertEqual(first_record["text"], "New marker text")
            self.assertIs(first_record["has_math"], True)
            self.assertEqual(second_record["zotero_attachment_key"], "ATTACH2")
            self.assertEqual(second_record["text"], "Untouched text two")

    def test_raises_key_error_for_missing_attachment_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            record = _make_record("ATTACH1")
            jsonl.write_text(json.dumps(record.__dict__, ensure_ascii=False) + "\n", encoding="utf-8")

            with self.assertRaises(KeyError):
                replace_text_index_record(jsonl, "MISSING", record)

    def test_tolerates_unicode_line_separator_in_other_records_text(self):
        # str.splitlines() (unlike a literal "\n" split) also breaks on U+2028/U+2029,
        # which can legitimately appear in extracted PDF text -- this must not corrupt
        # an unrelated record's JSON line while replacing a different record.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            tricky = _make_record("ATTACH1", text="Line one" + chr(0x2028) + "Line two")
            other = _make_record("ATTACH2", text="Untouched text two")
            with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(tricky.__dict__, ensure_ascii=False) + "\n")
                handle.write(json.dumps(other.__dict__, ensure_ascii=False) + "\n")

            updated = replace(other, text="New text two")
            replace_text_index_record(jsonl, "ATTACH2", updated)

            lines = jsonl.read_text(encoding="utf-8").split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            self.assertEqual(len(lines), 2)
            first_record = json.loads(lines[0])
            second_record = json.loads(lines[1])
            self.assertEqual(first_record["zotero_attachment_key"], "ATTACH1")
            self.assertEqual(first_record["text"], "Line one" + chr(0x2028) + "Line two")
            self.assertEqual(second_record["text"], "New text two")

    def test_raises_file_not_found_for_missing_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "does_not_exist.jsonl"
            record = _make_record("ATTACH1")
            with self.assertRaises(FileNotFoundError):
                replace_text_index_record(jsonl, "ATTACH1", record)


def _make_record(attachment_key: str, *, text: str = "Body text") -> TextIndexRecord:
    return TextIndexRecord(
        zotero_parent_key="PARENT",
        zotero_attachment_key=attachment_key,
        title="Title",
        creators="Jane Smith",
        year="2024",
        doi="10.1000/test",
        citation_key="smithTitle2024",
        source_path="paper.pdf",
        markdown_path="paper.md",
        markdown_sha256="abc123",
        extraction_tool="pymupdf4llm.to_markdown",
        char_count=len(text),
        word_count=len(text.split()),
        page_count="2",
        classification="mapped_verified",
        identity_status="verified",
        identity_rule="doi_exact",
        has_math=False,
        text=text,
    )


def _write_manifest(path: Path, markdown: Path, *, has_math: str | None = None) -> None:
    fieldnames = [
        "status",
        "extraction_tool",
        "zotero_parent_key",
        "zotero_attachment_key",
        "title",
        "creators",
        "year",
        "doi",
        "citation_key",
        "source_path",
        "output_path",
        "page_count",
        "classification",
        "identity_status",
        "identity_rule",
        "has_math",
        "error",
    ]
    row = {
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
        "has_math": has_math if has_math is not None else "",
        "error": "",
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


if __name__ == "__main__":
    unittest.main()
