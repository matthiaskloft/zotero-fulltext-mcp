"""The Zotero bridge code against recorded reality.

Two layers, both driven by ``tests/fixtures/zotero_bridge/``:

* Python: recorded debug-bridge payloads go through a mocked ``execute_javascript`` into the
  parsers, so a change in what Zotero returns (or in how it is parsed) shows up as a fixture diff.
* JavaScript: the script ``find_available_pdf_for_item`` actually generates runs in Node against a
  fake ``Zotero`` seeded from the recorded environment (prefs exactly as recorded, including
  ``allowed_fileext`` as a JSON string) with a fake ZotMoov that performs the auto-move. Mocking
  ``execute_javascript`` alone never executes that script, so this is the only offline check that
  it runs and waits correctly. Skipped when ``node`` is not on PATH.

Each fixture's ``_provenance`` says whether it is a raw capture or reconstructed from parsed
output; ``tests/test_live_zotero.py`` (``ZOTERO_LIVE_RECORD=1``) replaces the reconstructed ones.
"""

from __future__ import annotations

import copy
import io
import json
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from zotero_pdf_text.bibtex import (
    FIND_PDF_SETTLE_CAP_SECONDS,
    FIND_PDF_SETTLE_MARGIN_SECONDS,
    FIND_PDF_TIMEOUT_SECONDS,
    JavaScriptResult,
    find_available_pdf_for_item,
    import_doi_via_connector,
)
from zotero_pdf_text.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "zotero_bridge"
HARNESS = FIXTURES / "fake_zotero.js"
NODE = shutil.which("node")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _bridge(payload: object) -> JavaScriptResult:
    return JavaScriptResult(ok=True, result=payload, error="", endpoint="http://x")


def _subset(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    return {name: actual.get(name) for name in expected}


# ---------------------------------------------------------------------------
# Python level: recorded payloads through the parsers
# ---------------------------------------------------------------------------


def test_every_fixture_states_its_provenance() -> None:
    for path in sorted(FIXTURES.glob("*.json")):
        provenance = json.loads(path.read_text(encoding="utf-8")).get("_provenance", "")
        assert provenance.startswith(("probe, captured", "reconstructed", "raw capture")), path.name


def test_import_doi_parses_the_recorded_payload() -> None:
    fixture = _fixture("import_doi_created_item.json")
    with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=_bridge(fixture["payload"])):
        result = import_doi_via_connector(fixture["doi"])

    assert _subset(result.to_dict(), fixture["expected"]) == fixture["expected"]
    assert not result.item_type.isdigit(), "item_type must be a type name, not an itemTypeID"


def test_import_doi_cli_reports_the_recorded_payload(tmp_path: Path) -> None:
    fixture = _fixture("import_doi_created_item.json")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "zotero.sqlite").write_bytes(b"")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "zotero_root": str(tmp_path),
            "zotero_data_directory": str(tmp_path / "data"),
            "linked_attachments": str(tmp_path),
            "output_root": str(tmp_path / "out"),
        }),
        encoding="utf-8",
    )
    output = io.StringIO()
    with (
        patch("zotero_pdf_text.cli.check_doi_duplicate", return_value={"key": None, "source": "debug_bridge", "live": True}),
        patch("zotero_pdf_text.bibtex.execute_javascript", return_value=_bridge(fixture["payload"])),
        patch("zotero_pdf_text.cli.find_item_key_via_connector") as lookup,
        redirect_stdout(output),
    ):
        rc = main(["import-doi", "--doi", fixture["doi"], "--config", str(config_path)])

    assert rc == 0
    lookup.assert_not_called()
    reported = json.loads(output.getvalue())
    assert _subset(reported, fixture["expected_cli"]) == fixture["expected_cli"]


def test_find_pdf_parses_the_recorded_payload() -> None:
    fixture = _fixture("find_pdf_attached.json")
    with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=_bridge(fixture["payload"])):
        result = find_available_pdf_for_item(fixture["item_key"])

    assert _subset(result.to_dict(), fixture["expected"]) == fixture["expected"]


