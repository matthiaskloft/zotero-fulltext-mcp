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

### Extraction Timeout and Fallback

`convert-verified`/`convert-sample`/`verify-unverified` try `pymupdf4llm.to_markdown` (the primary
extractor, which preserves structure and extracts images) before falling back to the plain-text
`pymupdf.get_text` on timeout or crash. The primary extractor's timeout scales with document length
and complexity so long or diagram-dense books are not needlessly demoted to the plain-text
fallback:

- Base budget: `page_count * 4` seconds (`SECONDS_PER_PAGE_TIMEOUT` in `converter.py`), floored at
  the `--timeout-seconds` CLI value (default 600s).
- That budget is further multiplied (up to 5x) based on a cheap page-sampled vector-drawing density
  scan, since `pymupdf4llm`'s layout parser walks every vector path and pages full of statistical
  plots/diagrams cost far more per page than plain text.

A row's `error` field records `"Primary extractor failed; fallback used. Primary error: ..."` when
this happened, even though `status` is still `converted` — check `extraction_tool` and `error`
together, not `status` alone, to find rows that lost structure/images to the fallback.

### Timeout Candidates

Every row whose primary extractor genuinely timed out (not crashed — see `PrimaryExtractorTimeoutError`
in `converter.py`) is recorded as a "timeout candidate": once per run in `timeout_candidates.csv`/
`.jsonl` next to that run's `manifest.csv`, and merged into a persistent master file at
`<output_root>/index/timeout_candidates.jsonl`, deduped by `zotero_attachment_key` with a `status`
of `pending`, `skipped`, or `resolved` and an `occurrence_count` that increments on repeat timeouts.
A later automatic conversion run never reopens a `skipped`/`resolved` entry — only the commands
below change status.

Use `retry-timeout` to resolve a pending candidate, either permanently:

```powershell
& $python -m zotero_pdf_text retry-timeout --config .\config.json --key <attachment_key> --skip --reason "confirmed to exceed even the scaled timeout cap"
```

which records the decision in `<output_root>/timeout_skip_list.json` (not hardcoded in source, so
no code change/PR is needed) — future conversions of that attachment go straight to the plain-text
fallback. Or retry with more headroom:

```powershell
& $python -m zotero_pdf_text retry-timeout --config .\config.json --key <attachment_key> --retry
```

which defaults to the candidate's `suggested_next_timeout_seconds` (2x the last attempted budget,
capped at 6h); override with `--timeout-seconds` or `--multiplier` (hard-capped at 24h even by
explicit request). A successful retry converts in a fresh, isolated run directory — the originally
converted Markdown file from the earlier run is never overwritten — and only then promotes the
result into `zotero_text_index.jsonl`/`.sqlite`, updating the candidate's status to `resolved`. A
failed retry leaves the index and the candidate's status untouched (its `occurrence_count` still
refreshes, since the nested conversion detects the new timeout the same way any other run would).

Both the skip list and the master candidates file are fail-open: a missing or corrupt file just
means no entries are skipped/reported, same as the drawing-density scan. One extreme outlier
(`CTDZ69WI`, Gelman et al. — Bayesian Data Analysis) is already recorded in `timeout_skip_list.json`
in the field, confirmed by direct testing to run past 13,540s / ~3.75h without finishing even at
the drawing-density-scaled cap.

The MCP server exposes the same workflow: `list_timeout_candidates` (read-only, always available)
to see pending candidates, and `skip_timeout_extraction`/`retry_timeout_extraction` (opt-in via
`--enable-retry-timeout`, each gated behind its own literal `confirm` string) to act on one. See
`README.md`'s "Tool contract" section.

### Orphan Candidates

`mapper.py`'s own `_metadata_candidates` fallback only matches an `orphan_pdf` row's *filename*
against Zotero item titles -- it never opens the PDF at that stage, so a generically-named file
(`1-s2.0-S0022-...-main.pdf`, `downloaded.pdf`) falls through to `orphan_pdf` even when its actual
first-page content would trivially identify a Zotero item already in the library. The explicit,
opt-in `find-orphan-parents` command closes that gap the other way around: it extracts each orphan
PDF's early-page text (the same window/config the rest of the pipeline uses) and scores it with
`classify_identity` -- the same deterministic engine used everywhere else in this codebase -- against
every Zotero item that has no PDF attachment of its own, since those are the only items an orphan
PDF could plausibly belong to.

