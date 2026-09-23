# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Local image OCR is held back from the release notes, not from the release artifact. The code below
is packaged with every tagged install and `ocr-images` is a registered CLI command, so it is
unannounced rather than absent: present, inert unless explicitly configured and invoked, and not yet
validated for general use. Its config shape and output conventions may still change. It moves into a
dated section once it has been stress-tested against a large library.

### Added

- `library_status` MCP tool: the same health answer on the read-only MCP surface, deliberately
  shaped so index row counts can never be read as library coverage. It reports the published
  generation's statistics and the audit's comparison as two separate fields. The comparison is
  `null`, with a reason, only when none could be produced at all: no config, no snapshot, a
  failed audit, or a publication landing mid-measurement. When the audit ran but Zotero could
  not be read it is present and explicitly partial -- `inventory_available` false, membership
  statuses withheld under `membership_unchecked`, `attachments_compared` null -- so a client
  must check `inventory_available` rather than treat any non-null comparison as complete. It takes no arguments -- the mapping snapshot is discovered server-side so no local
  path crosses the boundary, and the expensive `--full` re-hash is not reachable from MCP.
  Only the audit half is cached, for two minutes; the index half is measured on every call,
  so a re-publish cannot be reported under the previous generation's id. The response states
  `from_cache` and `cache_age_seconds`, so a reused answer is never presented as freshly
  measured. `inventory_error` reports the failing exception's type plus a written
  explanation rather than its message, because several of those messages embed the Zotero
  database path. When no index generation is published the tool answers
  `index_not_published` and names `rebuild-index` instead of falling through to the generic
  `operation_unavailable`. `attachments_compared` is withheld when Zotero could not be read,
  since it is not a library total in that case. The cached audit is keyed on the index
  generation *and* the mapping snapshot it ran against, and the payload reports the audited
  generation, so neither a `rebuild-index` nor a `dry-run` can leave a stale comparison in
  place. The audit is also pointed at the same index root the server reads, and a response
  whose two halves would describe different generations is refused rather than returned. An
  audit that could not read Zotero is not cached at all, because its own message tells the
  user to close Zotero and ask again.

- `library-status`: the summary form of `audit-library`, reporting the same read-only
  comparison as counts without the per-item evidence. It names the published generation and its
  publication time, states that the health counts overlap, and says in its own output that these
  are source-library health counts rather than index statistics. The audit that produces them
  has existed since v0.5.0 but was reachable from no command.

- `ocr-images --key <ATTACHMENT_KEY>`: recover the equations, tables and figure content that
  conversion left stranded in extracted PNGs. `pymupdf4llm` pulls vector-drawn display equations
  out of a PDF into their own crop files and leaves an opaque `![](…png)` placeholder behind, so
  that notation never reaches the text index. This command walks the converted Markdown,
  classifies each referenced crop, asks a locally served OCR model the matching question
  (formula / table / figure recognition), and splices the answer back at the placeholder's
  position — equations and tables replace their placeholder, figures keep the image link and gain
  a searchable description. Because the crops are already isolated regions, no PDF re-rendering
  or layout analysis is involved and **no new dependency is added**: the model is reached over
  HTTP through Ollama using only the standard library, on GPU or CPU. `--dry-run` prints the full
  classification table without contacting the model. Runs are resumable through a content-keyed
  cache beside the crops, guarded against accidental re-runs, and commit under the same pipeline
  write lock as every other index writer, and refusing to commit if the original changed while
  OCR was running. Configured through an optional `image_ocr` block; `check-setup` reports the
  runtime's availability.
- Image OCR is non-destructive: the original converted Markdown is never modified. The enriched
  result is written to a sibling file (`<stem>_ocr_eq.md` by default; `image_ocr.enriched_suffix`
  is configurable, `""` overwrites in place) and the index is repointed at it, so the original
  remains on disk as a permanent anchor and search still returns the recovered content. Each run
  regenerates the sibling from the pristine original, so `--force` always starts from clean
  placeholders.
- Crop classification (`classify_crop`) decides per crop whether it is a formula, table, figure,
  or decoration to skip, which selects the OCR task prompt. Alongside crop geometry and
  neighbouring caption / picture-marker text, it uses a compression signal — compressed PNG bytes
  per pixel — to tell a solid decorative bar from a wide display equation, which are otherwise
  indistinguishable by shape or surrounding text and need no image library to separate. A "Table N"
  or "Figure N" mention is treated as a caption only for a blockier crop, so a thin single-line
  equation strip beside a running-prose cross-reference (e.g. "Table 4 shows the coefficients")
  stays a formula rather than being mislabelled — a false positive observed on real documents.
- Synthetic OCR validation corpus (`tests/fixtures/ocr_corpus/`, built by
  `tools/build_ocr_corpus.py`). The suite previously had no real PDF at all — every test wrote
  `b"%PDF"` and mocked the extractor — so nothing exercised real PDF → real crops. The corpus is a
  LaTeX document covering equation varieties (numbered, unnumbered, multi-line aligned, matrix,
  cases, quantifier- and Greek-heavy), a table, a captioned vector figure, and adversarial
  negatives: a decorative separator band whose proportions match a display equation, a publisher
  spine bar, and a solid logo block. Elements are tied to their observed crops through marker
  tokens in the text layer rather than by ordering or filename, and the generated PDF is committed
  so CI needs no LaTeX toolchain. Ground truth is recorded from an observed conversion run rather
  than assumed, since whether a construct becomes a crop is a property of the extractor.
