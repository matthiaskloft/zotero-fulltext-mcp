"""link-pdf --url and import-doi --with-pdf, with the bridge and the download mocked (no network)."""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from zotero_pdf_text.bibtex import (
    FIND_PDF_TIMEOUT_SECONDS,
    _SETTLE_JS,
    ConnectorImportResult,
    FindPdfResult,
    JavaScriptResult,
    LinkPdfUrlResult,
    attach_pdf_from_url,
    find_available_pdf_for_item,
)
from zotero_pdf_text.cli import main
from zotero_pdf_text.pdf_fetch import FetchedPdf, PdfFetchError

PARENT = "AAAA1111"
URL = "https://papers.example/paper.pdf"
PDF_BYTES = b"%PDF-1.7\nfake\n%%EOF\n"
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()


def _ok(payload: object) -> JavaScriptResult:
    return JavaScriptResult(ok=True, result=payload, error="", endpoint="http://x")


def _timeout() -> JavaScriptResult:
    return JavaScriptResult(
        ok=False, result=None, error="debug-bridge did not answer within 90 s", endpoint="http://x", timed_out=True
    )


NO_ATTACHMENTS = _ok({"attachments": []})
HAS_PDF = _ok({"attachments": [{"key": "PDF00001", "linkMode": 2, "contentType": "application/pdf"}]})
HAS_HTML = _ok({"attachments": [{"key": "HTML0001", "linkMode": 1, "contentType": "text/html"}]})


class FakeFetch:
    """Stands in for fetch_pdf: writes PDF_BYTES into the staging dir and remembers it."""

    def __init__(self, error: str = "") -> None:
        self.error = error
        self.dest_dirs: list[Path] = []
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url: str, dest_dir: Path, max_bytes: int, *, file_name: str) -> FetchedPdf:
        self.dest_dirs.append(dest_dir)
        self.calls.append((url, max_bytes))
        if self.error:
            raise PdfFetchError(self.error)
        path = dest_dir / file_name
        path.write_bytes(PDF_BYTES)
        return FetchedPdf(path=path, size=len(PDF_BYTES), sha256=PDF_SHA, final_url=URL, content_type="application/pdf")


def _settled(final_path: Path | str, *, final_key: str = "CCCC3333", moved: bool = True) -> JavaScriptResult:
    return _ok({
        "found": True,
        "settled": True,
        "originalKey": "BBBB2222",
        "attachmentKey": final_key if moved else "BBBB2222",
        "linkMode": 2 if moved else 0,
        "path": str(final_path),
        "attachments": [{"key": final_key if moved else "BBBB2222", "linkMode": 2 if moved else 0,
                         "contentType": "application/pdf"}],
    })


def _attach(bridge_results: list[JavaScriptResult], fetch: FakeFetch | None = None, **kwargs: Any):
    fetch = fetch or FakeFetch()
    with (
        patch("zotero_pdf_text.bibtex.execute_javascript", side_effect=bridge_results) as bridge,
        patch("zotero_pdf_text.bibtex.fetch_pdf", side_effect=fetch),
    ):
        result = attach_pdf_from_url(PARENT, kwargs.pop("url", URL), **kwargs)
    return result, bridge, fetch


def test_attached_after_zotmoov_move_with_verified_hash(tmp_path: Path) -> None:
    final = tmp_path / "moved.pdf"
    final.write_bytes(PDF_BYTES)

    result, bridge, fetch = _attach([HAS_HTML, _settled(final)])

    assert result.ok is True
    assert result.outcome == "attached"
    assert result.attachment_key == result.key == "CCCC3333"
    assert result.moved is True
    assert result.link_mode == "linked_file"
    assert result.verified_hash is True
    assert result.sha256 == PDF_SHA
    assert result.size == len(PDF_BYTES)
    assert result.final_url == URL
    assert result.parent_key == PARENT
    assert "ZotMoov replaced" in result.message
    # The staging directory is gone and the import used the staged file with the bridge timeout.
    assert not fetch.dest_dirs[0].exists()
    import_js = bridge.call_args_list[1].args[0]
    assert "A.importFromFile(" in import_js
    assert str(fetch.dest_dirs[0] / f"{PARENT}.pdf").replace("\\", "\\\\") in import_js
    assert bridge.call_args_list[1].kwargs["timeout"] == FIND_PDF_TIMEOUT_SECONDS


