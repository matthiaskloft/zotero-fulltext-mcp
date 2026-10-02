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
  body text, which is split into ~6000-character **chunks**.
- **Locators.** Each search hit carries a `source_locator` with a `chunk_sha256`. When a client reads
  the chunk back with that hash and the text has since changed, it gets `stale_locator` instead of
  different text under an old citation. Re-chunking the library therefore invalidates every locator
  issued before it. That is by design, but it is why the plan below re-chunks only once.

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

Version 0.11.0 (2026-09-30), see `CHANGELOG.md`. The server is
installable on Windows, macOS and Linux, read-only by default, and has crash-safe index
publication, library auditing, and tools to search the library, search within one paper, look up a
citation key, and read chunks with verified locators. Version 0.11.0 adds the canonical
library layout, performance baselines, end-to-end tests, a `guide` tool, the opt-in Zotero
write commands (`import-doi --with-pdf`, `find-pdf`, `link-pdf --url`) with duplicate and PDF checks
that read live from Zotero, a retrieval-quality harness, and `author`, `title`, `citation_key` and
year-range filters on search. The completed work is listed under "History" at the end. Since that release, merged work
adds portable developer-test tooling, a pinned public PDF corpus, a disposable real-Zotero
container and a manual independent-agent acquisition workflow (see `CHANGELOG.md`, Unreleased).

There are three tracks of remaining work. They mostly don't depend on each other. The exceptions are
noted in each table's "Needs" column.

## Track 1: Search and retrieval (active)

The goal is to make search find more of the right papers. Lexical search improves first; optional
semantic discovery follows. Two rules shape the order:

1. **Measure before changing defaults.** Step S1 builds the yardstick. Any later step that changes
   ranking or chunking is checked against it before its behavior becomes the default. S1a extends
   the completed paper-level harness with evidence-span judgments and equal returned-token budgets.
2. **Reconvert and re-chunk once.** Page offsets, new chunking, section tags, stemming and new FTS
   columns all need a new index schema, and some need reconversion. They ship together in S4, so a
   library is reconverted once and locators are invalidated once. S4 retains source blocks and
   parent links so later context assembly and embedding experiments reuse the same source spans.
   Boundary/size sweeps run against separate experimental indexes before choosing the production
   chunking; they do not repeatedly replace a user's current generation.