Findings are written once per run in `orphan_candidates.csv`/`.jsonl` next to that run's own output
folder (`<output_root>/orphan_discovery/<timestamp>`), and merged into a persistent master file at
`<output_root>/index/orphan_candidates.jsonl`, deduped by the pair (`orphan_sha256`,
`candidate_parent_key`) with a `status` of `pending`, `skipped`, or `resolved` and an
`occurrence_count` that increments on repeat discovery. A later automatic run never reopens a
`skipped`/`resolved` entry. Each record carries the orphan's `orphan_sha256`/`orphan_safe_folder_id`
(never its local path), the candidate parent's key/title/DOI/creators, `title_score`,
`author_evidence`, `year_evidence`, `observed_dois`, a `confidence_tier` (`high`/`medium`/`low`,
derived from `classify_identity`'s own status/rule/title_score rather than a second scoring
algorithm), and `identity_rule`.

Use `orphan-candidate` to resolve a pending pairing, either dismissing it:

```powershell
& $python -m zotero_pdf_text orphan-candidate --config .\config.json --orphan-sha256 <sha256> --parent-key <parent_key> --skip --reason "not the same paper"
```

or recording that it was confirmed and already attached:

```powershell
& $python -m zotero_pdf_text link-pdf --config .\config.json --key <parent_key> --file <orphan_pdf_path>
& $python -m zotero_pdf_text orphan-candidate --config .\config.json --orphan-sha256 <sha256> --parent-key <parent_key> --mark-resolved
```

`orphan-candidate --mark-resolved` is bookkeeping only -- it never calls `link-pdf` itself and never
touches Zotero; `link-pdf` (or a human, directly in Zotero) is what actually attaches the file.

The master file is fail-open, same as `timeout_candidates.jsonl`: a missing or corrupt file just
means no candidates are reported.

The MCP server exposes `list_orphan_candidates` (read-only, always available) to see pending
candidates. There is no MCP tool that discovers candidates or attaches anything -- discovery is
CLI-only (`find-orphan-parents`), and attachment stays gated behind the existing CLI-only
`link-pdf` command. See `README.md`'s "Tool contract" section.

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
Stored chunk character ranges refer exactly to their trimmed stored text. FTS ranking deliberately
weights title matches most strongly, citation-key matches next, and body text as the baseline.
Search results include `markdown_sha256` and `matched_fields`. The latter is an ordered subset of
`title`, `creators`, `text`, and `citation_key`, determined by comparing FTS5-highlighted field
values with their original values. A metadata-only match still selects a representative chunk for
navigation, but does not claim that the query occurs in that chunk's body text.

Search normalizes query text into at most 20 word terms. `all_terms` is the default mode,
`any_terms` matches any normalized term, and `phrase` requires the normalized terms in order.

## Confidence Fields

- `classification`: mapper decision such as `mapped_verified` or
  `mapped_unverified`.
- `identity_status`: evidence status such as `verified`, `manual_accepted`, or
  `candidate`.
- `identity_rule`: rule that produced the status.

MCP search and passage responses derive normalized warning codes from these fields:

- `identity_unverified` unless `identity_status` is `verified`, `manual_accepted`, or
  `fulltext_verified` (unknown future values warn conservatively).
- `attachment_match_unverified` unless `classification` is `mapped_verified`.
- `math_extraction_may_be_lossy` when `has_math` is true and `extraction_tool` is not `marker`.

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

The CLI reports `endpoint` for local diagnostics. The optional MCP export response intentionally
does not expose it and is bounded to 500,000 UTF-8 bytes.

`bibtex-add` also reports:

- `references_bib`
- `added_keys`
- `skipped_existing_keys`

## MCP Response Contract

MCP search, passage, and context results never include `source_path` or `markdown_path`. Every
record containing converted-paper material carries a `provenance` object with
`content_trust: "untrusted_source"`, `source_kind: "converted_pdf"`, attachment key, extraction
tool, classification, and identity status. Search and passage results also include a stable
`source_locator` with `attachment_key`, `content_sha256`, `chunk_index`, `char_start`, `char_end`,
`truncated`, `stored_chunk_char_start`, and `stored_chunk_char_end`. `content_sha256` is the
converted Markdown SHA-256: it detects changed content after reconversion or rebuild but is not an
index-generation identifier or a PDF-page locator.

A search locator describes the complete stored chunk. An untruncated exact passage has the same
locator; a truncated exact passage retains attachment/hash/chunk identity, reports the smaller
returned character range, and preserves the complete stored range separately. A leading preview
uses `chunk_index: null` and null stored-chunk spans because it can combine multiple chunks.
Passage responses also include `chunk_count`; exact reads include `previous_chunk_index`,
`next_chunk_index`, and `has_more`, while leading previews set those navigation fields to null.

Every enabled MCP tool advertises a concrete success `outputSchema`. Successful calls return
structured content conforming to that schema. Expected failures return a protocol-level tool result
with `isError: true`, one path-free text message, and no success structured content. The message
contains a stable public code followed by a safe explanation (for example,
`invalid_context_key: Supply exactly one of parent_key and attachment_key.`). Disabled optional
tools are absent from `list_tools`; their calls use MCP's ordinary unknown-tool behavior rather
than a project-defined error code.

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