def test_find_pdf_fixture_key_is_the_single_pdf_check_pdf_saw_afterwards() -> None:
    fixture = _fixture("find_pdf_attached.json")
    after = fixture["check_pdf_after"]
    assert [a["key"] for a in after["attachments"]] == [fixture["expected"]["attachment_key"]]
    if fixture["expected"]["link_mode"] == "linked_file":
        # ZotMoov stores the moved file relative to the linked-attachments base directory.
        assert after["attachments"][0]["path"].startswith("attachments:")


# ---------------------------------------------------------------------------
# JavaScript level: the generated find-pdf script in Node against a fake Zotero
# ---------------------------------------------------------------------------

PARENT, STORED, LINKED, OTHER = "AAAA1111", "BBBB2222", "CCCC3333", "DDDD4444"
DOWNLOAD_MS = 3000
PDF = {"key": STORED, "linkMode": 1, "contentType": "application/pdf", "path": "/fake/storage/BBBB2222/paper.pdf"}


def _generated_find_pdf_js(key: str) -> str:
    captured: dict[str, str] = {}

    def capture(code: str, **_: object) -> JavaScriptResult:
        captured["code"] = code
        return JavaScriptResult(ok=False, result=None, error="captured", endpoint="http://x")

    with patch("zotero_pdf_text.bibtex.execute_javascript", side_effect=capture):
        find_available_pdf_for_item(key)
    return captured["code"]


def _scenario(**overrides: Any) -> dict[str, Any]:
    scenario: dict[str, Any] = {
        "env": _fixture("zotero_env.json"),
        "parent": {"key": PARENT, "itemTypeID": 11},
        # A pre-existing non-PDF attachment must not be mistaken for the new file.
        "existingAttachments": [
            {"key": OTHER, "linkMode": 1, "contentType": "text/html", "path": "/fake/storage/DDDD4444/page.html"}
        ],
        "found": True,
        "attachment": dict(PDF),
        "downloadMs": DOWNLOAD_MS,
        "movedKey": LINKED,
        "zotmoovStalls": False,
    }
    scenario.update(overrides)
    return scenario


def _run_in_node(tmp_path: Path, scenario: dict[str, Any], script: str) -> dict[str, Any]:
    assert NODE is not None
    scenario_path = tmp_path / "scenario.json"
    script_path = tmp_path / "script.js"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(HARNESS), str(scenario_path), str(script_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert completed.returncode == 0, f"node failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def _find_pdf_in_node(tmp_path: Path, scenario: dict[str, Any]):
    run = _run_in_node(tmp_path, scenario, _generated_find_pdf_js(PARENT))
    with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=_bridge(run["response"])):
        result = find_available_pdf_for_item(PARENT)
    return result, run


needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


@needs_node
def test_node_zotmoov_move_is_awaited_and_new_key_reported(tmp_path: Path) -> None:
    result, run = _find_pdf_in_node(tmp_path, _scenario())

    assert result.outcome == "attached"
    assert result.moved is True
    assert result.attachment_key == LINKED
    assert result.link_mode == "linked_file"
    assert [a["key"] for a in result.attachments] == [LINKED]
    # The reported key is the one the library holds at the end, and the download is gone.
    assert {c["key"] for c in run["children"]} == {OTHER, LINKED}
    # Settled right after ZotMoov's auto_process_delay, well before the deadline.
    delay = _fixture("zotero_env.json")["zotmoovPrefs"]["auto_process_delay"]
    assert DOWNLOAD_MS + delay <= run["elapsedMs"] < DOWNLOAD_MS + delay + 1000


@needs_node
def test_node_empty_dst_dir_attaches_immediately_with_original_key(tmp_path: Path) -> None:
    result, run = _find_pdf_in_node(tmp_path, _scenario(prefOverrides={"dst_dir_set": False}))

    assert result.outcome == "attached"
    assert result.moved is False
    assert result.attachment_key == STORED
    assert result.link_mode == "imported_url"
    assert run["elapsedMs"] == DOWNLOAD_MS


@needs_node
def test_node_extension_outside_allowed_fileext_attaches_immediately(tmp_path: Path) -> None:
    html_page = dict(PDF, contentType="text/html", path="/fake/storage/BBBB2222/paper.html")
    result, run = _find_pdf_in_node(tmp_path, _scenario(attachment=html_page))

    assert result.outcome == "attached"
    assert result.moved is False
    assert result.attachment_key == STORED
    assert run["elapsedMs"] == DOWNLOAD_MS


