import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.lock import pipeline_write_lock
from zotero_pdf_text.orphan_discovery import (
    mark_orphan_candidate_resolved,
    run_orphan_discovery,
    score_candidates,
    skip_orphan_candidate,
)
from zotero_pdf_text.zotero_db import ParentCandidateRecord


def _parent(
    parent_key: str = "PARENT1",
    title: str = "A Very Specific Research Title About Bayesian Inference",
    doi: str = "",
    year: str = "2024",
    creator_surnames: list[str] | None = None,
) -> ParentCandidateRecord:
    return ParentCandidateRecord(
        parent_key=parent_key,
        item_type="journalArticle",
        title=title,
        doi=doi,
        citation_key="smithSpecific2024",
        year=year,
        venue="",
        creators=["Jane Smith"],
        creator_surnames=creator_surnames if creator_surnames is not None else ["Smith"],
    )


class ScoreCandidatesTests(unittest.TestCase):
    def test_content_match_found_despite_generic_filename(self):
        # The whole point of this feature: a PDF with a meaningless filename
        # (e.g. "1-s2.0-S0022-main.pdf") can still match on its actual content.
        text = "Jane Smith\n2024\nA Very Specific Research Title About Bayesian Inference\nAbstract..."
        candidates = [
            _parent(),
            _parent(
                parent_key="UNRELATED",
                title="zzz qqq xxx unrelated content here",
                year="1999",
                creator_surnames=["Doe"],
            ),
        ]

        matches = score_candidates(text, candidates)

        self.assertEqual(len(matches), 1)
        candidate, evidence, tier = matches[0]
        self.assertEqual(candidate.parent_key, "PARENT1")
        self.assertEqual(tier, "high")
        self.assertEqual(evidence.status, "verified")

    def test_doi_exact_match_yields_high_confidence(self):
        text = "See doi:10.1000/xyz123 for details."
        candidates = [_parent(doi="10.1000/xyz123", title="Completely Unrelated Words", creator_surnames=[], year="1900")]

        matches = score_candidates(text, candidates)

        self.assertEqual(len(matches), 1)
        _, evidence, tier = matches[0]
        self.assertEqual(evidence.rule, "doi_exact")
        self.assertEqual(tier, "high")

    def test_no_candidate_found_returns_empty_list_not_an_error(self):
        text = "zzz qqq xxx unrelated content here"
        candidates = [_parent(title="A Very Specific Research Title About Bayesian Inference", year="1999", creator_surnames=["Doe"])]

        matches = score_candidates(text, candidates)

        self.assertEqual(matches, [])

    def test_conflicting_doi_is_excluded_not_merely_low_confidence(self):
        # classify_identity only flags a conflicting DOI as a disqualifying possible_mismatch
        # when the title match is itself weak (score < 50) -- a strong title match with a
        # conflicting DOI is a separate, harder case this deterministic engine does not resolve
        # here, and orphan discovery deliberately does not add a second scoring algorithm on top.
        text = "doi:10.1000/other-paper Some totally different words about gardening and cooking recipes for dinner"
        candidates = [_parent(doi="10.1000/xyz123", year="1900", creator_surnames=["Doe"])]

        matches = score_candidates(text, candidates)

        self.assertEqual(matches, [])

    def test_results_are_capped_and_sorted_by_tier_then_score(self):
        text = "Jane Smith 2024 A Very Specific Research Title About Bayesian Inference"
        many_candidates = [_parent(parent_key=f"P{i}", title="A Very Specific Research Title About Bayesian Inference") for i in range(10)]

        matches = score_candidates(text, many_candidates)

        self.assertLessEqual(len(matches), 5)

    def test_generic_chapter_titles_are_excluded_by_default(self):
        # Real-library smoke test regression: an edited volume's individual chapter/section
        # entries ("Citations", "Index", "Preface", "What is mentalizing?") are each their own
        # Zotero item with no PDF of its own, so they land in the no-PDF candidate pool. A short,
        # generic title gets a trivially high fuzzy partial-ratio score against almost any
        # academic PDF's text, but classify_identity itself never verifies these -- status stays
        # "unverified" with rule "insufficient_evidence". They must not be reported by default.
        text = (
            "Jane Smith 2024 A Very Specific Research Title About Bayesian Inference. "
            "References. Bibliography. Author index. Subject index. See also citations above."
        )
        generic_candidates = [
            _parent(parent_key="CH-CITATIONS", title="Citations", year="", creator_surnames=[]),
            _parent(parent_key="CH-INDEX", title="Index", year="", creator_surnames=[]),
            _parent(parent_key="CH-PREFACE", title="Preface", year="", creator_surnames=[]),
            _parent(parent_key="CH-MENTALIZING", title="What is mentalizing?", year="", creator_surnames=[]),
        ]

        matches = score_candidates(text, generic_candidates)

        self.assertEqual(matches, [])

    def test_high_confidence_verified_match_still_reported(self):
        text = "Jane Smith\n2024\nA Very Specific Research Title About Bayesian Inference\nAbstract..."
        candidates = [_parent()]

        matches = score_candidates(text, candidates)

        self.assertEqual(len(matches), 1)
        _, evidence, tier = matches[0]
        self.assertEqual(tier, "high")
        self.assertEqual(evidence.status, "verified")