- `ocr-images` re-roots converted-output paths recorded by a previous machine, matching the
  deepest suffix that exists under `output_root`, and resolves crop PNGs by filename rather than
  by the absolute image link embedded in the Markdown. A library that moved between machines
  keeps working instead of reporting that it has no images.
- Real-article classification benchmark tier (`benchmarks/preprints/`) plus a reusable scoring
  harness (`benchmarks/scoring.py`) and CI test (`tests/test_benchmark_preprints.py`). It scores
  `classify_crop` on 103 crops from open first-author preprints (used with the author's
  permission; see `benchmarks/preprints/ATTRIBUTION.md`), complementing the synthetic corpus with
  real-world content. The benchmark freezes the classifier's *inputs* — each crop's geometry and
  the two neighbouring Markdown lines it reads — not a stale prediction, so CI re-runs the current
  algorithm offline with no PDF, model or network. Source PDFs are fetched on first use into a
  git-ignored cache and never redistributed; only the small crops, labels and short caption
  snippets are tracked. Assertions are invariants rather than a per-crop table (accuracy floor,
  every equation reaches the formula prompt, mistakes are only figures conservatively over-routed
  to formula), so the tier can grow with harder examples without churn.
- Recognition-quality scoring (`benchmarks/recognition.py`) that measures the layer *after*
  classification: given a crop's OCR output, how much of the expected notation actually survived.
  It scores **token recall** against the corpus's `expected_tokens` rather than an exact LaTeX
  match — many spellings render the same mathematics — with a normalization that erases meaningless
  differences (backslashes, whitespace) while keeping case significant (`\Gamma` ≠ `\gamma`). The
  metric is pure and unit-tested offline (`tests/test_recognition_scoring.py`); the live corpus
  recognition tier now reports aggregate micro/macro recall with per-element and corpus-wide floors
  instead of a pass-if-any-token-appears check. A pressure-test harness
  (`tools/score_recognition.py`) runs any Ollama-served model over the corpus and reports recall,
  with `--model` to compare models on identical crops.
- End-to-end search-recovery test (`tests/test_recognition_scoring.py`): drives canned OCR text
  through the real `render_replacement` → `splice` → `build_fts_index` → `search_fts` path and
  proves a term that lived *only* inside an equation image is unfindable before enrichment and
  findable after. This promotes the plan's manual "search now hits" verification into an automated,
  offline (no model, no network) invariant — the payoff of the whole feature, guarded in CI.
- Engine comparison harness (`benchmarks/engines.py`, `tools/compare_engines.py`) that scores whole
  *engines* against each other rather than pieces of one: the crop path (`crop-mvp`), the
  whole-document path (`marker`), and MinerU as a declared-but-unwired candidate. All three are
  judged on the one axis they share — the final Markdown a reader searches — so a crop engine that
  splices and a whole-document engine that re-renders become commensurable. Per-element scoring
  inside a foreign engine's output works by anchoring on the corpus's `CORPUSMARK` tokens, which
  survive any engine that reads the page, so a token only counts where its element actually is: an
  engine that swapped two elements' content scores zero where a document-wide search would have
  scored it 100%. An element whose anchor is missing, or repeated (a duplicated text layer, a
  contents block), is reported *and* scored zero — dropping the prose around an element is itself an
  extraction defect. The harness reports its own **comparability** rather than overstating its
  numbers: because a missing anchor widens its neighbour's window, a lost anchor inflates that
  neighbour's recall — and where the lost element carried no expected tokens, both aggregate figures
  invert in favour of the *worse* engine. That limitation is measured, pinned by a test, printed in
  the report and exposed as `comparable` in the JSON, since it cannot be scored away without
  ground-truth offsets. Comparison logic and hardware-verdict parsing are pure and unit-tested
  offline (`tests/test_engine_comparison.py`); the runner detects the GPU through `nvidia-smi` (no
  new dependency, no `torch` import), takes `--config` so the crop path is benchmarked against the
  model actually configured, and exits non-zero when nothing was attempted rather than emitting an
  empty comparison. An engine that is *absent* is reported and omitted — there is nothing to say
  about it — while one that *ran* and then failed or timed out stays in the comparison scoring zero,
  with the reason beside it, so a broken engine places last in the ranking instead of vanishing from
  it and leaving the survivor looking unopposed. The
  GPU-aware selection policy (`recommend_engine`) is a documented stub with its trade-off written
  out; three tests are staged against its contract and skip until it exists.

### Changed

- `coverage-report` is now `index-stats`, and its aggregates are computed in SQL. The old
  command reported index row counts under a word that means "share of the library", which is a
  question index rows cannot answer: an attachment Zotero holds but that was never converted is
  absent from every number in the report. The payload now states its own scope
  (`indexed_snapshot`) and names the generation and publication time it read, so two reports
  taken across a re-publish are distinguishable. `coverage-report` remains as a deprecated alias
  that warns on stderr, leaving `--json` output on stdout parseable.
- Index aggregates no longer pull every metadata row into memory. The previous implementation
  ran `SELECT *` over the whole metadata table and counted in Python to produce eight numbers,
  so its cost scaled with the library and with the width of the text-bearing columns it never
  read. The counts, their key types and their names are unchanged, and a parity test recounts
  the same rows in Python to keep them that way.

