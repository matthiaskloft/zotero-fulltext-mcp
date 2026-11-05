# Development Roadmap

What to work on next, and why. Reviewed against local code on **2026-11-05**;
package version **0.15.2**. Status describes implementation, not live GitHub issue/alert state.

## Current capability

Read-only Zotero PDFs → Markdown → immutable SQLite FTS5 generations → MCP/CLI search.
Implemented: metadata filters, quality/language heuristics, physical page maps where measured,
verified passage locators, passage images, library audits, crash-safe publication and retrieval
benchmark harnesses. Production chunks use structure-v1: 1500 characters with up to 150 overlap
within split paragraphs. Semantic retrieval and automatic context assembly remain planned.

Unreleased: Marker whole-paper reconversion and its ML dependencies removed; source distributions
exclude local agent directories. Existing Marker records remain readable. Remove obsolete
`--enable-reconvert` registration flags. Normal conversion, timeout retries and opt-in local OCR remain.

Updating the package does not rebuild existing indexes or backfill page maps. Re-chunking can
invalidate locators; older extractions need reconversion for measured pages. See
[architecture](docs/architecture.md), [data dictionary](docs/data-dictionary.md),
[operations](docs/operations.md) and [release notes](CHANGELOG.md).

## Next milestones

1. **E1/E2 — extraction baseline:** internal artifact schema, reviewed pilot of 20 varied public
   pages plus a few whole papers, scorer sensitivity checks and a baseline report.
2. **S1a — retrieval evidence:** owner-reviewed judgments, broader agent trials and budget/overlap
   sweeps on frozen, hashed Markdown. The harness and initial measurements already exist.
3. **Z1 — compatibility:** supported-version matrix and tests, independently of extraction work.

Chunker prototypes can proceed now on frozen Markdown. Promote production extraction/chunking
defaults only after reviewed extraction evidence and downstream held-out retrieval checks.
Prefer lower-risk work and evidence-producing tasks first; coordinate schema rebuilds where useful.
Source PDFs and Zotero remain unchanged by the default workflow. Derived changes use staged
publication; optional Zotero writes remain explicitly separated.

## Open work

Four workstreams follow pipeline order. Existing task IDs are retained; E2/S3b and E3/S3a are
aliases. S4's remaining work is split into S4-structure and S4-lexical; S4a still means context
assembly. Completed foundations are in the [archive](docs/roadmap-archive.md).

Status: **planned**, **partial**, **validation pending**, or **implemented, unreleased**.
Dependencies name remaining prerequisites; completed S1/S2/S3/M1 foundations are not blockers.

### Extraction and reference quality

Ownership: PDF-to-Markdown/reference fidelity. Detailed decisions and acceptance gates are in the
[extraction-quality plan](docs/extraction-quality-plan.md); corpus acquisition and rights are in
[the corpus plan](docs/public-corpus-plan.md) and [workflow corpus](docs/workflow-test-corpus.md).

