# Development Roadmap

This file answers two questions: **what should be worked on next, and why in that order?** It
links to the detailed plans and GitHub issues rather than repeating them. If you are new to the
project, read "How the project fits together" first. It explains the terms the rest of the file
uses.

## How the project fits together

The project turns a researcher's Zotero PDFs into searchable text and serves that text to an LLM
client over MCP (Model Context Protocol). Data flows in one direction:

```
Zotero library (read-only)  →  PDFs  →  converted Markdown  →  search index  →  MCP server  →  LLM client
     zotero_db.py               converter / _extract_*.py       fts.py, artifacts.py   mcp_server.py, mcp_contract.py
```

- **Zotero and the source PDFs are never modified** by the default workflow. Everything the project
  writes is a *derived* artifact in the output folder: the "sidecar".
- **Conversion** (`convert-new`, `reconvert-*`) turns PDFs into Markdown. It is slow: minutes to
  hours for a whole library.
- **Indexing** (`rebuild-index`, `update-index`) builds a SQLite FTS5 full-text index from the
  Markdown plus a read-only snapshot of Zotero metadata. It is fast compared with conversion. Each
  build is a new immutable **generation**; a small `current.json` pointer is switched atomically to
  the new one, so a failed build leaves the previous index working (`artifacts.py`).
- **Search** is lexical: FTS5 with BM25 ranking over each record's title, creators, citation key and
  body text, which is split into structure-based **chunks** of up to ~1500 characters (headings,
  then paragraphs, then sentences), each with a heading path and physical PDF pages where a measured page map exists.
- **Locators.** Each search hit carries a `source_locator` with a `chunk_sha256`. When a client reads
  the chunk back with that hash and the text has since changed, it gets `stale_locator` instead of
  different text under an old citation. Re-chunking can change chunk indexes and hashes, invalidating affected locators.
  Future schema and boundary changes should be coordinated to limit repeated rebuilds.

`AGENTS.md` has the full architecture, conventions and safety rules. `docs/architecture.md` and
`docs/data-dictionary.md` describe the modules and the stored fields.

## How work is ordered

Work is ordered by **risk to a researcher's existing data**, lowest first. Among items with the same
risk, the one that produces evidence for a later decision goes first.

| Risk | Meaning |
|------|---------|
| **none** | Reads and reports only. Cannot change converted Markdown, the index, or source PDFs. |
| **contained** | Writes only through the staged-generation and atomic-pointer layer, so a failure leaves the previous generation in place. May require a reconversion or rebuild. |
| **high** | Moves or rewrites files across the whole converted library. |

## Current state

Version **0.15.2 (2026-10-28)**; roadmap reviewed on 2026-11-05. See
`CHANGELOG.md` for release details. The package is installable on Windows, macOS and Linux,
read-only by default, with lexical search, metadata filters, verified passage locators, library
auditing and crash-safe publication. Optional CLI acquisition/write commands and explicitly
enabled OCR/reconversion tools remain separate from the default read surface.

Unreleased: Marker whole-paper reconversion and its ML dependency tree have been removed.
Normal conversion, timeout retries and local-service image OCR remain available. Existing Marker
records are still readable; remove `--enable-reconvert` from older MCP registrations.

Since v0.11.0, released work includes:

- **v0.12.0:** per-attachment library folders, portable image links and catalogs; richer Zotero
  metadata, detected language and extraction-quality labels; the passage-level benchmark and
  separate experimental chunking indexes; portable host tests, a pinned public PDF corpus,
  disposable real-Zotero tests and a manual independent-agent acquisition workflow.
- **v0.12.1–v0.14.0:** tag-triggered release automation, startup without an index, passage-image
  retrieval, recovery before reading a publication base, WAL-aware Zotero snapshots and optional
  Zotero launch.
- **v0.15.0:** measured physical PDF page maps, `get_page`, heading paths and deterministic
  structure-based chunking (`structure-v1`, 1500-character budget, up to 150-character overlap
  only within split paragraphs). Tiny sections may merge forward. Production is character-bounded;
  token-bounded chunkers currently belong to the benchmark harness.
