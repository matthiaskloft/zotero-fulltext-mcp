import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.mapper import SourceFile, _add_absolute_record_sources, build_mapping_rows
from zotero_pdf_text.zotero_db import AttachmentRecord


class MapperTests(unittest.TestCase):
    def test_orphan_pdf_is_reported_without_zotero_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = SourceFile(
                path=root / "orphan.pdf",
                suffix=".pdf",
                size=1,
                modified="2026-05-29T12:00:00",
                sha256="abc",
            )
            config = ProjectConfig(root, root, root, root)
            rows = build_mapping_rows(config, [], [source], [])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].classification, "orphan_pdf")

    def test_epub_is_reported_as_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = SourceFile(
                path=root / "book.epub",
                suffix=".epub",
                size=1,
                modified="2026-05-29T12:00:00",
                sha256="abc",
            )
            config = ProjectConfig(root, root, root, root)
            rows = build_mapping_rows(config, [], [], [source])
            self.assertEqual(rows[0].classification, "unsupported")
            self.assertEqual(rows[0].identity_rule, "epub_excluded_v1")

    def test_orphan_pdf_can_get_unverified_metadata_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = SourceFile(
                path=root / "Smith - 2024 - A Very Specific Research Title.pdf",
                suffix=".pdf",
                size=1,
                modified="2026-05-29T12:00:00",
                sha256="abc",
            )
            record = AttachmentRecord(
                attachment_item_id=1,
                attachment_key="ATTACH",
                parent_item_id=2,
                parent_key="PARENT",
                link_mode=2,
                content_type="application/pdf",
                zotero_path="attachments:elsewhere.pdf",
                item_type="journalArticle",
                title="A Very Specific Research Title",
                doi="",
                citation_key="smithSpecific2024",
                year="2024",
                venue="",
                creators=["Jane Smith"],
                creator_surnames=["Smith"],
            )
            config = ProjectConfig(root, root, root, root)
            with patch("zotero_pdf_text.mapper.extract_early_text", return_value=("A Very Specific Research Title", 1)):
                rows = build_mapping_rows(config, [record], [source], [])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].classification, "mapped_unverified")
            self.assertEqual(rows[0].mapping_method, "filename_metadata_candidate")
            self.assertEqual(rows[0].identity_status, "candidate")
            self.assertGreaterEqual(rows[0].metadata_match_score, 90)
            self.assertEqual(rows[0].zotero_parent_key, "PARENT")
            self.assertEqual(rows[0].citation_key, "smithSpecific2024")

    def test_manual_accept_promotes_possible_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = SourceFile(
                path=root / "attached.pdf",
                suffix=".pdf",
                size=1,
                modified="2026-05-29T12:00:00",
                sha256="abc",
            )
            record = AttachmentRecord(
                attachment_item_id=1,
                attachment_key="ATTACH",
                parent_item_id=2,
                parent_key="PARENT",
                link_mode=2,
                content_type="application/pdf",
                zotero_path="attachments:attached.pdf",
                item_type="journalArticle",
                title="A Very Specific Research Title",
                doi="10.1000/expected",
                citation_key="smithSpecific2024",
                year="2024",
                venue="",
                creators=["Jane Smith"],
                creator_surnames=["Smith"],
            )
            config = ProjectConfig(
                root,
                root,
                root,
                root,
                manually_accepted_mappings=frozenset({("ATTACH", "attached.pdf")}),
            )
            with patch("zotero_pdf_text.mapper.extract_early_text", return_value=("10.1000/other", 1)):
                rows = build_mapping_rows(config, [record], [source], [])
            self.assertEqual(rows[0].classification, "mapped_verified")
            self.assertEqual(rows[0].identity_status, "manual_accepted")
            self.assertEqual(rows[0].identity_rule, "manual_accept:conflicting_doi_low_title")

    def test_absolute_record_source_outside_linked_root_is_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "outside.pdf"
            source_path.write_bytes(b"%PDF-1.4\n")
            record = AttachmentRecord(
                attachment_item_id=1,
                attachment_key="ATTACH",
                parent_item_id=2,
                parent_key="PARENT",
                link_mode=2,
                content_type="application/pdf",
                zotero_path=str(source_path),
                item_type="journalArticle",
                title="Outside Linked File",
                doi="",
                citation_key="outsideLinked2026",
                year="2026",
                venue="",
                creators=["Jane Smith"],
                creator_surnames=["Smith"],
            )
            sources = _add_absolute_record_sources([], [record], ".pdf")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].path, source_path)
            self.assertEqual(sources[0].suffix, ".pdf")


if __name__ == "__main__":
    unittest.main()