- `ocr-images` preserves an attachment's recorded `source_sha256` through its index upsert.
  Enrichment rewrites derived Markdown from images already extracted, so the hash recorded
  against the attachment still describes the right PDF and must survive the upsert rather than
  being recomputed or cleared.

### Fixed

- Path-containment assertions on the MCP surface could not fail on Windows. They compared a
  local path against `json.dumps` output, where every backslash is escaped, so the raw path
  was never a substring of the serialized response. They now walk the response structure
  and compare against the values. This is what let an absolute-path leak reach review
  with a green suite.

- Figures whose caption label sits two lines above them are no longer routed to the formula
  prompt, where the splice replaced their image link with LaTeX invented from a plot. In the
  common journal layout the label (`Figure 3`) is separated from the crop by an italicised title
  line, so the label the classifier looks for is never on a line it can see; only the title above
  and the closing `Note.` line below are reachable. Recognising that pair as a caption block
  raises real-article routing accuracy from 85.4% to 100% (103 labelled crops), recovering 15
  figures — slider screenshots, balance-beam diagrams and density curves — that geometry alone
  cannot distinguish from wide display equations. Both halves are required: measured across the
  whole converted library (75,205 crops), a rule keyed on the note line alone would have claimed
  two real display equations, because a note line terminates a caption in one document style but
  opens a pedagogical aside in another. `tests/test_image_ocr.py` carries both shapes as
  regression cases, since the benchmark tier now scores 100% either way and can no longer tell the
  two rules apart. The caption label itself is read from one line further back than the classifier
  previously looked (`CropRef.text_lead`), because the same layout carries tables — so the block
  establishes only that a crop is *captioned*, and the label decides whether it reaches the figure
  or the table prompt. Without that, a wide table would have been forced through the figure prompt,
  keeping its image and gaining prose instead of its cells. The label must *be* the lead line rather
  than open it, since the existing caption patterns are anchored only at the start and so also match
  a running-prose cross-reference ("Table 4 shows the coefficients") — the very shape the aspect
  guard exists to reject, and which this rule deliberately runs outside of. A caption title is
  likewise required to be one emphasised span filling its line, which is not the same as carrying no
  interior emphasis: real titles contain inline markup such as a superscript, so what disqualifies a
  line is prose *resuming* between spans.
- Records whose math was recovered by a math-capable pass no longer carry a spurious
  `math_extraction_may_be_lossy` warning in MCP responses. The check was an equality test against
  a single extractor name (`marker`); it is now a per-component membership test, so a composite
  provenance label such as `pymupdf4llm.to_markdown+glm-ocr` is recognised while an unknown
  extractor still warns.


## [0.5.0] - 2026-09-22

Read-only library auditing, recorded source provenance, and a hardened path for reading a live
Zotero database.

`audit-library` compares four independent views of the same library -- Zotero's attachment
inventory, the `dry-run` mapping snapshot, the files on disk, and the published index
generation's JSONL -- and reports where they disagree. It moves, renames and rewrites nothing.
It is also the evidence the deferred canonical-layout migration was always meant to rest on,
rather than an intuition about whether the timestamped-run layout hurts yet.

**This release changes the index schema, and every existing index needs one `rebuild-index`.**
What that buys and what it does not is in Changed below: provenance is established per
attachment as it is genuinely reconverted, so until that coverage is high a low `source_changed`
count means *not measured* rather than *not drifted*, which is what `source_provenance_unknown`
reports.

Reading a live `zotero.sqlite` without writing to it turned out to be the substance of the
release. Six read-only connections built their `file:` URI by interpolating the path, which a
`#` in a directory name silently truncated into a read-write open somewhere else; the audit's
own snapshot went through several corrections before it could be trusted, ending in a refusal to
hand SQLite a rollback journal that names a file for it to delete. Those are Fixed entries
below, and `docs/data-dictionary.md` records the limits that remain.

### Added

- `audit-library --mapping-report <snapshot>`: a read-only drift report. It compares four
  independent views of the same library -- Zotero's attachment inventory, the `dry-run` mapping
  snapshot, the files on disk, and the published index generation's JSONL -- and reports where
  they disagree. Zotero's inventory, not the snapshot, is the authority on membership: the
  mapper walks files on disk, so an attachment whose PDF is already gone never reaches the
  snapshot at all. When the inventory cannot be read the audit withholds every membership
  conclusion and reports `inventory_available: false` with the reason, rather than silently
  answering a different question from the snapshot. It moves, renames and rewrites nothing, and it reads the live
  `zotero.sqlite` by copying it to a temporary directory rather than opening the live file, so
  that SQLite cannot checkpoint, recover or create a sidecar inside the user's Zotero folder.
  The `-wal` is copied alongside it, so attachments Zotero committed since its last checkpoint
  stay visible. Statuses are a **set**, not a bucket: an attachment can hold
  several at once, so the reported counts overlap and do not sum to the attachment total,
  and the output says so. Two counts sit outside the status vocabulary to keep it honest --
  `ineligible_items` explains why correctly-quarantined attachments report nothing at all, and
  `source_provenance_unknown` states how many records cannot be checked for source drift yet.
  `--full` re-hashes every source PDF from disk; without it `source_changed` is still evaluated,
  against the hashes the mapper recorded during `dry-run`, so the default mode is current as of
  the snapshot rather than blind. Also adds `library_status()` as a data function for
  later CLI/MCP use, reporting health categories and last successful publication rather than
  calling index row counts "coverage".
