import sqlite3
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.zotero_db import (
    _citation_key,
    check_pdf_attachment,
    load_attachment_records,
    load_items_without_pdf_attachment,
)


class ZoteroDbTests(unittest.TestCase):
    def test_citation_key_prefers_zotero_field(self):
        self.assertEqual(
            _citation_key({"citationKey": "fieldKey2024", "extra": "Citation Key: extraKey2024"}),
            "fieldKey2024",
        )

    def test_citation_key_falls_back_to_extra(self):
        self.assertEqual(
            _citation_key({"extra": "Other: value\nCitation Key: extraKey2024\nMore: text"}),
            "extraKey2024",
        )

    def test_citation_key_missing_is_empty(self):
        self.assertEqual(_citation_key({"extra": "Other: value"}), "")


class CheckPdfAttachmentTests(unittest.TestCase):
    def _make_db(self, tmp: Path) -> Path:
        db = tmp / "zotero.sqlite"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
            CREATE TABLE itemAttachments (
                itemID INTEGER PRIMARY KEY,
                parentItemID INTEGER,
                linkMode INTEGER,
                contentType TEXT,
                path TEXT
            );
            INSERT INTO items VALUES (1, 'PARENTKEY', 1);
            INSERT INTO items VALUES (2, 'PDFKEY001', 2);
            INSERT INTO items VALUES (3, 'OTHKEY002', 2);
            INSERT INTO itemAttachments VALUES (2, 1, 2, 'application/pdf', 'storage:paper.pdf');
            INSERT INTO itemAttachments VALUES (3, 1, 2, 'text/html', 'storage:page.html');
            """
        )
        con.commit()
        con.close()
        return db

    def test_check_pdf_attachment_finds_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_db(Path(tmp))
            result = check_pdf_attachment("PARENTKEY", db)
            self.assertTrue(result["found"])
            self.assertEqual(len(result["attachments"]), 1)
            self.assertEqual(result["attachments"][0]["key"], "PDFKEY001")

    def test_check_pdf_attachment_no_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_db(Path(tmp))
            # Only HTML attachment, no PDF
            result = check_pdf_attachment("PARENTKEY", db)
            # The HTML entry is filtered out by content_type check
            non_pdf = [a for a in result["attachments"] if "html" in a.get("content_type", "")]
            self.assertEqual(non_pdf, [])

    def test_check_pdf_attachment_unknown_key_returns_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_db(Path(tmp))
            result = check_pdf_attachment("MISSING1", db)
            self.assertFalse(result["found"])
            self.assertEqual(result["attachments"], [])

    def test_check_pdf_attachment_detects_pdf_by_path_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_db(Path(tmp))
            con = sqlite3.connect(db)
            con.execute("INSERT INTO items VALUES (4, 'EXTKEY01', 2)")
            con.execute("INSERT INTO itemAttachments VALUES (4, 1, 2, NULL, 'storage:extra.pdf')")
            con.commit()
            con.close()
            result = check_pdf_attachment("PARENTKEY", db)
            keys = {a["key"] for a in result["attachments"]}
            self.assertIn("EXTKEY01", keys)


class LoadAttachmentRecordsTests(unittest.TestCase):
    def _make_db(self, tmp: Path) -> Path:
        db = tmp / "zotero.sqlite"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
            CREATE TABLE itemAttachments (
                itemID INTEGER PRIMARY KEY,
                parentItemID INTEGER,
                linkMode INTEGER,
                contentType TEXT,
                path TEXT
            );
            CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
            CREATE TABLE itemTypesCombined (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
            CREATE TABLE fieldsCombined (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
            CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
            CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
            CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);

            INSERT INTO itemTypesCombined VALUES (1, 'journalArticle');
            INSERT INTO fieldsCombined VALUES (1, 'title');
            INSERT INTO itemDataValues VALUES (1, 'Active Paper');
            INSERT INTO itemDataValues VALUES (2, 'Trashed Paper');
            INSERT INTO itemDataValues VALUES (3, 'Orphaned-Parent Paper');

            -- Active item + active attachment
            INSERT INTO items VALUES (1, 'PARENT01', 1);
            INSERT INTO items VALUES (2, 'ATTACH01', 2);
            INSERT INTO itemAttachments VALUES (2, 1, 2, 'application/pdf', 'attachments:active.pdf');
            INSERT INTO itemData VALUES (1, 1, 1);

            -- Trashed attachment itself (parent active)
            INSERT INTO items VALUES (3, 'PARENT02', 1);
            INSERT INTO items VALUES (4, 'ATTACH02', 2);
            INSERT INTO itemAttachments VALUES (4, 3, 2, 'application/pdf', 'attachments:trashed_attachment.pdf');
            INSERT INTO itemData VALUES (3, 1, 2);
            INSERT INTO deletedItems VALUES (4);

            -- Active attachment but trashed parent
            INSERT INTO items VALUES (5, 'PARENT03', 1);
            INSERT INTO items VALUES (6, 'ATTACH03', 2);
            INSERT INTO itemAttachments VALUES (6, 5, 2, 'application/pdf', 'attachments:orphaned_parent.pdf');
            INSERT INTO itemData VALUES (5, 1, 3);
            INSERT INTO deletedItems VALUES (5);
            """
        )
        con.commit()
        con.close()
        return db

    def test_excludes_trashed_attachments_and_trashed_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_db(Path(tmp))
            records = load_attachment_records(db)
            keys = {r.attachment_key for r in records}
            self.assertIn("ATTACH01", keys)
            self.assertNotIn("ATTACH02", keys)
            self.assertNotIn("ATTACH03", keys)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].title, "Active Paper")