def test_attached_without_move_keeps_stored_key(tmp_path: Path) -> None:
    final = tmp_path / "stored.pdf"
    final.write_bytes(PDF_BYTES)

    result, _, _ = _attach([NO_ATTACHMENTS, _settled(final, moved=False)])

    assert result.ok is True
    assert result.outcome == "attached"
    assert result.attachment_key == "BBBB2222"
    assert result.moved is False
    assert result.link_mode == "imported_file"
    assert result.message == ""


def test_hash_mismatch_after_attaching_is_reported_not_retried(tmp_path: Path) -> None:
    final = tmp_path / "moved.pdf"
    final.write_bytes(b"%PDF-1.7 something else")

    result, bridge, _ = _attach([NO_ATTACHMENTS, _settled(final)])

    assert result.ok is False
    assert result.outcome == "attached"
    assert result.verified_hash is False
    assert "does not match" in result.error
    assert "do not rerun" in result.message
    assert bridge.call_count == 2


def test_unreadable_final_file_is_not_verified(tmp_path: Path) -> None:
    result, _, _ = _attach([NO_ATTACHMENTS, _settled(tmp_path / "missing.pdf")])

    assert result.ok is False
    assert result.verified_hash is False
    assert "could not read" in result.error


def test_existing_pdf_is_refused_before_downloading() -> None:
    result, bridge, fetch = _attach([HAS_PDF])

    assert result.ok is False
    assert result.outcome == "refused_existing_pdf"
    assert result.existing_pdfs == [{"key": "PDF00001", "link_mode": "linked_file", "content_type": "application/pdf"}]
    assert "--allow-additional" in result.message
    assert fetch.calls == []
    assert bridge.call_count == 1


def test_allow_additional_proceeds_despite_existing_pdf(tmp_path: Path) -> None:
    final = tmp_path / "moved.pdf"
    final.write_bytes(PDF_BYTES)

    result, bridge, _ = _attach([HAS_PDF, _settled(final)], allow_additional=True)

    assert result.outcome == "attached"
    assert "!true" in bridge.call_args_list[1].args[0]  # the in-script re-check is disabled too


def test_pdf_added_between_precheck_and_import_is_refused_in_script() -> None:
    raced = _ok({"refused": True, "attachments": [{"key": "PDF00001", "linkMode": 0, "contentType": "application/pdf"}]})

    result, _, fetch = _attach([NO_ATTACHMENTS, raced])

    assert result.outcome == "refused_existing_pdf"
    assert result.message.startswith("Nothing was attached.")
    assert not fetch.dest_dirs[0].exists()


def test_bridge_timeout_during_import_is_unknown_and_not_retried() -> None:
    result, bridge, fetch = _attach([NO_ATTACHMENTS, _timeout()])

    assert result.ok is False
    assert result.outcome == "unknown"
    assert "Do not rerun link-pdf" in result.message
    assert f"check-pdf --key {PARENT}" in result.message
    assert result.sha256 == PDF_SHA
    assert bridge.call_count == 2
    assert not fetch.dest_dirs[0].exists()


def test_unsettled_import_reports_no_key() -> None:
    unsettled = _ok({
        "found": True, "settled": False, "originalKey": "BBBB2222", "attachmentKey": "", "linkMode": None,
        "path": "", "attachments": [{"key": "BBBB2222", "linkMode": 0, "contentType": "application/pdf"}],
    })

    result, _, _ = _attach([NO_ATTACHMENTS, unsettled])

    assert result.ok is True
    assert result.outcome == "unsettled"
    assert result.attachment_key == ""
    assert "Do not rerun link-pdf" in result.message


