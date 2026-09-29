"""Opt-in end-to-end test of import-doi / check-pdf / find-pdf against a running Zotero.

This WRITES to Zotero (it imports an item and attaches a PDF), so it is built to run only against a
separate Zotero profile and never in normal CI. See docs/live-zotero-test.md.

Skipped unless all of these hold:
* ``ZOTERO_LIVE_TEST=1``;
* ``ZOTERO_LIVE_TEST_CONFIG`` names an existing test config;
* the debug-bridge answers a read-only probe.

Refuses (fails, before any write) unless the running Zotero's data directory
(``Zotero.DataDirectory.dir``) equals the test config's ``zotero_data_directory``, and that directory
is not the one your normal config (``resolve_config_path()``) points at.

``ZOTERO_LIVE_RECORD=1`` additionally saves sanitized raw bridge payloads (placeholder keys, no
titles, no paths) into ``tests/fixtures/zotero_bridge/``, replacing the reconstructed fixtures.
Nothing is ever deleted; the created keys are printed.

The guard and sanitizer tests at the bottom of this module are offline and run in the normal suite.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from zotero_pdf_text.bibtex import execute_javascript
from zotero_pdf_text.config import load_config, resolve_config_path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "zotero_bridge"
RECORDER = REPO_ROOT / "tests" / "live_zotero_recorder.py"

# Open-access arXiv DOIs (DataCite); the first one not yet in the test library is imported.
LIVE_DOIS = (
    "10.48550/arXiv.1810.04805",
    "10.48550/arXiv.1706.03762",
    "10.48550/arXiv.1512.03385",
    "10.48550/arXiv.1412.6980",
    "10.48550/arXiv.1409.0473",
)

# Read-only. dataDirectory is used for the guard and never recorded; dst_dir is reduced to a flag.
PROBE_JS = """
var P = function (name) { return Zotero.Prefs.get('extensions.zotmoov.' + name, true); };
var A = Zotero.Attachments;
var names = {};
names['11'] = Zotero.ItemTypes.getName(11);
var preprint = Zotero.ItemTypes.getID('preprint');
if (preprint) { names[String(preprint)] = Zotero.ItemTypes.getName(preprint); }
return {
    zoteroVersion: Zotero.version,
    dataDirectory: Zotero.DataDirectory.dir,
    functions: {
        'Zotero.Attachments.addAvailableFile': typeof A.addAvailableFile,
        'Zotero.Attachments.addAvailablePDF': typeof A.addAvailablePDF,
        'Zotero.ItemTypes.getName': typeof Zotero.ItemTypes.getName,
        'Zotero.Items.exists': typeof Zotero.Items.exists,
        'Zotero.File.getExtension': typeof Zotero.File.getExtension,
        'Zotero.Promise.delay': typeof Zotero.Promise.delay
    },
    itemTypeNames: names,
    linkModes: {
        LINK_MODE_IMPORTED_FILE: A.LINK_MODE_IMPORTED_FILE,
        LINK_MODE_IMPORTED_URL: A.LINK_MODE_IMPORTED_URL,
        LINK_MODE_LINKED_FILE: A.LINK_MODE_LINKED_FILE
    },
    zotmoov: !!Zotero.ZotMoov,
    zotmoovPrefs: {
        enable_automove: P('enable_automove'),
        file_behavior: P('file_behavior'),
        dst_dir_set: !!P('dst_dir'),
        allowed_fileext: P('allowed_fileext'),
        auto_process_delay: P('auto_process_delay')
    }
};
"""

ZOTERO_KEY = re.compile(r"^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$")
PLACEHOLDER_KEYS = ("AAAA1111", "BBBB2222", "CCCC3333", "DDDD4444", "EEEE5555", "FFFF6666")
KEY_FIELDS = ("key", "keys", "originalKey", "attachmentKey", "item_key", "item_keys", "attachment_key", "parent_key")


def _same_dir(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _refusal(config_path: Path, env: dict[str, Any]) -> str | None:
    """Why writing would be unsafe, or None. Checked before any write."""
    config = load_config(config_path)
    # Guard 1: the Zotero answering on the bridge port is the profile the test config describes.
    running = str(env.get("dataDirectory") or "")
    if not running or not _same_dir(running, config.zotero_data_directory):
        return (
            "the running Zotero's data directory does not match the test config's "
            "zotero_data_directory. Close your normal Zotero and start the test profile "
            "(zotero -P <test profile>) before running this test."
        )
    # Guard 2: that directory is not the one your everyday config uses.
    saved = os.environ.pop("ZOTERO_PDF_TEXT_CONFIG", None)
    try:
        candidates = {resolve_config_path()}
    finally:
        if saved is not None:
            os.environ["ZOTERO_PDF_TEXT_CONFIG"] = saved
    candidates.add(resolve_config_path())
    for normal_path in sorted(candidates):
        if not normal_path.is_file():
            continue
        if _same_dir(normal_path, config_path):
            return (
                f"the test config is also your normally resolved config ({normal_path.name}). Point "
                "ZOTERO_LIVE_TEST_CONFIG at a separate test config and unset ZOTERO_PDF_TEXT_CONFIG "
                "for this run."
            )
        if _same_dir(load_config(normal_path).zotero_data_directory, config.zotero_data_directory):
            return (
                f"the test config's zotero_data_directory is the same one your normal config "
                f"({normal_path.name}) uses. The live test must run against a separate Zotero profile "
                "with its own data directory."
            )
    return None


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    if os.environ.get("ZOTERO_LIVE_TEST") != "1":
        pytest.skip("live Zotero test: set ZOTERO_LIVE_TEST=1 (see docs/live-zotero-test.md)")
    config_env = os.environ.get("ZOTERO_LIVE_TEST_CONFIG", "")
    if not config_env or not Path(config_env).is_file():
        pytest.skip("live Zotero test: ZOTERO_LIVE_TEST_CONFIG must name an existing test config")
    config_path = Path(config_env).resolve()

    probe = execute_javascript(PROBE_JS, timeout=15)
    if not probe.ok or not isinstance(probe.result, dict):
        pytest.skip(f"live Zotero test: debug-bridge did not answer the probe ({probe.error or probe.result!r})")
    env: dict[str, Any] = probe.result

    refusal = _refusal(config_path, env)
    if refusal:
        pytest.fail(f"REFUSING to write: {refusal} Nothing was written.", pytrace=False)
    return {"config_path": config_path, "env": env}


def _cli(config_path: Path, records: Path | None, *args: str) -> tuple[int, dict[str, Any], str]:
    command = [sys.executable, "-m", "zotero_pdf_text"] if records is None else [sys.executable, str(RECORDER), str(records)]
    run_env = dict(os.environ, ZOTERO_PDF_TEXT_CONFIG=str(config_path))
    completed = subprocess.run(
        [*command, *args, "--config", str(config_path)],
        capture_output=True, text=True, encoding="utf-8", env=run_env, timeout=180,
    )
    stream = completed.stdout if completed.stdout.strip() else completed.stderr
    try:
        payload = json.loads(stream)
    except json.JSONDecodeError:
        payload = {}
    return completed.returncode, payload, completed.stdout + completed.stderr


def _check_pdf_until_found(config_path: Path, key: str, attempts: int = 15) -> dict[str, Any]:
    # check-pdf reads live through debug-bridge, so it sees commits immediately; the retry only
    # covers the moment a ZotMoov auto-move takes to settle the attachment's final path.
    result: dict[str, Any] = {}
    for _ in range(attempts):
        _, result, _ = _cli(config_path, None, "check-pdf", "--key", key, "--json")
        if result.get("found"):
            return result
        time.sleep(2)
    return result


@pytest.mark.live_zotero
def test_import_doi_then_find_pdf_against_a_live_zotero_profile(live: dict[str, Any], tmp_path: Path) -> None:
    config_path: Path = live["config_path"]
    record = os.environ.get("ZOTERO_LIVE_RECORD") == "1"
    import_records = tmp_path / "import_records.json" if record else None
    find_records = tmp_path / "find_records.json" if record else None

    imported: dict[str, Any] | None = None
    doi = ""
    for doi in LIVE_DOIS:
        rc, out, text = _cli(config_path, import_records, "import-doi", "--doi", doi)
        assert rc == 0, f"import-doi failed for {doi}:\n{text}"
        if out.get("status") == "already_in_library":
            continue
        imported = out
        break
    if imported is None:
        pytest.skip("every test DOI is already in the test library -- reset the test profile")

    assert imported["status"] == "imported", imported
    item_key = str(imported["key"])
    print(f"\nlive Zotero test: imported {doi} as item {item_key}")
    assert re.fullmatch(r"[a-z][A-Za-z]+", str(imported["item_type"])), (
        f"item_type should be a type name, got {imported['item_type']!r}"
    )
    assert imported["key_source"] == "created_item", imported
    assert imported["keys"] == [item_key], imported

    _, before, text = _cli(config_path, None, "check-pdf", "--key", item_key, "--json")
    assert before.get("source") == "debug_bridge", (
        f"check-pdf did not read live, so 'no PDF yet' is not trustworthy (is debug-bridge reachable?):\n{text}"
    )
    assert before.get("found") is False, (
        f"item {item_key} already has a PDF before find-pdf, so find-pdf would add a second copy:\n{text}"
    )

    rc, found, text = _cli(config_path, find_records, "find-pdf", "--key", item_key)
    print(f"live Zotero test: find-pdf on {item_key}: {json.dumps(found)}")
    assert rc == 0, text
    assert found["outcome"] == "attached", found
    attachment_key = str(found["attachment_key"])
    assert attachment_key, found

    after = _check_pdf_until_found(config_path, item_key)
    assert after.get("found"), f"check-pdf never saw the PDF attached to {item_key}: {after}"
    assert [a["key"] for a in after["attachments"]] == [attachment_key], after
    print(f"live Zotero test: created item {item_key} with PDF attachment {attachment_key} (nothing deleted)")

    if record:
        assert import_records is not None and find_records is not None
        _record_fixtures(live["env"], doi, imported, import_records, found, find_records, after)


# ---------------------------------------------------------------------------
# Recorder: sanitized raw payloads into tests/fixtures/zotero_bridge/
# ---------------------------------------------------------------------------


def _collect_keys(value: object, keymap: dict[str, str], field: str = "") -> None:
    if isinstance(value, dict):
        for name, inner in value.items():
            _collect_keys(inner, keymap, name)
    elif isinstance(value, list):
        for inner in value:
            _collect_keys(inner, keymap, field)
    elif isinstance(value, str) and field in KEY_FIELDS and ZOTERO_KEY.match(value) and value not in keymap:
        if len(keymap) >= len(PLACEHOLDER_KEYS):
            raise AssertionError("more distinct keys than placeholders; extend PLACEHOLDER_KEYS")
        keymap[value] = PLACEHOLDER_KEYS[len(keymap)]


def _sanitize(value: object, keymap: dict[str, str], field: str = "") -> object:
    if isinstance(value, dict):
        return {name: _sanitize(inner, keymap, name) for name, inner in value.items() if name != "dataDirectory"}
    if isinstance(value, list):
        return [_sanitize(inner, keymap, field) for inner in value]
    if isinstance(value, str):
        if field == "title":
            return "<title withheld>"
        if field in ("path", "filePath"):
            return "attachments:<relative>" if value.startswith("attachments:") else "<path withheld>"
        return keymap.get(value, value)
    return value


def _assert_no_real_keys(value: object) -> None:
    text = json.dumps(value)
    for token in re.findall(r'"([23456789A-Z]{8})"', text):
        assert token in PLACEHOLDER_KEYS or not ZOTERO_KEY.match(token), f"an unmapped Zotero key would be recorded: {token}"


def _write_fixture(name: str, content: dict[str, Any]) -> None:
    _assert_no_real_keys(content)
    (FIXTURES / name).write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _record_fixtures(
    env: dict[str, Any],
    doi: str,
    imported: dict[str, Any],
    import_records: Path,
    found: dict[str, Any],
    find_records: Path,
    after: dict[str, Any],
) -> None:
    import_payload = json.loads(import_records.read_text(encoding="utf-8"))[-1]["result"]
    find_payload = json.loads(find_records.read_text(encoding="utf-8"))[-1]["result"]
    keymap: dict[str, str] = {}
    for value in (imported, import_payload, find_payload, found, after):
        _collect_keys(value, keymap)

    stamp = f"raw capture from the live test's recorder, {date.today().isoformat()}, Zotero {env.get('zoteroVersion')}"
    _write_fixture("zotero_env.json", {
        "_provenance": f"probe, captured by the live test's recorder, {date.today().isoformat()} "
                       "(read-only probe; dst_dir reduced to dst_dir_set)",
        **{name: value for name, value in env.items() if name != "dataDirectory"},
    })
    _write_fixture("import_doi_created_item.json", {
        "_provenance": f"{stamp}. Keys are placeholders, the title is withheld.",
        "doi": doi,
        "payload": _sanitize(import_payload, keymap),
        "expected": {
            "ok": True,
            "item_type": imported["item_type"],
            "item_key": keymap[imported["key"]],
            "item_keys": [keymap[k] for k in imported["keys"]],
            "outcome": "",
        },
        "expected_cli": _sanitize(
            {name: imported[name] for name in ("status", "item_type", "key", "key_source", "keys")}, keymap
        ),
    })
    _write_fixture("find_pdf_attached.json", {
        "_provenance": f"{stamp}. Keys are placeholders, paths withheld.",
        "item_key": keymap[imported["key"]],
        "payload": _sanitize(find_payload, keymap),
        "expected": _sanitize(
            {name: found[name] for name in ("ok", "found", "outcome", "moved", "attachment_key", "link_mode", "attachments")},
            keymap,
        ),
        "check_pdf_after": _sanitize(after, keymap),
    })
    print("live Zotero test: recorded sanitized fixtures into tests/fixtures/zotero_bridge/ -- review the diff before committing")


# ---------------------------------------------------------------------------
# Offline checks of the guards and the sanitizer (run in normal CI, touch no Zotero)
# ---------------------------------------------------------------------------


def _write_config(path: Path, data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "zotero_root": str(path.parent),
            "zotero_data_directory": str(data_dir),
            "linked_attachments": str(path.parent),
            "output_root": str(path.parent / "out"),
        }),
        encoding="utf-8",
    )
    return path


def test_guard_refuses_when_the_running_zotero_is_another_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZOTERO_PDF_TEXT_CONFIG", raising=False)
    test_config = _write_config(tmp_path / "live" / "test.json", tmp_path / "test-profile")

    refusal = _refusal(test_config, {"dataDirectory": str(tmp_path / "main-profile")})

    assert refusal is not None and "does not match" in refusal
    assert _refusal(test_config, {}) is not None


def test_guard_refuses_when_the_test_profile_is_the_normal_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZOTERO_PDF_TEXT_CONFIG", raising=False)
    shared = tmp_path / "main-profile"
    _write_config(tmp_path / "config.json", shared)
    test_config = _write_config(tmp_path / "live" / "test.json", shared)

    refusal = _refusal(test_config, {"dataDirectory": str(shared)})

    assert refusal is not None and "same one your normal config" in refusal


def test_guard_refuses_when_the_normal_config_env_var_is_the_test_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    test_config = _write_config(tmp_path / "live" / "test.json", tmp_path / "test-profile")
    monkeypatch.setenv("ZOTERO_PDF_TEXT_CONFIG", str(test_config))

    refusal = _refusal(test_config, {"dataDirectory": str(tmp_path / "test-profile")})

    assert refusal is not None and "also your normally resolved config" in refusal


def test_guard_allows_a_separate_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZOTERO_PDF_TEXT_CONFIG", raising=False)
    _write_config(tmp_path / "config.json", tmp_path / "main-profile")
    test_config = _write_config(tmp_path / "live" / "test.json", tmp_path / "test-profile")

    assert _refusal(test_config, {"dataDirectory": str(tmp_path / "test-profile")}) is None


def test_sanitizer_replaces_keys_and_drops_titles_and_paths() -> None:
    raw_import = {"key": "Q7XK2M9P", "keys": ["Q7XK2M9P"], "title": "A Real Title", "itemType": "preprint"}
    raw_find = {
        "found": True, "settled": True, "originalKey": "R4TN8W2Z", "attachmentKey": "H6JV3C5B", "linkMode": 2,
        "attachments": [{"key": "H6JV3C5B", "linkMode": 2, "contentType": "application/pdf"}],
    }
    after = {"parent_key": "Q7XK2M9P", "found": True,
             "attachments": [{"key": "H6JV3C5B", "path": "attachments:Author/file.pdf", "content_type": "application/pdf"}]}
    keymap: dict[str, str] = {}
    for value in (raw_import, raw_find, after):
        _collect_keys(value, keymap)

    assert keymap == {"Q7XK2M9P": "AAAA1111", "R4TN8W2Z": "BBBB2222", "H6JV3C5B": "CCCC3333"}
    clean = [_sanitize(v, keymap) for v in (raw_import, raw_find, after, {"dataDirectory": "x"})]
    text = json.dumps(clean)
    assert "A Real Title" not in text and "Author/file.pdf" not in text and "dataDirectory" not in text
    assert clean[2]["attachments"][0]["path"] == "attachments:<relative>"  # type: ignore[index]
    _assert_no_real_keys(clean)
    with pytest.raises(AssertionError):
        _assert_no_real_keys({"key": "Q7XK2M9P"})


def test_recorded_fixtures_keep_the_shape_the_offline_tests_read(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    from zotero_pdf_text.bibtex import JavaScriptResult, find_available_pdf_for_item, import_doi_via_connector

    monkeypatch.setattr(sys.modules[__name__], "FIXTURES", tmp_path)
    env = {"zoteroVersion": "9.9.9", "dataDirectory": "somewhere", "itemTypeNames": {"11": "conferencePaper"}}
    import_payload = {"key": "Q7XK2M9P", "keys": ["Q7XK2M9P"], "title": "A Real Title", "itemType": "preprint"}
    find_payload = {
        "found": True, "settled": True, "originalKey": "R4TN8W2Z", "attachmentKey": "H6JV3C5B", "linkMode": 2,
        "attachments": [{"key": "H6JV3C5B", "linkMode": 2, "contentType": "application/pdf"}],
    }
    import_records = tmp_path / "import.json"
    find_records = tmp_path / "find.json"
    import_records.write_text(json.dumps([{"ok": True, "result": import_payload}]), encoding="utf-8")
    find_records.write_text(json.dumps([{"ok": True, "result": find_payload}]), encoding="utf-8")
    bridge = lambda payload: JavaScriptResult(ok=True, result=payload, error="", endpoint="x")  # noqa: E731
    with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=bridge(find_payload)):
        found = find_available_pdf_for_item("Q7XK2M9P").to_dict()
    imported = {"status": "imported", "item_type": "preprint", "key": "Q7XK2M9P",
                "key_source": "created_item", "keys": ["Q7XK2M9P"]}
    after = {"parent_key": "Q7XK2M9P", "found": True,
             "attachments": [{"key": "H6JV3C5B", "path": "attachments:x.pdf", "content_type": "application/pdf"}]}

    _record_fixtures(env, "10.48550/arXiv.1810.04805", imported, import_records, found, find_records, after)

    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json") if p.name not in ("import.json", "find.json"))
    for secret in ("Q7XK2M9P", "R4TN8W2Z", "H6JV3C5B", "A Real Title", "somewhere", "x.pdf"):
        assert secret not in text
    imp = json.loads((tmp_path / "import_doi_created_item.json").read_text(encoding="utf-8"))
    with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=bridge(imp["payload"])):
        parsed = import_doi_via_connector(imp["doi"]).to_dict()
    assert {k: parsed[k] for k in imp["expected"]} == imp["expected"]
    fnd = json.loads((tmp_path / "find_pdf_attached.json").read_text(encoding="utf-8"))
    with patch("zotero_pdf_text.bibtex.execute_javascript", return_value=bridge(fnd["payload"])):
        parsed = find_available_pdf_for_item(fnd["item_key"]).to_dict()
    assert {k: parsed[k] for k in fnd["expected"]} == fnd["expected"]
    assert json.loads((tmp_path / "zotero_env.json").read_text(encoding="utf-8"))["_provenance"].startswith("probe, captured")
