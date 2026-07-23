# Troubleshooting

Paths below use the same `$repo`/`$data` placeholders as `docs/operations.md` — substitute your
own repo checkout and `converted_text` location. Commands assume `$python` is set to your own
venv (see `docs/operations.md`) — not `.\.venv\...` inside the project folder, which is never
correct on any machine.

## What Platform Support CI Actually Verifies

The [CI workflow](../.github/workflows/ci.yml) runs the full pytest suite on Windows, macOS, and
Linux for every push/PR, so that's the source of truth for whether the package installs and the
conversion/indexing/search logic works on a given OS. It does **not** verify Zotero executable
auto-detection or process-name checks (`ensure-zotero`) against a real Zotero installation on
macOS/Linux, since CI has no Zotero installed. Those remain best-effort defaults on non-Windows
platforms; pass `--zotero-exe` explicitly if `ensure-zotero` doesn't find your install.

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

## SQLite FTS Missing, Pointer Invalid, or Schema Unsupported

If `search-fts` (or the MCP server at startup) reports a missing database, an invalid managed
index pointer (`index_pointer_invalid`), or an unsupported index schema
(`index_schema_unsupported`), publish a fresh managed generation:

```powershell
& $python -m zotero_pdf_text rebuild-index --config .\config.json
```

This snapshots the current (or legacy) JSONL sidecar into a validated generation and atomically
repoints `current.json` at it. A corrupt/hand-edited `current.json` is never silently ignored in
favor of stale data — readers fail loudly until a valid generation is re-published.

## Fresh Build Cannot Import the Converter or PDF Dependencies

Use the project environment configured as `$python`, not a system Python, a generic assistant
runtime, or a repository-local `.venv`. Confirm it before starting a long rebuild:

```powershell
& $python -c "import zotero_pdf_text, fitz, pymupdf4llm; print('converter environment ready')"
```

If this reports `No module named zotero_pdf_text`, `$python` points to the wrong environment. If
it reports a missing `fitz` or `pymupdf4llm`, install this project's runtime dependencies into the
selected environment before retrying. Do not start a second conversion while diagnosing the
environment: a pipeline lock prevents concurrent writers, but any partially created run directory
still needs to be kept separate from the next fresh run.

## Fresh Mapping Reports Many Unreadable Linked Files

`dry-run` can warn about linked attachment paths that no longer exist locally, for example after a
folder move or a library previously used on another computer. These paths are skipped; the mapper
can still finish and write `mapping_report.csv`. Inspect its classification counts before conversion
and continue only with `mapped_verified` rows. Do not edit the live Zotero database or bulk-change
linked files merely to silence these warnings; repair paths through Zotero when the attachments are
actually needed.

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
c.close()
```

Caveat: `immutable=1` ignores the WAL, so a very recently connector-created item may not appear
until Zotero checkpoints (usually visible within seconds in practice).

## Stale Full-Text Index

Rebuild in this order:

1. `dry-run`
2. `convert-verified --resume`
3. `rebuild-index --manifest <that run's manifest.csv>`

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
3. `rebuild-index --manifest <that run's manifest.csv>`

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

## Converted Output Paths Recorded By A Previous Machine

Index records store the absolute `markdown_path` written by whichever machine ran the
conversion, and converted Markdown embeds absolute image links the same way. Move the library —
a new machine, a renamed sync folder, a different cloud provider — and search keeps working
(the text lives in the index) while every recorded path silently stops resolving on disk.

`ocr-images` handles both cases: it re-roots a stale `markdown_path` by matching the deepest
suffix that exists under `output_root`, and it locates crop PNGs by filename against the images
directory derived from the Markdown's own location rather than trusting the embedded link. If
it reports that no matching file was found under `output_root`, the recorded path shares no
suffix with the current tree — check that `output_root` in your config points at the directory
that actually contains `markdown/` and `images/`.

Other commands that consume recorded paths (`reconvert-math`) do not re-root and will report
the file as missing. Republishing the index from the current tree (`rebuild-index`) rewrites the
stored paths.

## Image OCR Runtime Is Unavailable

`ocr-images` and the `image-ocr runtime` row in `check-setup` report three distinct states, each
with its own fix:

- **"Ollama is not reachable at …"** — the server is not running, or is on a different host/port
  than the `image_ocr` block in your config. Start Ollama and retry.
- **"no `glm-ocr` model is installed"** — run `ollama pull glm-ocr:q8_0`.
- **"`glm-ocr:q8_0` is not pulled. Installed tags: …"** — a different tag of the same model is
  already present. Either pull the configured tag or set `image_ocr.model` to one listed.

The probe never runs an inference, so it stays fast even on a CPU-only machine where a real OCR
call takes minutes.

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

Run `zotero-pdf-text check-setup --config .\config.json` first — it catches a bad path, a
missing `zotero.sqlite`, an unwritable `output_root`, or a missing extra in seconds, before any of
the roadblocks below have a chance to surface as a confusing mid-command failure.

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

### TODO — Not Yet Fixed

Snags hit while updating an existing install to the latest version (2026-07-13). Unlike the
roadblocks above, these are still open:

4. **TODO.** Editable installs go stale silently. `pip show zotero-fulltext-mcp` can report an
   old version (e.g. `0.1.0`) even after the source repo has advanced past it (e.g. to `0.2.0`
   per `pyproject.toml` / the latest git tag), because editable-install metadata is only
   refreshed on reinstall, not on `git pull`. Neither `check-setup` nor `install-mcp` currently
   compares the installed package version against the source repo's `pyproject.toml` version, so
   nothing flags the drift — the fix (`pip install -U -e .[mcp]`) has to be discovered manually.
   Consider adding a version-mismatch check to `check-setup` (or a dedicated flag) when running
   from an editable install.

