"""Console-script registration check, independent of pytest's `pythonpath` sys.path override.

`pyproject.toml` sets `[tool.pytest.ini_options] pythonpath = ["src"]`, so every other test
imports `zotero_pdf_text` straight from the source tree regardless of what was actually built and
installed. Entry points come from installed dist-info instead, so this is the one check that
would catch a broken `[project.scripts]` entry or a `[tool.hatch.build.targets.wheel]` packages
mapping that silently drops the console scripts.
"""

from __future__ import annotations

import importlib.metadata as metadata


def test_console_scripts_registered() -> None:
    entry_points = {ep.name for ep in metadata.entry_points(group="console_scripts")}
    assert {"zotero-pdf-text", "zotero-fulltext-mcp"} <= entry_points