| ID | Deliverable | Status | Dependencies | Issue |
|---|---|---|---|---|
| **E1** | Internal artifact schema, strict fidelity checks and thin external benchmark adapter | Planned | Existing page maps/corpus pins | [#166](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/166) |
| **E2 / S3b** | Reviewed page/reference expectations, quality/language calibration and extraction baseline | Partial: acquisition shipped; labels/calibration pending | E1 | [#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115) |
| **E3 / S3a** | Source-preserving cleanup and isolated extractor comparisons | Partial: initial experiments retained defaults; new transforms/comparisons planned | E1, E2; S1a for retrieval impact | [#111](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/111) |
| **E4** | Optional GROBID reference parsing and artifact-based validation | Planned | E1, reviewed reference expectations from E2 | [#83](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/83) extraction scope |

### Chunking and retrieval

Ownership: divide, retrieve and assemble fixed extraction outputs. See the
[chunking design and experiment contract](docs/chunking-research-proposal.md),
[search evaluation](docs/search-quality.md) and [initial measurements](docs/chunking-results-2026-10.md).
Keep authoritative source spans/hashes shared across lexical and embedding views; derived prefixes,
repeated headers and assembled context retain source mappings and count toward complete token budgets.

| ID | Deliverable | Status | Dependencies | Issue |
|---|---|---|---|---|
| **S1a** | Reviewed evidence judgments, agent trials and size/boundary/overlap sweeps | Validation pending: harness and first measurements shipped | Frozen Markdown; E1/E2 baseline before default promotion | [#107](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/107) |
| **S4-structure** | Source-block IDs, hierarchy, neighbor links, section labels and production token budgets; preserve equations/captions/table rows | Partial: pages, spans, heading paths and character chunks shipped | S1a; E1/E2 before default promotion | [#108](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/108) prerequisites; [#81](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/81) section scope |
| **S4-lexical** | Abstract/tag FTS columns, stemming and prefix/proximity search | Planned | S1a; existing metadata; independent of hierarchy where possible | [#77](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/77) C–F |
| **S4a** | Bounded parent/neighbor/caption context and deduplicated path assembly, with per-span citations and explicit gaps | Planned: manual navigation exists | S1a, S4-structure | [#108](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/108) assembly scope |
| **S5** | Section filtering and reference-entry search; raw bibliography search may precede parsed-entry search | Planned | S4-structure section labels; E4 for parsed entries | [#77](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/77) H; [#81](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/81) |
| **S6** | Optional semantic paper discovery with an independently published local embedding index/tool | Planned: no production backend | Existing metadata/evaluation; S4-structure for passage extension | [#1](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/1) |
| **S6a** | Fixed-span leaf versus ancestor-context embeddings and lexical/semantic/hybrid passage ranking | Planned, conditional on passage-retrieval benefit | S1a, S4a, S6 | [#109](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/109) |
| **S6b** | Late versus independent embeddings on fixed spans/model weights, keeping assembly fixed | Planned experiment | S1a, S6a | [#110](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/110) |
| **S7** | Citation graph construction and queries; distinguish authorship, shared references and co-citation | Planned | E4; source structure for passage-linked graph evidence | [#83](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/83) graph scope |

### Zotero compatibility and evidence

Ownership: tested Zotero integration and source-linked annotation evidence. Current container tests
pin Zotero 9.0.6; a version-support matrix is still needed. Keep search normalization (Z3) distinct
from transcription repair (E3), and coordinate index rebuilds with S4-lexical where useful.

| ID | Deliverable | Status | Dependencies | Issue |
|---|---|---|---|---|
| **Z1** | Zotero 7/10, plugin and OS support matrix; runtime version detection and versioned fixture/live tests | Planned extension of existing pinned/live tests | — | [#137](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/137) |
| **Z2** | Investigate synced full-text as fallback/cross-check; deliver a recommendation | Planned investigation | Z1 | [#136](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/136) |
| **Z3** | Evaluate broader Unicode/typographic normalization and CJK matching | Planned; baseline folding exists, previous normalization experiment retained defaults | Retrieval regression evidence | [#138](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/138) |
| **Z4** | Searchable read-only highlights/annotation notes/colours linked to source chunks | Planned | Z1, stable source spans from S4-structure | [#139](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/139) |
| **Z5** | Migrate writes to the local API; deprecate debug-bridge only if no remaining consumer | Planned | Z1; existing safe pre-write checks | [#135](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/135) |

### Optional writes and user workflows

Ownership: explicitly enabled mutations and reader-owned sidecars. Default MCP remains read-only.
Live collection/tag/note browsing belongs to the companion Zotero MCP server.

| ID | Deliverable | Status | Dependencies | Issue |
|---|---|---|---|---|
| **M2** | Release merged Marker removal; security follow-up complete | Implemented, unreleased | Release; zero open alerts verified, #92 closed | [#92](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/92) |
| **M3** | Opt-in curation group on the existing server: state, jobs, review/approval and guarded apply; default remains read-only | Planned; #34 superseded | #134 WP0–WP6; coordinate Z5 before write implementation where practical | [#134](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/134) |
| **M4** | Reader comments in a sidecar, linked to verified source passages | Planned | Stable locators/lifecycle across S4-structure; embeddings optional | [#2](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/2), [#3](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/3) |
| **M5** | Large-library stress validation of opt-in image OCR and announcement decision | Validation pending: implementation packaged, unannounced | Reviewed stress run | [#168](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/168) |

## Completed work and decision records

- [Roadmap archive](docs/roadmap-archive.md): completed S1/S2/S3/M1, retired hardening track,
  dropped migration and historical PRs. Completed #79/#80 work stays complete.
- [Changelog](CHANGELOG.md): release history and unreleased maintenance, including packaging exclusions.
- [Extraction-quality plan](docs/extraction-quality-plan.md): internal schema, benchmark/model strategy,
  reference expectations and dependency decisions; evidence-ledger adoption remains deferred.
- [Chunking proposal](docs/chunking-research-proposal.md): research, seven-project source review,
  block/tokenizer candidates, embedding identity and evaluation gates. No mandatory RAGFlow or Marker tree.
- [Status audit](docs/roadmap-review-2026-11.md): code evidence behind this reorganization.