class RunOrphanDiscoveryTests(unittest.TestCase):
    def _write_mapping_report(self, path: Path, source_path: Path, sha256: str = "orphansha") -> None:
        fieldnames = ["classification", "source_path", "sha256", "safe_folder_id"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(
                {
                    "classification": "orphan_pdf",
                    "source_path": str(source_path),
                    "sha256": sha256,
                    "safe_folder_id": f"sha256_{sha256}",
                }
            )
            writer.writerow(
                {
                    "classification": "mapped_verified",
                    "source_path": str(source_path.parent / "other.pdf"),
                    "sha256": "otherhash",
                    "safe_folder_id": "sha256_otherhash",
                }
            )

    def test_discovery_writes_run_and_master_files_for_a_content_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "1-s2.0-S0022-main.pdf"
            source_path.write_bytes(b"%PDF-fake")
            mapping_report = root / "mapping_report.csv"
            self._write_mapping_report(mapping_report, source_path)

            config = ProjectConfig(root, root, root, root / "output")
            parent = _parent()

            with patch("zotero_pdf_text.orphan_discovery.load_items_without_pdf_attachment", return_value=[parent]), patch(
                "zotero_pdf_text.orphan_discovery.extract_early_text",
                return_value=("Jane Smith 2024 A Very Specific Research Title About Bayesian Inference", 5),
            ):
                run_dir = run_orphan_discovery(config, mapping_report)

            csv_path = run_dir / "orphan_candidates.csv"
            jsonl_path = run_dir / "orphan_candidates.jsonl"
            self.assertTrue(csv_path.exists())
            self.assertTrue(jsonl_path.exists())
            records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["candidate_parent_key"], "PARENT1")
            self.assertEqual(records[0]["confidence_tier"], "high")

            master_path = config.output_root / "index" / "orphan_candidates.jsonl"
            master_records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(master_records), 1)
            self.assertEqual(master_records[0]["status"], "pending")

    def test_discovery_is_fail_open_for_unreadable_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "unreadable.pdf"
            source_path.write_bytes(b"not really a pdf")
            mapping_report = root / "mapping_report.csv"
            self._write_mapping_report(mapping_report, source_path)

            config = ProjectConfig(root, root, root, root / "output")

            with patch("zotero_pdf_text.orphan_discovery.load_items_without_pdf_attachment", return_value=[_parent()]), patch(
                "zotero_pdf_text.orphan_discovery.extract_early_text",
                side_effect=RuntimeError("corrupt pdf"),
            ):
                run_dir = run_orphan_discovery(config, mapping_report)

            jsonl_path = run_dir / "orphan_candidates.jsonl"
            self.assertTrue(jsonl_path.exists())
            self.assertEqual(jsonl_path.read_text(encoding="utf-8"), "")

    def test_discovery_writes_empty_reports_when_no_orphans_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mapping_report = root / "mapping_report.csv"
            fieldnames = ["classification", "source_path", "sha256", "safe_folder_id"]
            with mapping_report.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {"classification": "mapped_verified", "source_path": "x.pdf", "sha256": "h", "safe_folder_id": "sha256_h"}
                )

            config = ProjectConfig(root, root, root, root / "output")
            with patch("zotero_pdf_text.orphan_discovery.load_items_without_pdf_attachment", return_value=[]):
                run_dir = run_orphan_discovery(config, mapping_report)

            self.assertEqual((run_dir / "orphan_candidates.jsonl").read_text(encoding="utf-8"), "")
            self.assertFalse((config.output_root / "index" / "orphan_candidates.jsonl").exists())


