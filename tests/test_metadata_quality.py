"""Stored Zotero metadata fields and the per-record extraction-quality score (roadmap step S3)."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from pathlib import Path

from zotero_pdf_text.artifacts import (
    GENERATION_DB_FILENAME,
    resolve_generation_dir,
    read_current_pointer,
    stage_and_publish,
    write_jsonl_from_existing,
    write_jsonl_from_manifest_keeping_current,
    write_jsonl_upserting_record,
)
from zotero_pdf_text.cli import main
from zotero_pdf_text.fts import (
    IndexSchemaUnsupportedError,
    build_fts_index,
    get_item_context,
    index_statistics,
    list_extraction_quality,
    lookup_citation_key,
)
from zotero_pdf_text.indexer import TextIndexRecord
from zotero_pdf_text.mcp_contract import MCP_INSTRUCTIONS, serialize_item_context
from zotero_pdf_text.quality import (
    QUALITY_DEGRADED,
    QUALITY_GOOD,
    QUALITY_UNUSABLE,
    detect_language,
    score_extraction,
)
from zotero_pdf_text.zotero_db import SnapshotUnstableError
from zotero_pdf_text.zotero_metadata import load_parent_metadata, with_zotero_metadata

PROSE = (
    "Cultural consensus theory models how a group of informants shares knowledge about a domain. "
    "The estimated competence of each informant is used to weight their answers, and the posterior "
    "consensus answers are compared with the answer key. "
) * 12


def _record(key: str = "ATTACH1", parent: str = "PARENT1", text: str = PROSE, **extra: object) -> dict:
    record: dict = {
        "zotero_parent_key": parent,
        "zotero_attachment_key": key,
        "title": "Cultural consensus theory",
        "creators": "Jane Smith",
        "year": "2024",
        "doi": "10.1000/one",
        "citation_key": f"smith{key}",
        "source_path": "one.pdf",
        "markdown_path": "one.md",
        "markdown_sha256": "abc",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "char_count": len(text),
        "word_count": len(text.split()),
        "page_count": "1",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": False,
        "text": text,
    }
    record.update(extra)
    return record


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


class QualityScoreTests(unittest.TestCase):
    def test_normal_text_is_good(self):
        score, label, signals = score_extraction(PROSE * 3, "2")
        self.assertEqual(label, QUALITY_GOOD)
        self.assertGreaterEqual(score, 0.6)
        self.assertGreater(signals["wordlike_share"], 0.8)
        self.assertEqual(signals["garbage_rate"], 0.0)

    def test_empty_or_scanned_text_is_unusable(self):
        for text in ("", "   \n", "![scan](page1.png)\n\n![scan](page2.png)"):
            score, label, _ = score_extraction(text, "12")
            self.assertEqual(label, QUALITY_UNUSABLE, text)
            self.assertEqual(score, 0.0)

    def test_sparse_text_over_many_pages_is_not_good(self):
        _, label, signals = score_extraction(PROSE[:600], "20")
        self.assertEqual(label, QUALITY_UNUSABLE)
        self.assertLess(signals["chars_per_page"], 100)

    def test_garbled_text_is_not_good(self):
        garbled = ("�� x§  q# 4$ �z ") * 200
        _, label, signals = score_extraction(garbled, "1")
        self.assertIn(label, (QUALITY_DEGRADED, QUALITY_UNUSABLE))
        self.assertGreater(signals["garbage_rate"], 0.1)

    def test_equation_heavy_text_stays_good(self):
        text = (PROSE + "$$ x_i = \\sum_j w_{ij} \\theta_j $$ " * 20) * 2
        self.assertEqual(score_extraction(text, "2")[1], QUALITY_GOOD)

    def test_missing_page_count_falls_back_to_total_length(self):
        _, label, signals = score_extraction(PROSE * 3, "")
        self.assertEqual(label, QUALITY_GOOD)
        self.assertEqual(signals["pages"], 0.0)

    def test_language_heuristic(self):
        self.assertEqual(detect_language(PROSE + " the of and to in is that for with"), "en")
        german = "Die Ergebnisse der Studie sind nicht eindeutig und es ist ein weiterer Test mit den Daten nötig. " * 10
        self.assertEqual(detect_language(german), "de")
        self.assertEqual(detect_language("xyz qrs"), "")
        self.assertEqual(detect_language(""), "")


class StoredFieldsTests(unittest.TestCase):
    def _build(self, tmp: str, records: list[dict]) -> Path:
        jsonl, db = Path(tmp) / "i.jsonl", Path(tmp) / "i.sqlite"
        _write_jsonl(jsonl, records)
        build_fts_index(jsonl, db)
        return db

    def test_context_returns_stored_fields_and_quality(self):
        extra = {
            "item_type": "journalArticle",
            "abstract": "We study consensus.",
            "venue": "Journal of Tests",
            "volume": "7",
            "issue": "2",
            "pages": "10-20",
            "date": "2024-03-01",
            "language": "en",
            "tags": [{"name": "consensus", "type": "manual"}],
            "creators_structured": [
                {"role": "author", "first": "Jane", "last": "Smith", "order": 0},
                {"role": "editor", "first": "Ed", "last": "Itor", "order": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = self._build(tmp, [_record(**extra), _record("ATTACH2", "PARENT2")])
            full = get_item_context(db, attachment_key="ATTACH1")["records"][0]
            bare = get_item_context(db, attachment_key="ATTACH2")["records"][0]
        served = serialize_item_context({"records": [full]})["records"][0]
        self.assertEqual(served["item_type"], "journalArticle")
        self.assertEqual(served["venue"], "Journal of Tests")
        self.assertEqual([c["role"] for c in served["creators_structured"]], ["author", "editor"])
        self.assertEqual(served["tags"], [{"name": "consensus", "type": "manual"}])
        self.assertEqual(served["extraction_quality"]["label"], "good")
        self.assertEqual(served["detected_language"], "en")
        # A record without the fields stores them empty, never guessed.
        empty = serialize_item_context({"records": [bare]})["records"][0]
        self.assertEqual((empty["abstract"], empty["venue"], empty["tags"], empty["creators_structured"]), ("", "", [], []))

    def test_response_caps_untrusted_content(self):
        extra = {
            "abstract": "a" * 5000,
            "tags": [{"name": f"t{i}", "type": "manual"} for i in range(100)],
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = self._build(tmp, [_record(**extra)])
            record = get_item_context(db, attachment_key="ATTACH1")["records"][0]
            looked_up = lookup_citation_key(db, "smithATTACH1")["records"][0]
        self.assertEqual(len(record["abstract"]), 2000)
        self.assertEqual(len(record["tags"]), 30)
        self.assertEqual(looked_up["extraction_quality"]["label"], "good")

    def test_statistics_and_worklist(self):
        records = [
            _record("GOOD1"),
            _record("EMPTY1", "P2", text="", page_count="9"),
            _record("EMPTY2", "P3", text="![x](a.png)", page_count="3"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db = self._build(tmp, records)
            stats = index_statistics(db)
            worklist = list_extraction_quality(db)
            only_good = list_extraction_quality(db, labels=("good",))
            limited = list_extraction_quality(db, limit=1)
        self.assertEqual(stats["by_extraction_quality"], {"good": 1, "unusable": 2})
        self.assertTrue(stats["schema_current"])
        # Columns exist but nothing was ever refreshed from Zotero.
        self.assertEqual(stats["zotero_metadata_populated"], 0)
        self.assertEqual({r["attachment_key"] for r in worklist}, {"EMPTY1", "EMPTY2"})
        self.assertEqual([r["attachment_key"] for r in only_good], ["GOOD1"])
        self.assertEqual(len(limited), 1)
        # Path-free: no source or Markdown path in the worklist.
        self.assertNotIn("source_path", worklist[0])
        self.assertNotIn("markdown_path", worklist[0])

    def test_instructions_mention_degraded_text(self):
        self.assertIn("degraded", MCP_INSTRUCTIONS)
        self.assertLessEqual(len(MCP_INSTRUCTIONS), 2048)


class LegacyIndexTests(unittest.TestCase):
    """An index built before these columns existed stays readable and says how to upgrade."""

    def _legacy_db(self, tmp: str) -> Path:
        db = Path(tmp) / "legacy.sqlite"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE metadata (
                record_id INTEGER PRIMARY KEY, zotero_parent_key TEXT, zotero_attachment_key TEXT,
                title TEXT, creators TEXT, year TEXT, doi TEXT, citation_key TEXT, source_path TEXT,
                markdown_path TEXT, markdown_sha256 TEXT, extraction_tool TEXT, char_count INTEGER,
                word_count INTEGER, page_count TEXT, classification TEXT, identity_status TEXT,
                identity_rule TEXT, has_math INTEGER, source_sha256 TEXT, indexed_at TEXT);
            CREATE TABLE chunks (chunk_id INTEGER PRIMARY KEY, record_id INTEGER, chunk_index INTEGER,
                start_char INTEGER, end_char INTEGER, text TEXT);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(title, creators, text, citation_key,
                record_id UNINDEXED, chunk_id UNINDEXED);
            INSERT INTO metadata VALUES (1, 'P1', 'A1', 'Old paper', 'Jane Smith', '2020', '', 'old20',
                's.pdf', 'm.md', 'h', 'pymupdf4llm.to_markdown', 10, 2, '1', 'mapped_verified',
                'verified', 'doi_exact', 0, '', '');
            """
        )
        con.commit()
        con.close()
        return db

    def test_context_and_statistics_degrade_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._legacy_db(tmp)
            record = get_item_context(db, attachment_key="A1")["records"][0]
            served = serialize_item_context({"records": [record]})["records"][0]
            stats = index_statistics(db)
        self.assertEqual(served["title"], "Old paper")
        self.assertEqual(served["abstract"], "")
        self.assertEqual(served["extraction_quality"], {"label": "", "score": None, "signals": {}})
        self.assertFalse(stats["schema_current"])
        self.assertEqual(stats["zotero_metadata_populated"], 0)
        self.assertEqual(stats["by_extraction_quality"], {"unscored": 1})

    def test_worklist_refuses_an_unscored_index_with_the_recovery_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._legacy_db(tmp)
            with self.assertRaises(IndexSchemaUnsupportedError) as caught:
                list_extraction_quality(db)
        self.assertIn("rebuild-index", str(caught.exception))


