# Ingestion

## Goal

LLM literature searches should be able to propose articles for Zotero, but
Zotero writes must stay approval-gated. The current implementation supports the
safe first step: candidate queue parsing and dedupe dry-runs.

Commands below assume `$python` is set to this machine's own venv (see `docs/operations.md`).

## Candidate Queue

Use JSONL or a JSON array. Each candidate may contain:

```json
{
  "doi": "10.1000/example",
  "title": "Example Article",
  "authors": "Jane Smith; John Doe",
  "year": "2026",
  "venue": "Journal",
  "url": "https://example.org/article",
  "pdf_url": "https://example.org/article.pdf",
  "pdf_path": "",
  "metadata_strategy": "zotero_identifier",
  "pdf_strategy": "find_available_pdf",
  "zotmoov_expected": true,
  "source_query": "cultural consensus Bayesian model",
  "reason": "Relevant method comparison"
}
```

## Dry Run

```powershell
& $python -m zotero_pdf_text ingest-candidates `
  --config .\config.json `
  --input C:\path\to\candidate_queue.jsonl `
  --output C:\path\to\ingest_dry_run.jsonl
```

Actions:

- `skip_existing`: DOI or title/year duplicate found.
- `needs_review`: ambiguous candidate or incomplete metadata.
- `add_candidate`: no duplicate found in the local Zotero database.

## Approved Writes

Use `zotero-write` for approval-gated Zotero changes. It uses a local Zotero
JavaScript bridge and never writes directly to `zotero.sqlite`.

Create a write plan:

```powershell
& $python -m zotero_pdf_text zotero-write plan `
  --config .\config.json `
  --input C:\path\to\candidate_queue.jsonl `
  --output C:\path\to\write_plan.jsonl
```

Review the JSONL plan. Then approve only intended write rows by 1-based row
number:

```powershell
& $python -m zotero_pdf_text zotero-write approve `
  --plan C:\path\to\write_plan.jsonl `
  --rows 1,3
```

Validate and generate the Zotero script:

```powershell
& $python -m zotero_pdf_text zotero-write validate `
  --plan C:\path\to\write_plan.jsonl `
  --require-approved

& $python -m zotero_pdf_text zotero-write apply `
  --plan C:\path\to\write_plan.jsonl `
  --approve `
  --out-script C:\path\to\zotero_write_apply.js
```

If auto-run is unavailable, open Zotero and run the generated script with
`Tools -> Developer -> Run JavaScript`. Keep `Run as async function` enabled in
that dialog; generated scripts use `await` and return a line-delimited result
summary such as `CREATED item <key>` and `LINKED <key> -> <pdf>`.

Supported write operations:

- `create_item`
- `link_pdf`
- `create_item_with_linked_pdf`
- `create_item_and_find_pdf`
- `find_pdf_for_item`
- `update_metadata`
- `trash_item`

`trash_item` moves exact Zotero keys to trash only. Permanent deletion is not
supported. Candidate PDFs must already exist locally when `pdf_path` is used;
the write module does not download PDFs from `pdf_url`.

PDF strategies:

- `link_local_pdf`: link an existing local `pdf_path`.
- `metadata_only`: create/update metadata without handling a PDF.
- `find_available_pdf`: ask Zotero to search for an available PDF after item creation or for an exact existing item key.

Metadata strategies:

- `zotero_identifier`: prefer Zotero's identifier lookup from DOI before falling back to supplied metadata.
- `supplied_metadata`: create the item from the candidate fields as provided.

ZotMoov is optional but recommended. `zotero-write` does not configure or call
ZotMoov directly; if `zotmoov_expected` is true, generated script output notes
that ZotMoov may move/rename Zotero-found attachments afterward.

After successful Zotero writes:

1. Run `dry-run`.
2. Inspect the dry-run mapping row for the new Zotero parent and attachment
   keys. A good linked-PDF import should normally be `mapped_verified` with a
   strong identity rule such as `doi_exact`.
3. Convert newly linked verified PDFs. For a small import, export just the new
   mapping row to a one-row CSV and pass that to `convert-verified` so the full
   corpus is not reconverted.
4. Publish the new rows with `update-index --manifest <that run's manifest.csv>` — it appends
   only not-yet-indexed attachment keys to the current managed generation and atomically
   publishes the successor, then smoke-test with `search-fts`.

No LLM should silently add, delete, or overwrite Zotero records.