- `source_unchecked`: an audit status for the gap between "the source changed" and "nobody
  looked". The index records a source hash, but the default audit has nothing to compare it
  against -- the attachment was relinked, so the snapshot's hash describes the previous file, or
  it never reached the mapper and has no snapshot hash at all. Without a status such an item
  falls through to `current`, certifying a file the audit never examined. `--full` resolves it
  by hashing what is actually on disk.
- `membership_unchecked`: an audit status for an attachment whose membership cannot be decided
  because Zotero's inventory could not be read. Index-only rows were previously reported as
  `orphaned_index`, which recommends dropping a row that may still be perfectly valid —
  an attachment missing from the mapping snapshot may simply have lost its PDF.
- `unverified_indexed`: an audit status beyond the nine originally planned, naming an attachment
  whose identity was never verified but which is nonetheless in the published index and being
  returned by search. Distinct from `orphaned_index` because the repair differs: verify the
  identity, rather than drop a row Zotero no longer represents.

### Changed

- **Breaking (index schema).** Index records now carry `source_sha256` -- the SHA-256 of the
  source PDF the text was extracted from, hashed at conversion time -- and `indexed_at`. Without
  recorded source provenance, "the source differs from what was indexed" was not computable at
  all, and `audit-library` could not report `source_changed` honestly. Readers reject an index
  built before this change with a named error pointing at `rebuild-index`, rather than failing on
  a missing column deep inside a query. Two consequences worth stating plainly: every existing
  index needs one `rebuild-index`, and because a rebuild re-reads conversion manifests that carry
  no source hash, existing records keep an empty `source_sha256` until they are reconverted. Until
  then a low `source_changed` count means *not measured*, not *not drifted* -- which is what
  `source_provenance_unknown` exists to report.
- `reconvert-math` records the hash of the PDF it actually extracted, verified unchanged across
  the extraction. A full reconversion reads whatever is at the source path now and must name
  that file rather than inheriting the previous record's hash. Publishing requires the
  provenance fields to survive `get_item_context`, so they are now part of its metadata
  projection -- without that, a reconversion republished an empty hash even when the index held
  a valid one.
- `load_attachment_records` closes its SQLite connection on every path, not only on success. A
  failing query previously leaked the handle until garbage collection, which on Windows holds a
  lock on a live Zotero database.
- `source_sha256` is deliberately empty for `skipped_existing` conversions. That row reused
  Markdown converted from whatever the PDF was at the time, so hashing the file now would record
  confident provenance for text that may predate it. Empty means *not known*, and is never read
  as *unchanged*.

### Fixed

- Conversion now hashes the source PDF before *and* after extraction and refuses to publish the
  Markdown if the bytes changed in between. Hashing only afterwards would attach the new file's
  hash to the old file's text — worse than recording no hash, because `audit-library` would
  then read `source_changed` as clean and the drift would never surface. `reconvert-math`
  carries the same guard; `ocr-images` keeps the original hash, which is correct there because
  it operates on already-extracted images rather than on the PDF.
- Every read-only connection to `zotero.sqlite` now builds its `file:` URI with `as_uri()`
  instead of interpolating the path. A `#` anywhere in the path — a legal directory name —
  ended the URI and turned `?mode=ro` into a fragment, so SQLite opened the *truncated* path
  under its default read-write/create mode: a stray file appeared in the user's Zotero folder
  and the read-only guarantee was silently dropped, surfacing only as a `no such table` error.
  A `?` in the path misparsed the same way. This affected the three pre-existing
  `mode=ro&immutable=1` readers — `find_item_by_doi`, `check_pdf_attachment` and
  `load_items_without_pdf_attachment` — as well as the audit's new one. Three further readers
  built the same URI by hand and now go through the helper too: `ingestion.load_existing_items`,
  which `ingest-candidates` points at the live Zotero database, and `fts.connect_readonly` and
  `artifacts.validate_generation`, which open the project's own index beneath the configured
  `output_root` and were exposed to a `#` there by the same mechanism. `load_existing_items`
  also closes its handle on every path now rather than leaking it to garbage collection on a
  query error — on Windows that handle is a lock on the user's live database.
- `ingest-candidates` now reads a temporary copy of `zotero.sqlite` rather than opening the
  live file. Escaping the URI stopped the truncated-path write but not this one: `mode=ro`
  forbids writes to the database and still creates the `-shm` and `-wal` that any reader of a
  WAL database needs, in the database's own directory. Zotero runs in WAL mode, so a dry run
  was leaving two new files in the user's Zotero folder. `load_existing_items` keeps its
  `mode=ro` connection and now documents that it must be given a copy; `zotero-write` already
  supplied one.
- `audit-library` reads a temporary copy of `zotero.sqlite` instead of opening the live file.
  `mode=ro` forbids writes but still creates the `-shm` sidecar any reader of a WAL database
  needs — a new file inside the user's Zotero folder, which the command promises not to
  produce. `immutable=1` creates nothing but cannot see the WAL, and fails outright when the
  rows live in an uncheckpointed one. Copying is the only option that is both complete and
  genuinely non-writing.