def test_download_failure_attaches_nothing_and_removes_staging() -> None:
    fetch = FakeFetch(error="the response is not a PDF")

    result, bridge, _ = _attach([NO_ATTACHMENTS], fetch=fetch)

    assert result.outcome == "error"
    assert "not a PDF" in result.error
    assert bridge.call_count == 1
    assert not fetch.dest_dirs[0].exists()


@pytest.mark.parametrize("url", ["http://papers.example/paper.pdf", "https://u:p@example.org/paper.pdf"])
def test_refused_url_never_reaches_the_bridge(url: str) -> None:
    result, bridge, fetch = _attach([], url=url)

    assert result.outcome == "error"
    assert bridge.call_count == 0
    assert fetch.calls == []


def test_precheck_failure_is_an_error_without_download() -> None:
    failed = JavaScriptResult(ok=False, result=None, error="debug-bridge unreachable", endpoint="http://x")

    result, _, fetch = _attach([failed])

    assert result.outcome == "error"
    assert "pre-check" in result.error
    assert fetch.calls == []


def test_find_pdf_and_link_url_share_one_settle_implementation(tmp_path: Path) -> None:
    captured: list[str] = []

    def capture(code: str, **_: object) -> JavaScriptResult:
        captured.append(code)
        return NO_ATTACHMENTS if len(captured) == 1 else _timeout()

    with patch("zotero_pdf_text.bibtex.execute_javascript", side_effect=capture), \
            patch("zotero_pdf_text.bibtex.fetch_pdf", side_effect=FakeFetch()):
        attach_pdf_from_url(PARENT, URL)
    with patch("zotero_pdf_text.bibtex.execute_javascript", side_effect=capture):
        find_available_pdf_for_item(PARENT)

    assert _SETTLE_JS in captured[1]
    assert _SETTLE_JS in captured[2]


# ---------------------------------------------------------------------------
# CLI: link-pdf --url and import-doi --with-pdf
# ---------------------------------------------------------------------------


def _config(root: Path) -> Path:
    (root / "data").mkdir()
    (root / "data" / "zotero.sqlite").write_bytes(b"")
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps({
            "zotero_root": str(root),
            "zotero_data_directory": str(root / "data"),
            "linked_attachments": str(root),
            "output_root": str(root / "out"),
        }),
        encoding="utf-8",
    )
    return config_path


def _url_result(outcome: str = "attached", ok: bool = True) -> LinkPdfUrlResult:
    return LinkPdfUrlResult(
        ok=ok, key="CCCC3333", attachment_key="CCCC3333", path="", moved=True, warning="", error="",
        endpoint="http://x", outcome=outcome, parent_key=PARENT, source_url=URL,
    )


def test_link_pdf_file_and_url_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["link-pdf", "--key", PARENT, "--file", "a.pdf", "--url", URL])
    assert exc.value.code == 2


def test_link_pdf_url_options_are_refused_with_file() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["link-pdf", "--key", PARENT, "--file", "a.pdf", "--allow-additional"])
    assert exc.value.code == 2


def test_link_pdf_url_passes_cap_and_flags() -> None:
    output = io.StringIO()
    with patch("zotero_pdf_text.cli.attach_pdf_from_url", return_value=_url_result()) as attach, redirect_stdout(output):
        rc = main(["link-pdf", "--key", PARENT, "--url", URL, "--max-mb", "1.5", "--allow-additional"])

    assert rc == 0
    assert attach.call_args.args == (PARENT, URL)
    assert attach.call_args.kwargs["max_bytes"] == int(1.5 * 1024 * 1024)
    assert attach.call_args.kwargs["allow_additional"] is True
    assert json.loads(output.getvalue())["outcome"] == "attached"


