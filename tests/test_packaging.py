"""Console-script registration check, independent of pytest's `pythonpath` sys.path override.

`pyproject.toml` sets `[tool.pytest.ini_options] pythonpath = ["src"]`, so every other test
imports `zotero_pdf_text` straight from the source tree regardless of what was actually built and
installed. Entry points come from installed dist-info instead, so this is the one check that
would catch a broken `[project.scripts]` entry or a `[tool.hatch.build.targets.wheel]` packages
mapping that silently drops the console scripts.
"""

from __future__ import annotations

import importlib.metadata as metadata
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_console_scripts_registered() -> None:
    entry_points = {ep.name for ep in metadata.entry_points(group="console_scripts")}
    assert {"zotero-pdf-text", "zotero-fulltext-mcp"} <= entry_points


def test_release_metadata_and_install_instructions_stay_in_sync() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_project = next(package for package in lock["package"] if package["name"] == project["name"])
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")

    assert locked_project["version"] == project["version"]
    assert f"@v{project['version']}" in readme
    assert f"{project['name']}[mcp] @ git+https://" in readme
    assert "#egg=" not in readme
    assert f"version: {project['version']}" in citation
    assert (ROOT / project["readme"]).is_file()
    assert (ROOT / project["license"]["file"]).is_file()


def test_legacy_requirements_mcp_range_matches_authoritative_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    mcp_requirement = project["optional-dependencies"]["mcp"][0]
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert re.fullmatch(r"mcp>=\d+(?:\.\d+)*,<\d+", mcp_requirement)
    assert mcp_requirement in requirements