- `audit-library` reads `current.json` once and resolves the generation JSONL, the generation
  id and `last_published_at` from that single read. Reading the pointer again let a concurrent
  publish pair one generation's id with another's rows or timestamp, and the report serialised
  that false provenance as fact.
- `audit-library` fails with a named error when `current.json` names a generation whose
  directory or JSONL is missing. It previously returned the generation id with zero rows, so a
  broken publication was reported as every eligible attachment being `unindexed` — the
  reading most likely to send someone re-converting a library that is fine.
- The snapshot of `zotero.sqlite` is verified by content: the SHA-256 of the copied database
  and WAL are compared against the source after copying, and any mismatch discards the copy and
  retries. A file-level copy of a database being written is not a consistent snapshot, and
  SQLite's per-frame checksums do not detect a main database and a `-wal` that were never a
  matching pair. Size and modification time were not sufficient to catch it: a checkpoint
  rewrites pages in place, so the database can change content while keeping its exact size.
  A database that will not settle is reported as an unavailable inventory rather than read.
- The snapshot's known limits are documented rather than implied: the revert-in-flight window,
  the unverified assumption behind the super-journal case, the configurations no test covers,
  the I/O cost per audit, and the unavailability of the audit while the database is under
  sustained write load. `docs/data-dictionary.md` also records why the file copy is kept as the
  only inventory path over Zotero's local HTTP API, which is disabled by default.
- The snapshot refuses a rollback journal that names a super-journal instead of letting SQLite
  open it. A journal's trailer can carry a super-journal pathname -- an absolute path -- and
  SQLite follows it when the journal is hot, deleting the file it names once no database still
  references it. Copying the journal into a temporary directory did not confine that to the
  temporary directory, because the path travels inside the file: a copy read through
  `snapshot_for_reading` deleted a file outside the snapshot directory, while returning correct
  rows and reporting no error. Such a snapshot is now refused and the inventory reported
  unavailable, which also means a transaction spanning attached databases is a stated gap
  rather than a reading.
- The snapshot copies the `-journal` as well as the `-wal`. In rollback-journal mode SQLite
  spills dirty pages into the main database before the commit, so a copy taken without the
  journal exposed an uncommitted transaction as ordinary data — silently, since such a copy
  is structurally intact and `PRAGMA integrity_check` returns `ok`. Carried along, the journal
  is hot in the copy and SQLite rolls it back on open. A transaction spanning several attached
  databases names a super-journal that is not copied and remains a stated limit; Zotero does
  not commit across attached databases.
- The `-shm` is no longer copied into the snapshot. SQLite documents it as transient cache and
  lock state reconstructed from the `-wal`, so copying it preserved nothing while feeding
  reader-driven churn into the stability check.
- `audit-library` withholds every membership conclusion when Zotero's inventory cannot be read,
  instead of falling back to the mapping snapshot. The snapshot proves membership as of the
  last `dry-run`, so an attachment deleted from Zotero afterwards was reported `current` if it
  was indexed and `unindexed` if it was not — the first vouching for a document search can
  still return, the second queueing conversion work for something that no longer exists.
  `membership_unchecked` now covers every attachment in such a run, and file-level findings
  are still reported.
- `audit-library` reports `inventory_error` alongside `inventory_available`, in the JSON, the
  `library_status()` payload and the human output. The flag alone could not distinguish a
  database that is merely busy — where closing Zotero and re-running works — from a
  permission or schema failure, and the only actionable recovery instruction the audit has was
  being discarded by the catch that keeps a partial audit running.
- `canonical_markdown_path()` is keyed on the attachment key alone. It appended a title slug,
  which made the path a function of current metadata and contradicted its own documented
  guarantee: a retitled item resolved to a different file, so `canonical_markdown_exists` looked
  at the wrong path and the previous file was orphaned.
- `audit-library` now audits an attachment at the path Zotero currently gives it, rather than
  the one the snapshot and index remember. After a relink those disagree, and auditing the old
  path missed the source change when the old file was still present and reported a spurious
  `missing_source` when it was not. The snapshot hash is no longer used as a fallback across a
  relink either, since it describes the previous file and would report the item clean at
  exactly the moment it changed most. Clearing it is not sufficient on its own, so such items
  are now reported as `source_unchecked`.
- `metadata_changed` compares against Zotero's live record whenever the inventory could be read,
  rather than against the mapping snapshot's memory of it. A title, DOI or citation key edited
  in Zotero after the last `dry-run` was previously invisible, and an indexed attachment that
  never reached the mapper was skipped by the comparison entirely.
- Metadata rows were bound to SQLite columns by hard-coded position (`values[11]`, `values[17]`),
  so inserting any column above one of those indices would have silently bound an integer into
  the wrong column with no error. Now coerced by column name. This was latent: with the previous
  18 columns those indices were correct, so no shipped index ever held wrong data. Found while
  adding the provenance columns, which is exactly the edit that would have triggered it.


## [0.4.0] - 2026-09-10

Hardening, installability and citation binding. Transactional index generations, a
read-only-by-default MCP surface with native schemas and safety hints, timeout and orphan-PDF
triage workflows, identity-classification precision fixes, and locator verification that binds
a retrieved passage to the text the citation was formed against.

This release supersedes 0.3.0, which was withdrawn; everything it contained is included here.
Local image OCR (`ocr-images`) is packaged and callable but unannounced and unsupported -- see
the note under [Unreleased].

### Added

