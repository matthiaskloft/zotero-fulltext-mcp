"""Opt-in stemmed search (#77 C).

The stemmed index is an external-content FTS5 table over chunks_fts, built beside it. Exact search
stays the default and must return exactly what it returned before.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.fts import (
    STEMMED_FTS_TABLE,
    StemmingUnavailableError,
    build_fts_index,
    get_fulltext,
    index_statistics,
    search_fts,
)
from zotero_pdf_text.mcp_contract import create_server

from test_fts import _filter_record
from test_mcp_server import FakeFastMCP, _assert_tool_error, _build_index


def _build(tmp: str) -> Path:
    root = Path(tmp)
    records = [
        _filter_record("S1", title="Modeling growth", text="We estimated two models of reading growth."),
        _filter_record("S2", title="A model", text="The estimation of a single model is described."),
        _filter_record("S3", title="Unrelated", text="Survey responses about libraries."),
        _filter_record("S4", creators="Ann Modell", title="Notes", text="Nothing about the topic."),
    ]
    jsonl = root / "index.jsonl"
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    db = root / "index.sqlite"
    build_fts_index(jsonl, db)
    return db


def _keys(results) -> set[str]:
    return {r.zotero_attachment_key for r in results}


class StemmedSearchTests(unittest.TestCase):
    def test_stemming_matches_inflections_that_exact_search_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(_keys(search_fts(db, "estimation models")), set())
            self.assertEqual(_keys(search_fts(db, "estimation models", stemming=True)), {"S1", "S2"})
            self.assertEqual(_keys(search_fts(db, "model")), {"S2"})
            # S4 is a creator named Modell: Porter stems it to "model", which is why the guidance is
            # to leave stemming off for names.
            self.assertEqual(_keys(search_fts(db, "model", stemming=True)), {"S1", "S2", "S4"})

    def test_exact_search_is_the_default_and_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            for query in ("model", "estimated models", "libraries"):
                with self.subTest(query=query):
                    self.assertEqual(search_fts(db, query), search_fts(db, query, stemming=False))

    def test_stemmed_hits_keep_snippets_locators_and_matched_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            hits = {h.zotero_attachment_key: h for h in search_fts(db, "estimate", stemming=True)}
            self.assertEqual(set(hits), {"S1", "S2"})
            self.assertIn("[estimated]", hits["S1"].snippet)
            self.assertEqual(hits["S1"].matched_fields, ["text"])
            (title_hit,) = [h for h in search_fts(db, "modeling", stemming=True) if h.zotero_attachment_key == "S2"]
            self.assertEqual(title_hit.matched_fields, ["title", "text"])
            chunk = get_fulltext(db, attachment_key="S1", chunk_index=hits["S1"].chunk_index)
            self.assertEqual(chunk.chunk_sha256, hits["S1"].chunk_sha256)

    def test_stemming_combines_with_modes_prefixes_filters_and_one_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(_keys(search_fts(db, "estimate model", search_mode="phrase", stemming=True)), set())
            self.assertEqual(_keys(search_fts(db, "estimate two model", search_mode="phrase", stemming=True)), {"S1"})
            self.assertEqual(_keys(search_fts(db, "model estimate", search_mode="near", stemming=True)), {"S1", "S2"})
            # Filters are stemmed too, names included.
            self.assertEqual(_keys(search_fts(db, None, title="models", stemming=True)), {"S1", "S2"})
            self.assertEqual(_keys(search_fts(db, None, author="model")), set())
            self.assertEqual(_keys(search_fts(db, None, author="model", stemming=True)), {"S4"})
            (hit,) = search_fts(db, "estimate", attachment_key="S2", stemming=True)
            self.assertEqual(hit.matched_fields, ["text"])

    def test_prefix_terms_are_rejected_with_stemming(self):
        # FTS5 stems the prefix itself ("estimat" -> "estimat", indexed stem "estim"), so the
        # combination would silently miss matches that exact prefix search finds.
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(_keys(search_fts(db, "estimat*")), {"S1", "S2"})
            for mode in ("all_terms", "any_terms", "phrase", "near"):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    search_fts(db, "model estimat*", search_mode=mode, stemming=True)
            # A "*" too short to be a prefix is ignored, so it does not block stemming.
            self.assertEqual(_keys(search_fts(db, "models es*", search_mode="any_terms", stemming=True)), {"S1", "S2", "S4"})

    def test_stemming_must_be_a_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            for value in ("yes", 1, None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    search_fts(db, "model", stemming=value)


class LegacyIndexTests(unittest.TestCase):
    """An index built before stemming stays searchable and says how to get stemming."""

    def _drop_stemmed_table(self, db: Path) -> None:
        con = sqlite3.connect(db)
        con.execute(f"DROP TABLE {STEMMED_FTS_TABLE}")
        con.commit()
        con.close()

    def test_exact_search_works_and_stemmed_search_names_the_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertTrue(index_statistics(db)["schema_current"])
            self._drop_stemmed_table(db)
            self.assertEqual(_keys(search_fts(db, "model")), {"S2"})
            with self.assertRaises(StemmingUnavailableError) as raised:
                search_fts(db, "model", stemming=True)
            self.assertIn("rebuild-index", str(raised.exception))
            self.assertFalse(index_statistics(db)["schema_current"])

    def test_mcp_reports_stemming_unavailable_and_validates_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sqlite_path, _ = _build_index(Path(tmp))
            server = create_server(sqlite_path, mcp_factory=FakeFastMCP)
            search = server.tools["search_fulltext"]
            within = server.tools["search_within_fulltext"]
            self.assertTrue(search("instruction sources", stemming=True)["results"])
            self.assertTrue(search("instruction sources")["no_results"])
            self.assertTrue(within("ATTACH1", "sources", stemming=True)["results"])
            _assert_tool_error(self, lambda: search("source", stemming="yes"), "invalid_stemming")
            _assert_tool_error(self, lambda: within("ATTACH1", "source", stemming=1), "invalid_stemming")
            _assert_tool_error(self, lambda: search("sourc*", stemming=True), "invalid_query")
            _assert_tool_error(self, lambda: within("ATTACH1", "sourc*", stemming=True), "invalid_query")

            from zotero_pdf_text.artifacts import resolve_reader_db_path

            self._drop_stemmed_table(resolve_reader_db_path(sqlite_path))
            _assert_tool_error(self, lambda: search("source", stemming=True), "stemming_unavailable")
            self.assertTrue(search("source")["results"])


if __name__ == "__main__":
    unittest.main()
