# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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

### Fixed

- Records whose math was recovered by a math-capable pass no longer carry a spurious
  `math_extraction_may_be_lossy` warning in MCP responses. The check was an equality test against
  a single extractor name (`marker`); it is now a per-component membership test, so a composite
  provenance label such as `pymupdf4llm.to_markdown+glm-ocr` is recognised while an unknown
  extractor still warns.

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
- Index builds now reject duplicate `zotero_attachment_key` values with an actionable error
  (previously a duplicate silently made full-text retrieval return an arbitrary row), and
  readers detect a foreign/legacy SQLite schema and name the `rebuild-index` recovery command
  instead of surfacing a low-level "no such table" error (`index_schema_unsupported`, and
  `index_pointer_invalid` for a corrupt/tampered pointer, as MCP startup/tool error codes).

- Conversion timeouts now scale with page count and a cheap vector-drawing-density scan (long or
  diagram-dense books no longer lose structure/images to the plain-text fallback needlessly), and
  every genuine primary-extractor timeout is recorded as a "timeout candidate" (per-run
  `timeout_candidates.csv`/`.jsonl` plus a persistent, deduped master file) instead of only a manifest
  `error` note.
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

[Unreleased]: https://github.com/matthiaskloft/zotero-fulltext-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/matthiaskloft/zotero-fulltext-mcp/releases/tag/v0.2.0
