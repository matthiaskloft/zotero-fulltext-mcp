import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text._ollama_client import OllamaError, RuntimeStatus
from zotero_pdf_text.artifacts import (
    current_generation_jsonl,
    read_current_pointer,
    stage_and_publish,
    write_jsonl_from_existing,
)
from zotero_pdf_text.config import ImageOcrSettings
from zotero_pdf_text.image_ocr import CLASS_FIGURE, CLASS_FORMULA, ocr_images_for_attachment

SETTINGS = ImageOcrSettings(model="glm-ocr:test")
INPLACE = ImageOcrSettings(model="glm-ocr:test", enriched_suffix="")
HEALTHY = RuntimeStatus(server_running=True, model_present=True, detail="ok")


def _enriched_path(markdown_path: Path, settings: ImageOcrSettings = SETTINGS) -> Path:
    suffix = settings.enriched_suffix
    return markdown_path.with_name(markdown_path.stem + suffix + markdown_path.suffix)


def _png_bytes(width: int, height: int) -> bytes:
    ihdr = struct.pack(">II", width, height) + bytes([8, 2, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


def _build_fixture(root: Path) -> tuple[Path, Path, Path]:
    """A converted document with one equation crop, plus a published index generation."""
    source_path = root / "paper.pdf"
    source_path.write_bytes(b"%PDF")

    output_root = root / "output"
    markdown_dir = output_root / "markdown"
    markdown_dir.mkdir(parents=True)
    markdown_path = markdown_dir / "0001_paper.md"
    body = "# Paper\n\nThe result follows:\n\n![](crop-01.png)\n\nwhere x is free."
    markdown_path.write_text(
        "---\n"
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
        "---\n\n" + body,
        encoding="utf-8",
    )

    images_dir = output_root / "images" / "0001_paper"
    images_dir.mkdir(parents=True)
    (images_dir / "crop-01.png").write_bytes(_png_bytes(537, 28))

    index_root = output_root / "index"
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
        "char_count": 9999,
        "word_count": 4,
        "extraction_tool": "pymupdf4llm.to_markdown",
        "page_count": "2",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": True,
        "text": body,
    }
    legacy_jsonl.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    stage_and_publish(index_root, write_jsonl_from_existing(legacy_jsonl), command="test")
    return output_root, markdown_path, index_root


def _build_two_crop_fixture(root: Path) -> tuple[Path, Path, Path]:
    """Like _build_fixture but with two crops, so a run can partially recover the mathematics."""
    output_root, markdown_path, index_root = _build_fixture(root)
    images_dir = output_root / "images" / "0001_paper"
    (images_dir / "crop-02.png").write_bytes(_png_bytes(540, 30))
    body = (
        "# Paper\n\nThe result follows:\n\n![](crop-01.png)\n\n"
        "and also:\n\n![](crop-02.png)\n\nwhere x is free."
    )
    text = markdown_path.read_text(encoding="utf-8")
    head, _, _ = text.partition("---\n\n")
    markdown_path.write_text(head + "---\n\n" + body, encoding="utf-8")

    # Keep the published record's text in step with the two-crop body.
    jsonl = current_generation_jsonl(index_root)
    record = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[0])
    record["text"] = body
    legacy = index_root / "zotero_text_index.jsonl"
    legacy.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    stage_and_publish(index_root, write_jsonl_from_existing(legacy), command="test")
    return output_root, markdown_path, index_root


def _run(output_root: Path, index_root: Path, *, settings: ImageOcrSettings = SETTINGS, **kwargs):
    return ocr_images_for_attachment(
        "ATTACH1",
        index_root=index_root,
        lock_root=output_root,
        output_root=output_root,
        settings=settings,
        **kwargs,
    )


