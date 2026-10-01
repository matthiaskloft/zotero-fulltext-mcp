"""Run real Zotero and the tests in an isolated Linux display/profile."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from zotero_pdf_text.bibtex import execute_javascript

ROOT = Path("/work")
PROFILE = ROOT / "profile"
CONFIG = ROOT / "config.live-test.json"


def configure() -> None:
    for folder in (PROFILE / "extensions", ROOT / "data", ROOT / "linked", ROOT / "converted_text"):
        folder.mkdir(parents=True, exist_ok=True)
    for plugin in Path("/opt/plugins").glob("*.xpi"):
        with zipfile.ZipFile(plugin) as archive:
            metadata = json.loads(archive.read("manifest.json"))
        plugin_id = metadata["applications"]["zotero"]["id"]
        shutil.copy2(plugin, PROFILE / "extensions" / f"{plugin_id}.xpi")
    token_path = ROOT / "bridge-token.txt"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        token_path.chmod(0o600)
    os.environ["ZOTERO_DEBUG_BRIDGE_TOKEN"] = token_path.read_text(encoding="utf-8")
    prefs = {
        "extensions.zotero.useDataDir": True,
        "extensions.zotero.dataDir": str(ROOT / "data"),
        "extensions.zotero.baseAttachmentPath": str(ROOT / "linked"),
        "extensions.zotero.saveRelativeAttachmentPath": True,
        "extensions.zotero.sync.autoSync": False,
        "extensions.zotero.httpServer.port": 23119,
        "extensions.zotero.debug-bridge.token": os.environ["ZOTERO_DEBUG_BRIDGE_TOKEN"],
        "extensions.zotero.firstRun2": False,
        "extensions.zotero.firstRunGuidance": False,
        "extensions.zotero.automaticScraperUpdates": False,
        "extensions.zotmoov.dst_dir": str(ROOT / "linked"),
        "extensions.zotmoov.enable_automove": True,
        "extensions.zotmoov.file_behavior": "move",
        "extensions.zotmoov.enable_subdir_move": False,
        "extensions.zotmoov.auto_process_delay": 1000,
        "extensions.autoDisableScopes": 0,
        "extensions.enabledScopes": 15,
        "app.update.auto": False,
        "app.update.enabled": False,
    }
    (PROFILE / "user.js").write_text(
        "".join(f"user_pref({json.dumps(key)}, {json.dumps(value)});\n" for key, value in prefs.items()),
        encoding="utf-8",
    )
    CONFIG.write_text(json.dumps({
        "zotero_root": str(ROOT), "zotero_data_directory": str(ROOT / "data"),
        "linked_attachments": str(ROOT / "linked"), "output_root": str(ROOT / "converted_text"),
    }, indent=2) + "\n", encoding="utf-8")
    # Do not inherit a host config: the normal-config guard must stay independent of the test config.
    os.environ.pop("ZOTERO_PDF_TEXT_CONFIG", None)
    os.environ.update({
        "ZOTERO_LIVE_TEST": "1", "ZOTERO_LIVE_TEST_CONFIG": str(CONFIG),
        "ZOTERO_LIVE_PIPELINE": "1", "ZOTERO_LIVE_CONTAINER": "1",
    })


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    if mode not in {"test", "network", "serve"}:
        raise SystemExit("Usage: run.py [test|network|serve]")
    configure()
    processes: list[subprocess.Popen] = []

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        processes.append(subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp"]))
        for _ in range(50):
            if Path("/tmp/.X11-unix/X99").exists():
                break
            time.sleep(0.1)
        log_path = ROOT / "zotero.log"
        with log_path.open("w", encoding="utf-8") as log:
            processes.append(subprocess.Popen(
                ["/opt/zotero/zotero", "-no-remote", "-profile", str(PROFILE)], stdout=log, stderr=log,
            ))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if processes[-1].poll() is not None:
                raise RuntimeError("Zotero exited during startup; inspect /work/zotero.log")
            probe = execute_javascript(
                "return {dataDirectory: Zotero.DataDirectory.dir, zotmoov: !!Zotero.ZotMoov, version: Zotero.version};",
                timeout=2,
            )
            if probe.ok and isinstance(probe.result, dict) and probe.result.get("zotmoov"):
                if Path(probe.result["dataDirectory"]).resolve() != (ROOT / "data").resolve():
                    raise RuntimeError("Zotero is using an unexpected data directory; refusing tests")
                print(f"Ready: Zotero {probe.result['version']}, isolated test data, debug-bridge and ZotMoov", flush=True)
                break
            time.sleep(1)
        else:
            raise RuntimeError("Zotero plugins were not ready within 120 seconds; inspect /work/zotero.log")
        if mode == "serve":
            return processes[-1].wait()
        tests = ["tests/test_live_zotero_container.py"] if mode == "test" else ["tests/test_live_zotero.py", "-m", "live_zotero"]
        return subprocess.call([sys.executable, "-m", "pytest", *tests, "-v", "-s", "-p", "no:cacheprovider", *sys.argv[2:]])
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
