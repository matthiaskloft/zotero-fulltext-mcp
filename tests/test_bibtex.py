import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.bibtex import (
    JavaScriptResult,
    append_bibtex_entries,
    export_bibtex_entries,
    find_available_pdf_for_item,
    link_local_pdf,
)
from zotero_pdf_text.bibtex import _read_bounded


class BibtexTests(unittest.TestCase):
    def test_export_bibtex_entries_dedupes_keys_and_calls_bbt(self):
        with patch("zotero_pdf_text.bibtex._json_rpc", return_value="@article{smith2024,\n}\n") as rpc:
            export = export_bibtex_entries(["smith2024", "smith2024,doe2020"], translator="Better BibLaTeX")

        self.assertEqual(export.citation_keys, ["smith2024", "doe2020"])
        self.assertIn("@article{smith2024", export.entry)
        rpc.assert_called_once_with(
            "http://127.0.0.1:23119/better-bibtex/json-rpc",
            "item.export",
            [["smith2024", "doe2020"], "Better BibLaTeX"],
            max_response_bytes=None,
        )

    def test_export_bibtex_entries_forwards_max_response_bytes(self):
        with patch("zotero_pdf_text.bibtex._json_rpc", return_value="@article{smith2024,\n}\n") as rpc:
            export_bibtex_entries(["smith2024"], max_response_bytes=500_000)

        rpc.assert_called_once_with(
            "http://127.0.0.1:23119/better-bibtex/json-rpc",
            "item.export",
            [["smith2024"], "Better BibLaTeX"],
            max_response_bytes=500_000,
        )

    def test_append_bibtex_entries_skips_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            references = root / "references.bib"
            references.write_text("@article{smith2024,\n  title = {Existing}\n}\n", encoding="utf-8")

            with patch("zotero_pdf_text.bibtex._json_rpc", return_value="@article{doe2020,\n  title = {New}\n}\n"):
                result = append_bibtex_entries(["smith2024", "doe2020"], references)

            self.assertEqual(result.added_keys, ["doe2020"])
            self.assertEqual(result.skipped_existing_keys, ["smith2024"])
            text = references.read_text(encoding="utf-8")
            self.assertEqual(text.count("@article{smith2024"), 1)
            self.assertEqual(text.count("@article{doe2020"), 1)

    def test_find_available_pdf_for_item_reports_found_attachment(self):
        js_result = JavaScriptResult(
            ok=True, result={"found": True, "attachmentKey": "WXYZ5678"}, error="", endpoint="http://x"
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertTrue(result.ok)
        self.assertTrue(result.found)
        self.assertEqual(result.attachment_key, "WXYZ5678")
        self.assertEqual(result.error, "")

    def test_find_available_pdf_for_item_reports_not_found(self):
        js_result = JavaScriptResult(ok=True, result={"found": False}, error="", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertTrue(result.ok)
        self.assertFalse(result.found)
        self.assertEqual(result.attachment_key, "")

    def test_find_available_pdf_for_item_surfaces_js_error(self):
        js_result = JavaScriptResult(ok=True, result={"error": "item not found"}, error="", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertFalse(result.ok)
        self.assertFalse(result.found)
        self.assertEqual(result.error, "item not found")

    def test_find_available_pdf_for_item_surfaces_bridge_failure(self):
        js_result = JavaScriptResult(ok=False, result=None, error="debug-bridge unreachable", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "debug-bridge unreachable")

    def test_link_local_pdf_reports_zotmoov_move(self):
        js_result = JavaScriptResult(
            ok=True,
            result={"linked": True, "moved": True, "key": "NEWKEY1", "path": "C:\\dst\\Author - Title.pdf"},
            error="",
            endpoint="http://x",
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = link_local_pdf("ABCD1234", "C:\\src\\paper.pdf")

        self.assertTrue(result.ok)
        self.assertTrue(result.moved)
        self.assertEqual(result.attachment_key, "NEWKEY1")
        self.assertEqual(result.path, "C:\\dst\\Author - Title.pdf")
        self.assertEqual(result.warning, "")

    def test_link_local_pdf_reports_unmoved_with_warning(self):
        js_result = JavaScriptResult(
            ok=True,
            result={
                "linked": True,
                "moved": False,
                "key": "ORIGKEY",
                "path": "C:\\src\\paper.pdf",
                "warning": "ZotMoov not installed/active -- file left at its original location",
            },
            error="",
            endpoint="http://x",
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = link_local_pdf("ABCD1234", "C:\\src\\paper.pdf")

        self.assertTrue(result.ok)
        self.assertFalse(result.moved)
        self.assertIn("ZotMoov not installed", result.warning)

    def test_link_local_pdf_surfaces_js_error(self):
        js_result = JavaScriptResult(ok=True, result={"error": "parent item not found"}, error="", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = link_local_pdf("ABCD1234", "C:\\src\\paper.pdf")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "parent item not found")

    def test_link_local_pdf_surfaces_bridge_failure(self):
        js_result = JavaScriptResult(ok=False, result=None, error="debug-bridge unreachable", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = link_local_pdf("ABCD1234", "C:\\src\\paper.pdf")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "debug-bridge unreachable")


class _FakeHttpResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk, self._data = self._data, b""
            return chunk
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class ReadBoundedTests(unittest.TestCase):
    """Regression tests for the bounded Better BibTeX read (previously response.read() with
    no size cap, so a misbehaving local endpoint could exhaust memory before the MCP
    contract's post-hoc size check ever ran)."""

    def test_unbounded_reads_everything(self):
        response = _FakeHttpResponse(b"x" * 1000)
        self.assertEqual(len(_read_bounded(response, None)), 1000)

    def test_bounded_read_rejects_oversized_response(self):
        response = _FakeHttpResponse(b"x" * 1000)
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            _read_bounded(response, 500)

    def test_bounded_read_accepts_response_within_limit(self):
        response = _FakeHttpResponse(b"x" * 500)
        self.assertEqual(len(_read_bounded(response, 500)), 500)


if __name__ == "__main__":
    unittest.main()