class ResolutionHelperTests(unittest.TestCase):
    def test_skip_orphan_candidate_marks_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "1-s2.0-S0022-main.pdf"
            source_path.write_bytes(b"%PDF-fake")
            mapping_report = root / "mapping_report.csv"
            fieldnames = ["classification", "source_path", "sha256", "safe_folder_id"]
            with mapping_report.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {"classification": "orphan_pdf", "source_path": str(source_path), "sha256": "orphansha", "safe_folder_id": "sha256_orphansha"}
                )

            config = ProjectConfig(root, root, root, root / "output")
            with patch("zotero_pdf_text.orphan_discovery.load_items_without_pdf_attachment", return_value=[_parent()]), patch(
                "zotero_pdf_text.orphan_discovery.extract_early_text",
                return_value=("Jane Smith 2024 A Very Specific Research Title About Bayesian Inference", 5),
            ):
                run_orphan_discovery(config, mapping_report)

            result = skip_orphan_candidate(config, "orphansha", "PARENT1", reason="not a match")
            self.assertTrue(result.ok)
            self.assertEqual(result.new_status, "skipped")

    def test_skip_orphan_candidate_raises_key_error_surfaced_as_failed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ProjectConfig(root, root, root, root / "output")
            result = skip_orphan_candidate(config, "missing", "MISSING", reason="x")
            self.assertFalse(result.ok)
            self.assertIn("No orphan candidate found", result.error)

    def test_mark_orphan_candidate_resolved_marks_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "1-s2.0-S0022-main.pdf"
            source_path.write_bytes(b"%PDF-fake")
            mapping_report = root / "mapping_report.csv"
            fieldnames = ["classification", "source_path", "sha256", "safe_folder_id"]
            with mapping_report.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {"classification": "orphan_pdf", "source_path": str(source_path), "sha256": "orphansha", "safe_folder_id": "sha256_orphansha"}
                )

            config = ProjectConfig(root, root, root, root / "output")
            with patch("zotero_pdf_text.orphan_discovery.load_items_without_pdf_attachment", return_value=[_parent()]), patch(
                "zotero_pdf_text.orphan_discovery.extract_early_text",
                return_value=("Jane Smith 2024 A Very Specific Research Title About Bayesian Inference", 5),
            ):
                run_orphan_discovery(config, mapping_report)

            result = mark_orphan_candidate_resolved(config, "orphansha", "PARENT1", note="attached via link-pdf")
            self.assertTrue(result.ok)
            self.assertEqual(result.new_status, "resolved")

    def test_skip_orphan_candidate_respects_pipeline_lock(self):
        # Regression test: skip_orphan_candidate used to read-modify-write orphan_candidates.jsonl
        # without taking the pipeline lock, so a concurrent discovery/resolution command could read
        # the same prior state and then atomically replace one another, silently losing candidates
        # or status changes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            source_path = root / "1-s2.0-S0022-main.pdf"
            source_path.write_bytes(b"%PDF-fake")
            mapping_report = root / "mapping_report.csv"
            fieldnames = ["classification", "source_path", "sha256", "safe_folder_id"]
            with mapping_report.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {"classification": "orphan_pdf", "source_path": str(source_path), "sha256": "orphansha", "safe_folder_id": "sha256_orphansha"}
                )

            config = ProjectConfig(root, root, root, output_root)
            with patch("zotero_pdf_text.orphan_discovery.load_items_without_pdf_attachment", return_value=[_parent()]), patch(
                "zotero_pdf_text.orphan_discovery.extract_early_text",
                return_value=("Jane Smith 2024 A Very Specific Research Title About Bayesian Inference", 5),
            ):
                run_orphan_discovery(config, mapping_report)

            with pipeline_write_lock(output_root, command="convert-new"):
                result = skip_orphan_candidate(config, "orphansha", "PARENT1", reason="not a match")

            self.assertFalse(result.ok)
            self.assertIn("is held by host", result.error)

    def test_mark_orphan_candidate_resolved_respects_pipeline_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            source_path = root / "1-s2.0-S0022-main.pdf"
            source_path.write_bytes(b"%PDF-fake")
            mapping_report = root / "mapping_report.csv"
            fieldnames = ["classification", "source_path", "sha256", "safe_folder_id"]
            with mapping_report.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {"classification": "orphan_pdf", "source_path": str(source_path), "sha256": "orphansha", "safe_folder_id": "sha256_orphansha"}
                )

            config = ProjectConfig(root, root, root, output_root)
            with patch("zotero_pdf_text.orphan_discovery.load_items_without_pdf_attachment", return_value=[_parent()]), patch(
                "zotero_pdf_text.orphan_discovery.extract_early_text",
                return_value=("Jane Smith 2024 A Very Specific Research Title About Bayesian Inference", 5),
            ):
                run_orphan_discovery(config, mapping_report)

            with pipeline_write_lock(output_root, command="convert-new"):
                result = mark_orphan_candidate_resolved(config, "orphansha", "PARENT1", note="attached")

            self.assertFalse(result.ok)
            self.assertIn("is held by host", result.error)


if __name__ == "__main__":
    unittest.main()
