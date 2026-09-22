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
- `source_sha256`: SHA-256 of the source PDF this text was extracted from, hashed at conversion
  time. Empty means *not known*, which is not the same as unchanged: a `skipped_existing` row
  reused Markdown converted from whatever the PDF was at the time, so hashing the file now would
  record confident provenance for text that may predate it. `audit-library` treats an empty value
  as unverifiable and never as evidence that the source is current.
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
every Zotero item that has no *working* PDF attachment of its own: either no PDF attachment row at
all, or a PDF attachment row whose recorded path no longer resolves to a real file on disk (moved,
renamed, or deleted outside Zotero's own management). These are the only items an orphan PDF could
plausibly belong to. Path resolution for the stale-attachment check mirrors `mapper.py`'s own
attachment-path convention exactly (shared via `identity.resolve_attachment_paths`): Zotero's
`attachments:`-relative paths resolve against `config.linked_attachments`, and both Windows
drive-letter and POSIX absolute paths are recognized regardless of the OS this tool runs on.

Only `high`-confidence pairings are reported -- `classify_identity`'s own `verified` status (a DOI
exact match, or a strong title match corroborated by an author/year hit). A fuzzy title-match score
alone, on a result `classify_identity` itself left `unverified`, is not trusted: real-library smoke
testing found this to be almost pure noise, since an edited volume's individual chapter/section
entries ("Citations", "Index", "Preface", each its own Zotero item with no PDF of its own) get a
trivially high `fuzz.partial_ratio` score against almost any academic PDF's text once the title is
one or two common words, regardless of the specific threshold chosen.

