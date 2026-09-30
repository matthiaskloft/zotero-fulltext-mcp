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

Version 0.10.0 (2026-09-26), plus unreleased work on `master` (see `CHANGELOG.md`). The server is
installable on Windows, macOS and Linux, read-only by default, and has crash-safe index
publication, library auditing, and tools to search the library, search within one paper, look up a
citation key, and read chunks with verified locators. The unreleased work adds the canonical
library layout, performance baselines, end-to-end tests, a `guide` tool, and the opt-in Zotero
write commands (`import-doi --with-pdf`, `find-pdf`, `link-pdf --url`). The completed work is
listed under "History" at the end.

There are three tracks of remaining work. They mostly don't depend on each other. The exceptions are
noted in each table's "Needs" column.

## Track 1: Search and retrieval (active)

The goal is to make search find more of the right papers. Lexical search improves first; optional
semantic discovery follows. Two rules shape the order:

1. **Measure before changing defaults.** Step S1 builds the yardstick. Any later step that changes
   ranking or chunking is checked against it before its behavior becomes the default.
2. **Reconvert and re-chunk once.** Page offsets, new chunking, section tags, stemming and new FTS
   columns all need a new index schema, and some need reconversion. They ship together in S4, so a
   library is reconverted once and locators are invalidated once.

| Step | What it does | Issues | Risk | Needs | Why here |
|------|--------------|--------|------|-------|----------|
| **S1** | Scores search quality (recall@k, MRR) on a personal question set that is never committed | [#78](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/78) | none | — | Every later step is judged by it. |
| **S2** | Adds `author`, `title`, `citation_key` and year-range filters to `search_fulltext` | [#77](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/77) A + B | none | — | Query code only, with no schema change or rebuild. Fixes the most visible gap: an author search today also matches every reference list that cites that author. |
| **S3** | Stores the Zotero fields the index drops (abstract, tags, venue, item type, creator roles, …), and scores each paper's extraction quality | [#76](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/76), [#82](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/82) | contained | — | Needs a rebuild but no reconversion. The abstracts and tags are what S4's new columns and S6 build on. The quality score explains papers that are missing because extraction failed. |
| **S4** | Records page offsets during conversion, splits chunks at headings and paragraphs with page ranges, and tags each chunk's section (front matter, body, references, appendix). In the same schema bump it adds stemming, prefix and proximity search, and abstract/tag columns. | [#79](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/79) → [#80](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/80) → [#81](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/81), #77 C, D, E, F | contained | S1, S3 | The single reconversion and schema bump for this track. Rewrites derived Markdown and the index, never PDFs or Zotero. Existing locators answer `stale_locator` afterwards. |
| **S5** | Adds a section filter to search, and search over reference-list entries ("which of my papers cite X?") | #77 H, #81 | none | S4 | Query code on top of S4's section tags. |
| **S6** | Optional semantic paper discovery with a local embedding model, as a separate opt-in index and MCP tool | [#1](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/1) | contained | S1, S3 (S4 for passage embeddings) | Built as its own artifact, so publishing the full-text index never depends on an embedding service running. S1's numbers decide whether hybrid ranking or passage embeddings are worth adding. The phased plan is in a comment on #1. |
| **S7** | Citation graph within the library, built from reference entries | [#83](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/83) | contained | S3, S4 | Future. It is filed now so that S4's reference-entry format keeps what it needs. |

**Status:** no Track 1 step has started. **Start here:** S1 and S2 are unblocked, zero-risk, and
independent of each other.

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
| **M1** | Fixes the duplicate check in `check-pdf` and `import-doi`. Today they read the live `zotero.sqlite` with `immutable=1`, which can miss just-written rows (WAL mode) or see a half-written state, so a wrong answer can create a duplicate item or a second PDF copy. Proposed fix: a live debug-bridge read where available. | [#91](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/91) | none | — | A correctness bug in shipped write commands, so it comes first. It changes no derived data. |
| **M2** | Dismisses the 22 Dependabot alerts on `uv.lock` and records why. The optional `[marker]` extra pins `pillow<11` and, before Marker 2.0, `transformers<5`. Marker 2.0 lifts the pins but needs Docker or a `llama-server` binary. Decision: stay on marker-pdf 1.10.2, because the pipeline only processes the user's own PDFs. | [#92](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/92) | none | — | The decision is made. What remains is the dismissal and a note. Revisit when Marker 2.0 no longer needs Docker. |
| **M3** | An opt-in review-and-apply workflow for Zotero item writes from MCP, as a separate entry point because the default server stays read-only. The CLI write commands (`import-doi`, `find-pdf`, `link-pdf`) already exist. | [#34](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/34) | contained | M1 | It builds on the same duplicate check, so M1 must land first. |
| **M4** | A sidecar library of the reader's own comments, linked to source passages. #3 tracks it together with #1. | [#2](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/2), [#3](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/3) | contained | S4, S6 | Links to passages by locator, and S4 re-chunks the library once. Building it earlier would mean re-linking every comment. |
| **M5** | Image OCR (`ocr-images`): stress-test on a large library, then decide whether to document it in the release notes. It is packaged but left out of the notes by decision. | — | none | a large-library run | No date. It is released once the stress test passes (see `CHANGELOG.md`). |

**Status:** no Track 3 step has started. **Start here:** M1 and M2 are unblocked and can run
alongside S1 and S2.

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
| Canonical library layout, performance baselines, end-to-end tests (unreleased) | `convert-verified`/`convert-new` write to `library/markdown` and `library/images`; recorded latency baselines and a passage-lookup index; real-process fixture tests (PRs #84–#87). |
| Zotero write CLI (unreleased) | `import-doi`, `find-pdf`, `link-pdf --url`, `import-doi --with-pdf`, and a `guide("writes")` listing of every write path (PRs #89, #90). |
| Issue-driven work through v0.10.0 | Checkpointed conversion, selective index repair, provenance reconversion, `search_within_fulltext`, `lookup_citation_key`, a first-use guide and more. See `CHANGELOG.md`. |