| Step | What it does | Issues | Risk | Needs | Why here |
|------|--------------|--------|------|-------|----------|
| **S1** (done) | Scores search quality (recall@k, MRR) on a personal question set that is never committed | [#78](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/78) | none | — | Every later step is judged by it. |
| **S1a** (harness done) | Extends S1 with private evidence-span judgments, held-out questions, passage metrics and equal returned-token budgets; compares chunk sizes, boundaries and overlap | [#107](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/107) | contained | S1 | Experimental indexes are separate derived artifacts. Establishes the yardstick for S4/S4a and S6a/S6b before promoting defaults. |
| **S2** (done) | Adds `author`, `title`, `citation_key` and year-range filters to `search_fulltext` | [#77](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/77) A + B | none | — | Query code only, with no schema change or rebuild. Fixes the most visible gap: an author search today also matches every reference list that cites that author. |
| **S3** | Stores the Zotero fields the index drops (abstract, tags, venue, item type, creator roles, …), and scores each paper's extraction quality | [#76](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/76), [#82](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/82) | contained | — | Needs a rebuild but no reconversion. The abstracts and tags are what S4's new columns and S6 build on. The quality score explains papers that are missing because extraction failed. |
| **S3b** | Builds a multi-field public PDF corpus (open-access journals chosen field, then journal, then popular article; German/French/Spanish items, slides, scans) with reviewed quality labels, to calibrate S3's extraction-quality thresholds and `detected_language`, and to decide whether `convert-new` refreshes Zotero metadata | [#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115) | none | S3 | Thresholds are tuned only on synthetic fixtures. Metadata-only pins; downloads are opt-in on the host and no PDFs are committed. |
| **S3a** | Evaluates source-preserving Markdown cleanup before chunking/embedding: Unicode repair, wrapping, conservative dehyphenation and page boilerplate, with original-span mappings | [#111](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/111) | contained | S1a (S4 page maps for page-aware rules) | #107 measures quality; this issue implements candidate transforms. Coordinate storage changes with S4, preserve original evidence and benchmark each rule before choosing defaults. |
| **S4** | Records page offsets; builds deterministic, token-bounded heading/paragraph chunks with exact source spans, block IDs, parent/child and neighbor links, page ranges and section tags (front matter, body, references, appendix). Adds stemming, prefix/proximity search and abstract/tag columns in the same schema bump. | [#79](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/79) → [#80](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/80) → [#81](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/81), #77 C, D, E, F | contained | S1a, S3 | The single production reconversion and schema bump. Retains the hierarchy S4a/S6a need. Rewrites derived Markdown and the index, never PDFs or Zotero; changed chunks invalidate old locators. |
| **S4a** | Compares leaf retrieval, bounded parent/neighbor expansion and POMA-style deduplicated path assembly, with per-span citations and explicit gaps | [#108](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/108) | contained | S1a, S4 | First structural retrieval experiment; requires no embedding service. Reuses S4 source spans and measures assembly separately from ranking. |
| **S5** | Adds a section filter to search, and search over reference-list entries ("which of my papers cite X?") | #77 H, #81 | none | S4 | Query code on top of S4's section tags. |
| **S6** | Optional semantic paper discovery with a local embedding model, as a separate opt-in index and MCP tool | [#1](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/1) | contained | S1, S3 (S4 for passage embeddings) | Built as its own artifact, so publishing the full-text index never depends on an embedding service running. S1's numbers decide whether hybrid ranking or passage embeddings are worth adding. The phased plan is in a comment on #1. |
| **S6a** | If passage retrieval is justified, compares independent leaf embeddings with bounded ancestor-context chunkset embeddings and lexical/semantic/hybrid ranking | [#109](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/109) | contained | S1a, S4a, S6 | Implements #1 phase 4 as an explicit experiment. Uses identical source spans and separately ablates embeddings and assembly; semantic artifacts remain optional and independently publishable. |
| **S6b** | Compares late chunking with independent embeddings on fixed source spans and model weights, keeping assembly fixed | [#110](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/110) | contained | S1a, S6a | A separate embedding-order experiment after the leaf/chunkset baseline. Uses bounded document windows with explicit ownership; no further source re-chunk. |
| **S7** | Citation graph within the library, built from reference entries | [#83](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/83) | contained | S3, S4 | Future. It is filed now so that S4's reference-entry format keeps what it needs. |

**Status:** S1 ([#98](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/98)) and S2
([#99](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/99)) shipped in v0.11.0. Run the
S1 harness before and after any change to ranking or chunking. **Start here:** S3 for metadata (rebuild, no reconversion), and S1a for
evaluation before cleanup/chunking defaults are selected. #77 stays open for its parts C to H.
S1a's harness is implemented (`benchmarks/retrieval.py` passage mode, `benchmarks/passages.py`,
`benchmarks/chunking.py`; see [`docs/search-quality.md`](docs/search-quality.md)): evidence-span
judgments, held-out split, passage metrics, equal top-k and token budgets, paired uncertainty and
chunk-size/boundary/overlap sweeps on separate experimental indexes. Writing the private 80 to 120
question set and running the sweeps on a real library is the remaining S1a work; no chunking
default is chosen until a sweep has been inspected.

### Chunking experiment protocol

The design and research rationale are in
[`docs/chunking-research-proposal.md`](docs/chunking-research-proposal.md). Delivery order is
S1a evaluation → S4 source hierarchy → S4a context assembly → S6a chunkset embeddings/hybrid
retrieval → S6b late embeddings; S6 paper discovery can proceed independently once S3 is ready.

S1a extends the existing lexical-only runner with a common ranked-result interface for lexical,
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
implementations and source mappings; S1a owns their evaluation. Develop both before selecting S4
chunking defaults, with page-aware cleanup following S4's page provenance work.

S4 starts with extracted headings and explicit Markdown blocks, with a bounded sentence/paragraph
fallback for flat or malformed Markdown. Preserve equations, captions and table source spans.
A pinned tokenizer measures the proposed 384-token target and 512-token ordinary-prose ceiling;
these are starting candidates, not chosen defaults. S1a sweeps 128/256/384/512/768-token targets,
zero versus bounded overlap, and current character slices versus sentence and structural boundaries.
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
publication interrupted before the pointer swap, and audit/status output. One finding: after such
a crash, `convert-new` converts the interrupted run's new items again, because it picks new items
before recovery runs. The result is correct; only the conversion time is spent twice
([#94](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/94)).

**Package 3, steps 1 and 2: canonical library layout.** Done in reduced form, without migration.
`convert-verified` and `convert-new` write straight to `library/markdown/<key>.md` and
`library/images/<key>/`, and the index records those paths. The trigger was real: on the
reference library a hand-made folder rename inside a synced output folder cut every indexed paper
off from its Markdown, and the whole library is being reconverted. A reconversion fills the new
layout directly, so `migrate-library-layout` (step 5) and its rename-heavy copy are not needed and
are not planned. Still open:

- Provenance reconversion, index repair, `retry-timeout` and unverified reviews write into run
  folders, so a record they replace points there again. They must not change the file the current
  index reads before their own publication succeeds. Publishing their validated output into
  `library/` is the next step, tracked in [#93](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/93).
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

| Step | What it does | Issues | Risk | Needs | Why here |
|------|--------------|--------|------|-------|----------|
| **M1** (done) | Fixes the duplicate check in `check-pdf` and `import-doi`. Today they read the live `zotero.sqlite` with `immutable=1`, which can miss just-written rows (WAL mode) or see a half-written state, so a wrong answer can create a duplicate item or a second PDF copy. Proposed fix: a live debug-bridge read where available. | [#91](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/91) | none | — | A correctness bug in shipped write commands, so it comes first. It changes no derived data. |
| **M2** | Dismisses the 22 Dependabot alerts on `uv.lock` and records why. The optional `[marker]` extra pins `pillow<11` and, before Marker 2.0, `transformers<5`. Marker 2.0 lifts the pins but needs Docker or a `llama-server` binary. Decision: stay on marker-pdf 1.10.2, because the pipeline only processes the user's own PDFs. | [#92](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/92) | none | — | The decision is made. What remains is the dismissal and a note. Revisit when Marker 2.0 no longer needs Docker. |
| **M3** | An opt-in review-and-apply workflow for Zotero item writes from MCP, as a separate entry point because the default server stays read-only. The CLI write commands (`import-doi`, `find-pdf`, `link-pdf`) already exist. | [#34](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/34) | contained | M1 | It builds on the same duplicate check, so M1 must land first. |
| **M4** | A sidecar library of the reader's own comments, linked to source passages. #3 tracks it together with #1. | [#2](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/2), [#3](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/3) | contained | S4, S6 | Links to passages by locator, and S4 re-chunks the library once. Building it earlier would mean re-linking every comment. |
| **M5** | Image OCR (`ocr-images`): stress-test on a large library, then decide whether to document it in the release notes. It is packaged but left out of the notes by decision. | — | none | a large-library run | No date. It is announced once the stress test passes (see `CHANGELOG.md`). |

**Status:** M1 is done ([#97](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/97)); its
live path is checked against Zotero's source and a fake bridge, and `pytest -m live_zotero` has not
been run against a real Zotero. M2's decision is made; what remains is dismissing the alerts on
GitHub, which is a repository security setting and stays with the maintainer. M3 is unblocked now
that M1 has landed.

Out of scope for this project: live Zotero browsing of collections, tags and notes. That belongs to
the companion Zotero MCP server (see `AGENTS.md`).

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
