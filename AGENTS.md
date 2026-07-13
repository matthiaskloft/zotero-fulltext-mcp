# AGENTS.md

Guidance for agents working in this repository.

## Project Shape

This is a standalone, publishable project: a read-only-by-default pipeline that converts a
Zotero library's linked PDF attachments to Markdown, builds a sidecar full-text search index,
and exposes it through an MCP server (`zotero-fulltext-mcp`) plus a CLI (`zotero-pdf-text`).

- Source package: `src/zotero_pdf_text`.
- Tests: `tests`.
- Docs: `docs` (architecture, data dictionary, ingestion, operations, troubleshooting,
  debug-bridge setup).
- User-facing install/setup instructions: `README.md`.

**This repository never contains anyone's actual Zotero data.** A researcher's converted
Markdown, FTS index, linked PDFs, and Zotero SQLite database all live in a separate location of
their own choosing, referenced only through a config file. `config.example.json` is the only
config file that belongs in this repo; real configs (`config.json`, `config.<hostname>.json`)
are gitignored and must never be committed.

## Configuration Contract

Do not invent a new config system — reuse what exists in `src/zotero_pdf_text/config.py`:

- `ProjectConfig` (frozen dataclass): `zotero_root`, `zotero_data_directory`,
  `linked_attachments`, `output_root`, plus tuning fields.
- `resolve_config_path()` resolution order: `ZOTERO_PDF_TEXT_CONFIG` env var →
  `config.<hostname>.json` (via `platform.node()`) next to cwd → `config.json`.
- `validate_config()` checks that all referenced paths exist on disk.

Because this repo's code and a researcher's config/data are expected to live in different
locations (the code is a git clone, the data is wherever they keep their Zotero workspace), do
not assume the config file lives next to the source tree. Prefer explicit `--config`/`--db` CLI
arguments or the `ZOTERO_PDF_TEXT_CONFIG` env var over cwd-relative assumptions. The
`install-mcp` CLI command (`cli.py:_install_mcp`) already generates MCP client registrations with
both `--db` and `--config` baked in explicitly for exactly this reason — follow that pattern in
any new code path that launches the server or CLI programmatically.

## Data Safety Rules

Any path resolved from a config file (`zotero_data_directory`, `linked_attachments`,
`output_root`, and the derived `zotero_sqlite`) is someone else's live data, not a fixture in
this repo. Treat it accordingly:

- Never move, delete, rename, or bulk-rewrite files under these paths unless a task explicitly
  asks for that operation.
- Keep Zotero database access read-only. The opt-in `reconvert_with_math_ocr` MCP capability
  intentionally writes only to converted-text output (Markdown + extracted images + index), requires explicit
  startup enablement/config, and is documented as such — don't add other write paths without
  equally clear capability separation, documentation, and user awareness.
- Never hardcode a real absolute path (a personal home directory, a specific hostname, a real
  library size or paper count) into source, docstrings, or the MCP server's `instructions`
  string — those are read by everyone who installs this project, not just the original author.
  Prefer resolving such facts at runtime (e.g. via `coverage_report()`) or describing them
  qualitatively.

## Development Commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

If the virtual environment is unavailable, use Python 3.11+ and install dependencies from
`pyproject.toml` (`pip install -e .[mcp,test]` for MCP support plus pytest, add
`[zotero-write]`/`[marker]` as needed).

The reproducible install path is `uv sync --extra mcp --extra test --locked` followed by
`uv run pytest -q` — this resolves the exact dependency versions pinned in `uv.lock`, the same
versions CI tests against on Windows, macOS, and Linux. Run `uv lock` to regenerate `uv.lock`
after changing `pyproject.toml` dependencies; commit the updated lockfile in the same change.

## Release Convention

1. Update `CHANGELOG.md`: move relevant `[Unreleased]` entries under a new `## [X.Y.Z] - YYYY-MM-DD`
   heading, and add the corresponding link reference at the bottom of the file.
2. Bump `version` in `pyproject.toml` to match.
3. Run `uv lock` if dependencies changed since the last release; commit the updated `uv.lock`.
4. Once merged to `master` and CI is green, tag the release and push the tag:
   `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. Anyone not actively developing this project should install a pinned tag rather than `HEAD` —
   see README's install section for the exact command.

## Implementation Guidelines

- Keep paths `pathlib.Path`-based and cross-platform where practical. Zotero executable
  detection (`runtime.py`) and process-name checks are Windows-verified; macOS/Linux defaults
  are best-effort and documented as such — don't silently assume Windows elsewhere.
- Preserve manifest, report, and index schemas when changing conversion behavior.
- For long-running conversion commands, support resume behavior and avoid rerunning completed
  PDFs unnecessarily.
- For new CLI commands or MCP tools, update `README.md` (the user-facing entry point for anyone
  installing this project fresh).
- Keep unverified filename-based matches clearly separated from verified Zotero
  attachment-path matches when touching matching logic.

## Companion MCP Server

This project's MCP server (`zotero-fulltext-mcp`) is deliberately scoped to offline full-text
search over the sidecar index — it does not talk to Zotero's live API for collections, tags, or
notes. Do not add that functionality here; it duplicates the maintained official Zotero MCP
server, which is the intended companion for live-metadata access. See `README.md` for how the
two are meant to be paired.

## Verification

For source changes, run the focused pytest suite first. If changes touch CLI or MCP-tool
behavior, exercise the relevant command against a real (local, non-committed) config and inspect
actual output rather than assuming success from a clean exit code.