Findings are written once per run in `orphan_candidates.csv`/`.jsonl` next to that run's own output
folder (`<output_root>/orphan_discovery/<timestamp>`), and merged into a persistent master file at
`<output_root>/index/orphan_candidates.jsonl`, deduped by the pair (`orphan_sha256`,
`candidate_parent_key`) with a `status` of `pending`, `skipped`, or `resolved` and an
`occurrence_count` that increments on repeat discovery. A later automatic run never reopens a
`skipped`/`resolved` entry. Each record carries the orphan's `orphan_sha256`/`orphan_safe_folder_id`
(never its local path), the candidate parent's key/title/DOI/creators,
`candidate_had_stale_attachment` (true when the candidate qualified because its existing PDF
attachment path went stale, rather than because it never had one -- useful for deciding relink vs.
attach fresh), `title_score`, `author_evidence`, `year_evidence`, `observed_dois`, a `confidence_tier` (always
`high`, derived from `classify_identity`'s own `verified` status rather than a second scoring
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

### Duplicate Attachment Groups

`find-duplicate-attachments` groups a dry-run's mapping-report rows by `(zotero_parent_key,
sha256)` -- attachments on the same Zotero item with byte-identical file content. Within a group,
exactly one filename with no trailing-suffix reading (`Name.pdf` vs. `Name2.pdf`/`Name 1.pdf`/
`Name (1).pdf`) is required, and every other filename must strip down to that exact filename, to
auto-resolve; anything else (a genuinely different edition, an unexpected naming scheme, or a
filename that merely ends in a digit as part of its own title without another group member
matching its stripped form) is written as ambiguous instead of guessed. This is deliberately
narrower than the fuzzy/near-identical-text matching explored manually for this problem (see
`docs/troubleshooting.md` item 6) -- byte-identical content plus an unambiguous filename pairing is
the only case safe to resolve without a human looking at it.

`duplicate_groups.csv`/`.jsonl` (resolved) and `ambiguous_duplicate_groups.csv`/`.jsonl` are written
per-run under `<output_root>/duplicate_attachments/<timestamp>`, alongside `duplicate_trash_plan.jsonl`
-- a `trash_item` write plan (same schema as `zotero-write plan`'s output) covering every resolved
group's "drop" attachments, with `approval_status="pending"`. This command never calls Zotero itself;
running the plan through `zotero-write approve`/`validate --require-approved`/`apply --approve` is
what actually trashes the redundant attachments (see `docs/operations.md`'s "Duplicate Attachment
Cleanup" and "Approval-Gated Zotero Writes" sections).

## Managed Index Generations

Under `<output_root>/index/`, the managed layout consists of:

- `generations/<generation-id>/` — one immutable, complete publication of the derived index:
  `index.jsonl` (the JSONL sidecar), `index.sqlite` (the FTS database), and
  `artifact_manifest.json`. Generation IDs match `YYYYMMDDTHHMMSSZ-<8 hex>`; anything else is
  rejected on read, and resolved directories are containment-checked beneath `generations/`.
- `current.json` — the single atomically replaced publication pointer:
  `schema_version` (currently 1), `current_generation`, `previous_generation` (retained for
  rollback, `null` on the first publish), and `published_at` (UTC ISO-8601). It stores only
  validated relative generation IDs, never filesystem paths.
- `publish_journal.json` — transient; exists only between "generation validated" and "pointer
  replaced" during a publication. Fields: `schema_version`, `state` (`publishing`),
  `generation_id`, `previous_pointer` (the full prior pointer object or `null`), `started_at`.
  The next write command consumes it deterministically: it completes the publication if the
  named generation validates, otherwise removes the partial staging and leaves the prior
  pointer untouched.

`artifact_manifest.json` fields: `schema_version`, `generation_id`, `created_at`, `command`
(the publishing command), `chunk_chars`/`overlap_chars` (build parameters, preserved by
successor generations), `records`/`chunks`/`total_chars`/`total_words`, and `files` — a map of
`index.jsonl`/`index.sqlite` to `{sha256, bytes}`. Validation before every publish re-derives
the checksums and DB counts and refuses on any mismatch. Builds also refuse duplicate
`zotero_attachment_key` values, which would otherwise make full-text retrieval return an
arbitrary row.

There is no legacy fallback: readers and writers both require the managed layout, and a root
without `current.json` yields a hard error directing you to the one-time `rebuild-index`
migration (which snapshots a legacy `zotero_text_index.jsonl` when present).

## JSONL Sidecar

The JSONL sidecar (`index.jsonl` inside a generation; legacy `zotero_text_index.jsonl`)
contains one record per available converted full text:

- Zotero keys
- title, creators, year, DOI, citation key
- source PDF and Markdown paths
- Markdown SHA-256
- extraction tool
- character and word counts
- page count
- mapping classification and identity status
- `has_math`: boolean, carried from the manifest
- `source_sha256`: carried through from the conversion manifest, never recomputed at index time.
  Re-hashing the PDF here would record today's file against text extracted from an older one.
- `indexed_at`: UTC timestamp of when this record was built
- full Markdown-derived `text`

Reconvert a single paper with `reconvert-math --key <attachment_key>` when `has_math` is true and
the notation needs to be trustworthy (LaTeX-aware marker-pdf extraction, in place). This is
just-in-time only — roughly 27s/page, so it is not meant for bulk reconversion.

## SQLite FTS

The FTS database (`index.sqlite` inside a generation; legacy `zotero_text_index.sqlite`)
contains:

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

## Library Audit

`audit-library` compares four independent views of the same library -- Zotero's own attachment
inventory, the mapper snapshot, the filesystem, and the published index generation's JSONL --
and reports where they disagree. It is read-only: it moves, renames and rewrites nothing, and it
never opens the live `zotero.sqlite` at all: it copies the database and its journalling
sidecars to a temporary directory and reads the copy. No connection mode achieves the same
thing. A read-write connection runs recovery or a checkpoint on open and on close, rewriting the
main database and deleting an outstanding WAL even when every statement issued is a `SELECT`.
`mode=ro` forbids those writes but still creates the `-shm` that any reader of a WAL database
needs, which is a new file in the user's Zotero folder. `mode=ro&immutable=1` creates nothing
but cannot see the WAL at all, and fails outright when the rows live in an uncheckpointed one.
Copying costs one file copy per audit and is the only option that is both complete and
genuinely non-writing.

Both the `-wal` and the `-journal` are copied, because a database is in one journalling mode or
the other and the audit cannot assume which. They matter for opposite reasons. The `-wal` holds
committed data the main file does not have yet. The `-journal` holds the bytes that *undo*
uncommitted data the main file already does have: in rollback mode SQLite spills dirty pages
into the main database before the commit, so a copy taken without the journal exposes a
transaction that may never land, and does so silently — such a copy is structurally intact
and `PRAGMA integrity_check` returns `ok`. Carried along, the journal is hot in the copy and
SQLite rolls it back on open, which is the committed state the audit wants.

The `-shm` is deliberately not copied. It is the WAL index: transient cache and lock state that
SQLite reconstructs from the `-wal` when a database is first opened, so leaving it behind loses
nothing, including rows that live only in an uncheckpointed WAL. It also changes on plain reader
activity, which would feed unrelated churn into the check below.

A file-level copy of a database being written is not a transactionally consistent snapshot, so
the copy is verified rather than assumed: after copying, the SHA-256 of the copied database and
WAL are compared against the source's, and any mismatch discards the copy and retries. Size and
modification time are not sufficient, because a checkpoint rewrites existing pages in place and
can change the database's content while leaving its size identical, and timestamp granularity is
a filesystem property rather than a guarantee. A database that will not hold still is reported
as an unavailable inventory rather than read, with the reason in `inventory_error`.

The residual window is narrow and worth stating. The two digests are taken one after another, so
a source that changed and changed back between them would pass. A rollback journal belonging to
a transaction spanning several attached databases names a super-journal that is not copied, and
SQLite will not treat such a journal as hot without it; Zotero does not commit across attached
databases, so this is a stated limit rather than a handled case. SQLite's backup API or
`VACUUM INTO` would close all of it, but both require opening the live database, which creates
the `-shm` this approach exists to avoid.

Membership comes from Zotero, not from the snapshot. The mapper walks source *files*, so an
attachment whose PDF has been moved or deleted produces no mapping row at all; reading membership
off the snapshot would make `missing_source` nearly unreachable and would report such an
attachment as `orphaned_index`, claiming Zotero dropped it when the file is what went missing.
A successfully read inventory is the authority *outright*, not merely an additional source: a
mapping row proves membership as of the last `dry-run`, so unioning the two would let a stale row
vouch for an attachment the user has since deleted from Zotero and report it `current` when the
honest answer is `orphaned_index`. If Zotero's database cannot be read the audit still runs, but it answers no membership question
at all: every attachment is reported `membership_unchecked`, and `current`, `unindexed` and
`orphaned_index` are withheld rather than guessed. The mapping snapshot is not a fallback
authority here, because it proves membership as of the last `dry-run` and the question is
membership *now*. File-level findings are unaffected and still reported, since a changed PDF or
a stale Markdown file is a fact about disk regardless of who owns the attachment. The report
carries `inventory_available: false` and `inventory_error`, the failing call's own message:
a database that will not settle is resolved by closing Zotero and re-running, a permission or
schema failure is not, and the flag alone cannot tell those apart.

The canonical layout is recorded per item as evidence (`canonical_markdown_exists`) but is not
classified, because nothing writes to `library/` yet and a status derived from it would fire on
every attachment while meaning nothing. The audit reads the generation JSONL only and never
opens the SQLite index, so a JSONL/FTS divergence within one generation is out of its scope.

**Statuses are a set, not a bucket.** An attachment can hold several at once (a PDF that moved
*and* whose metadata changed *and* whose Markdown predates the current source holds three), so
the reported counts overlap and do not sum to the attachment total. Only `total_items` partitions
the library.

- `current`: indexed, eligible, source and Markdown present, no other status. The only status
  that excludes all the others.
- `unindexed`: mapped and library-eligible, but absent from the published index.
- `stale_markdown`: the Markdown on disk differs from what the index holds, so search returns
  text that no longer matches the file.
- `source_changed`: the source PDF differs from the one this text was extracted from. Compared
  against the mapping snapshot's hash by default and against a freshly computed hash under
  `--full`; either way it fires only when the indexed record actually carries a source hash.
- `source_unchecked`: the index records a source hash and this audit had nothing to compare it
  against, so the item was never examined. Fires when the attachment was relinked -- the
  snapshot's hash then describes the previous file -- or when it never reached the mapper and so
  has no snapshot hash at all. It exists because the alternative is silence, and silence here is
  read as `current`: a clean bill of health for a file nobody looked at. Suppressed when
  `missing_source` already says the same thing more precisely, and resolved by `--full`, which
  hashes the file the audit can actually see. Distinct from `source_provenance_unknown`, which
  counts the opposite gap -- records with no *indexed* hash, converted before the field existed.
- `metadata_changed`: title, DOI or citation key differ between Zotero and the index. Compared
  against Zotero's live record whenever the inventory could be read, falling back to the
  snapshot only when it could not: the snapshot's copy is only as current as the last `dry-run`,
  so an edit made in Zotero afterwards is invisible to it, and an attachment that never reached
  the mapper carries no snapshot metadata at all. Creators
  and year are deliberately excluded -- they change during ordinary bibliographic tidying and
  would fire across a large share of the library without indicating real drift.
- `missing_source`: the source PDF is gone from disk.
- `missing_markdown`: the converted Markdown is gone from disk.
- `orphaned_index`: the index holds a row for an attachment Zotero no longer represents. Decided
  against Zotero's inventory, so an attachment Zotero still lists whose PDF has vanished is
  reported as `missing_source` instead.
- `membership_unchecked`: Zotero's inventory could not be read, so the audit cannot say whether
  the library still contains this attachment. It applies to *every* attachment in such a run,
  not only to index-only rows, and that is the point: when the count equals `total_items` it
  says plainly that this run could not check membership for anything. Distinct from
  `orphaned_index` because the advice is opposite: that one says the row can go, this one says
  do not act until Zotero can be consulted. The mapping snapshot cannot settle it either way
  — it proves the attachment existed at the last `dry-run`, not that it exists now, and an
  attachment whose PDF went missing never reaches the snapshot in the first place.
- `unverified_indexed`: Zotero represents the attachment but its identity was never verified, yet
  it is in the published index and is being returned by search. Distinct from `orphaned_index`:
  the repair is to verify the identity, not to drop the row.
- `duplicate_key`: more than one index row shares an attachment key.

Two counts sit outside the status vocabulary and explain it:

- `ineligible_items`: attachments that are unverified or unmapped and therefore correctly absent
  from the library. They report no status at all, and this count is what distinguishes that
  intended silence from a broken rule.
- `source_provenance_unknown`: indexed records carrying no `source_sha256`, for which
  `source_changed` cannot be evaluated at all. Until those records are reconverted, a low
  `source_changed` count means *not measured*, not *not drifted*.

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
`source_locator` with `attachment_key`, `content_sha256`, `chunk_sha256`, `chunk_index`,
`char_start`, `char_end`, `truncated`, `stored_chunk_char_start`, and `stored_chunk_char_end`. `content_sha256` is the
converted Markdown SHA-256: it detects changed content after reconversion or rebuild but is not an
index-generation identifier or a PDF-page locator.

`chunk_sha256` is the SHA-256 of one stored chunk's text. It is derived at read time rather than
stored as a column, so an index built by any version can be verified per chunk without being
rebuilt; search computes it only for the rows it returns. It is `null` on a preview passage, which
is assembled from several stored chunks and so has no single chunk hash.

Passing either hash back to `get_fulltext_chunk` makes that detection enforceable: the value is
compared against what the index holds now, and a mismatch answers `stale_locator` instead of
retrieving the chunk. `chunk_sha256` requires an exact `chunk_index` and is the only hash that
verifies one: it refuses exactly when the cited passage was replaced, and it takes precedence when
both are supplied, so the document hash is not additionally enforced.

`content_sha256` covers the whole converted document. For an exact `chunk_index` it is rejected
**on its own** as `invalid_content_sha256` rather than answered, because for that request it is not
a coarser check but the wrong one: it verifies document content, not the meaning of a `chunk_index`.
Re-chunking the same text -- `rebuild-index --chunk-chars`/`--overlap-chars` -- moves chunk
boundaries while leaving `content_sha256` identical, so an old index would select a different
passage and a document-hash check would accept it, returning other text under the caller's citation
and reporting success. Only `chunk_sha256` can answer that question, and search returns it in the
same locator. Sending both hashes stays valid -- the chunk hash decides, as above -- and without a
`chunk_index` the document hash verifies the document the bounded leading preview was taken from.
What a
chunk hash guarantees is textual rather than positional: the passage returned is the passage cited,
while the surrounding document may have shifted, which is why character offsets in the response are
always the current ones rather than the request's. Verification is opt-in — a caller that omits both
hashes retrieves unverified, which is how a document is read without having searched for it first.

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
