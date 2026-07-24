from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ImageOcrSettings:
    """Where to reach the local OCR model that reads already-extracted figure PNGs.

    Nested under an optional ``image_ocr`` object rather than flattened onto ProjectConfig:
    these are all one subsystem's settings, and every existing config file predates them, so
    the whole block has to default cleanly when absent.
    """

    host: str = "localhost"
    port: int = 11434
    model: str = "glm-ocr:q8_0"
    per_image_timeout_seconds: int = 120
    # Enrichment is written to a sibling file named "<stem><enriched_suffix>.md" and the original
    # is never modified. Set to "" to overwrite the original in place instead (no safety copy).
    enriched_suffix: str = "_ocr_eq"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


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
    image_ocr: ImageOcrSettings = field(default_factory=ImageOcrSettings)

    @property
    def zotero_sqlite(self) -> Path:
        return self.zotero_data_directory / "zotero.sqlite"


def load_config(path: Path) -> ProjectConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
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
        image_ocr=_load_image_ocr(data.get("image_ocr")),
    )


def _load_image_ocr(data: object) -> ImageOcrSettings:
    """Build ImageOcrSettings from an optional config block, falling back per-key."""
    if not isinstance(data, dict):
        return ImageOcrSettings()
    defaults = ImageOcrSettings()
    return ImageOcrSettings(
        host=str(data.get("host", defaults.host)),
        port=int(data.get("port", defaults.port)),
        model=str(data.get("model", defaults.model)),
        per_image_timeout_seconds=int(
            data.get("per_image_timeout_seconds", defaults.per_image_timeout_seconds)
        ),
        enriched_suffix=str(data.get("enriched_suffix", defaults.enriched_suffix)),
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
