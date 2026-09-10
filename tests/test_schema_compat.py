"""Schema-compatibility tests for readers pointed at an index they cannot use.

The contract is narrow and worth stating exactly: an index that is not the current schema either
migrates through a documented command, or fails with an instruction that names the fix. What must
never reach a user is the raw `no such table: chunks_fts` / `no such column: has_math` that any
query would otherwise raise first -- an error that names neither the problem nor the remedy, and
that looks identical whether the file is a stale index, someone else's database, or a truncated
download.

`_assert_supported_schema` is the guard, and before this file nothing referenced it.
"""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from zotero_pdf_text.artifacts import (
    GENERATION_DB_FILENAME,
    read_current_pointer,
    resolve_generation_dir,
    resolve_reader_db_path,
)
from zotero_pdf_text.cli import main
from zotero_pdf_text.fts import (
    IndexSchemaUnsupportedError,
    build_fts_index,
    connect_readonly,
    get_fulltext,
    search_fts,
)
from zotero_pdf_text.mcp_contract import PublicMcpError, _public_call

REBUILD_COMMAND = "rebuild-index"


def _record(text: str = "Cultural consensus theory models shared knowledge.") -> dict:
    return {
        "zotero_parent_key": "PARENT1",
        "zotero_attachment_key": "ATTACH1",
        "title": "Cultural consensus theory",
        "creators": "Jane Smith",
        "year": "2024",
        "doi": "10.1000/one",
        "citation_key": "smithConsensus2024",
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


def _write_jsonl(path: Path) -> None:
    path.write_text(json.dumps(_record()) + "\n", encoding="utf-8")


def _write_unsupported_schema(db_path: Path) -> None:
    """Overwrite a database with a schema no reader supports, keeping the filename."""
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE metadata (record_id INTEGER PRIMARY KEY)")
        con.commit()
    finally:
        con.close()


class UnsupportedSchemaTests(unittest.TestCase):
    def _assert_actionable(self, db_path: Path) -> IndexSchemaUnsupportedError:
        """Every reader entry point must refuse alike, naming the file and the recovery command."""
        raised: list[IndexSchemaUnsupportedError] = []
        for label, call in (
            ("connect_readonly", lambda: connect_readonly(db_path)),
            ("search_fts", lambda: search_fts(db_path, "consensus")),
            ("get_fulltext", lambda: get_fulltext(db_path, attachment_key="ATTACH1")),
        ):
            with self.subTest(entry_point=label):
                with self.assertRaises(IndexSchemaUnsupportedError) as caught:
                    call()
                message = str(caught.exception)
                self.assertIn(REBUILD_COMMAND, message)
                self.assertIn(db_path.name, message)
                raised.append(caught.exception)
        return raised[0]

    def test_legacy_index_missing_a_column_names_the_column(self):
        """A database from before a column existed is the realistic upgrade case."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite"
            con = sqlite3.connect(db_path)
            try:
                # The current schema minus has_math: what an older release wrote.
                con.executescript(
                    """
                    CREATE TABLE metadata (
                        record_id INTEGER PRIMARY KEY,
                        zotero_parent_key TEXT NOT NULL,
                        zotero_attachment_key TEXT NOT NULL,
                        title TEXT NOT NULL,
                        creators TEXT NOT NULL,
                        year TEXT NOT NULL,
                        doi TEXT NOT NULL,
                        citation_key TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        markdown_path TEXT NOT NULL,
                        markdown_sha256 TEXT NOT NULL,
                        extraction_tool TEXT NOT NULL,
                        char_count INTEGER NOT NULL,
                        word_count INTEGER NOT NULL,
                        page_count TEXT NOT NULL,
                        classification TEXT NOT NULL,
                        identity_status TEXT NOT NULL,
                        identity_rule TEXT NOT NULL
                    );
                    CREATE TABLE chunks (
                        chunk_id INTEGER PRIMARY KEY,
                        record_id INTEGER NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        start_char INTEGER NOT NULL,
                        end_char INTEGER NOT NULL,
                        text TEXT NOT NULL
                    );
                    CREATE VIRTUAL TABLE chunks_fts USING fts5(
                        title, creators, text, citation_key,
                        record_id UNINDEXED, chunk_id UNINDEXED, tokenize='unicode61'
                    );
                    """
                )
                con.commit()
            finally:
                con.close()

            error = self._assert_actionable(db_path)
            # Naming the missing column is what turns "unsupported" into a diagnosis.
            self.assertIn("has_math", str(error))
            self.assertIn("predate", str(error))

    def test_missing_table_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "partial.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.execute("CREATE TABLE metadata (record_id INTEGER PRIMARY KEY)")
                con.commit()
            finally:
                con.close()

            error = self._assert_actionable(db_path)
            self.assertIn("chunks", str(error))

    def test_a_foreign_sqlite_database_is_refused(self):
        """Pointing --db at the wrong file is at least as likely as a stale index."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "somebody-elses.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, payload TEXT)")
                con.execute("INSERT INTO unrelated (payload) VALUES ('not an index')")
                con.commit()
            finally:
                con.close()

            self._assert_actionable(db_path)

    def test_an_empty_file_is_refused(self):
        """SQLite opens a zero-byte file as a valid empty database rather than erroring."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "empty.sqlite"
            db_path.write_bytes(b"")
            self._assert_actionable(db_path)

    def test_a_file_that_is_not_a_database_is_refused_as_unreadable(self):
        """A truncated download or a text file must not surface as a raw DatabaseError."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "not-a-database.sqlite"
            db_path.write_bytes(b"This is a text file, not a SQLite database.\n" * 40)

            with self.assertRaises(IndexSchemaUnsupportedError) as caught:
                connect_readonly(db_path)
            message = str(caught.exception)
            self.assertIn("not a readable SQLite database", message)
            self.assertIn(REBUILD_COMMAND, message)

    def test_no_low_level_sqlite_error_escapes_any_reader(self):
        """The guarantee stated positively: readers never leak sqlite3 exceptions for these files.

        `IndexSchemaUnsupportedError` is a RuntimeError, so a stray `sqlite3.DatabaseError` would
        not be caught by the assertions above -- it would fail the test as an error. This makes
        that explicit rather than incidental.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {}

            empty = root / "empty.sqlite"
            empty.write_bytes(b"")
            cases["empty"] = empty

            garbage = root / "garbage.sqlite"
            garbage.write_bytes(b"\x00\x01\x02not sqlite at all")
            cases["garbage"] = garbage

            foreign = root / "foreign.sqlite"
            con = sqlite3.connect(foreign)
            try:
                con.execute("CREATE TABLE t (a INTEGER)")
                con.commit()
            finally:
                con.close()
            cases["foreign"] = foreign

            for label, db_path in cases.items():
                for entry_point, call in (
                    ("search_fts", lambda p=db_path: search_fts(p, "consensus")),
                    ("get_fulltext", lambda p=db_path: get_fulltext(p, attachment_key="ATTACH1")),
                ):
                    with self.subTest(case=label, entry_point=entry_point):
                        try:
                            call()
                        except IndexSchemaUnsupportedError:
                            pass
                        except sqlite3.Error as exc:
                            self.fail(f"{label}/{entry_point} leaked a low-level SQLite error: {exc!r}")
                        else:
                            self.fail(f"{label}/{entry_point} accepted an unusable database")


class SchemaErrorAtTheMcpBoundaryTests(unittest.TestCase):
    """The refusal is useful locally and must stay safe remotely.

    The internal message deliberately names the database file so a CLI user can see which file is
    wrong. That same detail is a local path, so the MCP boundary has to answer a stable code and
    drop it. Tested against the mapping itself rather than a live server, so it holds for every
    tool that routes through it.
    """

    def test_schema_error_becomes_a_public_code_with_no_path(self):
        secret_path = Path("/home/a-researcher/private-library/converted/index.sqlite")
        internal = IndexSchemaUnsupportedError(
            f"{secret_path} is not a supported full-text index (missing: chunks_fts). It may "
            "predate the current index schema. Rebuild it with 'zotero-pdf-text rebuild-index'."
        )

        with self.assertRaises(PublicMcpError) as caught:
            _public_call(lambda: (_ for _ in ()).throw(internal))

        self.assertEqual(caught.exception.code, "index_schema_unsupported")
        message = str(caught.exception)
        self.assertNotIn("a-researcher", message)
        self.assertNotIn("private-library", message)
        self.assertNotIn("index.sqlite", message)
        # Still actionable: it names the command that repairs it.
        self.assertIn(REBUILD_COMMAND, message)

    def test_a_real_unusable_index_reaches_the_boundary_as_that_code(self):
        """End to end from a genuinely unsupported file, not just a hand-built exception."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.execute("CREATE TABLE metadata (record_id INTEGER PRIMARY KEY)")
                con.commit()
            finally:
                con.close()

            with self.assertRaises(PublicMcpError) as caught:
                _public_call(lambda: search_fts(db_path, "consensus"))
            self.assertEqual(caught.exception.code, "index_schema_unsupported")
            self.assertNotIn(tmp, str(caught.exception))