def test_link_pdf_url_default_cap_is_200_mb() -> None:
    with patch("zotero_pdf_text.cli.attach_pdf_from_url", return_value=_url_result("unknown", ok=False)) as attach, \
            redirect_stdout(io.StringIO()):
        rc = main(["link-pdf", "--key", PARENT, "--url", URL])

    assert rc == 1
    assert attach.call_args.kwargs["max_bytes"] == 200 * 1024 * 1024
    assert attach.call_args.kwargs["allow_additional"] is False


def _find(outcome: str, ok: bool = True) -> FindPdfResult:
    return FindPdfResult(
        ok=ok, key=PARENT, found=outcome in {"attached", "unsettled"},
        attachment_key="CCCC3333" if outcome == "attached" else "", error="", endpoint="http://x", outcome=outcome,
    )


def _import_with_pdf(
    tmp_path: Path,
    *,
    extra_args: list[str],
    existing_key: str | None = None,
    import_result: ConnectorImportResult | None = None,
    attachments: JavaScriptResult = NO_ATTACHMENTS,
    find: FindPdfResult | None = None,
    url_result: LinkPdfUrlResult | None = None,
):
    import_result = import_result or ConnectorImportResult(
        ok=True, doi="10.1000/example", item_type="journalArticle", title="A Paper", error="",
        connector_endpoint="http://x", item_key=PARENT, item_keys=[PARENT],
    )
    config_path = _config(tmp_path)
    output = io.StringIO()
    with (
        patch("zotero_pdf_text.cli.check_doi_duplicate", return_value={"key": existing_key, "source": "debug_bridge", "live": True}),
        patch("zotero_pdf_text.cli.import_doi_via_connector", return_value=import_result),
        patch("zotero_pdf_text.cli.find_item_key_via_connector", return_value=None),
        patch("zotero_pdf_text.bibtex.execute_javascript", return_value=attachments),
        patch("zotero_pdf_text.cli.find_available_pdf_for_item", return_value=find or _find("attached")) as find_mock,
        patch("zotero_pdf_text.cli.attach_pdf_from_url", return_value=url_result or _url_result()) as url_mock,
        patch("time.sleep"),
        redirect_stdout(output),
    ):
        rc = main(["import-doi", "--doi", "10.1000/example", "--config", str(config_path), *extra_args])
    return rc, json.loads(output.getvalue()), find_mock, url_mock


def test_import_doi_output_is_unchanged_without_with_pdf(tmp_path: Path) -> None:
    rc, report, find_mock, _ = _import_with_pdf(tmp_path, extra_args=[])

    assert rc == 0
    assert set(report) == {"status", "doi", "title", "item_type", "key", "key_source", "keys", "duplicate_check"}
    find_mock.assert_not_called()


