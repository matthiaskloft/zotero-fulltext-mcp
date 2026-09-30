"""Pre-write checks: live via debug-bridge, else a verified copy, else fail closed. Bridge mocked."""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from zotero_pdf_text.bibtex import JavaScriptResult
from zotero_pdf_text.pre_write_checks import (
    PreWriteCheckUnavailable,
    check_doi_duplicate,
    check_existing_pdf,
)
from zotero_pdf_text.zotero_db import DOI_ROWS_SQL, PDF_ATTACHMENTS_SQL, SnapshotUnstableError

BRIDGE = "zotero_pdf_text.bibtex.execute_javascript"
SQLITE_DOI = "zotero_pdf_text.pre_write_checks.find_item_by_doi"
SQLITE_PDF = "zotero_pdf_text.pre_write_checks.check_pdf_attachment"
DB = Path("unused.sqlite")


def _ok(result: object) -> JavaScriptResult:
    return JavaScriptResult(ok=True, result=result, error="", endpoint="http://x")


def _down(error: str = "debug-bridge unreachable: refused", timed_out: bool = False) -> JavaScriptResult:
    return JavaScriptResult(ok=False, result=None, error=error, endpoint="http://x", timed_out=timed_out)


def _embedded_sql(script: str) -> str:
    """The SQL literal the generated script passes to Zotero.DB.queryAsync."""
    start = script.index("queryAsync(") + len("queryAsync(")
    literal, _ = json.JSONDecoder().raw_decode(script[start:])
    return str(literal)


def _zotero_op(sql: str) -> str:
    """Zotero's own test (xpcom/db.js): rows are returned only when this is select/pragma."""
    match = re.match(r"^[^a-zA-Z]*[^ ]+", sql)
    assert match
    return match.group().lower()


MALFORMED = [
    _ok(None),
    _ok({"error": "unexpected"}),
    _ok({"rows": "nope"}),
    _ok({"rows": [], "extra": 1}),
    _ok({"rows": [{"unrelated": 1}]}),
    _ok({"status": "imported", "key": "ABCD1234"}),
    _ok("plain text"),
    _ok([]),
]


class TestDoiDuplicate:
    def test_live_match_does_not_touch_sqlite(self) -> None:
        rows = {"rows": [{"key": "AAAA1111", "doi_value": "https://doi.org/10.1000/Example"}]}
        with patch(BRIDGE, return_value=_ok(rows)), patch(SQLITE_DOI) as sqlite_path:
            result = check_doi_duplicate("10.1000/EXAMPLE", DB)
        assert result == {"key": "AAAA1111", "source": "debug_bridge", "live": True}
        sqlite_path.assert_not_called()

    def test_live_without_match_is_a_real_none(self) -> None:
        with patch(BRIDGE, return_value=_ok({"rows": []})), patch(SQLITE_DOI) as sqlite_path:
            result = check_doi_duplicate("10.1000/absent", DB)
        assert result == {"key": None, "source": "debug_bridge", "live": True}
        sqlite_path.assert_not_called()

    @pytest.mark.parametrize(
        "bridge_result",
        [_down(), _down("did not answer within 30 s", timed_out=True), _down("HTTP 401: bad token"), *MALFORMED],
    )
    def test_bridge_failure_falls_back_to_a_labelled_copy(self, bridge_result: JavaScriptResult) -> None:
        with patch(BRIDGE, return_value=bridge_result), patch(SQLITE_DOI, return_value="COPYKEY1") as sqlite_path:
            result = check_doi_duplicate("10.1000/example", DB)
        assert result["key"] == "COPYKEY1"
        assert result["source"] == "zotero_db_copy"
        assert result["live"] is False
        assert result["bridge_error"]
        sqlite_path.assert_called_once()

    def test_both_unavailable_fails_closed(self) -> None:
        with patch(BRIDGE, return_value=_down()), patch(SQLITE_DOI, side_effect=SnapshotUnstableError("moving")):
            with pytest.raises(PreWriteCheckUnavailable):
                check_doi_duplicate("10.1000/example", DB)

    def test_sqlite_errors_also_fail_closed(self) -> None:
        with patch(BRIDGE, return_value=_ok(None)), patch(SQLITE_DOI, side_effect=sqlite3.OperationalError("locked")):
            with pytest.raises(PreWriteCheckUnavailable):
                check_doi_duplicate("10.1000/example", DB)

    def test_generated_script_carries_the_constant_sql(self) -> None:
        with patch(BRIDGE, return_value=_ok({"rows": []})) as bridge:
            check_doi_duplicate("10.1000/example", DB)
        script = bridge.call_args.args[0]
        assert json.dumps(DOI_ROWS_SQL.strip()) in script
        assert _embedded_sql(script) == DOI_ROWS_SQL.strip()
        assert _zotero_op(_embedded_sql(script)) == "select"
        assert "Zotero.DB.queryAsync" in script
        assert "Array.isArray(rows)" in script


