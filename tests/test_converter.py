import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.converter import convert_sample, convert_unverified, convert_verified, default_worker_count


class ConverterTests(unittest.TestCase):
    def test_default_worker_count_leaves_four_cores_available(self):
        with patch("zotero_pdf_text.converter.os.cpu_count", return_value=12):
            self.assertEqual(default_worker_count(), 8)
        with patch("zotero_pdf_text.converter.os.cpu_count", return_value=4):
            self.assertEqual(default_worker_count(), 1)

    def test_convert_sample_writes_markdown_and_manifest_for_verified_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            config = ProjectConfig(root, root, root, root / "output")

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                run_dir = convert_sample(config, report, limit=1)

            manifest = run_dir / "manifest.csv"
            markdown_files = list((run_dir / "markdown").glob("*.md"))
            self.assertEqual(len(markdown_files), 1)
            self.assertTrue(manifest.exists())
            markdown = markdown_files[0].read_text(encoding="utf-8")
            self.assertTrue(markdown_files[0].name.startswith("0001_"))
            self.assertIn('zotero_attachment_key: "ATTACH"', markdown)
            self.assertIn('citation_key: "smithTitle2024"', markdown)
            self.assertIn('extraction_tool: "pymupdf4llm.to_markdown"', markdown)
            self.assertIn("# Extracted", markdown)
            with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "converted")
            self.assertEqual(rows[0]["extraction_tool"], "pymupdf4llm.to_markdown")
            self.assertEqual(rows[0]["zotero_parent_key"], "PARENT")
            self.assertEqual(rows[0]["citation_key"], "smithTitle2024")
            self.assertEqual(rows[0]["output_path"], str(markdown_files[0]))

    def test_convert_sample_passes_image_dir_for_primary_tool_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            config = ProjectConfig(root, root, root, root / "output")

            calls: list[list[str]] = []

            def _capture_and_write(args, **kwargs):
                calls.append(args)
                _write_raw_markdown(args, **kwargs)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_capture_and_write):
                run_dir = convert_sample(config, report, limit=1)

            markdown_files = list((run_dir / "markdown").glob("*.md"))
            expected_images_dir = run_dir / "images" / markdown_files[0].stem
            self.assertEqual(len(calls), 1)
            self.assertIn("--image-dir", calls[0])
            self.assertEqual(calls[0][calls[0].index("--image-dir") + 1], str(expected_images_dir))
            self.assertEqual(calls[0][calls[0].index("--tool") + 1], "pymupdf4llm.to_markdown")

    def test_convert_sample_scales_timeout_for_long_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="500")
            config = ProjectConfig(root, root, root, root / "output")

            calls: list[dict] = []

            def _capture_and_write(args, **kwargs):
                calls.append(kwargs)
                _write_raw_markdown(args, **kwargs)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_capture_and_write):
                convert_sample(config, report, limit=1, timeout_seconds=600)

            self.assertEqual(calls[0]["timeout"], 2000)

    def test_convert_sample_keeps_base_timeout_for_short_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="10")
            config = ProjectConfig(root, root, root, root / "output")

            calls: list[dict] = []

            def _capture_and_write(args, **kwargs):
                calls.append(kwargs)
                _write_raw_markdown(args, **kwargs)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_capture_and_write):
                convert_sample(config, report, limit=1, timeout_seconds=600)

            self.assertEqual(calls[0]["timeout"], 600)

    def test_convert_sample_extends_timeout_for_drawing_dense_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="200")
            config = ProjectConfig(root, root, root, root / "output")

            calls: list[dict] = []

            def _capture_and_write(args, **kwargs):
                calls.append(kwargs)
                _write_raw_markdown(args, **kwargs)

            with (
                patch("zotero_pdf_text.converter.subprocess.run", side_effect=_capture_and_write),
                patch("zotero_pdf_text.converter._sample_drawing_density", return_value=40.0),
            ):
                convert_sample(config, report, limit=1, timeout_seconds=600)

            # density 40 / divisor 10 = +4x, capped at the 5x multiplier: 200 * 4 * 5 = 4000
            self.assertEqual(calls[0]["timeout"], 4000)

    def test_convert_sample_ignores_drawing_density_when_scan_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="500")
            config = ProjectConfig(root, root, root, root / "output")

            calls: list[dict] = []

            def _capture_and_write(args, **kwargs):
                calls.append(kwargs)
                _write_raw_markdown(args, **kwargs)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_capture_and_write):
                convert_sample(config, report, limit=1, timeout_seconds=600)

            # unreadable/fake PDF bytes -> density scan fails closed to 0 -> plain page-count timeout
            self.assertEqual(calls[0]["timeout"], 2000)

    def test_persisted_skip_list_skips_straight_to_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            output_root = root / "output"
            output_root.mkdir(parents=True, exist_ok=True)
            (output_root / "timeout_skip_list.json").write_text(
                json.dumps({"version": 1, "entries": {"ATTACH": {"reason": "test"}}}), encoding="utf-8"
            )
            config = ProjectConfig(root, root, root, output_root)

            calls: list[list[str]] = []

            def _capture_and_write_fallback(args, **kwargs):
                calls.append(args)
                Path(args[4]).write_text("Fallback text", encoding="utf-8")

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_capture_and_write_fallback):
                run_dir = convert_sample(config, report, limit=1)

            markdown_file = next((run_dir / "markdown").glob("*.md"))
            markdown = markdown_file.read_text(encoding="utf-8")
            self.assertIn('extraction_tool: "pymupdf.get_text"', markdown)
            # only the fallback ran -- the primary extractor was never invoked
            self.assertEqual(len(calls), 1)
            self.assertNotIn("--image-dir", calls[0])
            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("primary extractor skipped", rows[0]["error"])

    def test_corrupt_skip_list_file_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            output_root = root / "output"
            output_root.mkdir(parents=True, exist_ok=True)
            (output_root / "timeout_skip_list.json").write_text("{not valid json", encoding="utf-8")
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                run_dir = convert_sample(config, report, limit=1)

            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            # corrupt skip list -> no entries skipped -> primary extractor still ran normally
            self.assertEqual(rows[0]["status"], "converted")
            self.assertEqual(rows[0]["extraction_tool"], "pymupdf4llm.to_markdown")

    def test_timeout_error_reports_scaled_duration_not_base_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="500")
            config = ProjectConfig(root, root, root, root / "output")

            def _raise_timeout(args, **kwargs):
                raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_raise_timeout):
                run_dir = convert_sample(config, report, limit=1, timeout_seconds=600)

            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "error")
            self.assertIn("exceeded 2000 seconds", rows[0]["error"])

    def test_convert_sample_uses_fallback_after_primary_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            config = ProjectConfig(root, root, root, root / "output")

            calls: list[list[str]] = []

            def _capture_then_fallback(args, **kwargs):
                calls.append(args)
                return _fail_primary_then_write_fallback(args, **kwargs)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_capture_then_fallback):
                run_dir = convert_sample(config, report, limit=1)

            markdown_file = next((run_dir / "markdown").glob("*.md"))
            markdown = markdown_file.read_text(encoding="utf-8")
            self.assertIn('extraction_tool: "pymupdf.get_text"', markdown)
            self.assertIn("Fallback text", markdown)
            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "converted")
            self.assertEqual(rows[0]["extraction_tool"], "pymupdf.get_text")
            self.assertEqual(len(calls), 2)
            self.assertIn("--image-dir", calls[0])
            self.assertNotIn("--image-dir", calls[1])
            self.assertIn("Primary extractor failed; fallback used", rows[0]["error"])

    def test_primary_timeout_writes_timeout_candidate_with_fallback_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="10")
            output_root = root / "output"
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_timeout_primary_then_write_fallback):
                run_dir = convert_sample(config, report, limit=1, timeout_seconds=600)

            with (run_dir / "timeout_candidates.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                run_rows = list(csv.DictReader(handle))
            self.assertEqual(len(run_rows), 1)
            self.assertEqual(run_rows[0]["fallback_outcome"], "fallback_used")
            self.assertEqual(run_rows[0]["conversion_status"], "converted")
            self.assertEqual(run_rows[0]["attempted_timeout_seconds"], "600")
            self.assertEqual(run_rows[0]["suggested_next_timeout_seconds"], "1200")

            master_path = output_root / "index" / "timeout_candidates.jsonl"
            master_records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(master_records), 1)
            self.assertEqual(master_records[0]["status"], "pending")
            self.assertEqual(master_records[0]["occurrence_count"], 1)
            self.assertEqual(master_records[0]["zotero_attachment_key"], "ATTACH")

    def test_primary_and_fallback_timeout_writes_timeout_candidate_with_fallback_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="10")
            output_root = root / "output"
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_timeout_primary_and_fallback):
                run_dir = convert_sample(config, report, limit=1, timeout_seconds=600)

            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "error")

            with (run_dir / "timeout_candidates.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                run_rows = list(csv.DictReader(handle))
            self.assertEqual(len(run_rows), 1)
            self.assertEqual(run_rows[0]["fallback_outcome"], "fallback_failed")
            self.assertEqual(run_rows[0]["conversion_status"], "error")

    def test_called_process_error_on_primary_does_not_write_timeout_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="10")
            output_root = root / "output"
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_fail_primary_then_write_fallback):
                run_dir = convert_sample(config, report, limit=1, timeout_seconds=600)

            with (run_dir / "timeout_candidates.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                run_rows = list(csv.DictReader(handle))
            self.assertEqual(run_rows, [])
            self.assertFalse((output_root / "index" / "timeout_candidates.jsonl").exists())

    def test_master_timeout_candidates_jsonl_accumulates_occurrence_count_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, page_count="10")
            output_root = root / "output"
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_timeout_primary_then_write_fallback):
                convert_sample(config, report, limit=1, timeout_seconds=600, output_dir=root / "run1")
                convert_sample(config, report, limit=1, timeout_seconds=600, output_dir=root / "run2")

            master_path = output_root / "index" / "timeout_candidates.jsonl"
            master_records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(master_records), 1)
            self.assertEqual(master_records[0]["occurrence_count"], 2)
            self.assertEqual(master_records[0]["first_detected_at"], master_records[0]["last_detected_at"])

    def test_convert_unverified_targets_unverified_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            config = ProjectConfig(root, root, root, root / "output")

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                run_dir = convert_unverified(config, report, workers=1)

            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["classification"], "mapped_unverified")

    def test_convert_unverified_skips_attachments_already_in_sidecar_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf_indexed = root / "indexed.pdf"
            pdf_new = root / "new.pdf"
            pdf_indexed.write_bytes(b"%PDF")
            pdf_new.write_bytes(b"%PDF")
            _write_unverified_mapping_report(
                report,
                [("ALREADY_INDEXED", pdf_indexed), ("NEW_ATTACH", pdf_new)],
            )
            output_root = root / "output"
            index_jsonl = output_root / "index" / "zotero_text_index.jsonl"
            index_jsonl.parent.mkdir(parents=True, exist_ok=True)
            index_jsonl.write_text(
                json.dumps({"zotero_attachment_key": "ALREADY_INDEXED"}) + "\n", encoding="utf-8"
            )
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                run_dir = convert_unverified(config, report, workers=1)

            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["zotero_attachment_key"], "NEW_ATTACH")

    def test_convert_unverified_accepts_explicit_index_jsonl_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf_indexed = root / "indexed.pdf"
            pdf_new = root / "new.pdf"
            pdf_indexed.write_bytes(b"%PDF")
            pdf_new.write_bytes(b"%PDF")
            _write_unverified_mapping_report(
                report,
                [("ALREADY_INDEXED", pdf_indexed), ("NEW_ATTACH", pdf_new)],
            )
            output_root = root / "output"
            custom_index = root / "custom_index.jsonl"
            custom_index.write_text(
                json.dumps({"zotero_attachment_key": "ALREADY_INDEXED"}) + "\n", encoding="utf-8"
            )
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                run_dir = convert_unverified(config, report, workers=1, index_jsonl=custom_index)

            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["zotero_attachment_key"], "NEW_ATTACH")

    def test_convert_unverified_fails_open_on_missing_or_corrupt_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_unverified_mapping_report(report, [("ATTACH", pdf)])
            output_root = root / "output"
            index_jsonl = output_root / "index" / "zotero_text_index.jsonl"
            index_jsonl.parent.mkdir(parents=True, exist_ok=True)
            index_jsonl.write_text("{not valid json", encoding="utf-8")
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                run_dir = convert_unverified(config, report, workers=1)

            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            # a corrupt index just means nothing is skipped, not a conversion failure
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["zotero_attachment_key"], "ATTACH")

    def test_convert_unverified_fails_open_on_valid_non_object_json_line(self):
        # A syntactically valid JSON line that isn't an object (e.g. "[]" or "null") must not
        # crash load_indexed_keys with AttributeError from calling .get() on a non-dict value --
        # it should be skipped like any other line with no zotero_attachment_key, not treated as a
        # fatal error that aborts the whole run.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_unverified_mapping_report(report, [("ATTACH", pdf)])
            output_root = root / "output"
            index_jsonl = output_root / "index" / "zotero_text_index.jsonl"
            index_jsonl.parent.mkdir(parents=True, exist_ok=True)
            index_jsonl.write_text("[]\nnull\n42\n", encoding="utf-8")
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                run_dir = convert_unverified(config, report, workers=1)

            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["zotero_attachment_key"], "ATTACH")

    def test_has_math_flows_from_sidecar_into_front_matter_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            config = ProjectConfig(root, root, root, root / "output")

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown_with_math):
                run_dir = convert_sample(config, report, limit=1)

            markdown_file = next((run_dir / "markdown").glob("*.md"))
            markdown = markdown_file.read_text(encoding="utf-8")
            self.assertIn("has_math: true", markdown)
            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["has_math"], "true")

    def test_resume_refreshes_front_matter_for_existing_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf, citation_key="newKey2024", title="New Title")
            run_dir = root / "output" / "run"
            markdown_dir = run_dir / "markdown"
            markdown_dir.mkdir(parents=True)
            markdown = markdown_dir / "0001_zotero_PARENT.md"
            markdown.write_text(
                '---\ntitle: "Old Title"\ncitation_key: "oldKey"\nextraction_tool: "pymupdf.get_text"\n---\n\n# Old Body\n',
                encoding="utf-8",
            )
            config = ProjectConfig(root, root, root, root / "output")

            result_dir = convert_verified(config, report, output_dir=run_dir, resume=True, workers=1)

            self.assertEqual(result_dir, run_dir)
            refreshed = markdown.read_text(encoding="utf-8")
            self.assertIn('title: "New Title"', refreshed)
            self.assertIn('citation_key: "newKey2024"', refreshed)
            self.assertIn('extraction_tool: "pymupdf.get_text"', refreshed)
            self.assertIn("# Old Body", refreshed)
            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "skipped_existing")
            self.assertEqual(rows[0]["citation_key"], "newKey2024")

    def test_force_reconverts_existing_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            run_dir = root / "output" / "run"
            markdown_dir = run_dir / "markdown"
            markdown_dir.mkdir(parents=True)
            markdown = markdown_dir / "0001_zotero_PARENT.md"
            markdown.write_text("---\n---\n\nOld body", encoding="utf-8")
            config = ProjectConfig(root, root, root, root / "output")

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                convert_verified(config, report, output_dir=run_dir, resume=True, force=True, workers=1)

            refreshed = markdown.read_text(encoding="utf-8")
            self.assertIn("# Extracted", refreshed)
            self.assertNotIn("Old body", refreshed)
            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "converted")

    def test_force_failure_preserves_existing_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mapping_report.csv"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _write_mapping_report(report, pdf)
            run_dir = root / "output" / "run"
            markdown_dir = run_dir / "markdown"
            markdown_dir.mkdir(parents=True)
            markdown = markdown_dir / "0001_zotero_PARENT.md"
            markdown.write_text("Old body", encoding="utf-8")
            config = ProjectConfig(root, root, root, root / "output")

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=subprocess.CalledProcessError(1, "cmd", stderr="failed")):
                convert_verified(config, report, output_dir=run_dir, resume=True, force=True, workers=1)

            self.assertEqual(markdown.read_text(encoding="utf-8"), "Old body")
            with (run_dir / "manifest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "error")
            self.assertIn("CalledProcessError", rows[0]["error"])


def _write_mapping_report(
    path: Path, pdf: Path, *, citation_key: str = "smithTitle2024", title: str = "Title", page_count: str = "3"
) -> None:
    fieldnames = [
        "classification",
        "source_path",
        "safe_folder_id",
        "zotero_parent_key",
        "zotero_attachment_key",
        "title",
        "creators",
        "year",
        "doi",
        "citation_key",
        "page_count",
        "identity_status",
        "identity_rule",
    ]
    rows = [
        {
            "classification": "mapped_unverified",
            "source_path": str(pdf),
            "safe_folder_id": "skip",
        },
        {
            "classification": "mapped_verified",
            "source_path": str(pdf),
            "safe_folder_id": "zotero_PARENT",
                "zotero_parent_key": "PARENT",
                "zotero_attachment_key": "ATTACH",
                "title": title,
                "creators": "Jane Smith",
                "year": "2024",
                "doi": "10.1000/test",
                "citation_key": citation_key,
                "page_count": page_count,
            "identity_status": "verified",
            "identity_rule": "doi_exact",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_unverified_mapping_report(path: Path, attachments: list[tuple[str, Path]]) -> None:
    fieldnames = [
        "classification",
        "source_path",
        "safe_folder_id",
        "zotero_parent_key",
        "zotero_attachment_key",
        "title",
        "creators",
        "year",
        "doi",
        "citation_key",
        "page_count",
        "identity_status",
        "identity_rule",
    ]
    rows = [
        {
            "classification": "mapped_unverified",
            "source_path": str(pdf),
            "safe_folder_id": f"zotero_{attachment_key}",
            "zotero_parent_key": f"PARENT_{attachment_key}",
            "zotero_attachment_key": attachment_key,
            "title": "Title",
            "creators": "Jane Smith",
            "year": "2024",
            "doi": "10.1000/test",
            "citation_key": f"smith{attachment_key}2024",
            "page_count": "3",
            "identity_status": "unverified",
            "identity_rule": "insufficient_evidence",
        }
        for attachment_key, pdf in attachments
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_raw_markdown(args, **kwargs):
    Path(args[4]).write_text("# Extracted\n\nBody text", encoding="utf-8")


def _write_raw_markdown_with_math(args, **kwargs):
    raw_output_path = Path(args[4])
    raw_output_path.write_text("# Extracted\n\nBody text", encoding="utf-8")
    raw_output_path.with_suffix(".math.json").write_text(
        '{"has_math": true, "font_signals": ["cmmi"], "unicode_math_char_count": 3, '
        '"unicode_math_density": 0.01, "pages_sampled": 2}',
        encoding="utf-8",
    )


def _fail_primary_then_write_fallback(args, **kwargs):
    tool = args[args.index("--tool") + 1]
    if tool == "pymupdf4llm.to_markdown":
        raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr="primary failed")
    Path(args[4]).write_text("Fallback text", encoding="utf-8")


def _timeout_primary_then_write_fallback(args, **kwargs):
    tool = args[args.index("--tool") + 1]
    if tool == "pymupdf4llm.to_markdown":
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
    Path(args[4]).write_text("Fallback text", encoding="utf-8")


def _timeout_primary_and_fallback(args, **kwargs):
    raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))


if __name__ == "__main__":
    unittest.main()
