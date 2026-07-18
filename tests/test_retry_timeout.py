import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.artifacts import (
    current_generation_jsonl,
    resolve_reader_db_path,
    stage_and_publish,
    write_jsonl_from_existing,
)
from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.retry_timeout import (
    MAX_RETRY_TIMEOUT_SECONDS,
    retry_timeout_candidate,
    skip_timeout_candidate,
)
from zotero_pdf_text.timeout_candidates import TimeoutCandidate, append_master_candidates, find_candidate


def _publish_generation(output_root: Path, records: list[dict]) -> None:
    """Publish a managed generation holding the given records (possibly none)."""
    index_root = output_root / "index"
    source = index_root / "seed.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    stage_and_publish(index_root, write_jsonl_from_existing(source), command="test")
    source.unlink()


def _seed_candidate(output_root: Path, source_path: Path, *, attempted_timeout_seconds: int = 600) -> None:
    candidate = TimeoutCandidate(
        zotero_parent_key="PARENT",
        zotero_attachment_key="ATTACH",
        item_type="attachment",
        title="Title",
        creators="Jane Smith",
        year="2024",
        doi="10.1000/test",
        citation_key="smithTitle2024",
        source_path=str(source_path),
        page_count="10",
        classification="mapped_verified",
        identity_status="verified",
        identity_rule="doi_exact",
        safe_folder_id="zotero_ATTACH",
        drawing_density=1.0,
        attempted_timeout_seconds=attempted_timeout_seconds,
        suggested_next_timeout_seconds=attempted_timeout_seconds * 2,
        fallback_outcome="fallback_used",
        conversion_status="converted",
        detected_at=datetime.now().isoformat(timespec="seconds"),
    )
    append_master_candidates(output_root / "index" / "timeout_candidates.jsonl", [candidate])


def _write_raw_markdown(args, **kwargs):
    Path(args[4]).write_text("# Extracted\n\nBody text", encoding="utf-8")


class SkipTimeoutCandidateTests(unittest.TestCase):
    def test_skip_writes_skip_list_and_marks_candidate_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)

            result = skip_timeout_candidate("ATTACH", config=config, reason="too slow")

            self.assertTrue(result.ok)
            self.assertEqual(result.new_status, "skipped")
            skip_list = json.loads((output_root / "timeout_skip_list.json").read_text(encoding="utf-8"))
            self.assertEqual(skip_list["entries"]["ATTACH"]["reason"], "too slow")
            candidate = find_candidate(output_root / "index" / "timeout_candidates.jsonl", "ATTACH")
            self.assertEqual(candidate["status"], "skipped")

    def test_skip_unknown_key_returns_error_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            config = ProjectConfig(root, root, root, output_root)

            result = skip_timeout_candidate("MISSING", config=config, reason="too slow")

            self.assertFalse(result.ok)
            self.assertIn("No timeout candidate found", result.error)

    def test_skip_rejects_invalid_attachment_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            config = ProjectConfig(root, root, root, output_root)

            result = skip_timeout_candidate("../escape", config=config, reason="too slow")

            self.assertFalse(result.ok)
            self.assertIn("letters and digits", result.error)