- Locator verification on `get_fulltext_chunk`. Search has always returned a `source_locator`
  carrying a content hash, but passing it back was ignored, so a passage cited before a reconversion
  -- a math pass, an image-OCR enrichment, or a plain rebuild -- would silently return whatever text
  now sits at the same chunk index. Retrieval now checks the caller's locator against what the index
  holds and answers a distinct `stale_locator` code, reporting the cited and current hashes so the
  caller can recognise what changed and search again. It is distinct from `attachment_not_found` and
  `chunk_not_found` because in this situation both the attachment and the chunk still exist.
  Verification is opt-in and refusal is strict, an asymmetry that follows from the failure modes
  rather than from caution: a refusal is loud and recoverable, while serving replaced content under
  a citation the caller has already formed is shaped exactly like a correct response and cannot be
  detected anywhere downstream. A caller that omits the hashes retrieves unverified, which is how a
  document is read without having searched for it first.
- Two hashes in `source_locator`, and which one applies depends on what is being retrieved.
  `chunk_sha256` verifies one exact chunk and is the hash to use for a cited passage: it refuses
  only the passage that actually changed, so an OCR pass that splices a single recovered equation no
  longer invalidates every other citation into that document -- the common case for exactly the
  enrichment workflow this project runs. `content_sha256` covers the whole converted document and
  applies to a whole-document read. For an exact `chunk_index` it is rejected *on its own* as
  `invalid_content_sha256`, because it verifies document content and not the meaning of a
  `chunk_index`: re-indexing the same text at a different chunk size leaves it matching while that
  index now addresses a different passage, so the check would pass and hand back other text under
  the caller's citation with a success response. Supplying both is valid -- the chunk hash decides
  and the document hash is not compared.
- What a chunk hash guarantees is textual rather than positional: the passage returned is the
  passage cited, while the surrounding document may have shifted, which is why the response's
  character offsets are always read back from the index rather than echoed from the request. It is
  checked against the full stored chunk rather than a `max_chars`-truncated slice, so a smaller
  window cannot change whether a locator verifies. Adding it needed no schema change and no rebuild:
  `chunks.text` is already stored, so the hash is derived at read time inside the query that already
  reads stored text for the rows a search returns. `content_sha256` is validated as a bounded
  non-empty string rather than as 64 hex characters, because it is echoed back from whatever the
  index holds -- validating it more strictly than the server emits it would let the server hand out
  a locator and then reject that same locator as malformed.
- Schema-compatibility tests (`tests/test_schema_compat.py`) for readers pointed at an index they
  cannot use. `_assert_supported_schema` exists so that a stale, foreign, empty or truncated
  database fails with an instruction naming the fix, rather than as the raw `no such table` /
  `no such column` that whichever query ran first would otherwise raise -- an error naming neither
  the problem nor the remedy, and identical whether the file is a legacy index, someone else's
  database, or a half-finished download. Nothing referenced that guard before. Both halves of the
  contract are now pinned: an unusable index is refused with the recovery command named, and the
  command it names actually restores a readable index -- exercised through `rebuild-index` itself on
  a published generation whose database has been replaced with an unsupported schema, not only
  through the builder it calls last, since locating the sidecar, staging a successor and swapping
  `current.json` are the steps most likely to break. Covered cases are a legacy schema missing a
  column that a later release added (the message names the column), a missing table, a foreign
  SQLite file, a zero-byte file -- which SQLite opens as a valid empty database rather than
  erroring -- and a file that is not a database at all. One test asserts the guarantee positively
  by failing if any reader lets a `sqlite3.Error` escape, since `IndexSchemaUnsupportedError` is a
  `RuntimeError` and a leaked SQLite error would otherwise surface as an unrelated test error. A
  forward-compatibility case pins that the guard requires a superset rather than an exact match, so
  an index carrying a column a reader does not know about stays readable and additive schema
  changes do not break in both directions. At the MCP boundary, `index_schema_unsupported` had no
  test either: the internal message deliberately names the database file so a CLI user can see
  which file is wrong, and that same detail is a local path, so the mapping is now asserted to
  answer the stable code while dropping it and still naming the repair command.
- Adversarial and containment tests over the guards that protect derived artifacts
  (`tests/test_containment.py`, plus additions to `tests/test_lock.py`, `tests/test_fts.py` and
  `tests/test_mcp_server.py`). Surveying first showed query-size limits, untrusted instruction text
  and attachment-key validation were already covered, so this adds only what was not. Three gaps
  had no coverage at all. Duplicate attachment keys: the build refuses them because two rows sharing
  a key would make retrieval return an arbitrary one, and the tests now also pin that a rejected
  rebuild leaves the previously published index byte-identical and queryable, since the refusal
  happens mid-build. Lock ownership: twelve threads released from one barrier contend for the write
  lock and exactly one proceeds, which is the atomicity a sequential acquire-then-refuse test cannot
  observe, alongside the retry branch for a holder that vanishes between a failed create and the
  read of its file, and proof that a live holder's lock is never overwritten. Tampered index
  pointers reaching MCP reads: `index_pointer_invalid` had no test in either of the two places it is
  raised, so hostile `current.json` values are now driven through the tool boundary and asserted to
  answer that stable code while leaking no local path -- the internal artifact error contains
  absolute paths, and the redaction was previously an untested claim -- and a repaired pointer is
  shown to recover without restarting the server, since resolution happens per request.
  Containment itself is tested through `resolve_generation_dir` against traversal, separator,
  absolute, UNC, NUL and newline identifiers, including with the escape target present and readable
  so that refusal cannot be an accident of a missing path. One boundary is pinned as a known limit
  rather than left as an assumed guarantee: replacing `generations/` itself with a symlink is not
  caught, because the check resolves both sides and they still match. Symlink cases skip where the
  platform refuses to create one, so they run on CI's Linux and macOS legs.

