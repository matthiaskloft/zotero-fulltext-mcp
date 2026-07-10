# zotero-fulltext-mcp

Convert a Zotero library's linked PDF attachments to Markdown, build a full-text search index,
and expose it to LLM tools (Claude Code, Codex, etc.) through an MCP server — so an assistant
can search and read your papers' full text, not just their metadata.

The pipeline is read-only with respect to Zotero: it never writes to your live `zotero.sqlite`
except through the optional, approval-gated `zotero-write` workflow. Everything else reads
Zotero's data and writes only to a separate `converted_text` output folder that you control.

## Prerequisites

- Python 3.11+.
- A Zotero library that uses **linked** attachments (`Zotero.Attachments.linkFromFile`, i.e. PDFs
  stay in a folder you choose rather than Zotero's internal `storage/`). Stored/managed
  attachments are not the tested path for this project.
- Windows is the primary, verified platform. macOS/Linux are supported in the code (path
  resolution, process detection) but not yet verified end-to-end — see "Cross-platform notes"
  below.

## Install

Create a virtual environment **outside** this repository — a venv contains a machine-specific
absolute Python path and compiled dependencies, so keeping it inside a folder you might sync or
re-clone elsewhere just produces a broken venv there.

```powershell
py -3.11 -m venv C:\Users\you\.venvs\zotero_fulltext_mcp
C:\Users\you\.venvs\zotero_fulltext_mcp\Scripts\python.exe -m pip install -U pip
cd C:\path\to\zotero_fulltext_mcp
C:\Users\you\.venvs\zotero_fulltext_mcp\Scripts\python.exe -m pip install -e .[mcp]
$python = C:\Users\you\.venvs\zotero_fulltext_mcp\Scripts\python.exe
```

macOS/Linux:

```bash
python3.11 -m venv ~/.venvs/zotero_fulltext_mcp
~/.venvs/zotero_fulltext_mcp/bin/python -m pip install -U pip
cd /path/to/zotero_fulltext_mcp
~/.venvs/zotero_fulltext_mcp/bin/python -m pip install -e '.[mcp]'
python=~/.venvs/zotero_fulltext_mcp/bin/python
```

Optional extras: `[zotero-write]` (write-plan workflow via pyzotero), `[marker]` (marker-pdf,
needed for `reconvert-math`/`reconvert_with_math_ocr`, GPU-bound), `[test]` (pytest, needed to
run the test suite — `pip install -e .[mcp,test]`). A plain `pip install -e .` with no extras
gets you the conversion pipeline and CLI but not the MCP server.

## Configure

Copy the template and fill in your own paths:

```powershell
Copy-Item config.example.json config.json
```

Edit `config.json`:

- `zotero_root` — a working folder for this project's own outputs/reports.
- `zotero_data_directory` — **your local, non-synced** Zotero data directory (where
  `zotero.sqlite` lives). Never point this at a cloud-synced folder (Dropbox, Nextcloud,
  OneDrive, etc.) — see `docs/architecture.md` for why.
- `linked_attachments` — the folder your Zotero library links PDFs from
  (`baseAttachmentPath` in Zotero's settings).
- `output_root` — where converted Markdown and the search index get written.

If you run this from more than one machine, name each machine's config
`config.<hostname>.json` (Python's `platform.node()`) instead of hand-maintaining separate
files — `zotero-pdf-text` picks the right one automatically. You can also point at any config
file explicitly via the `ZOTERO_PDF_TEXT_CONFIG` environment variable, which takes priority over
both the hostname-based file and the plain `config.json` fallback. This is the mechanism to use
when your config/data live somewhere other than next to this repo checkout.

## Build the index

```powershell
& $python -m zotero_pdf_text ensure-zotero
& $python -m zotero_pdf_text dry-run --config .\config.json
& $python -m zotero_pdf_text convert-new --config .\config.json
```

`convert-new` runs mapping, conversion, and index updates together for newly linked verified
PDFs, and is the incremental command you'll run repeatedly as you add papers. See
`docs/operations.md` for the full command reference (sampling, manual review of unverified
matches, rebuilding vs. appending to the index, etc.) and `docs/architecture.md`/
`docs/data-dictionary.md` for how the pipeline and schema fit together.

Smoke-test the index directly:

```powershell
& $python -m zotero_pdf_text search-fts --db .\converted_text\index\zotero_text_index.sqlite --query "some topic" --limit 3
```

## Register the MCP server

```powershell
& $python -m zotero_pdf_text install-mcp --config .\config.json
```

This resolves the current venv's `zotero-fulltext-mcp` executable, your config, and the FTS
database path, then prints a ready-to-paste `claude mcp add` command and a Codex
`config.toml` block — no manual path editing. Add `--apply` to also run the Claude Code
registration for you (falls back to printing the command if `claude` isn't on PATH). Codex's
`config.toml` is never edited automatically; paste the printed block in yourself.

Verify:

```powershell
claude mcp get zotero-fulltext
```

Expected status: `Connected`.

## Tool contract

The server exposes:

- `ensure_zotero_running` — check/start the Zotero application.
- `search_fulltext(query)` — ranked full-text search with bounded snippets.
- `get_fulltext_chunk(attachment_key)` — full converted text for one paper.
- `get_item_context(parent_key | attachment_key)` — sidecar metadata (title, authors, DOI,
  citation key, file paths).
- `coverage_report()` — how many items are indexed vs. total, including `has_math` counts.
- `export_bibtex_entries_by_key(citation_keys)` — Better BibTeX/BibLaTeX entries by citation key
  (requires Zotero + Better BibTeX running locally).
- `reconvert_with_math_ocr(attachment_key)` — re-extract one paper with marker-pdf when
  equations/figures look garbled; blocking, GPU-bound, just-in-time only (see the server's own
  tool description for the reasoning against bulk use).

## Companion MCP server: pairing with the official Zotero MCP

This server is deliberately scoped to **offline full-text search** over a pre-built index — it
works even when Zotero and its connector are closed, and it never talks to Zotero's live API. It
does **not** expose collections, tags, notes, or other live metadata.

For that, install the official Zotero MCP server alongside this one — the two are meant to be
used together, not merged:

- This server (`zotero-fulltext-mcp`): full-text search and retrieval from the sidecar index.
- Official Zotero MCP: live collections, tags, notes, child attachments, Zotero URIs. Requires
  Zotero running.

If the official server's connector is down, `zotero-fulltext-mcp` tools remain fully functional
since they never depend on it.

## Optional: write-side workflows (debug-bridge, ZotMoov)

`import-doi`, `find-pdf`, and `link-pdf` (CLI commands, also documented in the MCP server's own
instructions for use mid-conversation) drive Zotero's UI-equivalent actions through the
**debug-bridge** plugin — see `docs/debug-bridge-setup.md` for setup, including generating your
own bridge token. `link-pdf` additionally uses the **ZotMoov** plugin to relocate linked files
into your managed `linked_attachments` folder. Both are optional; core search/conversion works
without them.

## Cross-platform notes

- Zotero executable auto-detection (`--zotero-exe` default) and process-name checks
  (`ensure_zotero_running`) are Windows-verified. macOS (`/Applications/Zotero.app/...`) and
  Linux (`/usr/lib/zotero/zotero`) defaults are best-effort, untested guesses — pass
  `--zotero-exe` explicitly if the default doesn't match your install.
- Dependencies (`pyproject.toml`) are minimum-pinned (`>=`) only, with no lockfile and no tested
  upper bounds.

## License

MIT — see `LICENSE`.