- **v0.15.1:** first real-library chunking measurements and improved agent search guidance.
- **v0.15.2:** reconversion, repair, retries and accepted reviews publish Markdown, images and page
  maps into the canonical library, retaining immutable revisions for existing readers (#93).

**Installed code and an existing library's generation can differ.** Updating the package does not
re-chunk an existing index. A rebuild with current code moves old 6000/500-character windows to
the structural defaults; changed chunks invalidate their old locators. Existing text needs
reconversion to gain measured page maps unless its fallback page markers supply them. Semantic
retrieval, source-block hierarchy and automatic context assembly are still planned.

There are four tracks of remaining work. They mostly don't depend on each other. The exceptions are
noted in each table's "Needs" column.

The tables distinguish released implementation from remaining work. PR links identify merged
code or documentation; a linked planning PR does not mean the proposed feature shipped.
“Planned” describes the package state, not a live audit of GitHub issue or security-alert status.

## Track 1: Search and retrieval (active)

The goal is to make search find more of the right papers. Lexical search improves first; optional
semantic discovery follows. Two rules shape the order:

1. **Measure before changing defaults.** Step S1 builds the yardstick. Any later step that changes
   ranking or chunking is checked against it before its behavior becomes the default. S1a extends
   the completed paper-level harness with evidence-span judgments and equal returned-token budgets.
2. **Coordinate future rebuilds and reconversions.** Page provenance and character-bounded
   structural chunks already shipped in v0.15.0. Bundle remaining schema changes where practical,
   rather than promising one future migration for work that has partly shipped. Retain source
   blocks and parent links so later context assembly and embedding experiments reuse source spans.
   Boundary/size sweeps use separate experimental indexes, without replacing the current generation.

| Step | What it does | Shipped / still open | Issues | Risk | Needs | Why here |
|------|--------------|----------------------|--------|------|-------|----------|
| **S1** (done) | Scores search quality (recall@k, MRR) on a personal question set that is never committed | Shipped v0.11.0 — [PR #98](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/98). No implementation work remaining. | [#78](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/78) | none | — | Every later step is judged by it. |
| **S1a** (harness done) | Extends S1 with private evidence-span judgments, held-out questions, passage metrics and equal returned-token budgets; compares chunk sizes, boundaries and overlap | Harness shipped v0.12.0 — [PR #113](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/113); first measurements/loader fix v0.15.1 — [PR #158](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/158), search guidance — [PR #159](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/159). Open: owner-reviewed judgments, broader agent runs, budget/overlap sweeps. | [#107](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/107) | contained | S1 | Experimental indexes are separate derived artifacts. Establishes the yardstick for S4/S4a and S6a/S6b before promoting defaults. |
| **S2** (done) | Adds `author`, `title`, `citation_key` and year-range filters to `search_fulltext` | Author/title/citation-key/year filters shipped v0.11.0 — [PR #99](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/99). Other #77 features are tracked under S4/S5. | [#77](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/77) A + B | none | — | Query code only, with no schema change or rebuild. Fixes the most visible gap: an author search today also matches every reference list that cites that author. |
| **S3** (done, v0.12.0) | Stores previously omitted Zotero fields (abstract, tags, venue, item type, creator roles, …), and scores each paper's extraction quality | Metadata, detected language and extraction-quality scoring shipped v0.12.0 — [PR #114](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/114). Calibration remains S3b; searchable abstract/tag columns remain S4. | [#76](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/76), [#82](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/82) | contained | — | Needs a rebuild but no reconversion. The abstracts and tags are what S4's new columns and S6 build on. The quality score explains papers that are missing because extraction failed. |
| **S3b** | Builds a multi-field public PDF corpus (open-access journals chosen field, then journal, then popular article; German/French/Spanish items, slides, scans) with reviewed quality labels, to calibrate S3's extraction-quality thresholds and `detected_language`, and to decide whether `convert-new` refreshes Zotero metadata | Corpus plan, pinned sources and opt-in acquisition test shipped v0.12.0 — [PR #117](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/117). Open: reviewed page labels, threshold/language calibration and metadata-refresh decision. | [#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115) | none | S3 | Thresholds are tuned only on synthetic fixtures. Metadata-only pins; downloads are opt-in on the host and no PDFs are committed. 93 entries pinned; page labels pending. Opt-in container mode `corpus` acquires it through Zotero; further uses in [`docs/public-corpus-uses.md`](docs/public-corpus-uses.md). |
| **S3a** | Evaluates source-preserving Markdown cleanup before chunking/embedding: Unicode repair, wrapping, conservative dehyphenation and page boilerplate, with original-span mappings | Planned. Measurement of header/footer removal and normalization accompanied [PR #156](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/156); defaults were left unchanged. Candidate transforms and original-span mappings remain open. | [#111](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/111) | contained | S1a (shipped page maps for page-aware rules) | #107 measures quality; this issue implements candidate transforms. Coordinate storage changes with S4, preserve original evidence and benchmark each rule before choosing defaults. |
| **S4** (partly shipped) | Page offsets (#79), character-bounded structural chunks (#80), exact character spans, heading paths and page ranges shipped in v0.15.0. Remaining: production token budgets, block IDs, parent/child hierarchy, richer neighbor links, section tags (front matter, body, references, appendix), stemming, prefix/proximity search and abstract/tag FTS columns. | Page maps, get_page, exact character spans, heading paths and 1500/150-character structure-v1 chunks shipped v0.15.0 — [PR #156](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/156). Open: token budgets, block hierarchy, richer neighbor links, section labels and remaining lexical/schema features. | [#79](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/79) → [#80](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/80) → [#81](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/81), #77 C, D, E, F | contained | S1a, S3 | Coordinate remaining schema changes; retain the hierarchy S4a/S6a need. Rewrites derived Markdown and the index, never PDFs or Zotero; changed chunks invalidate old locators. |
| **S4a** | Compares leaf retrieval, bounded parent/neighbor expansion and POMA-style deduplicated path assembly, with per-span citations and explicit gaps | Planned. Manual chunk navigation exists; automatic bounded expansion and deduplicated path assembly remain open. | [#108](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/108) | contained | S1a, S4 | First structural retrieval experiment; requires no embedding service. Reuses S4 source spans and measures assembly separately from ranking. |
| **S5** | Adds a section filter to search, and search over reference-list entries ("which of my papers cite X?") | Planned. Section classification/filtering and reference-entry search remain open. | #77 H, #81 | none | S4 | Query code on top of S4's section tags. |
| **S6** | Optional semantic paper discovery with a local embedding model, as a separate opt-in index and MCP tool | Planned. No production embedding backend or semantic discovery tool shipped. | [#1](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/1) | contained | S1, S3 (S4 for passage embeddings) | Built as its own artifact, so publishing the full-text index never depends on an embedding service running. S1's numbers decide whether hybrid ranking or passage embeddings are worth adding. The phased plan is in a comment on #1. |
| **S6a** | If passage retrieval is justified, compares independent leaf embeddings with bounded ancestor-context chunkset embeddings and lexical/semantic/hybrid ranking | Planned. Leaf versus ancestor-context embeddings and hybrid passage ranking remain open. | [#109](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/109) | contained | S1a, S4a, S6 | Implements #1 phase 4 as an explicit experiment. Uses identical source spans and separately ablates embeddings and assembly; semantic artifacts remain optional and independently publishable. |
| **S6b** | Compares late chunking with independent embeddings on fixed source spans and model weights, keeping assembly fixed | Planned. Late-embedding comparison on fixed spans remains open. | [#110](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/110) | contained | S1a, S6a | A separate embedding-order experiment after the leaf/chunkset baseline. Uses bounded document windows with explicit ownership; no further source re-chunk. |
| **S7** | Citation graph within the library, built from reference entries | Planned. Reference-entry extraction and the library citation graph remain open. | [#83](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/83) | contained | S3, S4 | Future. It is filed now so that S4's reference-entry format keeps what it needs. |

**Status:** S1 and S2 shipped in v0.11.0; S3 and S1a's harness shipped in v0.12.0.
S4's page maps and character-bounded structural chunker shipped in v0.15.0; its token budgets,
source hierarchy and section classification remain planned. S1a includes passage metrics,
equal top-k/token budgets, paired uncertainty and separate size/boundary/overlap experiments
(`benchmarks/retrieval.py`, `benchmarks/passages.py`, `benchmarks/chunking.py`; see
[`docs/search-quality.md`](docs/search-quality.md)). Semantic and hybrid channels remain
unavailable without an embedding backend.

**Start here:** review and extend the private question/evidence set, run budget and overlap
sweeps, and calibrate extraction quality with reviewed public-corpus labels (S3b, #115).
The [first real-library comparison](docs/chunking-results-2026-10.md) used 70 questions and
22 held-out agent trials: structural chunks reduced text read, with similar overall retrieval
and some long-passage, table and cross-section losses. Owner review, broader agent testing and
budget/overlap sweeps remain pending. v0.15.1 also improved search guidance. These are initial
measurements, not a completed token-budget sweep. #77's remaining search features stay planned.

### Chunking experiment protocol

The design and research rationale are in
[`docs/chunking-research-proposal.md`](docs/chunking-research-proposal.md). Delivery order is
S1a evaluation → S4 source hierarchy → S4a context assembly → S6a chunkset embeddings/hybrid
retrieval → S6b late embeddings; S6 paper discovery can proceed independently using the shipped S3 metadata.

S1a already extends the lexical runner with a common ranked-result interface for lexical,
semantic and hybrid retrieval, using the same questions and paper/span judgments. Semantic runs
remain unavailable until an embedding backend exists; benchmark support does not imply a shipped
semantic retrieval implementation.

Evaluate pre-embedding preprocessing in two stages. Before any embedding call, measure source
coverage and dropped/duplicated text, offset/hash round-trips, heading/parent correctness, section
classification, equation/table/caption integrity, chunk-size distributions and complete embedding
input token budgets (including prefixes/ancestors). Use synthetic fixtures with known structure
and reviewed private examples; expose failures rather than silently truncating input. Then compare
each preprocessing variant through lexical, semantic and hybrid retrieval with source documents,
questions, embedding model and ranking settings held fixed. Record extraction/preprocessing,
chunking, tokenizer and model versions so retrieval gains can be attributed to the right stage.
S3a ([#111](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/111)) owns the cleanup
implementations and source mappings; S1a owns their evaluation. Evaluate candidate transforms before changing the shipped structural defaults; page maps
are available from v0.15.0 conversions. Header/footer removal and indexing normalization were
measured and left unchanged (see `docs/conversion-text-decisions.md`).

The remaining S4 hierarchy work starts with extracted headings and explicit Markdown blocks, with a bounded sentence/paragraph
fallback for flat or malformed Markdown. Preserve equations, captions and table source spans.
A pinned tokenizer measures the proposed 384-token target and 512-token ordinary-prose ceiling;
these are starting candidates, not chosen defaults. S1a sweeps 128/256/384/512/768-token targets,
zero versus bounded overlap, and legacy fixed windows and the production structural chunker versus token-bounded sentence and structural boundaries.
Semantic breakpoints remain an experimental challenger; generated prefixes/propositions and
trained boundary predictors are deferred until simpler methods justify their cost.

Evaluate on private held-out questions with original evidence annotations. Alongside paper
recall/MRR, record passage nDCG, evidence-span recall/precision, duplicate rate, locator validity,
build/storage cost and p50/p95 latency. Compare equal top-k and equal returned-token budgets,
including ancestor and expansion text. Ablate embeddings and assembly independently, report
paired uncertainty, and inspect exact-match regressions, missing qualifiers, bad headings and
cross-section dependencies before promoting any default.

Source blocks and hashes remain authoritative. Assembled non-contiguous context carries multiple
original locators and a display-to-source mapping, never one fabricated continuous range or chunk
hash. Repeated headings/table headers and embedding prefixes are identified as derived retrieval
content. The optional semantic index records source generation, chunking/tokenizer versions and
model identity; lexical search remains available when it is missing or stale.

## Track 2: Hardening leftovers

These are the remaining steps of [`plan-mcp-server-hardening.md`](plan-mcp-server-hardening.md). Its
package and step numbers are kept as-is because merged branches and commits refer to them.

**Package 5, step 6: performance baselines.** Done: recorded in
[`docs/performance-baselines.md`](docs/performance-baselines.md), re-runnable with
`benchmarks/latency.py`. They exposed a full-table scan on every passage fetch, now fixed with
an index, and searches on very common terms that still take seconds.

**Package 5, step 3: end-to-end fixture tests.** Done, except the migration case, which waits on
Package 3. `tests/test_end_to_end.py` runs `convert-new` and `rebuild-index` on synthetic PDFs and
queries a real server process started as `install-mcp` registers it. It covers recovery from a
publication interrupted before the pointer swap, and audit/status output. Its one finding, that
`convert-new` converted an interrupted run's items again because it picked new items before
recovery ran, is fixed: recovery now runs first
([#94](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/94)).

**Package 3, steps 1 and 2: canonical library layout.** Done in reduced form, without migration (reorganized in v0.12.0).
`convert-verified` and `convert-new` write straight to `library/<key>/<key>.md` and
`library/<key>/images/`, and the index records those paths. The trigger was real: on the
reference library a hand-made folder rename inside a synced output folder cut every indexed paper
off from its Markdown, which motivated reconversion into the canonical layout. A reconversion fills the new
layout directly, so `migrate-library-layout` (step 5) and its rename-heavy copy are not needed and
are not planned. Follow-up status:

- Provenance reconversion, index repair and `retry-timeout` now stage in run folders and publish
  accepted Markdown, images and page maps into `library/` with the successor index. Accepted
  unverified reviews use the same publication through manifest-based rebuild/update commands;
  quarantine output stays separate. Replacements use immutable `library/<key>/versions/<generation>/`
  folders, preserving older readers' Markdown and images even during a crash. Failed publication
  removes only unpublished revisions; published revisions are retained. `--keep-current`
  retains better extractions without replacing their files
  ([#93](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/93), shipped in v0.15.2).
- Steps 3 and 6 were covered by earlier work (source hashes in the index, the repair plan).

The evidence behind this came from `library-status --full --mapping-report <run>` and
`audit-library` against a real library. Read them with two cautions:

- A low `source_changed` count means little while `source_provenance_unknown` is high. Source hashes
  exist only for attachments converted or reconverted since they were introduced.
  `plan-provenance-reconvert` can close that gap.
- The status counts overlap and don't sum to the attachment total. When Zotero's database can't be
  read, membership statuses are withheld, and `inventory_available` says so.

**Package 5 step 5**, the upgrade guide, was tied to the migration and is dropped with it. An
upgrade to the library layout is a reconversion followed by
`rebuild-index --manifest <run>/manifest.csv --keep-current`.

## Track 3: Zotero writes, dependencies and later work

These items were previously unscheduled. They are ordered the same way as the other tracks. The
risk column here is about Zotero and dependencies, not the converted library.

| Step | What it does | Shipped / still open | Issues | Risk | Needs | Why here |
|------|--------------|----------------------|--------|------|-------|----------|
| **M1** (done) | Fixes the duplicate check in `check-pdf` and `import-doi`. Live debug-bridge reads with a verified-copy fallback replace unsafe live `immutable=1` reads; writes refuse to proceed when neither check works. | Shipped v0.11.0 — [PR #97](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/97). WAL-aware mapper snapshots shipped v0.14.0 — [PR #151](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/151). | [#91](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/91) | none | — | A correctness bug in shipped write commands, so it comes first. It changes no derived data. |
| **M2** (implemented, unreleased) | Remove unused Marker whole-paper reconversion and its optional ML dependency tree instead of dismissing the associated alerts. | Removed `[marker]`, its CLI/MCP tools, registration flag and executable benchmark runner; regenerated `uv.lock` without Marker, Torch, Transformers, Surya, Pillow or pyasn1. Historical conversions remain readable. Open: merge/release and confirm GitHub closes the alerts after the default branch updates. | [#92](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/92) | contained | — | Removes an unused feature and the dependency paths behind the current alerts; normal conversion and local image OCR remain available. |
| **M3** | An opt-in review-and-apply workflow for Zotero item writes from MCP, as a separate entry point because the default server stays read-only. The CLI write commands (`import-doi`, `find-pdf`, `link-pdf`) already exist. | Planned. CLI acquisition exists; a separate MCP review/apply workflow remains open. Prefer Z5 before implementation. | [#34](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/34) | contained | M1 | It builds on the same duplicate check, so M1 must land first. |
| **M4** | A sidecar library of the reader's own comments, linked to source passages. #3 tracks it together with #1. | Planned. Source-linked sidecar comments remain open; depends on stable source spans. | [#2](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/2), [#3](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/3) | contained | S4, S6 | Links to passages by locator, and future S4 boundary changes can invalidate them. Building it earlier would mean re-linking every comment. |
| **M5** | Image OCR (`ocr-images`): stress-test on a large library, then decide whether to document it in the release notes. It is packaged but left out of the notes by decision. | Packaged and opt-in, but unannounced and not validated for general use. Open: large-library stress test and release-note decision. | — | none | a large-library run | No date. It is announced once the stress test passes (see `CHANGELOG.md`). |

**Status:** M1 is done ([#97](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/97)); its
live path has recorded fixtures and disposable real-Zotero integration coverage. The v0.12.0
container tests exposed and fixed a PDF-check SQL incompatibility that had forced the copy fallback.
M2's removal is implemented for the next release; verify alert closure after it merges. M3 is unblocked now
that M1 has landed, but Track 4's Z5 should land first so the MCP write workflow is built on the
Zotero 10 local API rather than debug-bridge.

Out of scope for this project: live Zotero browsing of collections, tags and notes. That belongs to
the companion Zotero MCP server (see `AGENTS.md`).

## Track 4: Zotero 10 adoption

Zotero 10 (2026) added local API writes, synced full-text content, accent- and CJK-aware search,
and annotation search conditions. These items adopt what is useful and keep us compatible. The risk
column is about Zotero and the converted library, as in Track 3.

| Step | What it does | Shipped / still open | Issues | Risk | Needs | Why here |
|------|--------------|----------------------|--------|------|-------|----------|
| **Z1** | Support matrix (Zotero 7 + 10, plugins, OS), runtime version detection, and versioned fixture and live tests. The live tests currently pin Zotero 9.0.6 only. Verifies our SQLite reads and local-server calls against Zotero 10. | Planned. Existing pinned container/live tests are a starting point; the multi-version support matrix and version detection remain open. | [#137](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/137) | none | — | Every other Z step needs a tested Zotero 10 baseline, and silent breakage on v10 is the most immediate risk. |
| **Z2** | Explore Zotero's synced full-text content as a fallback and cross-check text source for missing, failed or timed-out attachments. Exploration only; ends in a recommendation. | Planned read-only investigation; no fallback source selected. | [#136](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/136) | none | Z1 | Read-only research that produces evidence for a later decision. |
| **Z3** | Search normalization: NFKC and typographic folding (H₂O, ligatures, curly quotes) and CJK matching, for parity with Zotero 10 search. | Planned. Index normalization was measured with [PR #156](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/156) and left unchanged; broader Unicode/CJK support remains open. | [#138](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/138) | contained | — | Changes the FTS schema and needs an index rebuild. If possible, bundle it with Track 1's remaining schema work (S4) to avoid a second rebuild. |
| **Z4** | Expose the user's Zotero annotations (highlights, notes, colours) as searchable read-only evidence, linked to converted chunks. | Planned. Searchable annotation evidence and links to source chunks remain open. | [#139](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/139) | contained | Z1, S4 | Links annotations to chunks, so it depends on stable source spans (same reason as M4). Unlike general note browsing (out of scope, end of Track 3), this treats annotations as passage-level evidence. |
| **Z5** | Move Zotero writes from debug-bridge to the Zotero 10 local API, then deprecate debug-bridge if nothing still needs it. | Planned. Existing writes still use the bridge; local-API migration and bridge deprecation remain open. | [#135](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/135) | contained | Z1, M1 | Changes shipped write paths, so it needs the version baseline first. It should land before M3 so the MCP write workflow is built on the local API. |

## History

These are the completed items, in the order they shipped. The source plans are
[`plan-mcp-server-hardening.md`](plan-mcp-server-hardening.md),
[`plan-public-release-readiness.md`](plan-public-release-readiness.md) and
[`plan-mcp-instruction-and-contract-robustness.md`](plan-mcp-instruction-and-contract-robustness.md).

| Item | What it delivered |
|------|-------------------|
| Hardening Package 1: safe MCP read surface | The default server exposes only read operations; returned content is labeled untrusted (PR #4). |
| Hardening Package 4A: retrieval foundation | Bounded, predictable search and content-hash locators (PR #5). |
| Release readiness, phases 1–4 | CI on three OSes, `uv.lock`, crash-safe index publication, `check-setup`, tagged releases (PR #7, v0.2.0). |
| Contract robustness, phases 1–3 | Opt-in mutation tools, evidence/discovery distinction, `matched_fields`, typed schemas and native MCP errors (PRs #8, #10, #11). |
| Hardening Package 2 (reduced scope): transactional artifacts | Immutable index generations, the atomic `current.json` pointer, the publish journal, `rebuild-index`/`update-index`. |
| Package 4B step 1: locator staleness | `get_fulltext_chunk` checks `chunk_sha256` and returns `stale_locator`. |
| Package 5 steps 2 and 4: schema-compatibility and adversarial tests | Old indexes migrate or fail with a recovery instruction; hostile generation IDs, path escapes and injected instructions are tested. |
| Package 3 steps 4 and 7 (read-only half): `audit-library`, `library_status` | Per-attachment drift report and a status tool. This also added `source_sha256` and `indexed_at` to index records, because `source_changed` can't be computed without them. |
| Package 4B step 2: SQL aggregates and truthful status | `index-stats` (formerly `coverage-report`) and `library_status`, which never conflates indexed counts with library coverage (PRs #8–#10). |
| Canonical library layout, performance baselines, end-to-end tests (v0.11.0) | `convert-verified`/`convert-new` write to `library/markdown` and `library/images`; recorded latency baselines and a passage-lookup index; real-process fixture tests (PRs #84–#87). |
| Zotero write CLI (v0.11.0) | `import-doi`, `find-pdf`, `link-pdf --url`, `import-doi --with-pdf`, and a `guide("writes")` listing of every write path (PRs #89, #90). |
| Retrieval-quality harness and search filters (v0.11.0) | `benchmarks/retrieval.py` scores recall@k and MRR on a private question set and compares runs (S1, PR #98). `search_fulltext` accepts `author`, `title`, `citation_key` and year-range filters (S2, #77 A and B, PR #99). |
| Live pre-write checks (v0.11.0) | `import-doi`'s duplicate check and `check-pdf` read live through debug-bridge, fall back to a verified copy, and refuse when neither works (M1, #91, PR #97). Conversion run folders got microsecond names after a collision made CI flaky (PR #101). |
| Issue-driven work through v0.10.0 | Checkpointed conversion, selective index repair, provenance reconversion, `search_within_fulltext`, `lookup_citation_key`, a first-use guide and more. See `CHANGELOG.md`. |
| Metadata, extraction quality and passage evaluation (v0.12.0) | S3 metadata/quality fields ([PR #114](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/114)), S1a passage metrics and experimental chunkers ([PR #113](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/113)), portable host/real-Zotero tooling ([PR #106](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/106)) and expanded public corpus ([PR #117](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/117)). |
| Portable library layout (v0.12.0) | Per-attachment Markdown/images, `index/versions`, `runs`, `review`, catalogs and portable image links ([PR #121](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/121)). |
| Release and retrieval hardening (v0.12.1–v0.14.0) | Publishing ([PR #126](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/126)), startup without an index ([PR #132](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/132)), passage images ([PR #142](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/142), enriched-image fix [PR #148](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/148)), publication recovery ([PR #144](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/144), [PR #145](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/145)), WAL-aware snapshots ([PR #151](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/151)) and optional launch ([PR #152](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/152)). |
| Page provenance and structural chunking (v0.15.0) | S4's page maps, page retrieval, heading paths and 1500/150-character structural chunker ([PR #156](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/156)); hierarchy and token budgets remain planned. |
| Retrieval measurements and guidance (v0.15.1) | First real-library chunking comparison and JSONL corpus-loader fix ([PR #158](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/158)); improved agent search instructions ([PR #159](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/159)). |
| Canonical publication of replacement conversions (v0.15.2) | #93: repair, retries, reconversion and accepted reviews publish immutable library revisions with images/page maps; independent manifest rebuild can recover a missing current JSONL ([PR #161](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/161)). |
