from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path

from .config import load_config, resolve_config_path
from .fts import connect_readonly
from .mcp_contract import PublicMcpError, create_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zotero-fulltext-mcp")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite FTS database path. Defaults to ZOTERO_FULLTEXT_DB or the resolved config's index path.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional project config. Required only for reconvert_with_math_ocr or when --db is omitted.",
    )
    parser.add_argument("--enable-bibtex", action="store_true", help="Expose the local Better BibTeX export integration.")
    parser.add_argument(
        "--bibtex-endpoint",
        default=None,
        help="Credential-free HTTP endpoint on Zotero's local Better BibTeX port; requires --enable-bibtex.",
    )
    args = parser.parse_args(argv)

    config = None
    if args.config is not None:
        config = _load_server_config(args.config)
    if args.db is None:
        if config is None:
            config = _load_server_config(resolve_config_path())
        db_path = Path(os.environ.get("ZOTERO_FULLTEXT_DB", str(config.output_root / "index" / "zotero_text_index.sqlite")))
    else:
        db_path = args.db

    if args.bibtex_endpoint is not None and not args.enable_bibtex:
        raise SystemExit(_startup_error("invalid_bibtex_endpoint", "--bibtex-endpoint requires --enable-bibtex."))
    _validate_startup_database(db_path)
    try:
        mcp = create_server(
            db_path,
            config=config,
            enable_bibtex=args.enable_bibtex,
            **({"bibtex_endpoint": args.bibtex_endpoint} if args.bibtex_endpoint else {}),
        )
    except PublicMcpError as exc:
        raise SystemExit(_startup_error(exc.code, exc.message)) from exc
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise SystemExit(
            _startup_error(
                "mcp_dependency_missing",
                "The optional MCP dependency is not installed. Install the project MCP extra before running the server.",
            )
        ) from exc

    logging.getLogger("mcp").setLevel(logging.WARNING)
    mcp.run()
    return 0


def _load_server_config(path: Path):
    try:
        return load_config(path)
    except (FileNotFoundError, KeyError, ValueError, OSError) as exc:
        raise SystemExit(_startup_error("config_unavailable", "A readable project config is required for this startup mode.")) from exc


def _validate_startup_database(db_path: Path) -> None:
    if not db_path.is_file():
        raise SystemExit(_startup_error("database_unavailable", "The selected local full-text index is unavailable."))
    try:
        connection = connect_readonly(db_path)
        try:
            connection.execute("PRAGMA schema_version").fetchone()
        finally:
            connection.close()
    except (sqlite3.DatabaseError, OSError, FileNotFoundError) as exc:
        raise SystemExit(_startup_error("database_unavailable", "The selected local full-text index is unavailable.")) from exc


def _startup_error(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": {"code": code, "message": message}})


if __name__ == "__main__":
    raise SystemExit(main())
