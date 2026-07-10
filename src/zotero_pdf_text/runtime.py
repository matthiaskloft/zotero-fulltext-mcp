from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


def _default_zotero_exe() -> Path:
    """Best-effort default Zotero executable path for the current OS.

    Only the Windows path has been verified against a real install. The macOS and
    Linux paths are the conventional install locations but untested — override with
    --zotero-exe if Zotero lives somewhere else on your machine.
    """
    if os.name == "nt":
        return Path(r"C:\Program Files\Zotero\zotero.exe")
    if sys.platform == "darwin":
        return Path("/Applications/Zotero.app/Contents/MacOS/zotero")
    return Path("/usr/lib/zotero/zotero")


def _default_process_name() -> str:
    if os.name == "nt":
        return "zotero.exe"
    return "zotero"


DEFAULT_ZOTERO_EXE = _default_zotero_exe()
DEFAULT_PROCESS_NAME = _default_process_name()
ZOTERO_CONNECTOR_PING = "http://127.0.0.1:23119/connector/ping"


@dataclass(frozen=True)
class ZoteroRuntimeStatus:
    zotero_exe: str
    running: bool
    launched: bool
    connector_ok: bool
    connector_message: str
    troubleshooting: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def is_zotero_running(
    *,
    process_name: str = DEFAULT_PROCESS_NAME,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    if os.name == "nt":
        result = run_process(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return process_name.casefold() in (result.stdout or "").casefold()

    result = run_process(["pgrep", "-f", process_name], capture_output=True, text=True, check=False)
    return result.returncode == 0


def probe_zotero_connector(
    *,
    url: str = ZOTERO_CONNECTOR_PING,
    timeout_seconds: float = 1.0,
) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            body = response.read(200).decode("utf-8", errors="replace").strip()
        return True, body or f"HTTP {response.status}"
    except urllib.error.URLError as exc:
        return False, f"{type(exc.reason).__name__ if hasattr(exc, 'reason') else type(exc).__name__}: {exc}"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def ensure_zotero_running(
    *,
    zotero_exe: Path = DEFAULT_ZOTERO_EXE,
    wait_seconds: int = 15,
    launch: bool = True,
    require_connector: bool = False,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    connector_probe: Callable[[], tuple[bool, str]] | None = None,
) -> ZoteroRuntimeStatus:
    if wait_seconds < 0:
        raise ValueError("wait_seconds must be non-negative")

    troubleshooting: list[str] = []
    launched = False
    running = is_zotero_running(run_process=run_process)

    if not running and not launch:
        troubleshooting.append("Zotero is not running and launch was disabled.")
        return _status(zotero_exe, False, False, False, "not checked", troubleshooting)

    if not running:
        if not zotero_exe.exists():
            troubleshooting.append(f"Zotero executable was not found: {zotero_exe}")
            return _status(zotero_exe, False, False, False, "not checked", troubleshooting)
        popen([str(zotero_exe)])
        launched = True

    deadline = time.monotonic() + wait_seconds
    while True:
        running = is_zotero_running(run_process=run_process)
        connector_ok, connector_message = _probe(connector_probe)
        if running and (connector_ok or not require_connector):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    if not running:
        troubleshooting.append("Zotero did not appear in the process list before the timeout.")
    if not connector_ok:
        troubleshooting.extend(
            [
                "Zotero's local connector endpoint did not respond.",
                "Open Zotero once manually if Windows is blocking first launch.",
                "If the Zotero MCP still reports connection refused, restart stale pyzotero-mcp processes.",
            ]
        )

    return _status(zotero_exe, running, launched, connector_ok, connector_message, troubleshooting)


def _probe(connector_probe: Callable[[], tuple[bool, str]] | None) -> tuple[bool, str]:
    if connector_probe is None:
        return probe_zotero_connector()
    return connector_probe()


def _status(
    zotero_exe: Path,
    running: bool,
    launched: bool,
    connector_ok: bool,
    connector_message: str,
    troubleshooting: list[str],
) -> ZoteroRuntimeStatus:
    return ZoteroRuntimeStatus(
        zotero_exe=str(zotero_exe),
        running=running,
        launched=launched,
        connector_ok=connector_ok,
        connector_message=connector_message,
        troubleshooting=troubleshooting,
    )
