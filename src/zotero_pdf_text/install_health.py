"""Read-only preflight for the installed package itself, as opposed to the configured library.

Two upgrade hazards are invisible until they bite: editable-install metadata keeps the version it
had at install time while the source checkout moves on, and on Windows pip cannot replace a
console-script executable that a running MCP client keeps alive. Nothing here stops processes or
modifies the environment; it only reports what a reinstall would run into.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

DIST_NAME = "zotero-fulltext-mcp"
SERVER_EXECUTABLE = "zotero-fulltext-mcp.exe"


@dataclass(frozen=True)
class InstallVersionStatus:
    installed_version: str | None
    editable: bool
    source_dir: Path | None = None
    source_version: str | None = None
    installer: str | None = None

    @property
    def stale(self) -> bool:
        return (
            self.editable
            and self.source_version is not None
            and self.installed_version != self.source_version
        )


def install_version_status(dist: metadata.Distribution | None = None) -> InstallVersionStatus:
    """Compare installed metadata with the source checkout it was installed from, if editable.

    Only the checkout recorded in the install's own `direct_url.json` is consulted, never the
    working directory, so a pinned non-editable install next to a checkout at another version
    is not reported as stale.
    """
    if dist is None:
        try:
            dist = metadata.distribution(DIST_NAME)
        except metadata.PackageNotFoundError:
            return InstallVersionStatus(installed_version=None, editable=False)
    installer = (dist.read_text("INSTALLER") or "").strip() or None
    source_dir = _editable_source_dir(dist)
    if source_dir is None:
        return InstallVersionStatus(dist.version, editable=False, installer=installer)
    return InstallVersionStatus(
        dist.version,
        editable=True,
        source_dir=source_dir,
        source_version=_source_version(source_dir),
        installer=installer,
    )


def _editable_source_dir(dist: metadata.Distribution) -> Path | None:
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        direct_url = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(direct_url, dict) or not (direct_url.get("dir_info") or {}).get("editable"):
        return None
    parsed = urlparse(str(direct_url.get("url", "")))
    if parsed.scheme != "file":
        return None
    # A UNC checkout (\\server\share) keeps its host in netloc; dropping it loses the share.
    if parsed.netloc and parsed.netloc != "localhost":
        return Path(url2pathname(f"//{parsed.netloc}{parsed.path}"))
    return Path(url2pathname(parsed.path))


def _source_version(source_dir: Path) -> str | None:
    """Read the version the checkout declares, only if the checkout is still this project."""
    try:
        with (source_dir / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if project.get("name") != DIST_NAME:
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def reinstall_command(status: InstallVersionStatus, extras: list[str]) -> str:
    """A reinstall into this interpreter's environment that adds nothing and removes nothing.

    `uv sync` is deliberately not suggested: it targets the checkout's own `.venv`, which need not
    be the stale environment, and removes packages (such as the test extra) it wasn't told about.
    """
    extras_suffix = f"[{','.join(extras)}]" if extras else ""
    target = f'"{status.source_dir}{extras_suffix}"'
    if status.installer == "uv":
        return f"uv pip install --python {_command_path(sys.executable)} -e {target}"
    return f"{_command_path(sys.executable, leading=True)} -m pip install -e {target}"


def _command_path(path: str, *, leading: bool = False) -> str:
    """Quote a path only when needed; PowerShell runs a quoted leading path only after `&`."""
    if not any(char in path for char in " &()'\""):
        return path
    return f'& "{path}"' if leading and sys.platform == "win32" else f'"{path}"'


def running_server_count() -> int | None:
    """Count running MCP server executables on Windows; None where it doesn't apply or can't tell.

    Only Windows locks a running console-script executable against replacement, so no other
    platform is checked.
    """
    if sys.platform != "win32":
        return None
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {SERVER_EXECUTABLE}", "/FO", "CSV", "/NH"],
            # Bytes, not text: the no-match INFO line is localized in the OEM code page and does
            # not decode as UTF-8/cp1252 on e.g. German Windows. Only the ASCII rows matter.
            capture_output=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # A match prints one quoted CSV row per process; no match prints a localized INFO line.
    prefix = f'"{SERVER_EXECUTABLE.lower()}"'.encode("ascii")
    return sum(1 for line in (completed.stdout or b"").splitlines() if line.strip().lower().startswith(prefix))