@needs_node
def test_node_already_linked_attachment_attaches_immediately(tmp_path: Path) -> None:
    linked = dict(PDF, linkMode=2)
    result, run = _find_pdf_in_node(tmp_path, _scenario(attachment=linked))

    assert result.outcome == "attached"
    assert result.moved is False
    assert result.attachment_key == STORED
    assert result.link_mode == "linked_file"
    assert run["elapsedMs"] == DOWNLOAD_MS


@needs_node
def test_node_predicted_move_that_never_happens_is_unsettled(tmp_path: Path) -> None:
    result, run = _find_pdf_in_node(tmp_path, _scenario(zotmoovStalls=True))

    assert result.outcome == "unsettled"
    assert result.attachment_key == ""
    assert [a["key"] for a in result.attachments] == [STORED]
    delay = _fixture("zotero_env.json")["zotmoovPrefs"]["auto_process_delay"]
    deadline = DOWNLOAD_MS + delay + FIND_PDF_SETTLE_MARGIN_SECONDS * 1000
    assert deadline <= run["elapsedMs"] < deadline + 1000


@needs_node
def test_node_long_auto_process_delay_is_capped_inside_the_bridge_timeout(tmp_path: Path) -> None:
    result, run = _find_pdf_in_node(tmp_path, _scenario(prefOverrides={"auto_process_delay": 600000}))

    assert result.outcome == "unsettled"
    assert run["elapsedMs"] < FIND_PDF_SETTLE_CAP_SECONDS * 1000 + 1000
    assert FIND_PDF_SETTLE_CAP_SECONDS < FIND_PDF_TIMEOUT_SECONDS


@needs_node
def test_node_no_file_found_is_not_found(tmp_path: Path) -> None:
    result, run = _find_pdf_in_node(tmp_path, _scenario(found=False))

    assert result.outcome == "not_found"
    assert result.found is False
    assert result.ok is True
    assert [c["key"] for c in run["children"]] == [OTHER]


@needs_node
def test_node_generated_import_js_reports_the_item_type_name(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def capture(code: str, **_: object) -> JavaScriptResult:
        captured["code"] = code
        return JavaScriptResult(ok=False, result=None, error="captured", endpoint="http://x")

    with (
        patch("zotero_pdf_text.bibtex.execute_javascript", side_effect=capture),
        patch("zotero_pdf_text.bibtex._fetch_doi_metadata", side_effect=RuntimeError("offline")),
    ):
        import_doi_via_connector("10.48550/arXiv.1810.04805")

    scenario = copy.deepcopy(_scenario(parent=None, existingAttachments=[]))
    scenario["translate"] = {
        "translators": [{"label": "fake"}],
        "items": [{"key": PARENT, "itemTypeID": 11, "title": "<title withheld>"}],
    }
    run = _run_in_node(tmp_path, scenario, captured["code"])
    with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=_bridge(run["response"])):
        result = import_doi_via_connector("10.48550/arXiv.1810.04805")

    assert result.ok is True
    assert result.item_type == scenario["env"]["itemTypeNames"]["11"]
    assert result.item_key == PARENT
    assert result.item_keys == [PARENT]


# ---------------------------------------------------------------------------
# JavaScript level: link-pdf --url (pre-check + importFromFile + the shared settle wait) in Node
# ---------------------------------------------------------------------------

URL = "https://papers.example/paper.pdf"
PDF_BYTES = b"%PDF-1.7\nfake pdf for the node harness\n%%EOF\n"


