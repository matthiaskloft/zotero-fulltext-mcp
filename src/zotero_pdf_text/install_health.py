"""Read-only preflight for the installed package itself, as opposed to the configured library.

Two upgrade hazards are invisible until they bite: editable-install metadata keeps the version it
had at install time while the source checkout moves on, and on Windows pip cannot replace a
console-script executable that a running MCP client keeps alive. Nothing here stops processes or
modifies the environment; it only reports what a reinstall would run into.
"""

from __future__ import annotations

import json
import re
import shlex
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
    source_project: str | None = None

    @property
    def source_is_other_project(self) -> bool:
        return self.source_project is not None and not _is_this_project(self.source_project)

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
    source_project, source_version = _source_project(source_dir)
    return InstallVersionStatus(
        dist.version,
        editable=True,
        source_dir=source_dir,
        # A checkout that now holds another project says nothing about this install's version.
        source_version=source_version if source_project and _is_this_project(source_project) else None,
        installer=installer,
        source_project=source_project,
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


def _is_this_project(name: str) -> bool:
    """Compare project names the way packaging does (PEP 503): case and `-_.` runs are equal."""
    return re.sub(r"[-_.]+", "-", name).lower() == DIST_NAME


def _source_project(source_dir: Path) -> tuple[str | None, str | None]:
    """Read the project name and version the checkout declares; (None, None) if unreadable."""
    try:
        with (source_dir / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    # ValueError covers TOMLDecodeError and a pyproject.toml that is not UTF-8.
    except (OSError, ValueError):
        return None, None
    if not isinstance(project, dict):
        return None, None
    name, version = project.get("name"), project.get("version")
    return (name if isinstance(name, str) else None), (version if isinstance(version, str) else None)


def reinstall_command(status: InstallVersionStatus, extras: list[str]) -> str:
    """A reinstall into this interpreter's environment that adds nothing and removes nothing.

    `uv sync` is deliberately not suggested: it targets the checkout's own `.venv`, which need not
    be the stale environment, and removes packages (such as the test extra) it wasn't told about.
    """
    extras_suffix = f"[{','.join(extras)}]" if extras else ""
    target = _shell_word(f"{status.source_dir}{extras_suffix}")
    if status.installer == "uv":
        return f"uv pip install --python {_shell_word(sys.executable)} -e {target}"
    return f"{_shell_word(sys.executable, leading=True)} -m pip install -e {target}"


_SAFE_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/\\-")


def _shell_word(value: str, *, leading: bool = False) -> str:
    """Quote for PowerShell on Windows and for a POSIX shell elsewhere, only when needed.

    Single quotes keep `$`, backticks and `[` literal in both shells. PowerShell runs a quoted
    command path only after the call operator `&`.
    """
    if value and all(char in _SAFE_WORD_CHARS for char in value):
        return value
    if sys.platform == "win32":
        quoted = "'" + value.replace("'", "''") + "'"
        return f"& {quoted}" if leading else quoted
    return shlex.quote(value)


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
