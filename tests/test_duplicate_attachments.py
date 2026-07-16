import csv
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.duplicate_attachments import (
    TRASH_PLAN_FILENAME,
    build_trash_plan,
    find_byte_identical_duplicates,
    run_duplicate_discovery,
)
from zotero_pdf_text.zotero_write import load_write_plan

FIELDNAMES = [
    "zotero_parent_key",
    "zotero_attachment_key",
    "citation_key",
    "source_name",
    "sha256",
]


def _write_mapping_report(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class FindByteIdenticalDuplicatesTests(unittest.TestCase):
    def test_pair_with_one_unsuffixed_filename_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "KEEP001",
                        "citation_key": "smith2024",
                        "source_name": "Smith - 2024 - A Paper.pdf",
                        "sha256": "abc123",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "DROP001",
                        "citation_key": "smith2024",
                        "source_name": "Smith - 2024 - A Paper2.pdf",
                        "sha256": "abc123",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)

        self.assertEqual(len(result.resolved), 1)
        self.assertEqual(result.ambiguous, [])
        group = result.resolved[0]
        self.assertEqual(group.keep_key, "KEEP001")
        self.assertEqual(len(group.drop_files), 1)
        self.assertEqual(group.drop_files[0].attachment_key, "DROP001")

    def test_space_before_digit_suffix_also_recognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "KEEP001",
                        "citation_key": "jones2019",
                        "source_name": "Jones - 2019 - Some Title.pdf",
                        "sha256": "def456",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "DROP001",
                        "citation_key": "jones2019",
                        "source_name": "Jones - 2019 - Some Title 1.pdf",
                        "sha256": "def456",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)

        self.assertEqual(len(result.resolved), 1)
        self.assertEqual(result.resolved[0].keep_key, "KEEP001")

    def test_parenthesized_suffix_also_recognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "KEEP001",
                        "citation_key": "park2015",
                        "source_name": "Park - 2015 - Some Title.pdf",
                        "sha256": "aaa111",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "DROP001",
                        "citation_key": "park2015",
                        "source_name": "Park - 2015 - Some Title (1).pdf",
                        "sha256": "aaa111",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)

        self.assertEqual(len(result.resolved), 1)
        self.assertEqual(result.resolved[0].keep_key, "KEEP001")
        self.assertEqual(result.resolved[0].drop_files[0].attachment_key, "DROP001")

    def test_title_ending_in_digit_is_not_wrongly_resolved_against_unrelated_file(self):
        # "Phase 2" is part of the actual title here, not a disambiguating suffix -- since the
        # other byte-identical file's name ("Random.pdf") doesn't match "Phase" once a suffix is
        # stripped, the pairing isn't trustworthy and must be left ambiguous rather than guessed.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "A1",
                        "citation_key": "study2021",
                        "source_name": "Study - 2021 - Phase 2.pdf",
                        "sha256": "bbb222",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "A2",
                        "citation_key": "study2021",
                        "source_name": "Random.pdf",
                        "sha256": "bbb222",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)

        self.assertEqual(result.resolved, [])
        self.assertEqual(len(result.ambiguous), 1)

    def test_group_with_no_unsuffixed_filename_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "A1",
                        "citation_key": "doe2020",
                        "source_name": "Doe - 2020 - Title2.pdf",
                        "sha256": "ghi789",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "A2",
                        "citation_key": "doe2020",
                        "source_name": "Doe - 2020 - Title3.pdf",
                        "sha256": "ghi789",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)

        self.assertEqual(result.resolved, [])
        self.assertEqual(len(result.ambiguous), 1)
        self.assertEqual(result.ambiguous[0].reason, "no attachment without a numeric suffix")

    def test_group_with_multiple_unsuffixed_filenames_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "A1",
                        "citation_key": "lee2018",
                        "source_name": "Lee - 2018 - Title.pdf",
                        "sha256": "jkl012",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "A2",
                        "citation_key": "lee2018",
                        "source_name": "Lee - 2018 - Title Other.pdf",
                        "sha256": "jkl012",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)

        self.assertEqual(result.resolved, [])
        self.assertEqual(len(result.ambiguous), 1)
        self.assertEqual(result.ambiguous[0].reason, "multiple attachments without a numeric suffix")

    def test_different_parents_or_hashes_are_not_grouped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "A1",
                        "citation_key": "a2021",
                        "source_name": "A - 2021 - Title.pdf",
                        "sha256": "hash1",
                    },
                    {
                        "zotero_parent_key": "PARENT2",
                        "zotero_attachment_key": "A2",
                        "citation_key": "b2021",
                        "source_name": "B - 2021 - Title.pdf",
                        "sha256": "hash2",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)

        self.assertEqual(result.resolved, [])
        self.assertEqual(result.ambiguous, [])

    def test_rows_without_parent_or_attachment_key_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "",
                        "zotero_attachment_key": "",
                        "citation_key": "",
                        "source_name": "orphan.pdf",
                        "sha256": "hash1",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "A1",
                        "citation_key": "a2021",
                        "source_name": "A - 2021 - Title.pdf",
                        "sha256": "hash1",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)

        self.assertEqual(result.resolved, [])
        self.assertEqual(result.ambiguous, [])


class BuildTrashPlanTests(unittest.TestCase):
    def test_plan_records_are_pending_and_destructive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.csv"
            _write_mapping_report(
                path,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "KEEP001",
                        "citation_key": "smith2024",
                        "source_name": "Smith - 2024 - A Paper.pdf",
                        "sha256": "abc123",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "DROP001",
                        "citation_key": "smith2024",
                        "source_name": "Smith - 2024 - A Paper2.pdf",
                        "sha256": "abc123",
                    },
                ],
            )
            result = find_byte_identical_duplicates(path)
            records = build_trash_plan(result.resolved)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.operation, "trash_item")
        self.assertEqual(record.approval_status, "pending")
        self.assertEqual(record.risk_level, "destructive")
        self.assertEqual(record.target["zotero_attachment_key"], "DROP001")
        self.assertIn("KEEP001", record.dedupe["reason"])


class RunDuplicateDiscoveryTests(unittest.TestCase):
    def test_writes_trash_plan_that_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mapping_report = root / "mapping_report.csv"
            _write_mapping_report(
                mapping_report,
                [
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "KEEP001",
                        "citation_key": "smith2024",
                        "source_name": "Smith - 2024 - A Paper.pdf",
                        "sha256": "abc123",
                    },
                    {
                        "zotero_parent_key": "PARENT1",
                        "zotero_attachment_key": "DROP001",
                        "citation_key": "smith2024",
                        "source_name": "Smith - 2024 - A Paper2.pdf",
                        "sha256": "abc123",
                    },
                ],
            )
            output_dir = root / "output"
            config = ProjectConfig(root, root, root, output_dir)
            run_dir = run_duplicate_discovery(config, mapping_report, output_dir=root / "run")

            plan_path = run_dir / TRASH_PLAN_FILENAME
            self.assertTrue(plan_path.exists())
            records = load_write_plan(plan_path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].target["zotero_attachment_key"], "DROP001")


if __name__ == "__main__":
    unittest.main()