- Transactional derived-index artifacts (hardening-plan Package 2, reduced scope): the JSONL
  sidecar and SQLite FTS database are now published together as immutable, checksummed *index
  generations* under `<output_root>/index/generations/<id>/`, behind a single atomically replaced
  `current.json` pointer. A failed, interrupted, or invalid build can never take the published
  index offline; a publish journal makes an interruption between validation and pointer swap
  deterministically recoverable by the next write command; the previous generation is retained
  for rollback and older ones are swept automatically. New `rebuild-index` (full generation from
  a conversion manifest or an existing JSONL — also the one-time migration command from the
  legacy layout) and `update-index` (successor generation from the current one plus a manifest's
  new rows) commands are the only publication paths. Readers — the MCP server and the
  `search-fts`/`get-fulltext`/`coverage-report` commands — resolve `current.json` per request
  next to the configured `--db` path, which now acts purely as the index-root anchor, so
  existing registrations keep working unchanged after the one-time migration. There is no
  legacy standalone-database fallback: an unmigrated root fails loudly naming `rebuild-index`.
- New `retry-timeout` CLI command resolves a pending timeout candidate: `--skip` permanently routes
  that attachment straight to the plain-text fallback (recorded in `timeout_skip_list.json`, not
  hardcoded in source), or `--retry` reconverts it with a longer budget and promotes a successful
  result into the live manifest/index without ever overwriting the originally converted Markdown
  file in place.
- New MCP tools: `list_timeout_candidates` (read-only, always available) and, opt-in via
  `--enable-retry-timeout`, `skip_timeout_extraction`/`retry_timeout_extraction` (each gated behind
  its own literal `confirm` string and independently rate-limited from math-OCR reconversion).
- New `find-orphan-parents` CLI command discovers plausible Zotero parents for `orphan_pdf` rows by
  scoring each orphan PDF's early-page content (not its filename) with the same `classify_identity`
  engine used elsewhere, scoped to Zotero items with no *working* PDF attachment of their own --
  either no PDF attachment row at all, or one whose recorded path no longer resolves to a real file
  on disk (moved/renamed/deleted outside Zotero's own management; resolution mirrors `mapper.py`'s
  own attachment-path convention). Only high-confidence (`classify_identity`-verified) pairings are
  reported; a fuzzy title match alone on a result `classify_identity` itself left unverified is not
  trusted -- real-library testing showed this is mostly noise, e.g. an edited volume's individual
  chapter entries ("Citations", "Index", "Preface") score a trivially high fuzzy match against
  nearly any PDF. Findings are written per-run (`orphan_candidates.csv`/`.jsonl`,
  including a `candidate_had_stale_attachment` flag) and merged into a persistent, deduped master
  file, mirroring the timeout-candidate pattern. The new `orphan-candidate` CLI command resolves a
  pending pairing: `--skip` dismisses it, or `--mark-resolved` records that it was confirmed and
  already attached via the existing `link-pdf` command (bookkeeping only; it does not attach
  anything itself). New read-only, always-available MCP tool `list_orphan_candidates` mirrors
  `list_timeout_candidates` for this workflow.

### Changed

- Conversion timeouts now scale with page count and a cheap vector-drawing-density scan (long or
  diagram-dense books no longer lose structure/images to the plain-text fallback needlessly), and
  every genuine primary-extractor timeout is recorded as a "timeout candidate" (per-run
  `timeout_candidates.csv`/`.jsonl` plus a persistent, deduped master file) instead of only a manifest
  `error` note.

- **Removed** `build-index`, `append-index`, and `build-fts` (and their `indexer.py` writer
  functions): each could publish a half-updated index (JSONL and SQLite replaced separately).
  `indexer.py` is now a pure record-building module. `convert-new`, `reconvert-math`, and a successful
  `retry-timeout --retry` publish successor generations instead of mutating the shared
  JSONL/FTS files in place, and their `--jsonl`/`--fts-db` overrides were removed with the
  in-place layout; math reconversion's index rollback logic is now simply "the pointer never
  moved". The MCP `reconvert`/`retry-timeout` capabilities require the managed layout at
  startup.
- The pipeline write lock is acquired with an atomic exclusive create (closing a
  check-then-write race between two local processes), and a stale or corrupt lock file now
  fails loudly naming the recorded holder instead of being silently overwritten — on a
  cloud-synced output tree a lock that merely looks stale can belong to a machine whose sync is
  lagging. Delete `.pipeline.lock` manually only after confirming no other machine is mid-run.
- Explicit `--output-dir` arguments on config-managed commands are resolved (including
  symlinks) and rejected when they escape `output_root`, since the pipeline lock only protects
  writes inside that tree.
- The default MCP surface is now entirely read-only. Single-attachment math OCR is registered only
  with `--enable-reconvert` plus an explicit valid config; its confirmation literal remains defense
  in depth rather than a substitute for user approval. Reconversion preflights its JSONL sidecar
  and rolls back derived Markdown/image/index state if a later commit step fails.