class ImageOcrCommitTests(unittest.TestCase):
    def test_happy_path_writes_a_sibling_and_leaves_the_original_untouched(self):
        """[R3] The republished generation must describe the enriched file now on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            original_before = markdown_path.read_bytes()
            generation_before = read_current_pointer(index_root)["current_generation"]

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="E = mc^2"):
                result = _run(output_root, index_root)

            self.assertTrue(result.ok, result.error)
            # The original is a permanent, byte-identical anchor.
            self.assertEqual(markdown_path.read_bytes(), original_before)

            enriched_path = _enriched_path(markdown_path)
            self.assertEqual(result.enriched_markdown_path, str(enriched_path))
            self.assertTrue(enriched_path.exists())
            enriched = enriched_path.read_text(encoding="utf-8")
            self.assertIn("$$\nE = mc^2\n$$", enriched)
            self.assertNotIn("![](crop-01.png)", enriched)
            self.assertIn('extraction_tool: "pymupdf4llm.to_markdown+glm-ocr"', enriched)
            self.assertIn("image_ocr_tool:", enriched)
            self.assertIn("has_math: true", enriched)

            self.assertNotEqual(
                read_current_pointer(index_root)["current_generation"], generation_before
            )
            records = [
                json.loads(line)
                for line in current_generation_jsonl(index_root).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(records), 1)
            published = records[0]
            # The index now points at the enriched sibling, not the original.
            self.assertEqual(published["markdown_path"], str(enriched_path))
            self.assertEqual(published["extraction_tool"], "pymupdf4llm.to_markdown+glm-ocr")
            self.assertIn("E = mc^2", published["text"])
            self.assertEqual(published["char_count"], len(published["text"]))
            self.assertEqual(published["word_count"], len(published["text"].split()))
            self.assertNotEqual(published["markdown_sha256"], "deadbeef")
            self.assertTrue(published["has_math"])

    def test_figure_only_run_records_participation_but_not_math_capability(self):
        """A run that recovered no mathematics must not shed the math-lossy warning. GLM-OCR
        participation is still recorded (image_ocr_tool), but the extractor label -- which gates
        math_extraction_may_be_lossy -- stays unchanged so the warning persists."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FIGURE
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="A scatter plot."):
                result = _run(output_root, index_root)

            self.assertTrue(result.ok, result.error)
            enriched = _enriched_path(markdown_path).read_text(encoding="utf-8")
            self.assertNotIn("+glm-ocr", enriched)            # not claimed math-capable
            self.assertIn("image_ocr_tool:", enriched)        # participation still recorded
            published = json.loads(
                current_generation_jsonl(index_root).read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(published["extraction_tool"], "pymupdf4llm.to_markdown")

    def test_partial_formula_failure_is_not_marked_math_capable(self):
        """When some formula crops failed, the mathematics was only partially recovered, so the
        record must not be marked math-capable even though a splice happened."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_two_crop_fixture(Path(tmp))
            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch(
                "zotero_pdf_text.image_ocr.generate",
                side_effect=["E = mc^2", OllamaError("second crop failed")],
            ):
                result = _run(output_root, index_root)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.ocr_failed, 1)
            enriched = _enriched_path(markdown_path).read_text(encoding="utf-8")
            self.assertIn("$$\nE = mc^2\n$$", enriched)       # the one that succeeded was spliced
            self.assertIn("![](crop-02.png)", enriched)       # the one that failed kept its placeholder
            self.assertNotIn("+glm-ocr", enriched)            # not claimed math-capable
            published = json.loads(
                current_generation_jsonl(index_root).read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(published["extraction_tool"], "pymupdf4llm.to_markdown")

    def test_all_formulas_recovered_is_marked_math_capable(self):
        """The positive control for the two above: when every formula crop succeeds, the composite
        math-capable marker is applied and the warning is correctly suppressed."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_two_crop_fixture(Path(tmp))
            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch("zotero_pdf_text.image_ocr.generate", side_effect=["E = mc^2", "a^2 + b^2"]):
                result = _run(output_root, index_root)

            self.assertTrue(result.ok, result.error)
            published = json.loads(
                current_generation_jsonl(index_root).read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(published["extraction_tool"], "pymupdf4llm.to_markdown+glm-ocr")

    def test_in_place_mode_overwrites_when_the_suffix_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="E = mc^2"):
                result = _run(output_root, index_root, settings=INPLACE)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.enriched_markdown_path, str(markdown_path))
            self.assertIn("$$\nE = mc^2\n$$", markdown_path.read_text(encoding="utf-8"))
            # No sibling is created in this mode.
            self.assertFalse(_enriched_path(markdown_path).exists())

    def test_publish_failure_removes_the_newly_written_enriched_file(self):
        """[R1] A failed publication must not leave an enriched file the index doesn't describe."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            original_before = markdown_path.read_bytes()
            generation_before = read_current_pointer(index_root)["current_generation"]

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="E = mc^2"), patch(
                "zotero_pdf_text.image_ocr.stage_and_publish", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(RuntimeError):
                    _run(output_root, index_root)

            # The original is untouched and the half-written sibling was cleaned up.
            self.assertEqual(markdown_path.read_bytes(), original_before)
            self.assertFalse(_enriched_path(markdown_path).exists())
            self.assertEqual(
                read_current_pointer(index_root)["current_generation"], generation_before
            )

    def test_in_place_publish_failure_restores_the_original(self):
        """[R1] With an empty suffix the target is the original, which must be restored."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            before = markdown_path.read_bytes()

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="E = mc^2"), patch(
                "zotero_pdf_text.image_ocr.stage_and_publish", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(RuntimeError):
                    _run(output_root, index_root, settings=INPLACE)

            self.assertEqual(markdown_path.read_bytes(), before)

    def test_original_changed_during_ocr_aborts_without_publishing(self):
        """[R2] The splice is built from the original read before OCR began, possibly long ago."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            generation_before = read_current_pointer(index_root)["current_generation"]
            concurrent_text = "---\ntitle: \"Replaced\"\n---\n\nwritten by another writer"

            def _generate_and_race(*args, **kwargs):
                markdown_path.write_text(concurrent_text, encoding="utf-8")
                return "E = mc^2"

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch("zotero_pdf_text.image_ocr.generate", side_effect=_generate_and_race):
                result = _run(output_root, index_root)

            self.assertFalse(result.ok)
            self.assertIn("changed while image OCR was running", result.error)
            # The concurrent write survives, no sibling was left behind, and nothing was published.
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), concurrent_text)
            self.assertFalse(_enriched_path(markdown_path).exists())
            self.assertEqual(
                read_current_pointer(index_root)["current_generation"], generation_before
            )

    def test_rerun_without_force_is_refused_when_the_sibling_exists(self):
        """[R4] Figure splices are not idempotent; a second pass would append twice."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FIGURE
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="A plot."):
                self.assertTrue(_run(output_root, index_root).ok)
                enriched_after_first = _enriched_path(markdown_path).read_bytes()
                second = _run(output_root, index_root)

            self.assertFalse(second.ok)
            self.assertIn("already exists", second.error)
            self.assertEqual(_enriched_path(markdown_path).read_bytes(), enriched_after_first)

    def test_force_regenerates_the_sibling_from_the_pristine_original(self):
        """A forced re-run reads the original, not the enriched sibling, so it stays correct."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="E = mc^2"):
                self.assertTrue(_run(output_root, index_root).ok)
                # The index now points at the sibling; a forced re-run must still succeed and
                # produce the same enrichment rather than choking on the already-spliced file.
                forced = _run(output_root, index_root, force=True)

            self.assertTrue(forced.ok, forced.error)
            self.assertIn("$$\nE = mc^2\n$$", _enriched_path(markdown_path).read_text(encoding="utf-8"))

    def test_cached_results_are_reused_on_a_forced_rerun(self):
        # Classified as a figure on purpose: figure splices keep the image link, so the crop is
        # still referenced on the second pass and the cache is actually exercised. A formula
        # splice consumes its placeholder, leaving nothing to re-read.
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FIGURE
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="A plot.") as generate:
                self.assertTrue(_run(output_root, index_root).ok)
                self.assertEqual(generate.call_count, 1)
                self.assertTrue(_run(output_root, index_root, force=True).ok)
                # Same crop bytes, same prompt: the cached result is reused.
                self.assertEqual(generate.call_count, 1)

    def test_all_decorative_crops_is_a_successful_no_op(self):
        # A document whose only crop is classified skip has nothing to OCR; that is success with
        # a note, not a failure, and it must not write an enriched sibling.
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            generation_before = read_current_pointer(index_root)["current_generation"]

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value="skip"
            ), patch("zotero_pdf_text.image_ocr.generate") as generate:
                result = _run(output_root, index_root)

            self.assertTrue(result.ok)
            self.assertIn("No crop needed OCR", result.note)
            generate.assert_not_called()
            self.assertFalse(_enriched_path(markdown_path).exists())
            self.assertEqual(
                read_current_pointer(index_root)["current_generation"], generation_before
            )

    def test_all_eligible_crops_failing_is_reported_as_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            before = markdown_path.read_bytes()

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch(
                "zotero_pdf_text.image_ocr.generate", side_effect=OllamaError("HTTP 500")
            ):
                result = _run(output_root, index_root)

            self.assertFalse(result.ok)
            self.assertIn("failed OCR", result.error)
            self.assertEqual(markdown_path.read_bytes(), before)

    def test_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            before = markdown_path.read_bytes()
            generation_before = read_current_pointer(index_root)["current_generation"]

            with patch("zotero_pdf_text.image_ocr.probe") as probe, patch(
                "zotero_pdf_text.image_ocr.generate"
            ) as generate:
                result = _run(output_root, index_root, dry_run=True)

            self.assertTrue(result.ok)
            self.assertTrue(result.dry_run)
            self.assertEqual(result.total_refs, 1)
            probe.assert_not_called()
            generate.assert_not_called()
            self.assertEqual(markdown_path.read_bytes(), before)
            self.assertEqual(
                read_current_pointer(index_root)["current_generation"], generation_before
            )

    def test_unreachable_runtime_reports_the_probe_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            before = markdown_path.read_bytes()
            down = RuntimeStatus(False, False, "Ollama is not reachable at http://localhost:11434")

            with patch("zotero_pdf_text.image_ocr.probe", return_value=down), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ):
                result = _run(output_root, index_root)

            self.assertFalse(result.ok)
            self.assertIn("not reachable", result.error)
            self.assertEqual(markdown_path.read_bytes(), before)

    def test_stale_recorded_path_is_rerooted_under_output_root(self):
        """Index records store absolute paths from whichever machine ran the conversion."""
        with tempfile.TemporaryDirectory() as tmp:
            output_root, markdown_path, index_root = _build_fixture(Path(tmp))
            jsonl = current_generation_jsonl(index_root)
            records = [
                json.loads(line)
                for line in jsonl.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            relative = markdown_path.relative_to(output_root)
            records[0]["markdown_path"] = str(Path("D:/OtherMachine/converted_text") / relative)
            jsonl.write_text(
                json.dumps(records[0], ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
            )

            with patch("zotero_pdf_text.image_ocr.probe", return_value=HEALTHY), patch(
                "zotero_pdf_text.image_ocr.classify_crop", return_value=CLASS_FORMULA
            ), patch("zotero_pdf_text.image_ocr.generate", return_value="E = mc^2"):
                result = _run(output_root, index_root, dry_run=True)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.total_refs, 1)
            self.assertEqual(result.missing_pngs, 0)


if __name__ == "__main__":
    unittest.main()
