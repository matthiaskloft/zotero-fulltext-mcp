# Data Dictionary

## Markdown YAML

Each converted Markdown file starts with front matter:

- `zotero_parent_key`
- `zotero_attachment_key`
- `title`
- `creators`
- `year`
- `doi`
- `citation_key`
- `source_path`
- `extraction_tool`

## Conversion Manifest

`manifest.csv` and `manifest.jsonl` contain one row per requested conversion:

- `status`: `converted`, `skipped_existing`, or `error`
- `extraction_tool`: primary or fallback extractor
- Zotero keys, citation key, and bibliographic metadata
- `item_type`: Zotero item type used by verification heuristics.
- `source_path`
- `output_path`
- `page_count`
- `classification`
- `identity_status`
- `identity_rule`
- `has_math`: `true`/`false`, auto-detected from math fonts and Unicode math-symbol density.
- `error`

## JSONL Sidecar

`zotero_text_index.jsonl` contains one record per available converted full text:

- Zotero keys
- title, creators, year, DOI, citation key
- source PDF and Markdown paths
- Markdown SHA-256
- extraction tool
- character and word counts
- page count
- mapping classification and identity status
- `has_math`: boolean, carried from the manifest
- full Markdown-derived `text`

Reconvert a single paper with `reconvert-math --key <attachment_key>` when `has_math` is true and
the notation needs to be trustworthy (LaTeX-aware marker-pdf extraction, in place). This is
just-in-time only — roughly 27s/page, so it is not meant for bulk reconversion.

## SQLite FTS

`zotero_text_index.sqlite` contains:

- `metadata`: one record per indexed attachment
- `chunks`: bounded text chunks with character ranges
- `chunks_fts`: FTS5 search table over title, creators, citation key, and chunk text

The default chunk size is 6,000 characters with 500 characters of overlap.

## Confidence Fields

- `classification`: mapper decision such as `mapped_verified` or
  `mapped_unverified`.
- `identity_status`: evidence status such as `verified`, `manual_accepted`, or
  `candidate`.
- `identity_rule`: rule that produced the status.

LLM tools should show these fields in search results.

## Unverified Review

`verify-unverified` writes `review.jsonl` and `review.csv` with one row per
converted unverified candidate:

- `decision`: `accept`, `reject`, or `manual_review`.
- `confidence`: numeric confidence between 0 and 1.
- `review_rule`: deterministic rule or agent rule that produced the decision.
- `reason`: short explanation.
- `matched_fields`: matched evidence fields such as `doi`, `title`, `author`, or `year`.
- `evidence_snippets`: bounded text snippets used for the decision.
- `evidence_status`, `evidence_rule`, `title_score`, `author_evidence`, `year_evidence`, `observed_dois`: full-text identity evidence.
- `conversion_status`, `conversion_error`: Markdown extraction outcome.
- Zotero keys, citation key, bibliographic metadata, source PDF path, Markdown path, extraction tool, page count, classification, and original mapper identity fields.

`apply-verification` turns accepted high-confidence review rows into normal
conversion-manifest rows with:

- `classification`: `mapped_verified`
- `identity_status`: `fulltext_verified`
- `identity_rule`: `fulltext_review:<review_rule>`

## Better BibTeX Export

`bibtex-export`, `bibtex-add`, and `export_bibtex_entries_by_key` use citation
keys as their join field.

Returned/exported fields:

- `citation_keys`: requested citation keys after deduplication.
- `translator`: Better BibTeX translator, default `Better BibLaTeX`.
- `entry`: full `.bib` entry text returned by Better BibTeX.
- `endpoint`: local Better BibTeX JSON-RPC endpoint.

`bibtex-add` also reports:

- `references_bib`
- `added_keys`
- `skipped_existing_keys`

## Zotero Write Plan

`zotero-write plan` writes JSONL with one row per candidate decision:

- `operation`: `create_item`, `link_pdf`, `create_item_with_linked_pdf`,
  `create_item_and_find_pdf`, `find_pdf_for_item`, `update_metadata`,
  `trash_item`, or `no_op`.
- `approval_status`: write records start as `pending` and must be approved with
  `zotero-write approve` before `apply`; `no_op` records use `not_required`.
- `risk_level`: `low`, `medium`, `high`, or `destructive`.
- `candidate`: original ingestion candidate metadata and local `pdf_path`.
- `pdf_strategy`: `link_local_pdf`, `metadata_only`, or `find_available_pdf`.
- `metadata_strategy`: `supplied_metadata` or `zotero_identifier`.
- `zotmoov_expected`: whether ZotMoov is expected to move/rename Zotero-found PDFs after attachment creation.
- `pdf_management_note`: human-readable audit note for PDF handling.
- `target`: exact Zotero keys or planned temporary id.
- `dedupe`: dry-run decision evidence such as duplicate action, reason, and
  existing Zotero parent key.
- `js_preview`: short human-readable description of the generated Zotero action.

`trash_item` means move to Zotero trash only, not permanent deletion.

`zotero-write status` also reports `by_pdf_strategy`, `by_metadata_strategy`,
and `zotmoov_expected_count`.
