from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfig:
    zotero_root: Path
    zotero_data_directory: Path
    linked_attachments: Path
    output_root: Path
    early_pages: int = 3
    max_page_chars: int = 12000
    manually_accepted_attachment_keys: frozenset[str] = frozenset()
    manually_accepted_mappings: frozenset[tuple[str, str]] = frozenset()

    @property
    def zotero_sqlite(self) -> Path:
        return self.zotero_data_directory / "zotero.sqlite"


def load_config(path: Path) -> ProjectConfig:
    """Load a machine-specific config, layered on top of a sibling ``config.shared.json``.

    ``config.shared.json`` (if present next to ``path``) holds settings that are the same
    across machines — ``early_pages``, ``max_page_chars``, ``manually_accepted_mappings``,
    etc. — so they live in one place instead of being copy-pasted into every machine's
    config file. Keys in ``path`` override the shared file; ``path`` is expected to hold
    only the genuinely machine-specific paths.
    """
    data: dict = {}
    shared_path = path.parent / "config.shared.json"
    if shared_path.exists() and shared_path != path:
        data.update(json.loads(shared_path.read_text(encoding="utf-8")))
    data.update(json.loads(path.read_text(encoding="utf-8")))
    return ProjectConfig(
        zotero_root=Path(data["zotero_root"]),
        zotero_data_directory=Path(data["zotero_data_directory"]),
        linked_attachments=Path(data["linked_attachments"]),
        output_root=Path(data["output_root"]),
        early_pages=int(data.get("early_pages", 3)),
        max_page_chars=int(data.get("max_page_chars", 12000)),
        manually_accepted_attachment_keys=frozenset(data.get("manually_accepted_attachment_keys", [])),
        manually_accepted_mappings=frozenset(
            (item["attachment_key"], item["source_name"]) for item in data.get("manually_accepted_mappings", [])
        ),
    )


def resolve_config_path(base_dir: Path | None = None) -> Path:
    """Resolve the machine-appropriate config file without any per-user hardcoding.

    Resolution order:
    1. ``ZOTERO_PDF_TEXT_CONFIG`` env var, if set.
    2. ``config.<hostname>.json`` next to the default ``config.json``, if it exists — this
       replaces hand-maintained files like the old ``config_lenovo.json`` with a name each
       machine picks automatically.
    3. ``config.json``.
    """
    env = os.environ.get("ZOTERO_PDF_TEXT_CONFIG")
    if env:
        return Path(env)
    base = base_dir if base_dir is not None else Path.cwd()
    machine_specific = base / f"config.{platform.node()}.json"
    if machine_specific.exists():
        return machine_specific
    return base / "config.json"


def validate_config(config: ProjectConfig) -> None:
    checks = {
        "zotero_root": config.zotero_root,
        "zotero_data_directory": config.zotero_data_directory,
        "linked_attachments": config.linked_attachments,
        "zotero.sqlite": config.zotero_sqlite,
    }
    missing = [f"{name}: {path}" for name, path in checks.items() if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required paths:\n" + "\n".join(missing))
