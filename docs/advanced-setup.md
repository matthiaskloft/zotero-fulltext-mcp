# Advanced setup and maintenance

For the recommended first-time setup, start with the [README](../README.md).
The examples below use `$python` for the installed environment's Python interpreter;
replace example config paths with your own absolute path.

## Configure

From a checkout, copy the [template](../config.example.json) to your private workspace and fill in your own absolute paths:

```powershell
Copy-Item config.example.json C:\path\to\workspace\config.json
```

Edit the copied `config.json`. Examples using `.\config.json` below assume you are in that workspace; otherwise pass its absolute path:

- `zotero_root` — the existing workspace folder for this project's data; reports and converted files go under `output_root`.
- `zotero_data_directory` — **your local, non-synced** Zotero data directory (where
  `zotero.sqlite` lives). Never point this at a cloud-synced folder (Dropbox, Nextcloud,
  OneDrive, etc.) — see `architecture.md` for why.
- `linked_attachments` — the folder your Zotero library links PDFs from
  (`baseAttachmentPath` in Zotero's settings).
- `output_root` — where converted Markdown and the search index get written.

If you run this from more than one machine, name each machine's config
`config.<hostname>.json` (Python's `platform.node()`) instead of hand-maintaining separate
files — `zotero-pdf-text` picks the right one automatically. You can also point at any config
file explicitly via the `ZOTERO_PDF_TEXT_CONFIG` environment variable, which takes priority over
both the hostname-based file and the plain `config.json` fallback. This is the mechanism to use
when your config/data live somewhere other than next to this repo checkout.

Before running anything else, validate the config and environment:

```powershell
& $python -m zotero_pdf_text check-setup --config .\config.json
```

This is read-only and fast — it checks that the config parses, that `zotero_root`, `zotero_data_directory`,
`linked_attachments`, and `zotero.sqlite` exist, that `output_root` exists (or is creatable) and
is writable, and reports which optional extras (`mcp`, `zotero-write`, `marker`) are installed.
Catching a bad path or a missing extra here takes seconds; catching it 40 minutes into a `dry-run`
or `convert-new` does not. Add `--require-mcp` before registering the MCP server: it also fails
if the `mcp` extra isn't installed, if no index generation is published yet (run `convert-new`),
or if the published index predates the current schema (run `rebuild-index --config`). Each failure
prints the exact recovery command.

## Register the MCP server

```powershell
& $python -m zotero_pdf_text install-mcp --config .\config.json
```

This resolves the current venv's `zotero-fulltext-mcp` executable, your config, and the FTS
database path, then prints a ready-to-paste `claude mcp add` command and a Codex
`config.toml` block — no manual path editing. Add `--apply` to also run the Claude Code
registration for you (falls back to printing the command if `claude` isn't on PATH). Re-running
`--apply` is safe: an identical user-scope registration is left unchanged, and a changed one (for
example a new `--config`, `--db`, or optional-tool flag) replaces the old entry, restoring it if
the new registration fails. To update a registration, rerun `install-mcp` with the new options
and `--apply`. Codex's
`config.toml` is never edited automatically; paste the printed block in yourself. `install-mcp`
does read it (`$CODEX_HOME/config.toml`, else `~/.codex/config.toml`, or `--codex-config PATH`)
and reports whether the existing Codex entry is current, missing, or differs in executable,
arguments, `enabled_tools`, `disabled_tools`, or `enabled`, including a second entry under the
hyphenated name. With `--enable-reconvert` it also reports timeouts below what that mode
needs; larger timeouts and per-tool approval overrides are yours and are not compared. A
project-scoped `.codex/config.toml` can override the user entry and is not checked; an
`omit_tools_from` setting is shown as a note, not as drift. After
changing the entry, restart Codex.

The generated registration enables the safe default MCP surface. To additionally expose the
local Better BibTeX export bridge, opt in at registration time with `--enable-bibtex`; its
endpoint is fixed at server startup and must be a credential-free loopback HTTP URL on Zotero's
local port. The server itself enforces this boundary; the generated client tool list is only
deployment hygiene.

Math OCR is also absent by default because it overwrites one attachment's converted Markdown,
extracted image assets, and index entry. Enable it only when that interactive repair workflow is
wanted:

```powershell
& $python -m zotero_pdf_text install-mcp --config .\config.json --enable-reconvert
```

This requires the `[marker]` extra. The selected database must be the sidecar index governed by
that config; startup rejects mismatched `--db`/`--config` pairs. The generated registration passes
both paths and adds only `reconvert_with_math_ocr`. The generated Codex registration raises its
tool timeout for the long GPU-bound operation. The tool still requires `confirm="reconvert"`, is
blocking and rate-limited, and should be called only after the user approves reconverting that
attachment.

Conversion also records "timeout candidates" -- attachments whose primary extractor exceeded its
scaled timeout budget and fell back to plain-text extraction (see [Timeout Candidates](operations.md#timeout-candidates)). Listing
them (`list_timeout_candidates`) is always available; acting on one (skip permanently, or retry
with a longer budget) is opt-in the same way math OCR is:

```powershell
& $python -m zotero_pdf_text install-mcp --config .\config.json --enable-retry-timeout
```

This requires no optional extra -- retries use the same `pymupdf4llm`/`pymupdf` extractors as
ordinary conversion, not marker-pdf. The selected database must likewise be the sidecar index
governed by that config. The generated registration adds `skip_timeout_extraction` and
`retry_timeout_extraction`; both require their own literal `confirm` string and should be called
only after the user approves that specific decision.

Conversion also makes one automatic lower-concurrency retry when a child extractor
exits with Windows native crash status `0xC000070A`. The run summary separates
recovered, fallback-only, and still-failed retries. Ordinary errors and timeouts
do not trigger this retry.

Verify:

```powershell
claude mcp list
```

Check that `zotero-fulltext` is listed as connected. In other clients, inspect the MCP server status and tool list.

## Optional dependencies and upgrades

The `[mcp]` extra installs the server. A base install provides conversion and the CLI only.
Other extras are `[zotero-write]` for the audited write-plan workflow, `[marker]` for
`reconvert-math` and the opt-in `reconvert_with_math_ocr` tool, and `[test]` for development.
The ordinary `import-doi`, `find-pdf`, and `link-pdf` commands do not need `[zotero-write]`.
To install an additional extra, use the README's tagged install command with, for example,
`[mcp,marker]` in place of `[mcp]`. In a checkout, use
`uv sync --extra mcp --extra test --extra marker --locked` as appropriate.
Experimental `ocr-images` uses a local HTTP OCR service and needs no extra; see
[Image OCR](operations.md#image-ocr-for-equations-tables-and-figures).

Before upgrading on Windows, quit MCP clients using this server. A running
`zotero-fulltext-mcp.exe` can block installation. Install the chosen release with the same
interpreter, then run `check-setup --config <absolute-config-path>` and follow any recovery
instructions. Read the [changelog](../CHANGELOG.md) for migrations. See
[upgrade troubleshooting](troubleshooting.md#updating-an-existing-install-with-write-extras-2026-07-15)
if an installation is left incomplete.

For editable installs, `git pull` alone does not update installed package metadata. Sync or
reinstall after pulling; `check-setup` reports an `install_version` warning when it detects
that the installed version differs from the checkout. For reproducible local development and testing,
use the locked environment in [Developer test setup](dev-test-setup.md).

## Cross-platform notes

- Zotero executable auto-detection (`--zotero-exe` default) and process-name checks
  (`ensure_zotero_running`) are Windows-verified. macOS (`/Applications/Zotero.app/...`) and
  Linux (`/usr/lib/zotero/zotero`) defaults are best-effort, untested guesses — pass
  `--zotero-exe` explicitly if the default doesn't match your install.
- Most dependencies (`pyproject.toml`) are minimum-pinned (`>=`). The MCP SDK is deliberately
  constrained to the tested v1 API (`>=1.28,<2`) until a separate v2 migration. `uv.lock` pins
  exact resolved versions for reproducible installs — see [Developer setup](dev-test-setup.md).
