import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.bibtex import (
    FIND_PDF_SETTLE_CAP_SECONDS,
    FIND_PDF_SETTLE_MARGIN_SECONDS,
    FIND_PDF_TIMEOUT_SECONDS,
    JavaScriptResult,
    append_bibtex_entries,
    execute_javascript,
    export_bibtex_entries,
    find_available_pdf_for_item,
    import_doi_via_connector,
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
            ok=True,
            result={
                "found": True, "settled": True, "originalKey": "WXYZ5678", "attachmentKey": "WXYZ5678",
                "linkMode": 1,
                "attachments": [{"key": "WXYZ5678", "linkMode": 1, "contentType": "application/pdf"}],
            },
            error="", endpoint="http://x",
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result) as bridge:
            result = find_available_pdf_for_item("ABCD1234")

        self.assertTrue(result.ok)
        self.assertTrue(result.found)
        self.assertEqual(result.outcome, "attached")
        self.assertEqual(result.attachment_key, "WXYZ5678")
        self.assertFalse(result.moved)
        self.assertEqual(result.link_mode, "imported_url")
        self.assertEqual(
            result.attachments, [{"key": "WXYZ5678", "link_mode": "imported_url", "content_type": "application/pdf"}]
        )
        self.assertEqual(result.error, "")
        js = bridge.call_args.args[0]
        self.assertIn("addAvailableFile", js)
        self.assertIn("Zotero.Items.exists(attachment.id)", js)
        # find-pdf waits for ZotMoov's auto-move but must never trigger a move itself.
        self.assertNotIn(".move(", js)

    def test_find_available_pdf_for_item_reports_key_after_zotmoov_move(self):
        js_result = JavaScriptResult(
            ok=True,
            result={
                "found": True, "settled": True, "originalKey": "STORED01", "attachmentKey": "LINKED01",
                "linkMode": 2,
                "attachments": [{"key": "LINKED01", "linkMode": 2, "contentType": "application/pdf"}],
            },
            error="", endpoint="http://x",
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, "attached")
        self.assertTrue(result.moved)
        self.assertEqual(result.attachment_key, "LINKED01")
        self.assertEqual(result.link_mode, "linked_file")
        self.assertIn("STORED01", result.message)

    def test_find_available_pdf_for_item_reports_unsettled_without_final_key(self):
        js_result = JavaScriptResult(
            ok=True,
            result={
                "found": True, "settled": False, "originalKey": "STORED01", "attachmentKey": "",
                "linkMode": None,
                "attachments": [{"key": "STORED01", "linkMode": 1, "contentType": "application/pdf"}],
            },
            error="", endpoint="http://x",
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertTrue(result.ok)
        self.assertTrue(result.found)
        self.assertEqual(result.outcome, "unsettled")
        self.assertEqual(result.attachment_key, "")
        self.assertEqual(result.attachments[0]["key"], "STORED01")
        self.assertIn("Do not rerun find-pdf", result.message)

    def test_find_available_pdf_for_item_reports_not_found(self):
        js_result = JavaScriptResult(ok=True, result={"found": False}, error="", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertTrue(result.ok)
        self.assertFalse(result.found)
        self.assertEqual(result.outcome, "not_found")
        self.assertEqual(result.attachment_key, "")
        self.assertIn("found no PDF", result.message)
        self.assertIn("link-pdf --key ABCD1234 --url <direct-pdf-url>", result.message)

    def test_find_available_pdf_for_item_surfaces_js_error(self):
        js_result = JavaScriptResult(ok=True, result={"error": "item not found"}, error="", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertFalse(result.ok)
        self.assertFalse(result.found)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.error, "item not found")

    def test_find_available_pdf_for_item_surfaces_bridge_failure(self):
        js_result = JavaScriptResult(ok=False, result=None, error="debug-bridge unreachable", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = find_available_pdf_for_item("ABCD1234")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "debug-bridge unreachable")

    def test_import_doi_via_bridge_reports_type_name_and_created_key(self):
        js_result = JavaScriptResult(
            ok=True,
            result={"key": "NEWITEM1", "title": "A Paper", "itemType": "journalArticle"},
            error="", endpoint="http://x",
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result) as bridge:
            result = import_doi_via_connector("10.1000/example")

        self.assertTrue(result.ok)
        self.assertEqual(result.item_type, "journalArticle")
        self.assertEqual(result.item_key, "NEWITEM1")
        self.assertIn("Zotero.ItemTypes.getName(item.itemTypeID)", bridge.call_args.args[0])
        self.assertEqual(result.item_keys, ["NEWITEM1"])

    def test_import_doi_via_bridge_reports_every_key_when_translator_creates_several(self):
        js_result = JavaScriptResult(
            ok=True,
            result={"key": "NEWITEM1", "keys": ["NEWITEM1", "NEWITEM2"], "title": "A Paper", "itemType": "journalArticle"},
            error="", endpoint="http://x",
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result):
            result = import_doi_via_connector("10.1000/example")

        self.assertTrue(result.ok)
        self.assertEqual(result.item_key, "")
        self.assertEqual(result.item_keys, ["NEWITEM1", "NEWITEM2"])

    def test_import_doi_bridge_timeout_is_unknown_and_never_falls_back_to_connector(self):
        # The translator may still have created the item; a connector save would duplicate it.
        js_result = JavaScriptResult(
            ok=False, result=None, error="debug-bridge did not answer within 30 s", endpoint="http://x",
            timed_out=True,
        )
        with (
            patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result),
            patch("zotero_pdf_text.bibtex._fetch_doi_metadata") as fetch,
            patch("zotero_pdf_text.bibtex.urllib.request.urlopen") as urlopen,
        ):
            result = import_doi_via_connector("10.1000/example")

        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, "unknown")
        self.assertIn("check Zotero", result.error)
        self.assertIn("do not rerun import-doi blindly", result.error)
        fetch.assert_not_called()
        urlopen.assert_not_called()

    def test_import_doi_falls_back_to_connector_when_bridge_is_unavailable(self):
        js_result = JavaScriptResult(ok=False, result=None, error="debug-bridge unreachable", endpoint="http://x")
        with (
            patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result),
            patch("zotero_pdf_text.bibtex._fetch_doi_metadata", side_effect=RuntimeError("offline")) as fetch,
        ):
            result = import_doi_via_connector("10.1000/example")

        fetch.assert_called_once_with("10.1000/example")
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, "")
        self.assertIn("Metadata fetch failed", result.error)

    def test_execute_javascript_flags_only_a_response_timeout_as_timed_out(self):
        with patch("zotero_pdf_text.bibtex.urllib.request.urlopen", side_effect=TimeoutError("timed out")) as urlopen:
            timed_out = execute_javascript("return 1;", timeout=90)
        self.assertFalse(timed_out.ok)
        self.assertTrue(timed_out.timed_out)
        self.assertIn("90 s", timed_out.error)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 90)

        refused = urllib.error.URLError(ConnectionRefusedError("refused"))
        with patch("zotero_pdf_text.bibtex.urllib.request.urlopen", side_effect=refused):
            unreachable = execute_javascript("return 1;")
        self.assertFalse(unreachable.ok)
        self.assertFalse(unreachable.timed_out)

    def test_find_available_pdf_for_item_bridge_timeout_is_unknown(self):
        js_result = JavaScriptResult(
            ok=False, result=None, error="debug-bridge did not answer within 90 s", endpoint="http://x",
            timed_out=True,
        )
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result) as bridge:
            result = find_available_pdf_for_item("ABCD1234")

        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.attachment_key, "")
        self.assertIn("Do not rerun find-pdf", result.message)
        self.assertEqual(bridge.call_args.kwargs["timeout"], FIND_PDF_TIMEOUT_SECONDS)

    def test_find_available_pdf_predicts_auto_move_from_zotmoov_prefs(self):
        js_result = JavaScriptResult(ok=True, result={"found": False}, error="", endpoint="http://x")
        with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=js_result) as bridge:
            find_available_pdf_for_item("ABCD1234")

        js = bridge.call_args.args[0]
        # ZotMoov's move() does nothing for an empty dst_dir or an extension outside a non-empty
        # allowed_fileext, and runs auto_process_delay after the attachment was added.
        self.assertIn("P('dst_dir')", js)
        self.assertIn("P('allowed_fileext')", js)
        self.assertIn("!allowedExt.length", js)
        self.assertIn("Zotero.File.getExtension(filePath).toLowerCase()", js)
        self.assertIn("P('auto_process_delay')", js)
        self.assertIn("moveDelay = 5000", js)
        self.assertIn(f"attachedAt + moveDelay + {FIND_PDF_SETTLE_MARGIN_SECONDS * 1000}", js)
        self.assertIn(f"scriptStart + {FIND_PDF_SETTLE_CAP_SECONDS * 1000}", js)
        self.assertLess(FIND_PDF_SETTLE_CAP_SECONDS, FIND_PDF_TIMEOUT_SECONDS)

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
