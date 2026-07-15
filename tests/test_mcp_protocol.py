from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema
from mcp.types import CallToolRequest, CallToolRequestParams

from zotero_pdf_text.bibtex import BibtexExport
from zotero_pdf_text.math_ocr import ReconvertResult
from test_mcp_server import _build_index
from zotero_pdf_text.mcp_contract import create_server


@unittest.skipUnless(importlib.util.find_spec("mcp"), "requires the optional MCP extra")
class McpProtocolTests(unittest.TestCase):
    def test_real_server_advertises_structured_success_schemas_and_protocol_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sqlite_path, _ = _build_index(Path(tmp))
            server = create_server(sqlite_path)

            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
            self.assertEqual(
                set(tools),
                {"search_fulltext", "get_fulltext_chunk", "get_item_context", "list_timeout_candidates"},
            )
            for tool in tools.values():
                self.assertIsNotNone(tool.outputSchema)
                self.assertEqual(tool.outputSchema["type"], "object")
            self.assertEqual(set(tools["search_fulltext"].outputSchema["required"]), {"search_mode", "no_results", "results"})
            self.assertIn("source_locator", tools["search_fulltext"].outputSchema["$defs"]["SearchRecord"]["properties"])
            self.assertIn("provenance", tools["search_fulltext"].outputSchema["$defs"]["SearchRecord"]["properties"])
            self.assertIn("text", tools["get_fulltext_chunk"].outputSchema["properties"])
            self.assertIn("records", tools["get_item_context"].outputSchema["properties"])
            self.assertEqual(tools["search_fulltext"].inputSchema["properties"]["query"]["type"], "string")
            self.assertEqual(tools["search_fulltext"].inputSchema["properties"]["limit"]["type"], "integer")
            self.assertEqual(tools["search_fulltext"].inputSchema["properties"]["search_mode"]["enum"], ["all_terms", "any_terms", "phrase"])

            search = self._call(server, "search_fulltext", {"query": "searchable"})
            self.assertFalse(search.isError)
            self.assertIsNotNone(search.structuredContent)
            self.assertEqual(search.structuredContent["search_mode"], "all_terms")
            self.assertTrue(search.structuredContent["results"])
            self._assert_schema(tools["search_fulltext"], search.structuredContent)

            no_results = self._call(server, "search_fulltext", {"query": "unfindable"})
            self.assertFalse(no_results.isError)
            self.assertTrue(no_results.structuredContent["no_results"])
            self.assertEqual(no_results.structuredContent["results"], [])

            passage = self._call(server, "get_fulltext_chunk", {"attachment_key": "ATTACH1", "chunk_index": 0})
            self.assertFalse(passage.isError)
            self.assertIn("text", passage.structuredContent)
            self.assertIn("source_locator", passage.structuredContent)
            self._assert_schema(tools["get_fulltext_chunk"], passage.structuredContent)

            context = self._call(server, "get_item_context", {"attachment_key": "ATTACH1"})
            self.assertFalse(context.isError)
            self.assertEqual(context.structuredContent["records"][0]["attachment_key"], "ATTACH1")
            self._assert_schema(tools["get_item_context"], context.structuredContent)

            invalid = self._call(server, "search_fulltext", {"query": " "})
            self.assertTrue(invalid.isError)
            self.assertIsNone(invalid.structuredContent)
            self.assertEqual(len(invalid.content), 1)
            self.assertIn("invalid_query: ", invalid.content[0].text)

            wrong_query_type = self._call(server, "search_fulltext", {"query": 123})
            self.assertTrue(wrong_query_type.isError)
            self.assertIn("invalid_query: ", wrong_query_type.content[0].text)
            self.assertNotIn("input_value", wrong_query_type.content[0].text)

            invalid_limit = self._call(server, "search_fulltext", {"query": "topic", "limit": "bad"})
            self.assertTrue(invalid_limit.isError)
            self.assertIn("invalid_limit: ", invalid_limit.content[0].text)
            self.assertNotIn("pydantic", invalid_limit.content[0].text.lower())

            invalid_mode = self._call(server, "search_fulltext", {"query": "topic", "search_mode": "bad"})
            self.assertTrue(invalid_mode.isError)
            self.assertIn("invalid_search_mode: ", invalid_mode.content[0].text)

            invalid_chunk = self._call(server, "get_fulltext_chunk", {"attachment_key": "ATTACH1", "chunk_index": "bad"})
            self.assertTrue(invalid_chunk.isError)
            self.assertIn("invalid_chunk_index: ", invalid_chunk.content[0].text)

            invalid_context = self._call(
                server,
                "get_item_context",
                {"parent_key": "PARENT1", "attachment_key": "ATTACH1"},
            )
            self.assertTrue(invalid_context.isError)
            self.assertIn("invalid_context_key: ", invalid_context.content[0].text)

            missing_attachment = self._call(server, "get_fulltext_chunk", {"attachment_key": "MISSING"})
            self.assertTrue(missing_attachment.isError)
            self.assertIn("attachment_not_found: ", missing_attachment.content[0].text)

            with patch("zotero_pdf_text.mcp_contract.search_fts", side_effect=Exception("C:/private/secret.sqlite")):
                unexpected = self._call(server, "search_fulltext", {"query": "topic"})
            self.assertTrue(unexpected.isError)
            self.assertIsNone(unexpected.structuredContent)
            self.assertIn("internal_error: ", unexpected.content[0].text)
            self.assertNotIn("secret.sqlite", unexpected.content[0].text)

    def test_unavailable_index_is_a_redacted_protocol_error(self):
        missing = Path("private-missing-index.sqlite")
        server = create_server(missing)

        result = self._call(server, "search_fulltext", {"query": "topic"})

        self.assertTrue(result.isError)
        self.assertIsNone(result.structuredContent)
        self.assertIn("database_unavailable: ", result.content[0].text)
        self.assertNotIn(str(missing), result.content[0].text)

    def test_enabled_optional_tools_preserve_public_error_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sqlite_path, config = _build_index(Path(tmp))
            with patch("zotero_pdf_text.mcp_contract.marker_dependency_available", return_value=True):
                server = create_server(sqlite_path, config=config, enable_bibtex=True, enable_reconvert=True)

            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
            self.assertEqual(
                set(tools),
                {
                    "search_fulltext",
                    "get_fulltext_chunk",
                    "get_item_context",
                    "list_timeout_candidates",
                    "export_bibtex_entries_by_key",
                    "reconvert_with_math_ocr",
                },
            )
            self.assertEqual(tools["export_bibtex_entries_by_key"].outputSchema["type"], "object")
            self.assertEqual(tools["reconvert_with_math_ocr"].outputSchema["type"], "object")
            self.assertEqual(
                set(tools["export_bibtex_entries_by_key"].outputSchema["required"]),
                {"citation_keys", "translator", "entry", "provenance"},
            )
            self.assertIn("reconverted_at", tools["reconvert_with_math_ocr"].outputSchema["properties"])

            exported = BibtexExport(["smith2024"], "Better BibLaTeX", "@article{smith2024}", "private endpoint")
            with patch("zotero_pdf_text.mcp_contract.export_bibtex_entries", return_value=exported):
                bibtex_success = self._call(
                    server,
                    "export_bibtex_entries_by_key",
                    {"citation_keys": ["smith2024"]},
                )
            self.assertFalse(bibtex_success.isError)
            self.assertEqual(bibtex_success.structuredContent["citation_keys"], ["smith2024"])
            self._assert_schema(tools["export_bibtex_entries_by_key"], bibtex_success.structuredContent)

            with patch("zotero_pdf_text.mcp_contract.export_bibtex_entries", side_effect=RuntimeError("private endpoint")):
                unavailable_integration = self._call(
                    server,
                    "export_bibtex_entries_by_key",
                    {"citation_keys": ["smith2024"]},
                )
            self.assertTrue(unavailable_integration.isError)
            self.assertIn("integration_unavailable: ", unavailable_integration.content[0].text)
            self.assertNotIn("private endpoint", unavailable_integration.content[0].text)

            failed = ReconvertResult(
                ok=False,
                attachment_key="ATTACH1",
                previous_extraction_tool="pymupdf4llm.to_markdown",
                new_extraction_tool="marker",
                previous_char_count=1,
                new_char_count=0,
                markdown_path="private.md",
                source_path="private.pdf",
                reconverted_at="",
                error="private failure",
            )
            with patch("zotero_pdf_text.math_ocr.reconvert_with_marker", return_value=failed):
                reconversion_failed = self._call(
                    server,
                    "reconvert_with_math_ocr",
                    {"attachment_key": "ATTACH1", "confirm": "reconvert"},
                )
            self.assertTrue(reconversion_failed.isError)
            self.assertIn("reconversion_failed: ", reconversion_failed.content[0].text)
            self.assertNotIn("private failure", reconversion_failed.content[0].text)

            with patch("zotero_pdf_text.mcp_contract.marker_dependency_available", return_value=True):
                crash_server = create_server(sqlite_path, config=config, enable_reconvert=True)
            with patch("zotero_pdf_text.math_ocr.reconvert_with_marker", side_effect=RuntimeError("private failure")):
                reconversion_crashed = self._call(
                    crash_server,
                    "reconvert_with_math_ocr",
                    {"attachment_key": "ATTACH1", "confirm": "reconvert"},
                )
            self.assertTrue(reconversion_crashed.isError)
            self.assertIn("reconversion_failed: ", reconversion_crashed.content[0].text)
            self.assertNotIn("private failure", reconversion_crashed.content[0].text)

            with patch("zotero_pdf_text.mcp_contract.marker_dependency_available", return_value=True):
                rate_limited_server = create_server(sqlite_path, config=config, enable_reconvert=True)
            success = ReconvertResult(
                ok=True,
                attachment_key="ATTACH1",
                previous_extraction_tool="pymupdf4llm.to_markdown",
                new_extraction_tool="marker",
                previous_char_count=1,
                new_char_count=2,
                markdown_path="private.md",
                source_path="private.pdf",
                reconverted_at="2026-07-13T00:00:00",
                error="",
            )
            with patch("zotero_pdf_text.math_ocr.reconvert_with_marker", return_value=success):
                reconvert_success = self._call(
                    rate_limited_server,
                    "reconvert_with_math_ocr",
                    {"attachment_key": "ATTACH1", "confirm": "reconvert"},
                )
            self.assertFalse(reconvert_success.isError)
            self.assertTrue(reconvert_success.structuredContent["ok"])
            self._assert_schema(tools["reconvert_with_math_ocr"], reconvert_success.structuredContent)
            limited = self._call(
                rate_limited_server,
                "reconvert_with_math_ocr",
                {"attachment_key": "ATTACH1", "confirm": "reconvert"},
            )
            self.assertTrue(limited.isError)
            self.assertIn("reconversion_rate_limited: ", limited.content[0].text)

    @staticmethod
    def _call(server, name: str, arguments: dict[str, object]):
        handler = server._mcp_server.request_handlers[CallToolRequest]
        request = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))
        return asyncio.run(handler(request)).root

    @staticmethod
    def _assert_schema(tool, content: dict[str, object]) -> None:
        jsonschema.validate(instance=content, schema=tool.outputSchema)
