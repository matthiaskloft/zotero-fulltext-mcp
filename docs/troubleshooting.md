# Troubleshooting

Paths below use the same `$repo`/`$data` placeholders as `docs/operations.md` — substitute your
own repo checkout and `converted_text` location. Commands assume `$python` is set to your own
venv (see `docs/operations.md`) — not `.\.venv\...` inside the project folder, which is never
correct on any machine.

## Zotero MCP Connection Refused

Symptoms:

- MCP tool returns connection refused.
- Zotero is closed.
- Stale `pyzotero-mcp` processes are running.

Steps:

1. Run `zotero-pdf-text ensure-zotero`.
2. Open Zotero manually once if Windows blocks first launch.
3. If MCP still fails, restart stale `pyzotero-mcp` processes.
4. Re-run the Zotero MCP search.

## SQLite FTS Missing

If `search-fts` reports a missing database, build it:

```powershell
& $python -m zotero_pdf_text build-fts `
  --index-jsonl $data\index\zotero_text_index.jsonl `
  --output $data\index\zotero_text_index.sqlite
```

## `import-doi` Returns No Item Key

`import-doi` posts to Zotero's local connector but does not hand back the new item's key, so
`check-pdf`/`find-pdf`/`link-pdf` (all require `--key`) can't be chained directly afterward — you
have to look the key up yourself. Also, CrossRef's metadata fetch can transiently return HTTP 404;
retry once before assuming a DOI is bad.

To find the key, query the **live** `zotero.sqlite` (the one at `zotero_data_directory` in your
config) — not any other `zotero.sqlite` that might exist elsewhere in an old/backup location, which
can be stale and missing the newest items. The live DB is normally locked by the running Zotero
(`mode=ro` fails with "database is locked"); open it with `immutable=1` instead to bypass the lock
and WAL:

```python
import sqlite3
c = sqlite3.connect(
    "file:C:/path/to/zotero_data_directory/zotero.sqlite?immutable=1", uri=True)
q = ("SELECT i.key FROM items i "
     "JOIN itemData d ON d.itemID=i.itemID "
     "JOIN itemDataValues v ON v.valueID=d.valueID "
     "JOIN fields f ON f.fieldID=d.fieldID "
     "WHERE f.fieldName='DOI' AND v.value LIKE '%<doi-tail>%'")
print(list(c.execute(q)))
```

Caveat: `immutable=1` ignores the WAL, so a very recently connector-created item may not appear
until Zotero checkpoints (usually visible within seconds in practice).

## Stale Full-Text Index

Rebuild in this order:

1. `dry-run`
2. `convert-verified --resume`
3. `build-index`
4. `build-fts`

`convert-verified --resume` refreshes YAML/front matter and manifest metadata
for existing Markdown files. It does not rerun PDF extraction.

Use `--force` only when the Markdown body itself should be regenerated:

```powershell
& $python -m zotero_pdf_text convert-verified `
  --mapping-report <mapping_report.csv> `
  --output-dir <existing conversion folder> `
  --resume `
  --force
```

## Stale Citation Keys Or Metadata

If `citation_key`, title, DOI, or creator metadata looks stale, run:

1. `dry-run`
2. `convert-verified --resume`
3. `build-index`
4. `build-fts`

Force reconversion is not required for metadata-only changes.

## Failed PDF Extraction

The converter first tries `pymupdf4llm.to_markdown`. If that fails, it falls
back to `pymupdf.get_text`. Fallback files remain searchable but should be
spot-checked when quality matters.

## Unverified PDFs

The current trusted full-text baseline indexes only `mapped_verified` PDFs.
Review `mapped_unverified` rows with `verify-unverified`, then promote accepted
rows with `apply-verification`.

Do not edit the old mapping report and rerun `convert-verified` into an
existing verified output folder after adding newly verified rows. The four-digit
Markdown prefixes are row-order based, so inserted rows can shift filenames and
create duplicate or mismatched paths.

Accepted unverified rows should enter the trusted index through a combined
manifest produced by `apply-verification`. Use cheap-agent decisions only as
review input; keep ambiguous rows as `manual_review`.

## Ingest Approved Refuses To Run

`ingest-approved` is deprecated. Use:

```powershell
& $python -m zotero_pdf_text zotero-write plan --config .\config.json --input <candidates.jsonl> --output <write_plan.jsonl>
& $python -m zotero_pdf_text zotero-write approve --plan <write_plan.jsonl> --rows <row_numbers>
& $python -m zotero_pdf_text zotero-write validate --plan <write_plan.jsonl> --require-approved
& $python -m zotero_pdf_text zotero-write apply --plan <write_plan.jsonl> --approve --out-script <zotero_write_apply.js>
```

## Zotero Write Script Was Generated But Not Auto-Run

This is the expected v1 fallback when no supported local Zotero JavaScript
execution route is detected. Open Zotero and run the generated script with
`Tools -> Developer -> Run JavaScript`.

If Zotero reports a JavaScript error:

- Check that every `pdf_path` still exists locally.
- Check that exact Zotero keys in `target` still exist.
- Re-run `zotero-write validate --require-approved`.
- Recreate the plan from a fresh `dry-run` if Zotero changed since approval.

If an older generated script reports `items is not iterable`, regenerate the
script with the current `zotero-write apply` command. The duplicate check uses
Zotero's async item-list API and older script artifacts may not include that
fix.