- MCP server/tool instructions now explain offline/stale scope, untrusted-content handling,
  search-to-passage workflow, bibliographic attribution, and the absence of PDF page locators.
- MCP tools now advertise read-only, destructive, and closed-world safety hints to compatible
  clients, with reconversion explicitly marked non-idempotent. The optional MCP dependency is
  constrained to the tested v1 API (`mcp>=1.28,<2`) pending a separate v2 migration.
- Search now reports the fields that actually matched and content-bound locator hashes. Passage
  retrieval distinguishes complete and truncated stored chunks, exposes bounded chunk navigation,
  and returns deterministic reliability warnings for identity, attachment mapping, and math
  extraction concerns. Item context now requires exactly one parent or attachment key.
- `install-mcp --enable-reconvert` now rejects registration up front when the optional `[marker]`
  extra is not importable, instead of generating a registration that only fails once the server
  itself starts.
- Enabled MCP tools now advertise concrete success output schemas. Expected failures are native
  MCP `isError` results with stable, path-free public codes rather than success-shaped error
  dictionaries; disabled optional tools remain absent from the advertised surface.
- `strip_front_matter` (previously duplicated verbatim in `indexer.py` and `verifier.py`) now lives
  in `identity.py` as a single shared helper; `math_ocr.py` was updated to import it from there too.

### Fixed

- Index builds now reject duplicate `zotero_attachment_key` values with an actionable error
  (previously a duplicate silently made full-text retrieval return an arbitrary row), and
  readers detect a foreign/legacy SQLite schema and name the `rebuild-index` recovery command
  instead of surfacing a low-level "no such table" error (`index_schema_unsupported`, and
  `index_pointer_invalid` for a corrupt/tampered pointer, as MCP startup/tool error codes).


- `classify_identity` no longer lets an embedded Markdown image filename (e.g.
  `![](.../A-Candidate-Title.png)`) inflate `title_score` into false-positive full-text evidence;
  Markdown image syntax is stripped before any title/DOI/author/year matching.
- A confidently-parsed DOI in the converted text that conflicts with the expected Zotero DOI is
  now treated as disqualifying evidence regardless of title score. Previously the
  `conflicting_doi_low_title` check only fired when the title score was below 50, so generic
  topic-vocabulary overlap between two unrelated works could push the score high enough to dodge
  the check and let a wrong-document mapping through as `mapped_unverified`/`manual_review` instead
  of `possible_mismatch`.
- `verify-unverified` now checks the sidecar full-text index (`zotero_text_index.jsonl`, path
  overridable via `--index-jsonl`) and skips any attachment key already present there, whether it
  was originally `mapped_verified` or promoted later via `apply-verification`. Previously every run
  re-derived classifications from filename/path signals alone with no memory of past resolutions,
  so the same already-resolved rows were reconverted and rescored on every future run.
- `classify_identity` now scans DOI/author/year evidence only within a leading 6,000-character
  window of the converted text, not the entire document. Previously a paper that merely cited a
  different-DOI work, or a bibliography entry sharing a claimed author's surname, could be wrongly
  penalized (`possible_mismatch`) or wrongly credited (`author_evidence`) by evidence that appeared
  only deep in a reference list, far from the document's own title/DOI/byline.

## [0.2.0] - 2026-07-12

First tagged release. Covers everything merged since the initial import, aimed at making the
project safe to expose to an LLM client and installable by researchers outside the original
author's own machine.

### Added

- Safe default MCP read surface: search, bounded passage retrieval, and item-context tools are
  read-only and path-free by default; `reconvert_with_math_ocr` requires an exact confirmation
  literal and is rate-limited (PR #4).
- Explicit, bounded lexical search modes (`all_terms`, `any_terms`, `phrase`) with deterministic
  ranking and exact trimmed-text citation offsets (PR #5).
- Cross-platform CI (Windows/macOS/Linux) running the full test suite and a wheel build on every
  push/PR.
- `uv.lock` for reproducible installs; `uv sync --extra mcp --extra test --locked` is now the
  recommended install path alongside the existing `pip install -e .[mcp]`.
- `check-setup` CLI command: fast, read-only validation of config paths, `output_root`
  writability, Python version, and optional-extra availability before running a conversion.
- Crash-safe index publication: `build-index`, `append-index`, and `build-fts` now build to a
  same-directory temp file (with a `PRAGMA integrity_check` gate for the SQLite build) and only
  replace the previous file via an atomic rename on success. An interrupted or failed rebuild
  leaves the previous index intact and queryable instead of destroying it.
- `reconvert-math` now acquires the same cross-process write lock as every other index writer,
  closing a last-writer-wins race with concurrent index rebuilds.

### Changed

- `pyproject.toml` gained an explicit `[build-system]` (hatchling) declaration so `uv sync`
  installs the project itself, not just its dependencies.

## 0.1.0 - 2026-07-10

Initial import of the Zotero full-text conversion pipeline, CLI, and MCP server. Not tagged.

[Unreleased]: https://github.com/matthiaskloft/zotero-fulltext-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/matthiaskloft/zotero-fulltext-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/matthiaskloft/zotero-fulltext-mcp/releases/tag/v0.4.0
[0.2.0]: https://github.com/matthiaskloft/zotero-fulltext-mcp/releases/tag/v0.2.0