class LoadItemsWithoutPdfAttachmentTests(unittest.TestCase):
    def _make_db(self, tmp: Path) -> Path:
        db = tmp / "zotero.sqlite"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
            CREATE TABLE itemAttachments (
                itemID INTEGER PRIMARY KEY,
                parentItemID INTEGER,
                linkMode INTEGER,
                contentType TEXT,
                path TEXT
            );
            CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
            CREATE TABLE itemTypesCombined (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
            CREATE TABLE fieldsCombined (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
            CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
            CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
            CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);

            INSERT INTO itemTypesCombined VALUES (1, 'journalArticle');
            INSERT INTO fieldsCombined VALUES (1, 'title');
            INSERT INTO itemDataValues VALUES (1, 'No Attachment At All');
            INSERT INTO itemDataValues VALUES (2, 'Stale Attachment Path');
            INSERT INTO itemDataValues VALUES (3, 'Working Attachment Path');

            -- Item with zero PDF attachment rows at all.
            INSERT INTO items VALUES (1, 'NOATTACH01', 1);
            INSERT INTO itemData VALUES (1, 1, 1);

            -- Item whose only PDF attachment row points at a path that does not exist on disk.
            INSERT INTO items VALUES (2, 'STALE01', 1);
            INSERT INTO items VALUES (3, 'STALEATT01', 2);
            INSERT INTO itemAttachments VALUES (3, 2, 2, 'application/pdf', 'attachments:missing/does_not_exist.pdf');
            INSERT INTO itemData VALUES (2, 1, 2);

            -- Item whose PDF attachment row resolves to a real file -- must stay excluded.
            INSERT INTO items VALUES (4, 'WORKING01', 1);
            INSERT INTO items VALUES (5, 'WORKINGATT01', 2);
            INSERT INTO itemAttachments VALUES (5, 4, 2, 'application/pdf', 'attachments:real.pdf');
            INSERT INTO itemData VALUES (4, 1, 3);
            """
        )
        con.commit()
        con.close()
        return db

    def test_includes_items_with_no_attachment_and_stale_attachment_excludes_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._make_db(root)
            linked_attachments = root / "linked"
            linked_attachments.mkdir()
            (linked_attachments / "real.pdf").write_bytes(b"%PDF-fake")
            # Deliberately do not create missing/does_not_exist.pdf.

            records = load_items_without_pdf_attachment(db, linked_attachments)

            by_key = {record.parent_key: record for record in records}
            self.assertIn("NOATTACH01", by_key)
            self.assertFalse(by_key["NOATTACH01"].had_stale_attachment)
            self.assertIn("STALE01", by_key)
            self.assertTrue(by_key["STALE01"].had_stale_attachment)
            self.assertNotIn("WORKING01", by_key)


if __name__ == "__main__":
    unittest.main()
