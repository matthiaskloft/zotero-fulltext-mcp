import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.ingestion import (
    ExistingItem,
    ImportCandidate,
    dedupe_candidates,
    load_candidates,
    load_existing_items,
)


class LoadExistingItemsPathTests(unittest.TestCase):
    """`ingest-candidates` reads the user's live database, so its URI must survive the path.

    With the path interpolated, a `#` in the Zotero directory -- a legal directory name --
    ended the URI and left SQLite opening the *truncated* path under its default
    read-write/create mode. The read then failed with `no such table: items`, which names the
    schema and so reads like a Zotero version problem rather than a path problem, and a stray
    file was left behind in the user's folder by an operation that only meant to read.
    """

    def _make_db(self, directory: Path) -> Path:
        db = directory / "zotero.sqlite"
        con = sqlite3.connect(db)
        try:
            con.executescript(
                """
                CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
                CREATE TABLE itemTypesCombined (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
                CREATE TABLE deletedItems (itemID INTEGER);
                CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT);
                CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
                CREATE TABLE fieldsCombined (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
                INSERT INTO itemTypesCombined VALUES (1, 'journalArticle');
                INSERT INTO fieldsCombined VALUES (1, 'title');
                INSERT INTO items VALUES (1, 'AAAA1111', 1);
                INSERT INTO itemDataValues VALUES (1, 'A Paper');
                INSERT INTO itemData VALUES (1, 1, 1);
                """
            )
            con.commit()
        finally:
            con.close()
        return db

    def test_a_fragment_in_the_path_neither_hides_items_nor_creates_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = root / "library#1"
            holder.mkdir()
            db = self._make_db(holder)
            before = {path.name for path in root.rglob("*")}

            items = load_existing_items(db)

            self.assertEqual([item.zotero_parent_key for item in items], ["AAAA1111"])
            self.assertEqual(items[0].title, "A Paper")
            created = {path.name for path in root.rglob("*")} - before
            self.assertEqual(created, set(), f"a read created files: {created}")


class IngestionTests(unittest.TestCase):
    def test_load_candidates_accepts_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.jsonl"
            path.write_text(
                json.dumps({"doi": "10.1000/example", "title": "Example Paper", "year": "2024"}) + "\n",
                encoding="utf-8",
            )

            candidates = load_candidates(path)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].doi, "10.1000/example")
            self.assertEqual(candidates[0].title, "Example Paper")

    def test_dedupe_candidates_marks_existing_and_new_items(self):
        existing = [
            ExistingItem(
                zotero_parent_key="PARENT",
                title="Existing Paper",
                doi="10.1000/existing",
                year="2024",
                url="",
            )
        ]
        candidates = [
            ImportCandidate(doi="https://doi.org/10.1000/existing", title="Existing Paper", year="2024"),
            ImportCandidate(doi="10.1000/new", title="New Paper", year="2025"),
        ]

        decisions = dedupe_candidates(candidates, existing)

        self.assertEqual(decisions[0].action, "skip_existing")
        self.assertEqual(decisions[0].reason, "doi_match")
        self.assertEqual(decisions[0].existing_zotero_parent_key, "PARENT")
        self.assertEqual(decisions[1].action, "add_candidate")


if __name__ == "__main__":
    unittest.main()
