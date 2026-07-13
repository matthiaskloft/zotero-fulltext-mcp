import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.fts import build_fts_index
from zotero_pdf_text.lock import pipeline_write_lock
from zotero_pdf_text.math_ocr import reconvert_with_marker


class ReconvertWithMarkerTests(unittest.TestCase):
    def test_happy_path_updates_markdown_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, jsonl_path, sqlite_path = _build_fixture(root)

            def _write_marker_output(args, **kwargs):
                Path(args[4]).write_text("# Better body\n\nEquation: $x^2$", encoding="utf-8")

            with patch("zotero_pdf_text.math_ocr.subprocess.run", side_effect=_write_marker_output):
                result = reconvert_with_marker(
                    "ATTACH1",
                    db_path=sqlite_path,
                    jsonl_path=jsonl_path,
                    fts_db_path=sqlite_path,
                    lock_root=root,
                    timeout_seconds=60,
                )

            self.assertTrue(result.ok)
            self.assertEqual(result.attachment_key, "ATTACH1")
            self.assertEqual(result.previous_extraction_tool, "pymupdf4llm.to_markdown")
            self.assertEqual(result.new_extraction_tool, "marker")
            self.assertNotEqual(result.new_char_count, result.previous_char_count)
            self.assertEqual(result.markdown_path, str(markdown_path))

            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn('extraction_tool: "marker"', markdown)
            self.assertIn('previous_extraction_tool: "pymupdf4llm.to_markdown"', markdown)
            self.assertIn("reconverted_at:", markdown)
            self.assertIn("# Better body", markdown)

            records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["extraction_tool"], "marker")
            self.assertIn("Better body", records[0]["text"])

    def test_not_found_returns_error_without_touching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, jsonl_path, sqlite_path = _build_fixture(root)
            original_markdown = markdown_path.read_text(encoding="utf-8")
            original_jsonl = jsonl_path.read_text(encoding="utf-8")

            with patch("zotero_pdf_text.math_ocr.subprocess.run") as mock_run:
                result = reconvert_with_marker(
                    "MISSING_KEY",
                    db_path=sqlite_path,
                    jsonl_path=jsonl_path,
                    fts_db_path=sqlite_path,
                    lock_root=root,
                )

            self.assertFalse(result.ok)
            self.assertIn("No indexed record found", result.error)
            mock_run.assert_not_called()
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), original_markdown)
            self.assertEqual(jsonl_path.read_text(encoding="utf-8"), original_jsonl)

    def test_subprocess_failure_preserves_existing_markdown_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, jsonl_path, sqlite_path = _build_fixture(root)
            original_markdown = markdown_path.read_text(encoding="utf-8")
            original_jsonl = jsonl_path.read_text(encoding="utf-8")

            with patch(
                "zotero_pdf_text.math_ocr.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "cmd", stderr="marker failed"),
            ):
                result = reconvert_with_marker(
                    "ATTACH1",
                    db_path=sqlite_path,
                    jsonl_path=jsonl_path,
                    fts_db_path=sqlite_path,
                    lock_root=root,
                )

            self.assertFalse(result.ok)
            self.assertIn("marker-pdf extraction failed", result.error)
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), original_markdown)
            self.assertEqual(jsonl_path.read_text(encoding="utf-8"), original_jsonl)

    def test_missing_source_pdf_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, jsonl_path, sqlite_path = _build_fixture(root)
            source_path.unlink()

            with patch("zotero_pdf_text.math_ocr.subprocess.run") as mock_run:
                result = reconvert_with_marker(
                    "ATTACH1",
                    db_path=sqlite_path,
                    jsonl_path=jsonl_path,
                    fts_db_path=sqlite_path,
                    lock_root=root,
                )

            self.assertFalse(result.ok)
            self.assertIn("no longer exists", result.error)
            mock_run.assert_not_called()

    def test_shares_lock_with_convert_new_over_the_same_output_root(self):
        # Regression test: reconvert-math used to lock jsonl_path.parent (the index directory)
        # while convert-new locks config.output_root -- different lock files, so both could run
        # concurrently and silently discard one writer's update. Both must now lock the same
        # config.output_root so one blocks the other.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, jsonl_path, sqlite_path = _build_fixture(root)
            original_jsonl = jsonl_path.read_text(encoding="utf-8")
            original_markdown = markdown_path.read_text(encoding="utf-8")

            def _write_marker_output(args, **kwargs):
                Path(args[4]).write_text("# Better body\n\nEquation: $x^2$", encoding="utf-8")

            # Simulate convert-new already holding root's pipeline lock.
            with pipeline_write_lock(root, command="convert-new"):
                with patch("zotero_pdf_text.math_ocr.subprocess.run", side_effect=_write_marker_output):
                    result = reconvert_with_marker(
                        "ATTACH1",
                        db_path=sqlite_path,
                        jsonl_path=jsonl_path,
                        fts_db_path=sqlite_path,
                        lock_root=root,
                    )

            self.assertFalse(result.ok)
            self.assertIn("is held by host", result.error)
            # Both the index and the Markdown were untouched -- the Markdown write happens inside
            # the lock alongside the JSONL/FTS update now, so a contested lock leaves no partial
            # state (previously the Markdown write ran before the lock check and would have been
            # updated here even though the index stayed stale).
            self.assertEqual(jsonl_path.read_text(encoding="utf-8"), original_jsonl)
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), original_markdown)


def _build_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    source_path = root / "paper.pdf"
    source_path.write_bytes(b"%PDF")
    markdown_dir = root / "output" / "markdown"
    markdown_dir.mkdir(parents=True)
    markdown_path = markdown_dir / "0001_paper.md"
    markdown_path.write_text(
        '---\n'
        'zotero_parent_key: "PARENT1"\n'
        'zotero_attachment_key: "ATTACH1"\n'
        'title: "Title"\n'
        'creators: "Jane Smith"\n'
        'year: "2024"\n'
        'doi: "10.1000/test"\n'
        'citation_key: "smithTitle2024"\n'
        f'source_path: "{source_path}"\n'
        'extraction_tool: "pymupdf4llm.to_markdown"\n'
        "has_math: true\n"
        "---\n\n"
        "# Old body\n\nGarbled equation text",
        encoding="utf-8",
    )

    jsonl_path = root / "index" / "zotero_text_index.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    record = {
        "zotero_parent_key": "PARENT1",
        "zotero_attachment_key": "ATTACH1",
        "title": "Title",
        "creators": "Jane Smith",
        "year": "2024",
        "doi": "10.1000/test",
        "citation_key": "smithTitle2024",
        "source_path": str(source_path),
        "markdown_path": str(markdown_path),
        "markdown_sha256": "deadbeef",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "char_count": 9999,
        "word_count": 4,
        "page_count": "2",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": True,
        "text": "# Old body\n\nGarbled equation text",
    }
    jsonl_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    sqlite_path = root / "index" / "zotero_text_index.sqlite"
    build_fts_index(jsonl_path, sqlite_path)

    return source_path, markdown_path, jsonl_path, sqlite_path


if __name__ == "__main__":
    unittest.main()