class _FakePdfServer:
    """fetch_pdf's resolver and connection factory: one public host serving PDF_BYTES."""

    def resolver(self, host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    def connect(self, host: str, port: int, address: str, timeout: float) -> "_FakePdfServer":
        self._sent = False
        return self

    def request(self, method: str, url: str, body: None = None, headers: object = None) -> None:
        pass

    def getresponse(self) -> "_FakePdfServer":
        return self

    status = 200

    def getheader(self, name: str) -> str | None:
        return "application/pdf" if name.lower() == "content-type" else None

    def read(self, amt: int | None = None) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return PDF_BYTES

    def close(self) -> None:
        pass


def _link_url_in_node(tmp_path: Path, scenario: dict[str, Any], **kwargs: Any):
    from zotero_pdf_text import pdf_fetch
    from zotero_pdf_text.bibtex import attach_pdf_from_url

    server = _FakePdfServer()
    staged: list[Path] = []

    def fetch(url: str, dest_dir: Path, max_bytes: int, **options: Any):
        staged.append(dest_dir)
        return pdf_fetch.fetch_pdf(
            url, dest_dir, max_bytes, resolver=server.resolver, connection_factory=server.connect, **options
        )

    runs: list[dict[str, Any]] = []

    def bridge(code: str, **_: object) -> JavaScriptResult:
        run = _run_in_node(tmp_path, scenario, code)
        runs.append(run)
        return _bridge(run["response"])

    with (
        patch("zotero_pdf_text.bibtex.execute_javascript", side_effect=bridge),
        patch("zotero_pdf_text.bibtex.fetch_pdf", side_effect=fetch),
    ):
        result = attach_pdf_from_url(PARENT, URL, **kwargs)
    return result, runs, staged


def _url_scenario(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    storage = tmp_path / "storage"
    zotmoov = tmp_path / "zotmoov"
    storage.mkdir()
    zotmoov.mkdir()
    scenario = _scenario(
        importKey=STORED, storageDir=str(storage), prefOverrides={"dst_dir": str(zotmoov)}, attachment=None,
    )
    scenario.update(overrides)
    return scenario


@needs_node
def test_node_link_url_awaits_zotmoov_move_and_verifies_the_moved_file(tmp_path: Path) -> None:
    result, runs, staged = _link_url_in_node(tmp_path, _url_scenario(tmp_path))

    assert result.outcome == "attached"
    assert result.ok is True
    assert result.moved is True
    assert result.attachment_key == LINKED
    assert result.link_mode == "linked_file"
    assert result.verified_hash is True
    assert Path(result.path).parent == tmp_path / "zotmoov"
    assert Path(result.path).read_bytes() == PDF_BYTES
    assert {c["key"] for c in runs[-1]["children"]} == {OTHER, LINKED}
    delay = _fixture("zotero_env.json")["zotmoovPrefs"]["auto_process_delay"]
    assert delay <= runs[-1]["elapsedMs"] < delay + 1000
    assert not staged[0].exists()


@needs_node
def test_node_link_url_without_zotmoov_move_keeps_the_stored_copy(tmp_path: Path) -> None:
    scenario = _url_scenario(tmp_path, prefOverrides={"dst_dir_set": False}, autoRename=True)
    result, runs, staged = _link_url_in_node(tmp_path, scenario)

    assert result.outcome == "attached"
    assert result.moved is False
    assert result.attachment_key == STORED
    assert result.link_mode == "imported_file"
    assert result.verified_hash is True
    assert Path(result.path) == tmp_path / "storage" / STORED / f"Renamed from {PARENT}.pdf"
    assert runs[-1]["elapsedMs"] == 0
    assert not staged[0].exists()


@needs_node
def test_node_link_url_predicted_move_that_never_happens_is_unsettled(tmp_path: Path) -> None:
    result, runs, _ = _link_url_in_node(tmp_path, _url_scenario(tmp_path, zotmoovStalls=True))

    assert result.outcome == "unsettled"
    assert result.attachment_key == ""
    assert result.verified_hash is False
    assert [a["key"] for a in result.attachments] == [STORED]
    delay = _fixture("zotero_env.json")["zotmoovPrefs"]["auto_process_delay"]
    deadline = delay + FIND_PDF_SETTLE_MARGIN_SECONDS * 1000
    assert deadline <= runs[-1]["elapsedMs"] < deadline + 1000


@needs_node
def test_node_link_url_refuses_an_item_that_already_has_a_pdf(tmp_path: Path) -> None:
    existing_pdf = {"key": OTHER, "linkMode": 2, "contentType": "application/pdf", "path": "/fake/linked/old.pdf"}
    result, runs, staged = _link_url_in_node(tmp_path, _url_scenario(tmp_path, existingAttachments=[existing_pdf]))

    assert result.outcome == "refused_existing_pdf"
    assert [a["key"] for a in result.existing_pdfs] == [OTHER]
    assert len(runs) == 1  # the read-only pre-check only
    assert staged == []