class RetryTimeoutCandidateTests(unittest.TestCase):
    def test_retry_success_appends_when_not_previously_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)
            _publish_generation(output_root, [])

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                result = retry_timeout_candidate("ATTACH", config=config)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.new_status, "resolved")
            self.assertEqual(result.extraction_tool, "pymupdf4llm.to_markdown")
            jsonl_path = current_generation_jsonl(output_root / "index")
            records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["zotero_attachment_key"], "ATTACH")
            self.assertTrue(resolve_reader_db_path(output_root / "index" / "zotero_text_index.sqlite").exists())
            candidate = find_candidate(output_root / "index" / "timeout_candidates.jsonl", "ATTACH")
            self.assertEqual(candidate["status"], "resolved")
            self.assertEqual(candidate["resolved_via"], "retry")

    def test_retry_success_replaces_existing_index_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)
            _publish_generation(
                output_root,
                [
                    {
                        "zotero_parent_key": "PARENT",
                        "zotero_attachment_key": "ATTACH",
                        "title": "Title",
                        "creators": "Jane Smith",
                        "year": "2024",
                        "doi": "10.1000/test",
                        "citation_key": "smithTitle2024",
                        "source_path": str(pdf),
                        "markdown_path": "old.md",
                        "markdown_sha256": "old",
                        "extraction_tool": "pymupdf.get_text",
                        "char_count": 5,
                        "word_count": 1,
                        "page_count": "10",
                        "classification": "mapped_verified",
                        "identity_status": "verified",
                        "identity_rule": "doi_exact",
                        "has_math": False,
                        "text": "Old",
                    }
                ],
            )

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                result = retry_timeout_candidate("ATTACH", config=config)

            self.assertTrue(result.ok, result.error)
            jsonl_path = current_generation_jsonl(output_root / "index")
            records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(records), 1)  # replaced, not duplicated
            self.assertEqual(records[0]["extraction_tool"], "pymupdf4llm.to_markdown")

    def test_retry_failure_leaves_index_and_candidate_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)
            _publish_generation(output_root, [])
            jsonl_before = current_generation_jsonl(output_root / "index")

            def _raise_timeout(args, **kwargs):
                raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_raise_timeout):
                result = retry_timeout_candidate("ATTACH", config=config)

            self.assertFalse(result.ok)
            # No successor generation was published.
            self.assertEqual(current_generation_jsonl(output_root / "index"), jsonl_before)
            candidate = find_candidate(output_root / "index" / "timeout_candidates.jsonl", "ATTACH")
            self.assertEqual(candidate["status"], "pending")
            self.assertEqual(candidate["occurrence_count"], 2)  # bumped by the nested convert_verified call

    def test_retry_unknown_key_returns_error_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            config = ProjectConfig(root, root, root, output_root)

            result = retry_timeout_candidate("MISSING", config=config)

            self.assertFalse(result.ok)
            self.assertIn("No timeout candidate found", result.error)

    def test_explicit_timeout_seconds_above_hard_cap_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)

            result = retry_timeout_candidate(
                "ATTACH",
                config=config,
                timeout_seconds=MAX_RETRY_TIMEOUT_SECONDS + 1,
            )

            self.assertFalse(result.ok)
            self.assertIn(str(MAX_RETRY_TIMEOUT_SECONDS), result.error)

    def test_explicit_timeout_seconds_below_one_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)

            result = retry_timeout_candidate(
                "ATTACH",
                config=config,
                timeout_seconds=0,
            )

            self.assertFalse(result.ok)
            self.assertIn("between 1 and", result.error)

    def test_zero_multiplier_is_rejected_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf, attempted_timeout_seconds=1000)
            config = ProjectConfig(root, root, root, output_root)

            result = retry_timeout_candidate(
                "ATTACH",
                config=config,
                multiplier=0,
            )

            self.assertFalse(result.ok)
            self.assertIn("multiplier must be a positive number", result.error)

    def test_negative_multiplier_is_rejected_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf, attempted_timeout_seconds=1000)
            config = ProjectConfig(root, root, root, output_root)

            result = retry_timeout_candidate(
                "ATTACH",
                config=config,
                multiplier=-2.0,
            )

            self.assertFalse(result.ok)
            self.assertIn("multiplier must be a positive number", result.error)

    def test_tiny_multiplier_floors_to_one_second_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf, attempted_timeout_seconds=10)
            config = ProjectConfig(root, root, root, output_root)
            _publish_generation(output_root, [])

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                result = retry_timeout_candidate(
                    "ATTACH",
                    config=config,
                    multiplier=0.001,
                )

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.timeout_seconds_used, 1)

    def test_invalid_attachment_key_is_rejected_not_interpolated_into_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            config = ProjectConfig(root, root, root, output_root)

            result = retry_timeout_candidate(
                "../escape",
                config=config,
            )

            self.assertFalse(result.ok)
            self.assertIn("letters and digits", result.error)

    def test_unexpected_exception_during_retry_returns_structured_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.retry_timeout.convert_verified", side_effect=RuntimeError("boom")):
                result = retry_timeout_candidate(
                    "ATTACH",
                    config=config,
                )

            self.assertFalse(result.ok)
            self.assertIn("boom", result.error)

    def test_timeout_seconds_and_multiplier_together_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)

            result = retry_timeout_candidate(
                "ATTACH",
                config=config,
                timeout_seconds=1000,
                multiplier=2.0,
            )

            self.assertFalse(result.ok)
            self.assertIn("at most one", result.error)

    def test_unmigrated_legacy_layout_returns_error_after_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf)
            config = ProjectConfig(root, root, root, output_root)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_write_raw_markdown):
                result = retry_timeout_candidate("ATTACH", config=config)

            self.assertFalse(result.ok)
            self.assertIn("rebuild-index", result.error)
            candidate = find_candidate(output_root / "index" / "timeout_candidates.jsonl", "ATTACH")
            self.assertEqual(candidate["status"], "pending")

    def test_multiplier_scales_the_last_attempted_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            _seed_candidate(output_root, pdf, attempted_timeout_seconds=1000)
            config = ProjectConfig(root, root, root, output_root)
            _publish_generation(output_root, [])

            calls = []

            def _capture_and_write(args, **kwargs):
                calls.append(kwargs.get("timeout"))
                _write_raw_markdown(args, **kwargs)

            with patch("zotero_pdf_text.converter.subprocess.run", side_effect=_capture_and_write):
                result = retry_timeout_candidate(
                    "ATTACH",
                    config=config,
                    multiplier=3.0,
                )

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.timeout_seconds_used, 3000)


if __name__ == "__main__":
    unittest.main()