`trash_item` sets Zotero's deleted flag. It does not permanently delete files or
records.

## Zotero Metadata Lookup Or PDF Search Fails

`metadata_strategy: "zotero_identifier"` uses a guarded Zotero internal
identifier-import call from the generated JavaScript. If that API is unavailable
or fails, the script falls back to the supplied candidate metadata and reports a
warning.

`pdf_strategy: "find_available_pdf"` uses a guarded
`Zotero.Attachments.addAvailablePDF` call. If Zotero does not expose that
function, the script reports an explicit error for that item. This is not an
indexing failure; it means the PDF must be added manually or linked with
`pdf_path`.

If ZotMoov is enabled, a found PDF may be moved/renamed after Zotero creates the
attachment. Run `dry-run` after ZotMoov finishes so the mapper sees the final
attachment path.

## Reference Index PDF Paths Do Not Resolve

Some external project reference indexes may point to stale relative PDF folders,
for example `docs\references\litreview\fulltexts\...`, while the actual PDFs
live elsewhere in another project's own tree (e.g. a sibling repository's
`litreview\fulltexts_pdf` folder). Resolve the actual absolute path for your
own setup before proceeding.

Before creating a Zotero write candidate, normalize the PDF path and confirm it
exists with `Test-Path`; `zotero-write validate --require-approved` will reject
missing local PDFs.

## Better BibTeX Export Fails

`bibtex-export`, `bibtex-add`, and `export_bibtex_entries_by_key` require
Zotero with Better BibTeX running locally.

Check:

```powershell
& $python -m zotero_pdf_text ensure-zotero
& $python -m zotero_pdf_text bibtex-check
```

The expected endpoint is:

```text
http://127.0.0.1:23119/better-bibtex/json-rpc
```

Use the default `Better BibLaTeX` translator for BibLaTeX projects. Use
`--translator "Better BibTeX"` only for classic BibTeX projects.

## Claude MCP Reports "Failed To Connect"

This can be a genuine broken registration, not just a sandbox artifact — check
both:

1. **Real breakage**: the registered `command` path points at a venv that
   doesn't work on this machine. This can happen if a venv created on one
   machine gets synced (e.g. via Dropbox/Nextcloud) into another machine's
   repo checkout under the same folder name — venvs contain a machine-specific
   absolute Python path and compiled dependencies, so a synced venv is
   non-functional on any other machine. Fix by re-running
   `zotero-pdf-text install-mcp --apply` (or applying its printed command)
   from the current machine's own working venv, then re-check:

   ```powershell
   claude mcp get zotero-fulltext
   ```

2. **Sandbox false negative**: Claude Code can also report `Failed to connect`
   when `claude mcp get` runs inside Codex's filesystem sandbox, because the
   health probe can't launch/read the project-local server normally. Re-run the
   health check from a normal terminal or with sandbox escalation before
   assuming the registration itself is broken.

## Zotero Sync Stalls With "Cannot Change Attachment LinkMode"

This can occur if you sync the same Zotero library across multiple machines via Zotero's own
account sync (zotero.org). It means the server already has a record under that item's key with
a different attachment link mode than the local one — Zotero's API can create or delete an
attachment record but never convert one type to another via sync. Fix: trash *and permanently
erase* the conflicting attachment (trash alone still fails, since it still pushes a blocked
update), then re-link the PDF fresh via `link-pdf` if it had a real parent item, so it gets a new
key with no stale server history.

## Installing / Registering On A New Machine — Known Roadblocks

Snags hit while installing the MCP server on a fresh Windows machine. All three
are now fixed as of the roadblock-fix pass below; kept here in case an older
checkout or a different platform still hits the underlying issue.

1. **Fixed.** `install-mcp --apply` used to print `'claude' was not found on
   PATH` even though `claude` worked in the same shell, because on Windows the
   Claude Code CLI is an npm shim (`claude.cmd`), not a `claude.exe`, and
   `subprocess.run(["claude", ...])` without `shell=True` does not resolve
   `.cmd`/extensionless shims the way an interactive shell does. `install-mcp`
   now resolves the executable via `shutil.which("claude")` first, which
   applies `PATHEXT` resolution the same way a shell would. If `which` still
   can't find it, the command reports the failure and falls back to the printed
   command for you to run manually — `--apply` remains a convenience, not the
   only path.

2. **Fixed.** `claude mcp add-json '<json>'` used to fail with `Invalid
   configuration: : Invalid input` under Git Bash / other POSIX shells, because
   the single-quoted JSON payload got mangled before `claude` saw it.
   `install-mcp` now prints (and applies) the equivalent positional form
   instead of JSON entirely, which is quoting-robust across PowerShell, cmd, and
   Git Bash alike:

   ```powershell
   claude mcp add --scope user zotero-fulltext `
     C:\path\to\venv\Scripts\zotero-fulltext-mcp.exe `
     -- --db <converted_text>\index\zotero_text_index.sqlite `
        --config <config path>
   ```

   The `--` separates the server executable from the flags passed to it, and the
   resulting stdio registration is identical to the old `add-json` one.

3. **Fixed.** `pytest` used to not be installed by `pip install -e .[mcp]`,
   since the `[mcp]` extra pulls only runtime deps — `python -m pytest -q` would
   fail with `No module named 'pytest'` in a fresh MCP-only venv. A `test` extra
   now bundles it: install with `pip install -e .[mcp,test]` before running the
   suite. Expected result: `107 passed`.