ESCAPING_ERRORS = [
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    http.client.IncompleteRead(b"par", 10),
    ConnectionResetError("reset"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
]


class TestEscapingTransportErrors:
    """Errors execute_javascript does not map must still reach the copy fallback, not a traceback."""

    @pytest.mark.parametrize("exc", ESCAPING_ERRORS, ids=lambda e: type(e).__name__)
    def test_doi_check_falls_back(self, exc: Exception) -> None:
        with patch(BRIDGE, side_effect=exc), patch(SQLITE_DOI, return_value=None):
            result = check_doi_duplicate("10.1000/example", DB)
        assert result["source"] == "zotero_db_copy" and result["live"] is False
        assert type(exc).__name__ in str(result["bridge_error"])

    @pytest.mark.parametrize("exc", ESCAPING_ERRORS, ids=lambda e: type(e).__name__)
    def test_pdf_check_falls_back(self, exc: Exception) -> None:
        copy = {"parent_key": "PARENTKY", "found": False, "attachments": []}
        with patch(BRIDGE, side_effect=exc), patch(SQLITE_PDF, return_value=copy):
            result = check_existing_pdf("PARENTKY", DB)
        assert result["source"] == "zotero_db_copy" and result["live"] is False

    def test_a_timeout_is_reported_plainly_not_as_a_possibly_running_write(self) -> None:
        runs_on = "the script may still be running in Zotero, so its outcome is unknown"
        with patch(BRIDGE, return_value=_down(runs_on, timed_out=True)), patch(SQLITE_DOI, return_value=None):
            result = check_doi_duplicate("10.1000/example", DB)
        assert result["bridge_error"] == "debug-bridge did not answer within 30 s"
        assert "rerunning" not in str(result["bridge_error"])

    def test_script_waits_for_an_open_transaction_before_reading(self) -> None:
        with patch(BRIDGE, return_value=_ok({"rows": []})) as bridge:
            check_doi_duplicate("10.1000/example", DB)
        script = bridge.call_args.args[0]
        assert script.index("Zotero.DB.waitForTransaction()") < script.index("Zotero.DB.queryAsync")

    def test_a_long_http_body_is_summarized(self) -> None:
        body = "HTTP 500: " + "x" * 5000
        with patch(BRIDGE, return_value=_down(body)), patch(SQLITE_DOI, return_value=None):
            result = check_doi_duplicate("10.1000/example", DB)
        assert len(str(result["bridge_error"])) <= 200


class TestExistingPdf:
    def test_live_rows_are_shaped_like_check_pdf(self) -> None:
        rows = {"rows": [{"attachment_key": "PDFKEY01", "path": None, "content_type": "application/pdf"}]}
        with patch(BRIDGE, return_value=_ok(rows)), patch(SQLITE_PDF) as sqlite_path:
            result = check_existing_pdf("PARENTKY", DB)
        assert result == {
            "parent_key": "PARENTKY",
            "found": True,
            "attachments": [{"key": "PDFKEY01", "path": "", "content_type": "application/pdf"}],
            "source": "debug_bridge",
            "live": True,
        }
        sqlite_path.assert_not_called()

    def test_live_empty_means_not_found(self) -> None:
        with patch(BRIDGE, return_value=_ok({"rows": []})):
            result = check_existing_pdf("PARENTKY", DB)
        assert result["found"] is False and result["live"] is True and result["attachments"] == []

    @pytest.mark.parametrize("bridge_result", [_down(), _down("timeout", timed_out=True), *MALFORMED])
    def test_bridge_failure_falls_back_to_a_labelled_copy(self, bridge_result: JavaScriptResult) -> None:
        copy = {"parent_key": "PARENTKY", "found": False, "attachments": []}
        with patch(BRIDGE, return_value=bridge_result), patch(SQLITE_PDF, return_value=copy):
            result = check_existing_pdf("PARENTKY", DB)
        assert result["found"] is False
        assert result["source"] == "zotero_db_copy"
        assert result["live"] is False
        assert result["bridge_error"]

    def test_both_unavailable_fails_closed(self) -> None:
        with patch(BRIDGE, return_value=_down()), patch(SQLITE_PDF, side_effect=SnapshotUnstableError("moving")):
            with pytest.raises(PreWriteCheckUnavailable):
                check_existing_pdf("PARENTKY", DB)

    def test_generated_script_has_constant_sql_and_escaped_key(self) -> None:
        hostile = 'A"B\'; return 1; //'
        with patch(BRIDGE, return_value=_ok({"rows": []})) as bridge:
            check_existing_pdf(hostile, DB)
        script = bridge.call_args.args[0]
        assert _embedded_sql(script) == PDF_ATTACHMENTS_SQL.strip()
        assert _zotero_op(_embedded_sql(script)) == "select"
        assert json.dumps([hostile]) in script
