import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text._atomic import replace_with_retry as atomic_replace_with_retry
from zotero_pdf_text.artifacts import (
    current_generation_jsonl,
    read_current_pointer,
    resolve_reader_db_path,
    stage_and_publish,
    write_jsonl_from_existing,
)
from zotero_pdf_text.fts import search_fts
from zotero_pdf_text.lock import pipeline_write_lock
from zotero_pdf_text.math_ocr import reconvert_with_marker


class ReconvertWithMarkerTests(unittest.TestCase):
    def test_happy_path_updates_markdown_and_publishes_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, index_root = _build_fixture(root)
            generation_before = read_current_pointer(index_root)["current_generation"]

            def _write_marker_output(args, **kwargs):
                Path(args[4]).write_text("# Better body\n\nEquation: $x^2$", encoding="utf-8")

            with patch("zotero_pdf_text.math_ocr.subprocess.run", side_effect=_write_marker_output):
                result = reconvert_with_marker(
                    "ATTACH1",
                    index_root=index_root,
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

            # A successor generation was published and carries the new record.
            self.assertNotEqual(read_current_pointer(index_root)["current_generation"], generation_before)
            jsonl_path = current_generation_jsonl(index_root)
            records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["extraction_tool"], "marker")
            self.assertIn("Better body", records[0]["text"])
            db_path = resolve_reader_db_path(index_root / "zotero_text_index.sqlite")
            self.assertTrue(search_fts(db_path, "Better"))

    def test_not_found_returns_error_without_touching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, index_root = _build_fixture(root)
            original_markdown = markdown_path.read_text(encoding="utf-8")
            pointer_before = read_current_pointer(index_root)

            with patch("zotero_pdf_text.math_ocr.subprocess.run") as mock_run:
                result = reconvert_with_marker("MISSING_KEY", index_root=index_root, lock_root=root)

            self.assertFalse(result.ok)
            self.assertIn("No indexed record found", result.error)
            mock_run.assert_not_called()
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), original_markdown)
            self.assertEqual(read_current_pointer(index_root), pointer_before)

    def test_subprocess_failure_preserves_existing_markdown_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, index_root = _build_fixture(root)
            original_markdown = markdown_path.read_text(encoding="utf-8")
            pointer_before = read_current_pointer(index_root)

            with patch(
                "zotero_pdf_text.math_ocr.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "cmd", stderr="marker failed"),
            ):
                result = reconvert_with_marker("ATTACH1", index_root=index_root, lock_root=root)

            self.assertFalse(result.ok)
            self.assertIn("marker-pdf extraction failed", result.error)
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), original_markdown)
            self.assertEqual(read_current_pointer(index_root), pointer_before)

    def test_unmigrated_legacy_layout_fails_before_starting_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_fixture(root, publish_generation=False)
            index_root = root / "index"

            with patch("zotero_pdf_text.math_ocr.subprocess.run") as mock_run:
                result = reconvert_with_marker("ATTACH1", index_root=index_root, lock_root=root)

            self.assertFalse(result.ok)
            self.assertIn("rebuild-index", result.error)
            mock_run.assert_not_called()

    def test_commit_failure_rolls_back_markdown_and_keeps_prior_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, markdown_path, index_root = _build_fixture(root)
            original_markdown = markdown_path.read_bytes()
            pointer_before = read_current_pointer(index_root)
            images_dir = markdown_path.parent.parent / "images" / markdown_path.stem
            images_dir.mkdir(parents=True)
            (images_dir / "existing.png").write_bytes(b"old-image")

            def _write_marker_output(args, **kwargs):
                Path(args[4]).write_text("# Better body\n\nEquation: $x^2$", encoding="utf-8")
                staged_images = Path(args[6])
                staged_images.mkdir(parents=True, exist_ok=True)
                (staged_images / "replacement.png").write_bytes(b"new-image")

            with patch("zotero_pdf_text.math_ocr.subprocess.run", side_effect=_write_marker_output), patch(
                "zotero_pdf_text.math_ocr.stage_and_publish",
                side_effect=RuntimeError("simulated publication failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated publication failure"):
                    reconvert_with_marker("ATTACH1", index_root=index_root, lock_root=root)

            # The Markdown/image swap was rolled back; the pointer never moved, so the previous
            # generation is still current and still searchable.
            self.assertEqual(markdown_path.read_bytes(), original_markdown)
            self.assertEqual((images_dir / "existing.png").read_bytes(), b"old-image")
            self.assertFalse((images_dir / "replacement.png").exists())
            self.assertEqual(read_current_pointer(index_root), pointer_before)
            db_path = resolve_reader_db_path(index_root / "zotero_text_index.sqlite")
            self.assertTrue(search_fts(db_path, "Garbled"))
            self.assertFalse(search_fts(db_path, "Better"))

    def test_sidecar_key_race_returns_clean_failure_without_committing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, markdown_path, index_root = _build_fixture(root)
            original_markdown = markdown_path.read_bytes()
            pointer_before = read_current_pointer(index_root)

            def _write_marker_output(args, **kwargs):
                Path(args[4]).write_text("# Better body", encoding="utf-8")

            with patch("zotero_pdf_text.math_ocr.subprocess.run", side_effect=_write_marker_output), patch(
                "zotero_pdf_text.math_ocr.load_indexed_keys",
                side_effect=[{"ATTACH1"}, set()],
            ):
                result = reconvert_with_marker("ATTACH1", index_root=index_root, lock_root=root)

            self.assertFalse(result.ok)
            self.assertIn("No text-sidecar record found", result.error)
            self.assertEqual(markdown_path.read_bytes(), original_markdown)
            self.assertEqual(read_current_pointer(index_root), pointer_before)
            db_path = resolve_reader_db_path(index_root / "zotero_text_index.sqlite")
            self.assertTrue(search_fts(db_path, "Garbled"))
            self.assertFalse(search_fts(db_path, "Better"))

    def test_image_restore_failure_preserves_the_prior_image_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, markdown_path, index_root = _build_fixture(root)
            images_dir = markdown_path.parent.parent / "images" / markdown_path.stem
            images_dir.mkdir(parents=True)
            (images_dir / "existing.png").write_bytes(b"old-image")
            original_markdown = markdown_path.read_bytes()

            def _write_marker_output(args, **kwargs):
                Path(args[4]).write_text("# Better body", encoding="utf-8")
                staged_images = Path(args[6])
                staged_images.mkdir(parents=True, exist_ok=True)
                (staged_images / "replacement.png").write_bytes(b"new-image")

            def _fail_prior_image_restore(source: Path, destination: Path, **kwargs):
                if source.name == "previous-images":
                    raise PermissionError("simulated image lock")
                return atomic_replace_with_retry(source, destination, **kwargs)

            with patch("zotero_pdf_text.math_ocr.subprocess.run", side_effect=_write_marker_output), patch(
                "zotero_pdf_text.math_ocr.stage_and_publish",
                side_effect=RuntimeError("simulated publication failure"),
            ), patch("zotero_pdf_text.math_ocr.replace_with_retry", side_effect=_fail_prior_image_restore):
                with self.assertRaisesRegex(RuntimeError, "could not restore prior image assets"):
                    reconvert_with_marker("ATTACH1", index_root=index_root, lock_root=root)

            self.assertEqual(markdown_path.read_bytes(), original_markdown)
            backups = list(images_dir.parent.glob(".0001_paper.marker-*/previous-images/existing.png"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"old-image")

    def test_missing_source_pdf_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path, markdown_path, index_root = _build_fixture(root)
            source_path.unlink()

            with patch("zotero_pdf_text.math_ocr.subprocess.run") as mock_run:
                result = reconvert_with_marker("ATTACH1", index_root=index_root, lock_root=root)

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
            source_path, markdown_path, index_root = _build_fixture(root)
            original_markdown = markdown_path.read_text(encoding="utf-8")
            pointer_before = read_current_pointer(index_root)

            def _write_marker_output(args, **kwargs):
                Path(args[4]).write_text("# Better body\n\nEquation: $x^2$", encoding="utf-8")

            # Simulate convert-new already holding root's pipeline lock.
            with pipeline_write_lock(root, command="convert-new"):
                with patch("zotero_pdf_text.math_ocr.subprocess.run", side_effect=_write_marker_output):
                    result = reconvert_with_marker("ATTACH1", index_root=index_root, lock_root=root)

            self.assertFalse(result.ok)
            self.assertIn("is held by host", result.error)
            # Both the index and the Markdown were untouched -- the Markdown write happens inside
            # the lock alongside the generation publication, so a contested lock leaves no partial
            # state.
            self.assertEqual(read_current_pointer(index_root), pointer_before)
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), original_markdown)


def _build_fixture(root: Path, *, publish_generation: bool = True) -> tuple[Path, Path, Path]:
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

    index_root = root / "index"
    legacy_jsonl = index_root / "zotero_text_index.jsonl"
    legacy_jsonl.parent.mkdir(parents=True)
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
    legacy_jsonl.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    if publish_generation:
        stage_and_publish(index_root, write_jsonl_from_existing(legacy_jsonl), command="test")

    return source_path, markdown_path, index_root


if __name__ == "__main__":
    unittest.main()