def _zotero_fixture(tmp: str) -> Path:
    db = Path(tmp) / "zotero.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE itemTypesCombined (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE fieldsCombined (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER, type INTEGER);

        INSERT INTO itemTypesCombined VALUES (1, 'journalArticle'), (2, 'bookSection'), (3, 'attachment'),
            (4, 'conferencePaper');
        INSERT INTO fieldsCombined VALUES (1, 'title'), (2, 'abstractNote'), (3, 'publicationTitle'),
            (4, 'bookTitle'), (5, 'volume'), (6, 'date'), (7, 'publisher'), (8, 'ISSN'), (9, 'proceedingsTitle');
        INSERT INTO creatorTypes VALUES (1, 'author'), (2, 'editor');
        INSERT INTO creators VALUES (1, 'Jane', 'Smith'), (2, 'Ed', 'Itor');
        INSERT INTO tags VALUES (1, 'consensus'), (2, '_auto');

        -- Journal article with every role and both tag types
        INSERT INTO items VALUES (1, 'ARTICLE1', 1);
        INSERT INTO itemDataValues VALUES (1, 'Paper'), (2, 'An abstract.'), (3, 'J. Tests'), (4, '7'),
            (5, '2024-03-01'), (6, '1234-5678');
        INSERT INTO itemData VALUES (1,1,1), (1,2,2), (1,3,3), (1,5,4), (1,6,5), (1,8,6);
        INSERT INTO itemCreators VALUES (1,1,1,0), (1,2,2,1);
        INSERT INTO itemTags VALUES (1,1,0), (1,2,1);

        -- Book section: venue falls back to bookTitle; no abstract, no tags, no creators
        INSERT INTO items VALUES (2, 'CHAPTER1', 2);
        INSERT INTO itemDataValues VALUES (7, 'Chapter'), (8, 'The Book'), (9, 'A Press');
        INSERT INTO itemData VALUES (2,1,7), (2,4,8), (2,7,9);

        -- Conference paper: venue from proceedingsTitle
        INSERT INTO items VALUES (3, 'CONF0001', 4);
        INSERT INTO itemDataValues VALUES (10, 'Proceedings of X');
        INSERT INTO itemData VALUES (3,9,10);

        -- Attachment and a trashed item are not parents
        INSERT INTO items VALUES (4, 'ATTACH01', 3);
        INSERT INTO items VALUES (5, 'TRASHED1', 1);
        INSERT INTO deletedItems VALUES (5);
        """
    )
    con.commit()
    con.close()
    return db


class ZoteroMetadataTests(unittest.TestCase):
    def test_loads_fields_roles_tags_and_leaves_missing_ones_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _zotero_fixture(tmp)
            before = db.read_bytes()
            loaded = load_parent_metadata(db)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["zotero.sqlite"])
        self.assertEqual(set(loaded), {"ARTICLE1", "CHAPTER1", "CONF0001"})
        article = loaded["ARTICLE1"]
        self.assertEqual(article["item_type"], "journalArticle")
        self.assertEqual(article["venue"], "J. Tests")
        self.assertEqual(article["abstract"], "An abstract.")
        self.assertEqual((article["volume"], article["date"], article["issn"]), ("7", "2024-03-01", "1234-5678"))
        self.assertEqual([c["role"] for c in article["creators_structured"]], ["author", "editor"])
        self.assertEqual(article["creators_structured"][1]["last"], "Itor")
        self.assertEqual(
            sorted((t["name"], t["type"]) for t in article["tags"]),
            [("_auto", "automatic"), ("consensus", "manual")],
        )
        chapter = loaded["CHAPTER1"]
        self.assertEqual((chapter["item_type"], chapter["venue"], chapter["publisher"]), ("bookSection", "The Book", "A Press"))
        self.assertEqual((chapter["abstract"], chapter["tags"], chapter["creators_structured"]), ("", [], []))
        self.assertEqual(loaded["CONF0001"]["venue"], "Proceedings of X")

    def test_type_specific_fields_resolve_to_publisher_and_venue(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _zotero_fixture(tmp)
            con = sqlite3.connect(db)
            con.executescript(
                """
                INSERT INTO itemTypesCombined VALUES (5, 'thesis'), (6, 'webpage');
                INSERT INTO fieldsCombined VALUES (10, 'university'), (11, 'websiteTitle');
                INSERT INTO itemDataValues VALUES (20, 'State University'), (21, 'A Website');
                INSERT INTO items VALUES (6, 'THESIS01', 5), (7, 'WEBPAGE1', 6);
                INSERT INTO itemData VALUES (6,10,20), (7,11,21);
                """
            )
            con.commit()
            con.close()
            loaded = load_parent_metadata(db)
        self.assertEqual(loaded["THESIS01"]["publisher"], "State University")
        self.assertEqual(loaded["THESIS01"]["venue"], "State University")
        self.assertEqual(loaded["WEBPAGE1"]["venue"], "A Website")

    def test_writer_wrapper_merges_by_parent_and_keeps_unmatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            zotero = _zotero_fixture(tmp)
            source = Path(tmp) / "source.jsonl"
            _write_jsonl(
                source,
                [
                    _record("A1", "ARTICLE1"),
                    _record("A2", "GONE0001", abstract="kept", tags=[{"name": "old", "type": "manual"}]),
                ],
            )
            writer, stats = with_zotero_metadata(write_jsonl_from_existing(source), zotero)
            staged = Path(tmp) / "staged.jsonl"
            writer(staged)
            rows = [json.loads(line) for line in staged.read_text(encoding="utf-8").splitlines()]
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".enriched")]
        self.assertEqual((stats.matched, stats.unmatched), (1, 1))
        self.assertEqual(rows[0]["venue"], "J. Tests")
        self.assertNotIn("venue", rows[1])
        self.assertEqual(rows[1]["abstract"], "kept")
        self.assertEqual(leftovers, [])


class KeepCurrentCarryTests(unittest.TestCase):
    def _manifest(self, root: Path, rows: list[tuple[str, str]]) -> Path:
        manifest = root / "manifest.csv"
        lines = ["status,output_path,zotero_attachment_key,zotero_parent_key,title,extraction_tool"]
        for key, parent in rows:
            md = root / f"{key}.md"
            md.write_text(PROSE, encoding="utf-8")
            lines.append(f"converted,{md},{key},{parent},New title,pymupdf4llm.to_markdown")
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return manifest

    def test_replaced_record_keeps_fields_only_for_the_same_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current.jsonl"
            fields = {"abstract": "Kept.", "venue": "J. Tests", "tags": [{"name": "x", "type": "manual"}]}
            _write_jsonl(
                current,
                [_record("A1", "P1", **fields), _record("A2", "P2", **fields)],
            )
            # A2's attachment now belongs to a different parent item.
            manifest = self._manifest(root, [("A1", "P1"), ("A2", "P9")])
            writer, _ = write_jsonl_from_manifest_keeping_current(manifest, current)
            out = root / "out.jsonl"
            writer(out)
            parsed = map(json.loads, out.read_text(encoding="utf-8").splitlines())
            rows = {r["zotero_attachment_key"]: r for r in parsed}
        self.assertEqual(rows["A1"]["title"], "New title")
        self.assertEqual((rows["A1"]["abstract"], rows["A1"]["venue"]), ("Kept.", "J. Tests"))
        self.assertNotIn("abstract", rows["A2"])
        self.assertNotIn("tags", rows["A2"])


class ManagedPublicationTests(unittest.TestCase):
    def _config(self, root: Path, zotero: Path) -> Path:
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "zotero_root": str(root),
                    "zotero_data_directory": str(zotero.parent),
                    "linked_attachments": str(root),
                    "output_root": str(root / "out"),
                }
            ),
            encoding="utf-8",
        )
        return config

    def _context(self, output_root: Path, key: str) -> dict:
        index_root = output_root / "index"
        pointer = read_current_pointer(index_root)
        generation = resolve_generation_dir(index_root, str(pointer["current_generation"]))
        return get_item_context(generation / GENERATION_DB_FILENAME, attachment_key=key)["records"][0]

    def test_refresh_failures_exit_2_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zotero = _zotero_fixture(tmp)
            config = self._config(root, zotero)
            index_root = root / "out" / "index"
            index_root.mkdir(parents=True)
            seed = root / "seed.jsonl"
            _write_jsonl(seed, [_record("A1", "ARTICLE1")])
            stage_and_publish(index_root, write_jsonl_from_existing(seed), command="rebuild-index")
            argv = ["rebuild-index", "--config", str(config), "--refresh-zotero-metadata"]

            err = io.StringIO()
            with patch(
                "zotero_pdf_text.zotero_metadata.snapshot_for_reading",
                side_effect=SnapshotUnstableError("changed while copying"),
            ), redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 2)
            self.assertIn("changed while copying", err.getvalue())

            zotero.write_bytes(b"not a sqlite database" * 100)
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 2)
            self.assertNotEqual(err.getvalue(), "")

            broken = root / "broken.json"
            broken.write_text("{}", encoding="utf-8")
            manifest = root / "none.csv"
            manifest.write_text("status\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(
                    ["update-index", "--config", str(broken), "--refresh-zotero-metadata",
                     "--manifest", str(manifest)]
                )
            self.assertEqual(code, 2)

    def test_refresh_flag_rebuilds_with_fields_and_default_rebuild_preserves_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zotero = _zotero_fixture(tmp)
            config = self._config(root, zotero)
            output_root = root / "out"
            index_root = output_root / "index"
            index_root.mkdir(parents=True)
            seed = root / "seed.jsonl"
            _write_jsonl(seed, [_record("A1", "ARTICLE1"), _record("A2", "NOTLISTED")])
            stage_and_publish(index_root, write_jsonl_from_existing(seed), command="rebuild-index")
            self.assertEqual(self._context(output_root, "A1")["venue"], "")

            out, err = io.StringIO(), io.StringIO()
            argv = ["rebuild-index", "--config", str(config), "--refresh-zotero-metadata"]
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(argv), 0, err.getvalue())
            self.assertEqual(json.loads(out.getvalue())["zotero_metadata"], {"matched": 1, "unmatched": 1})
            refreshed = self._context(output_root, "A1")
            self.assertEqual(refreshed["venue"], "J. Tests")
            self.assertEqual(refreshed["item_type"], "journalArticle")
            index_db = (
                index_root
                / "generations"
                / str(read_current_pointer(index_root)["current_generation"])
                / GENERATION_DB_FILENAME
            )
            self.assertEqual(index_statistics(index_db)["zotero_metadata_populated"], 1)
            self.assertEqual(self._context(output_root, "A2")["venue"], "")

            # A plain rebuild (no Zotero access) copies the JSONL, so the stored fields survive.
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(main(["rebuild-index", "--config", str(config)]), 0, err.getvalue())
            self.assertEqual(self._context(output_root, "A1")["venue"], "J. Tests")

            # A reconversion-style replacement of one record keeps its Zotero fields.
            replacement = TextIndexRecord(
                **{k: v for k, v in _record("A1", "ARTICLE1").items() if k in TextIndexRecord.__dataclass_fields__}
            )
            current = index_root / "generations" / str(read_current_pointer(index_root)["current_generation"])
            stage_and_publish(
                index_root,
                write_jsonl_upserting_record(current / "index.jsonl", "A1", replacement),
                command="reconvert-math",
            )
            self.assertEqual(self._context(output_root, "A1")["abstract"], "An abstract.")

            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["list-degraded-records", "--db", str(index_root / "x.sqlite"), "--json"]), 0
                )
            self.assertEqual(json.loads(out.getvalue()), {"records": []})


if __name__ == "__main__":
    unittest.main()