5. **TODO.** `claude mcp list` reports a generic `Failed to connect` for the `zotero-fulltext`
   server regardless of root cause — a broken/stale install and a missing FTS index (i.e. the
   conversion pipeline was never run for the given `--db` path) look identical from that output.
   The server's stdio entrypoint could fail fast with a clearer stderr message when `--db` points
   to a nonexistent file or an empty index (e.g. "no FTS index found at <path> — run
   `zotero-pdf-text convert` first"), so the failure is actionable instead of generic.

6. **Partially fixed.** Duplicate Zotero attachments inflate the fulltext index. Found 2026-07-15
   while auditing conversion timeouts: 70 Zotero parent items have 2+ linked PDF attachments (mostly
   the same paper saved twice under slightly different filenames — a trailing `2`/`3`/`4`, or a
   legitimate copy alongside a `z-lib.org` copy), producing 77 redundant converted/indexed rows out
   of 1604 (~4.8%). `find-duplicate-attachments` (see `docs/operations.md`'s "Duplicate Attachment
   Cleanup") now covers the safe subset: byte-identical duplicates within the same Zotero item,
   auto-resolved only when exactly one filename lacks a trailing numeric suffix, written as a
   `trash_item` write-plan gated by the existing `zotero-write approve`/`validate --require-approved`/
   `apply --approve` flow — nothing is ever removed without that explicit approval step. Groups that
   aren't byte-identical (same paper re-extracted from a different mirror/OCR pass, near-identical
   text) or where the "keep" filename can't be picked unambiguously (3+ attachments, genuinely
   different editions) are still a per-item judgment call and are reported as ambiguous rather than
   guessed — see `ambiguous_duplicate_groups.csv` in the command's output directory.

### Updating An Existing Install With Write Extras (2026-07-15)

Snags hit while reinstalling the stable venv to the latest version and adding the `zotero-write`
extra (marker deliberately excluded). All still open unless noted.

6. **TODO — Windows: the console-script `.exe` is locked while the MCP server is running.**
   `pip install -e .[...]` into the stable venv fails partway with
   `[WinError 32] The process cannot access the file ... zotero-fulltext-mcp.exe ... used by
   another process`. Claude Code keeps one or more `zotero-fulltext-mcp.exe` processes alive for
   the registered server, and on Windows pip cannot replace the script shim while it executes.
   Worse, pip has already uninstalled the old dist-info by the time it hits the lock, leaving the
   package **half-uninstalled** (`pip show` reports no version) plus a leftover
   `~otero_fulltext_mcp-<ver>.dist-info` directory in `site-packages`. Recovery:

   ```powershell
   Get-Process zotero-fulltext-mcp -ErrorAction SilentlyContinue | Stop-Process -Force
   & $python -m pip install -e "<repo>[mcp,zotero-write]"
   # then remove the stray leftover if present:
   Get-ChildItem <venv>\Lib\site-packages -Filter "~*" -Directory | Remove-Item -Recurse -Force
   ```

   Claude Code respawns the server from the freshly installed exe on next use. Consider having
   `install-mcp`/a dedicated update command detect running server processes and warn (or offer to
   stop them) before an editable reinstall on Windows.

7. **TODO — `install-mcp --apply` refuses when the server is already registered.** If a
   `zotero-fulltext` entry already exists in the user config, `--apply` fails with
   `MCP server zotero-fulltext already exists in user config` (from `claude mcp add`) and does not
   update the existing entry. To change the registration (e.g. repoint `--config` at a new path)
   you must remove it first:

   ```powershell
   claude mcp remove "zotero-fulltext" -s user
   & $python -m zotero_pdf_text install-mcp --config <new config path> --apply
   ```

   Consider making `install-mcp --apply` idempotent (remove-then-add, or detect and update in
   place) so repointing an existing registration is a single command.

8. **Fixed.** Only `install-mcp` used to auto-resolve the config; every other subcommand defaulted
   to a literal `config.json` next to cwd instead of calling `resolve_config_path()` (env var →
   `config.<hostname>.json` → `config.json`). A config kept outside the checkout (e.g.
   `~/.config/zotero_fulltext_mcp/config.json`) was **not** auto-found by those commands even
   though the MCP server found it — you had to pass `--config <abs path>` explicitly or set
   `ZOTERO_PDF_TEXT_CONFIG`. All subcommands' `--config` defaults now call `resolve_config_path()`.

9. **TODO — `config.<hostname>.json` uses `platform.node()` (the hostname), not the username.**
   A per-machine config named after the OS user (e.g. `config.<username>.json`) is silently
   ignored by `resolve_config_path()` because it looks for `config.{platform.node()}.json`. On a
   box where the hostname (`LIF-000058`) differs from the username (`nu006612`), a
   `config.nu006612.json` never auto-resolves and the registration has to bake in an explicit
   `--config`. Confirm the expected filename with
   `python -c "import platform; print('config.'+platform.node()+'.json')"`.

10. **Working as designed — `install-mcp --enable-reconvert` requires the `marker` extra.**
    Without `marker-pdf` installed, `--enable-reconvert` refuses with
    `--enable-reconvert requires the optional marker dependency (pip install -e .[marker])`. This
    is intentional capability separation, not a bug: the base install (`[mcp,zotero-write]`) gives
    the read-only search server plus the approval-gated `zotero-write` CLI workflow, but **not**
    the `reconvert_with_math_ocr` MCP tool, which needs marker's torch/transformers stack. Install
    `[marker]` and re-run `install-mcp --enable-reconvert` (and raise `MCP_TIMEOUT` to `180000`)
    only if you actually need math-OCR reconversion.