class SchemaRecoveryTests(unittest.TestCase):
    """The other half of the contract: the command the error names actually fixes it."""

    def test_rebuilding_over_a_legacy_index_restores_a_readable_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            db_path = root / "index.sqlite"
            _write_jsonl(jsonl)

            # Stand in for an index written by an older release: right filename, wrong schema.
            con = sqlite3.connect(db_path)
            try:
                con.execute("CREATE TABLE metadata (record_id INTEGER PRIMARY KEY)")
                con.commit()
            finally:
                con.close()
            with self.assertRaises(IndexSchemaUnsupportedError):
                search_fts(db_path, "consensus")

            # The recovery the message names: republish from the JSONL sidecar.
            build_fts_index(jsonl, db_path)

            results = search_fts(db_path, "consensus", limit=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].zotero_attachment_key, "ATTACH1")
            self.assertTrue(get_fulltext(db_path, attachment_key="ATTACH1").text)

    def _publish_a_generation(self, output_root: Path) -> str:
        """Publish a managed generation the way a user first gets one, and return its id."""
        index_root = output_root / "index"
        index_root.mkdir(parents=True, exist_ok=True)
        _write_jsonl(index_root / "zotero_text_index.jsonl")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(["rebuild-index", "--output-root", str(output_root)])
        self.assertEqual(exit_code, 0, buffer.getvalue())

        pointer = read_current_pointer(index_root)
        self.assertIsNotNone(pointer)
        return str(pointer["current_generation"])

    def test_rebuild_index_recovers_a_published_generation_with_an_unsupported_database(self):
        """The command named in the error repairs a *managed* index, not just a bare file.

        `build_fts_index` is only the last step of `rebuild-index`, which also locates the sidecar,
        inherits the current generation's settings, stages and validates a successor, and swaps
        `current.json`. Testing recovery through the builder alone would leave the claim the error
        message makes -- run this command and the index works again -- resting on the part of the
        path least likely to break.

        Only the published SQLite file is replaced here, which is the realistic shape of the
        problem: the sidecar is intact and the pointer is valid, so nothing signals damage until a
        reader opens the database.
        """
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "converted_text"
            index_root = output_root / "index"
            first_generation = self._publish_a_generation(output_root)

            # Remove the legacy sidecar the first publish was seeded from, so recovery can only
            # come from the current generation's own JSONL -- otherwise this would pass by
            # re-running the legacy migration path instead of the one users are on.
            (index_root / "zotero_text_index.jsonl").unlink()

            reader_anchor = index_root / GENERATION_DB_FILENAME
            self.assertEqual(len(search_fts(resolve_reader_db_path(reader_anchor), "consensus")), 1)

            published_db = resolve_generation_dir(index_root, first_generation) / GENERATION_DB_FILENAME
            _write_unsupported_schema(published_db)
            with self.assertRaises(IndexSchemaUnsupportedError):
                search_fts(resolve_reader_db_path(reader_anchor), "consensus")

            # The recovery exactly as the message states it: no arguments beyond the root, so the
            # command has to rediscover the sidecar itself.
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(["rebuild-index", "--output-root", str(output_root)])
            self.assertEqual(exit_code, 0, buffer.getvalue())

            pointer = read_current_pointer(index_root)
            self.assertIsNotNone(pointer)
            recovered_generation = str(pointer["current_generation"])
            # A new generation, rather than the damaged one repaired in place: the unusable
            # database stays where it was until retention removes it, so a failed publish cannot
            # leave the pointer aimed at a half-written file.
            self.assertNotEqual(recovered_generation, first_generation)

            recovered_db = resolve_reader_db_path(reader_anchor)
            self.assertEqual(
                recovered_db,
                resolve_generation_dir(index_root, recovered_generation) / GENERATION_DB_FILENAME,
            )
            results = search_fts(recovered_db, "consensus", limit=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].zotero_attachment_key, "ATTACH1")
            self.assertTrue(get_fulltext(recovered_db, attachment_key="ATTACH1").text)

    def test_an_index_with_extra_columns_is_still_readable(self):
        """Forward compatibility: the guard requires a superset, not an exact match.

        A newer release adding a metadata column must not make its index unreadable by a reader
        that predates the column -- otherwise every additive schema change becomes a hard break
        in both directions.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            db_path = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, db_path)

            con = sqlite3.connect(db_path)
            try:
                con.execute("ALTER TABLE metadata ADD COLUMN future_field TEXT DEFAULT ''")
                con.commit()
            finally:
                con.close()

            self.assertEqual(len(search_fts(db_path, "consensus", limit=5)), 1)
            self.assertTrue(get_fulltext(db_path, attachment_key="ATTACH1").text)


if __name__ == "__main__":
    unittest.main()