def test_import_doi_pdf_url_requires_with_pdf(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _import_with_pdf(tmp_path, extra_args=["--pdf-url", URL])
    assert exc.value.code == 2


def test_with_pdf_created_item_then_find_pdf_attached(tmp_path: Path) -> None:
    rc, report, find_mock, url_mock = _import_with_pdf(tmp_path, extra_args=["--with-pdf", "--pdf-url", URL])

    assert rc == 0
    assert report["status"] == "imported"
    assert report["key"] == PARENT
    assert report["key_source"] == "created_item"
    assert report["pdf_outcome"] == "attached"
    assert [step["step"] for step in report["steps"]] == ["check-existing-pdf", "find-pdf"]
    assert find_mock.call_args.args == (PARENT,)
    url_mock.assert_not_called()


def test_with_pdf_not_found_falls_back_to_the_url(tmp_path: Path) -> None:
    rc, report, _, url_mock = _import_with_pdf(
        tmp_path, extra_args=["--with-pdf", "--pdf-url", URL], find=_find("not_found")
    )

    assert rc == 0
    assert report["pdf_outcome"] == "attached"
    assert [step["step"] for step in report["steps"]] == ["check-existing-pdf", "find-pdf", "link-pdf --url"]
    assert url_mock.call_args.args == (PARENT, URL)


def test_with_pdf_not_found_without_url_suggests_link_pdf_url(tmp_path: Path) -> None:
    rc, report, _, url_mock = _import_with_pdf(tmp_path, extra_args=["--with-pdf"], find=_find("not_found"))

    assert rc == 0
    assert report["pdf_outcome"] == "not_found"
    assert f"link-pdf --key {PARENT} --url" in report["pdf_message"]
    url_mock.assert_not_called()


@pytest.mark.parametrize("outcome, ok, rc", [("unsettled", True, 0), ("unknown", False, 1), ("error", False, 1)])
def test_with_pdf_stops_after_an_uncertain_find_pdf(tmp_path: Path, outcome: str, ok: bool, rc: int) -> None:
    code, report, _, url_mock = _import_with_pdf(
        tmp_path, extra_args=["--with-pdf", "--pdf-url", URL], find=_find(outcome, ok=ok)
    )

    assert code == rc
    assert report["pdf_outcome"] == outcome
    url_mock.assert_not_called()


def test_with_pdf_url_step_outcome_becomes_pdf_outcome(tmp_path: Path) -> None:
    rc, report, _, _ = _import_with_pdf(
        tmp_path, extra_args=["--with-pdf", "--pdf-url", URL], find=_find("not_found"),
        url_result=_url_result("unknown", ok=False),
    )

    assert rc == 1
    assert report["pdf_outcome"] == "unknown"


@pytest.mark.parametrize(
    "import_result",
    [
        ConnectorImportResult(ok=True, doi="10.1000/example", item_type="", title="", error="",
                              connector_endpoint="http://x", item_keys=["NEWITEM1", "NEWITEM2"]),
        ConnectorImportResult(ok=True, doi="10.1000/example", item_type="", title="", error="",
                              connector_endpoint="http://x"),
    ],
    ids=["ambiguous", "no-key"],
)
def test_with_pdf_needs_a_certain_key(tmp_path: Path, import_result: ConnectorImportResult) -> None:
    rc, report, find_mock, url_mock = _import_with_pdf(
        tmp_path, extra_args=["--with-pdf", "--pdf-url", URL], import_result=import_result
    )

    assert rc == 1
    assert report["pdf_outcome"] == "skipped_no_key"
    assert report["steps"] == []
    find_mock.assert_not_called()
    url_mock.assert_not_called()


def test_with_pdf_runs_for_an_item_already_in_the_library(tmp_path: Path) -> None:
    rc, report, find_mock, _ = _import_with_pdf(tmp_path, extra_args=["--with-pdf"], existing_key="OLDITEM1")

    assert rc == 0
    assert report["status"] == "already_in_library"
    assert report["key"] == "OLDITEM1"
    assert report["key_source"] == "already_in_library"
    assert find_mock.call_args.args == ("OLDITEM1",)


def test_already_in_library_output_is_unchanged_without_with_pdf(tmp_path: Path) -> None:
    rc, report, _, _ = _import_with_pdf(tmp_path, extra_args=[], existing_key="OLDITEM1")

    assert rc == 0
    assert report == {
        "status": "already_in_library",
        "doi": "10.1000/example",
        "key": "OLDITEM1",
        "duplicate_check": {"source": "debug_bridge", "live": True},
    }


def test_with_pdf_skips_an_item_that_already_has_a_pdf(tmp_path: Path) -> None:
    rc, report, find_mock, _ = _import_with_pdf(
        tmp_path, extra_args=["--with-pdf"], existing_key="OLDITEM1", attachments=HAS_PDF
    )

    assert rc == 0
    assert report["pdf_outcome"] == "already_has_pdf"
    assert report["steps"][0]["outcome"] == "has_pdf"
    find_mock.assert_not_called()
