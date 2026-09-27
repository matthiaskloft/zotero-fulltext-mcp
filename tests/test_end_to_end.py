"""End-to-end fixture tests (hardening Package 5, step 3).

Every other test module exercises one layer against hand-built index rows. These run the chain a
user runs: a synthetic Zotero database and a real PDF go through `convert-new`, which maps,
converts, stages and publishes a generation, and a real server process started with the arguments
and tool list `install-mcp` prints for Codex serves it over stdio. Everything lives in a temporary
directory; no real Zotero path is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pymupdf

from zotero_pdf_text import artifacts
from zotero_pdf_text.cli import main

HAS_MCP = importlib.util.find_spec("mcp") is not None


@dataclass(frozen=True)
class Paper:
    attachment_key: str
    title: str
    doi: str
    citation_key: str
    body: str


FIRST = Paper("ATTKEY01", "Synthetic Fixture Paper", "10.1000/fixture.1", "fixture2026", "Heteroscedasticity drives the zebrafinch estimator.")
SECOND = Paper("ATTKEY02", "Another Fixture Paper", "10.1000/fixture.2", "another2026", "A quokka appears only in the second paper.")


class Library:
    """A config, a linked-attachment folder of real PDFs, and a Zotero-schema database listing them."""

    def __init__(self, root: Path):
        self.root = root
        self.linked = root / "linked"
        self.linked.mkdir()
        self.output_root = root / "converted_text"
        self.index_root = self.output_root / "index"
        self.config_path = root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "zotero_root": str(root),
                    "zotero_data_directory": str(root),
                    "linked_attachments": str(self.linked),
                    "output_root": str(self.output_root),
                }
            ),
            encoding="utf-8",
        )
        self.papers: list[Paper] = []

    def add(self, paper: Paper) -> None:
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), f"{paper.title}\ndoi:{paper.doi}\n{paper.body}")
        doc.save(self.linked / f"{paper.attachment_key}.pdf")
        doc.close()
        self.papers.append(paper)
        self._write_zotero_db()

    def _write_zotero_db(self) -> None:
        db = self.root / "zotero.sqlite"
        db.unlink(missing_ok=True)
        # closing() because a connection's own context manager commits but does not close, and
        # the next add() must be able to delete the file on Windows.
        with contextlib.closing(sqlite3.connect(db)) as con, con:
            con.executescript(
                """
                CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
                CREATE TABLE itemAttachments (
                    itemID INTEGER PRIMARY KEY, parentItemID INTEGER, linkMode INTEGER,
                    contentType TEXT, path TEXT
                );
                CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
                CREATE TABLE itemTypesCombined (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
                CREATE TABLE fieldsCombined (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
                CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
                CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
                CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
                CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
                INSERT INTO itemTypesCombined VALUES (1, 'journalArticle'), (2, 'attachment');
                INSERT INTO fieldsCombined VALUES (1, 'title'), (2, 'DOI'), (3, 'citationKey');
                """
            )
            value_id = 0
            for n, paper in enumerate(self.papers):
                parent_id, attachment_id = 2 * n + 1, 2 * n + 2
                con.execute("INSERT INTO items VALUES (?, ?, 1)", (parent_id, f"PARENT{n:02d}"))
                for field_id, value in ((1, paper.title), (2, paper.doi), (3, paper.citation_key)):
                    value_id += 1
                    con.execute("INSERT INTO itemDataValues VALUES (?, ?)", (value_id, value))
                    con.execute("INSERT INTO itemData VALUES (?, ?, ?)", (parent_id, field_id, value_id))
                con.execute("INSERT INTO items VALUES (?, ?, 2)", (attachment_id, paper.attachment_key))
                con.execute(
                    "INSERT INTO itemAttachments VALUES (?, ?, 2, 'application/pdf', ?)",
                    (attachment_id, parent_id, str(self.linked / f"{paper.attachment_key}.pdf")),
                )

    def cli(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([args[0], "--config", str(self.config_path), *args[1:]])
        return code, out.getvalue() + err.getvalue()

    def convert_new(self) -> None:
        code, output = self.cli("convert-new", "--workers", "1")
        if code != 0:
            raise AssertionError(output)

    def current_generation(self) -> str | None:
        pointer = artifacts.read_current_pointer(self.index_root)
        return None if pointer is None else pointer["current_generation"]

    def only_mapping_run(self) -> Path:
        (run,) = (self.output_root / "mapping-runs").iterdir()
        return run

    def registration(self, *flags: str) -> tuple[list[str], set[str]]:
        """The server command line and enabled tools from the Codex block `install-mcp` prints."""
        # install-mcp reads Codex's config.toml for its drift report; never the developer's own.
        with tempfile.TemporaryDirectory() as codex_home, patch.dict(os.environ, {"CODEX_HOME": codex_home}):
            code, output = self.cli("install-mcp", *flags)
        if code != 0:
            raise AssertionError(output)
        block = output[output.index("[mcp_servers.") :].split("\n\n")[0]
        (entry,) = tomllib.loads(block)["mcp_servers"].values()
        return [entry["command"], *entry["args"]], set(entry["enabled_tools"])


def _call_server(command_line: list[str], calls: list[tuple[str, dict]]) -> tuple[set[str], list]:
    """Start a real server process over stdio, list its tools, and make the calls in order."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command_line[0], args=command_line[1:], env=dict(os.environ))

    async def run():
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    results = [await session.call_tool(name, arguments) for name, arguments in calls]
                    return tools, results

    return asyncio.run(asyncio.wait_for(run(), timeout=60))


def _search(term: str) -> tuple[str, dict]:
    return "search_fulltext", {"query": term}


def _fetch(hit: dict) -> tuple[str, dict]:
    locator = hit["source_locator"]
    return "get_fulltext_chunk", {
        "attachment_key": hit["attachment_key"],
        "chunk_index": locator["chunk_index"],
        "chunk_sha256": locator["chunk_sha256"],
    }


def _keys(result) -> list[str]:
    return [hit["attachment_key"] for hit in result.structuredContent["results"]]


@unittest.skipUnless(HAS_MCP, "requires the optional MCP extra")
class ConvertIndexServeTests(unittest.TestCase):
    def test_converted_paper_is_searchable_and_readable_through_the_registered_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Library(Path(tmp))
            library.add(FIRST)
            library.convert_new()

            server_command, enabled_tools = library.registration()
            tools, (search, lookup) = _call_server(
                server_command,
                [_search("zebrafinch"), ("lookup_citation_key", {"citation_key": FIRST.citation_key})],
            )
            self.assertEqual(tools, enabled_tools)
            self.assertEqual(_keys(search), [FIRST.attachment_key])
            self.assertEqual(search.structuredContent["results"][0]["title"], FIRST.title)
            self.assertFalse(lookup.isError)
            self.assertIn(FIRST.attachment_key, json.dumps(lookup.structuredContent))

            _, (passage,) = _call_server(server_command, [_fetch(search.structuredContent["results"][0])])
            self.assertFalse(passage.isError)
            self.assertIn("zebrafinch", passage.structuredContent["text"])

    def test_incremental_conversion_and_reindex_keep_old_locators_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Library(Path(tmp))
            library.add(FIRST)
            library.convert_new()
            server_command, _ = library.registration()
            _, (before,) = _call_server(server_command, [_search("zebrafinch")])
            fetch_old_passage = _fetch(before.structuredContent["results"][0])
            first_generation = library.current_generation()

            library.add(SECOND)
            library.convert_new()
            self.assertNotEqual(library.current_generation(), first_generation)
            _, (old, new, passage) = _call_server(
                server_command, [_search("zebrafinch"), _search("quokka"), fetch_old_passage]
            )
            self.assertEqual(_keys(old), [FIRST.attachment_key])
            self.assertEqual(_keys(new), [SECOND.attachment_key])
            self.assertFalse(passage.isError)

            code, output = library.cli("convert-new", "--workers", "1")
            self.assertEqual(code, 0)
            self.assertIn("Index is up to date", output)

            # A full reindex of the same text publishes a new generation with the same chunks.
            before_rebuild = library.current_generation()
            code, output = library.cli("rebuild-index")
            self.assertEqual(code, 0, output)
            self.assertNotEqual(library.current_generation(), before_rebuild)
            _, (new, passage) = _call_server(server_command, [_search("quokka"), fetch_old_passage])
            self.assertEqual(_keys(new), [SECOND.attachment_key])
            self.assertFalse(passage.isError)

    def test_db_only_and_bibtex_startup_serve_the_published_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Library(Path(tmp))
            library.add(FIRST)
            library.convert_new()
            db = str(library.index_root / "zotero_text_index.sqlite")
            _, default_tools = library.registration()

            db_only = [sys.executable, "-m", "zotero_pdf_text.mcp_server", "--db", db]
            tools, (search,) = _call_server(db_only, [_search("zebrafinch")])
            self.assertEqual(tools, default_tools)
            self.assertEqual(_keys(search), [FIRST.attachment_key])

            server_command, enabled_tools = library.registration("--enable-bibtex")
            self.assertEqual(enabled_tools, default_tools | {"export_bibtex_entries_by_key"})
            tools, (search,) = _call_server(server_command, [_search("zebrafinch")])
            self.assertEqual(tools, enabled_tools)
            self.assertEqual(_keys(search), [FIRST.attachment_key])


@unittest.skipUnless(HAS_MCP, "requires the optional MCP extra")
class InterruptedPublicationTests(unittest.TestCase):
    def test_a_crash_before_the_pointer_swap_keeps_serving_and_the_next_run_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Library(Path(tmp))
            library.add(FIRST)
            library.convert_new()
            first_generation = library.current_generation()
            library.add(SECOND)

            real_write = artifacts._atomic_write_json

            def crash_on_pointer_swap(path: Path, payload: dict) -> None:
                if path.name == "current.json":
                    raise OSError("simulated crash before the pointer swap")
                real_write(path, payload)

            with patch.object(artifacts, "_atomic_write_json", crash_on_pointer_swap):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    library.convert_new()

            # The staged generation and the journal are left behind, but readers still see the
            # previous generation.
            journal = json.loads((library.index_root / artifacts.JOURNAL_FILENAME).read_text(encoding="utf-8"))
            interrupted_generation = journal["generation_id"]
            self.assertEqual(library.current_generation(), first_generation)
            server_command, _ = library.registration()
            _, (old, new) = _call_server(server_command, [_search("zebrafinch"), _search("quokka")])
            self.assertEqual(_keys(old), [FIRST.attachment_key])
            self.assertEqual(_keys(new), [])

            library.convert_new()

            # Recovery published the interrupted generation before this run published its own, so
            # it is now the previous generation; without recovery that would be first_generation.
            # convert-new still converts the second paper again, because it decides what is new
            # from the generation that was current before recovery ran.
            self.assertFalse((library.index_root / artifacts.JOURNAL_FILENAME).exists())
            pointer = artifacts.read_current_pointer(library.index_root)
            self.assertEqual(pointer["previous_generation"], interrupted_generation)
            self.assertNotIn(pointer["current_generation"], {first_generation, interrupted_generation})
            _, (old, new) = _call_server(server_command, [_search("zebrafinch"), _search("quokka")])
            self.assertEqual(_keys(old), [FIRST.attachment_key])
            self.assertEqual(_keys(new), [SECOND.attachment_key])


class AuditAndStatusTests(unittest.TestCase):
    def test_a_freshly_converted_library_audits_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Library(Path(tmp))
            library.add(FIRST)
            library.add(SECOND)
            library.convert_new()
            snapshot = str(library.only_mapping_run())

            code, output = library.cli("audit-library", "--mapping-report", snapshot, "--full", "--json")
            self.assertEqual(code, 0, output)
            report = json.loads(output)
            statuses = {item["attachment_key"]: item["statuses"] for item in report["items"]}
            self.assertEqual(statuses, {FIRST.attachment_key: ["current"], SECOND.attachment_key: ["current"]})

            code, output = library.cli("library-status", "--mapping-report", snapshot, "--json")
            self.assertEqual(code, 0, output)
            status = json.loads(output)
            self.assertEqual(status["generation_id"], library.current_generation())
            self.assertEqual(status["total_items"], 2)
            self.assertTrue(status["inventory_available"])
            self.assertEqual(status["health"].get("current"), 2)

    def test_a_paper_added_to_zotero_after_conversion_is_reported_unindexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Library(Path(tmp))
            library.add(FIRST)
            library.convert_new()
            library.add(SECOND)
            code, output = library.cli("dry-run")
            self.assertEqual(code, 0, output)
            snapshot = output.split("Dry-run complete: ", 1)[1].splitlines()[0]

            code, output = library.cli("audit-library", "--mapping-report", snapshot, "--json")
            self.assertEqual(code, 0, output)
            statuses = {item["attachment_key"]: item["statuses"] for item in json.loads(output)["items"]}
            self.assertIn("unindexed", statuses[SECOND.attachment_key])


if __name__ == "__main__":
    unittest.main()
