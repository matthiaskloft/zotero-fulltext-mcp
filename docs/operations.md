# Operations

Paths below use two placeholders — substitute your own:

- `$repo` — where you cloned this repository (e.g. `C:\Users\you\GitHub\zotero_fulltext_mcp`).
- `$data` — your `converted_text` output location, i.e. `config.output_root` from your own
  `config.json` (e.g. `C:\Users\you\ZoteroFullText\converted_text`).

```powershell
Set-Location $repo
$python = C:\Users\you\.venvs\zotero_fulltext_mcp\Scripts\python.exe
```

The venv should live **outside** `$repo` (see README's "Install" section) — a venv contains a
machine-specific absolute path and compiled dependencies, so keeping it inside a repo you might
sync or re-clone elsewhere just creates dead weight.

## Zotero Preflight

```powershell
& $python -m zotero_pdf_text ensure-zotero
```

Use `--require-connector` when an automation should fail if Zotero's local
connector endpoint is unavailable.

## Mapping

```powershell
& $python -m zotero_pdf_text dry-run --config .\config.json
```

The mapper reads a copied Zotero database and writes reports under
`converted_text\runs\<timestamp>`.

## Conversion

Normal resume mode reuses existing Markdown bodies and refreshes YAML front
matter plus manifest metadata from the latest mapping report. Use this after
Zotero metadata changes, including updated citation keys.

```powershell
& $python -m zotero_pdf_text convert-verified `
  --config .\config.json `
  --mapping-report $data\runs\20260602_145352\mapping_report.csv `
  --output-dir $data\verified\20260601_032323 `
  --resume
```

Default worker count is `max(1, CPU cores - 4)`. Use `--workers` to override.

Force reconversion reruns PDF extraction and overwrites existing Markdown only
after extraction succeeds:

```powershell
& $python -m zotero_pdf_text convert-verified `
  --config .\config.json `
  --mapping-report $data\runs\20260602_145352\mapping_report.csv `
  --output-dir $data\verified\20260601_032323 `
  --resume `
  --force
```

## JSONL Sidecar

`build-index` does a **full rebuild** from one manifest — it overwrites `--output` entirely, so it
is only correct for a from-scratch index or a single manifest that already covers everything
trusted. The production index has been built incrementally across many separate conversion runs
since the original full conversion, so running `build-index` against any one of those manifests
today would silently drop every row from the others.

```powershell
& $python -m zotero_pdf_text build-index `
  --manifest <first-full-conversion>\manifest.csv `
  --output $data\index\zotero_text_index.jsonl
```

For every subsequent manifest (new items, promoted `apply-verification` rows, etc.), use
`append-index` instead — it adds only rows whose `zotero_attachment_key` isn't already indexed,
then rebuilds SQLite FTS from the updated JSONL:

```powershell
& $python -m zotero_pdf_text append-index `
  --manifest <new-or-promoted>\manifest.csv `
  --index $data\index\zotero_text_index.jsonl
```

## Unverified PDF Review

Use this when a dry run reports `mapped_unverified` rows. The command converts
only those rows into a quarantine review folder, compares Zotero metadata
against full Markdown text, and writes deterministic decisions plus bounded
agent batches.

```powershell
& $python -m zotero_pdf_text verify-unverified `
  --config .\config.json `
  --mapping-report $data\runs\20260602_145352\mapping_report.csv
```

Outputs are written under
`converted_text\unverified_review\<timestamp>`:

- `markdown\`: quarantine Markdown for candidate PDFs.
- `manifest.csv`: conversion manifest for the reviewed candidates.
- `review.jsonl` and `review.csv`: decisions, confidence, metadata, paths, and evidence snippets.
- `agent_batches\*.jsonl`: ambiguous rows for cheap LLM subagents.
- `agent_review_prompt.md`: strict prompt and output schema for subagents.
- `verification_summary.md`: decision counts.

Resume a review folder without reconverting existing Markdown:

```powershell
& $python -m zotero_pdf_text verify-unverified `
  --config .\config.json `
  --mapping-report $data\runs\20260602_145352\mapping_report.csv `
  --output-dir $data\unverified_review\<timestamp> `
  --resume
```

After deterministic review and optional cheap-agent review (merge agent
`reviewed_*.jsonl` decisions into a copy of `review.jsonl` first — overlay `decision`/`confidence`
onto rows the agent actually resolved, leave the rest `manual_review`), promote accepted rows into
a manifest. Omit `--base-manifest` here: this only needs to produce the *new* rows to add, not a
prepended copy of the entire existing library.

```powershell
& $python -m zotero_pdf_text apply-verification `
  --review $data\unverified_review\<timestamp>\review_merged.jsonl `
  --output-manifest $data\unverified_review\<timestamp>\promoted_manifest.csv `
  --min-confidence 0.92
```

Then append the promoted manifest into the main index (see `append-index` above) rather than
rebuilding from it with `build-index`. `append-index` skips rows whose attachment key is already
indexed, so candidates that turn out to duplicate an already-trusted record are dropped rather than
overwriting the trusted one.

Note: agent-assigned confidence is a coarser, more conservative scale than the deterministic
rule engine's (which reserves 0.93+ for its own accepts) — a `min-confidence` tuned for
deterministic rows can silently exclude genuine agent accepts. Check the agent output's confidence
values before picking a threshold rather than reusing 0.92 by default.

## SQLite FTS

```powershell
& $python -m zotero_pdf_text build-fts `
  --index-jsonl $data\index\zotero_text_index.jsonl `
  --output $data\index\zotero_text_index.sqlite
```

Search:

```powershell
& $python -m zotero_pdf_text search-fts `
  --db $data\index\zotero_text_index.sqlite `
  --query "item response theory" `
  --limit 10
```

Fetch bounded text:

```powershell
& $python -m zotero_pdf_text get-fulltext `
  --db $data\index\zotero_text_index.sqlite `
  --attachment-key RM4KYL8Y `
  --max-chars 12000
```

Coverage:

```powershell
& $python -m zotero_pdf_text coverage-report `
  --db $data\index\zotero_text_index.sqlite
```

## Better BibTeX For LaTeX

LLMs should use the `citation_key` from full-text search results in LaTeX, then
export the exact Better BibTeX entry when the key needs to be added to a
project bibliography.

Check that Zotero and Better BibTeX are reachable:

```powershell
& $python -m zotero_pdf_text ensure-zotero
& $python -m zotero_pdf_text bibtex-check
```

Export one or more entries:

```powershell
& $python -m zotero_pdf_text bibtex-export `
  --citation-key andersCulturalConsensusTheory2014a
```

Append missing entries to a LaTeX project's bibliography:

```powershell
& $python -m zotero_pdf_text bibtex-add `
  --citation-key andersCulturalConsensusTheory2014a `
  --references-bib C:\path\to\latex-project\references.bib
```

The default translator is `Better BibLaTeX`. Use
`--translator "Better BibTeX"` for classic BibTeX projects.

## Approval-Gated Zotero Writes

The read-only MCP server never writes to Zotero. Use `zotero-write` when an LLM
literature search has produced candidates that should be imported or linked.

Create an audited plan:

```powershell
& $python -m zotero_pdf_text zotero-write plan `
  --config .\config.json `
  --input C:\path\to\candidate_queue.jsonl `
  --output C:\path\to\write_plan.jsonl
```

For DOI-based imports, prefer candidates with `metadata_strategy:
"zotero_identifier"` so Zotero imports richer metadata than the LLM-provided
fields. Use `pdf_strategy: "find_available_pdf"` when Zotero should search for
an available PDF. If ZotMoov is configured, set `zotmoov_expected: true`; the
script will not move files itself, but will report that ZotMoov may move/rename
found attachments afterward.

Review the JSONL. Write operations start as `approval_status: "pending"`;
duplicate and ambiguous candidates are emitted as `no_op` records for
auditability. Approve only intended write rows by 1-based row number:

```powershell
& $python -m zotero_pdf_text zotero-write approve `
  --plan C:\path\to\write_plan.jsonl `
  --rows 1,3
```

Validate and generate a Zotero JavaScript script:

```powershell
& $python -m zotero_pdf_text zotero-write validate `
  --plan C:\path\to\write_plan.jsonl `
  --require-approved

& $python -m zotero_pdf_text zotero-write apply `
  --plan C:\path\to\write_plan.jsonl `
  --approve `
  --out-script C:\path\to\zotero_write_apply.js
```

If the CLI reports that auto-run is unavailable, open Zotero and run the script
with `Tools -> Developer -> Run JavaScript`.

After the script runs successfully, refresh derived artifacts:

```powershell
& $python -m zotero_pdf_text dry-run --config .\config.json
& $python -m zotero_pdf_text convert-verified --config .\config.json --mapping-report <new_mapping_report.csv>
& $python -m zotero_pdf_text append-index --manifest <new_manifest.csv> --index $data\index\zotero_text_index.jsonl
```

(Or simply run `convert-new`, which does exactly this dry-run → convert → append-index sequence
for newly linked verified PDFs in one step.)

## Multi-Machine Write Lock

If `converted_text` is a single index shared across more than one machine (e.g. via a synced
cloud folder), every command
that writes under it (`convert-sample`, `convert-verified`, `convert-new`, `verify-unverified`,
`apply-verification`, `build-index`, `build-fts`) takes a lock file (`.pipeline.lock`, next to the
files it writes) before starting and releases it on exit, so two machines can never rebuild the
same SQLite/JSONL files at once — the same corruption class as syncing a live Zotero database.

If a command refuses to start with a message naming another host, pid, and start time, check that
the other machine isn't actually mid-run before doing anything. If that machine's process has
genuinely died (crash, forced shutdown) and left a stale lock, the command already treats locks
older than 6 hours as free automatically; for anything more recent, delete `.pipeline.lock` by hand
only once you're sure the other machine isn't running it.

## Tests

```powershell
& $python -m unittest discover -s tests
```
