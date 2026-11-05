# Roadmap status audit and condensation proposal

Reviewed 2026-11-05 against the local checkout, not just issue titles. Completed tasks,
release summaries and retired Track 2 have now moved to [the archive](roadmap-archive.md).
The six reorganization items were approved and applied on 2026-11-05. The active roadmap is
now 114 lines with four workstreams, five-column tables, split S4 responsibilities and revised
dependencies. The recommendations below preserve the review rationale; the active roadmap
and linked design plans define the current organization. Existing task IDs remain searchable.
Release status comes from `pyproject.toml` and `CHANGELOG.md`; implementation status comes
from source and tests. The initial audit did not inspect live issue dispositions. A subsequent live check found #34
closed as superseded by #134: M3 now follows the existing-server opt-in curation design, not
the older separate-server proposal. Alert closure and private validation remain unverified.

## Status audit

| Existing items | Verified state | Evidence / remaining work |
|---|---|---|
| S1, S2 | Implemented, released | `benchmarks/retrieval.py`; `fts.py` field/year filters. No remaining implementation task for these steps. |
| S3 | Implemented, released | `zotero_metadata.py`, `quality.py`, `fts.py` metadata schema; calibration is E2, searchable abstract/tags are S4. Heuristic scores are not extraction-fidelity validation. |
| S1a | Harness implemented; validation incomplete | `benchmarks/passages.py`, `chunking.py`, retrieval tests and October results. Reviewed judgments, broader agent trials and budget/overlap sweeps remain. |
| S4 | Partly implemented | `pages.py`, `_extract_markdown.py`, `fts.py`: measured pages, exact spans, heading paths, structure-v1 character chunks. Token limits, explicit source-block hierarchy/section labels, stemming, prefix/proximity and abstract/tag FTS columns remain. |
| S4a, S5 | Planned | Manual passage navigation exists; automatic bounded assembly, section filtering and structured reference-entry search do not. |
| S6, S6a, S6b | Planned | `SemanticRetriever` explicitly reports unavailable. Fusion code and benchmark interfaces exist, but no production embedding backend or semantic MCP tool. |
| S7, E4 | Planned | No GROBID integration or extracted-reference graph in the package. Separate extraction/validation from graph construction while keeping #83 as their shared issue. |
| Track 2 | Complete in retained scope | `artifacts.py`, `_library_publish.py`, library/publication and end-to-end tests; latency runner and recorded baselines. Layout migration was dropped, so its test cannot remain pending. Slow common-term queries are a recorded limitation, not evidence that the baseline harness is unfinished. |
| M1 | Implemented, released | `pre_write_checks.py`: live bridge reads, verified-copy fallback and refusal on unavailable checks. WAL-aware snapshots exist. |
| M2 | Implemented, unreleased | Marker commands/backend and dependency extra removed; current optional dependencies exclude Marker. Release/alert verification remains operational follow-up, not an unimplemented removal. |
| M3 | Planned | CLI writes exist in `cli.py`, `zotero_write.py`, `bibtex.py`; the MCP curation workflow remains planned under #134; #34 was superseded. |
| M4 | Planned | No source-linked sidecar comment feature. Locator foundations exist. |
| M5 | Implemented, validation pending | `image_ocr.py`, `_ollama_client.py`, CLI and crop/recognition tests exist. General-use stress validation and announcement decision remain. |
| Z1 | Planned extension | `runtime.py` detects processes/connectivity, not a supported-version matrix. Container pins Zotero 9.0.6; multi-version compatibility remains unverified. |
| Z2–Z5 | Planned | No synced-fulltext fallback, broader Unicode/CJK normalization, searchable annotation pipeline or Zotero local-API write migration. Existing FTS uses unicode61; baseline case/diacritic behavior must not be presented as absent. |
| E1 | Planned | No reviewed internal page/block/reference artifact schema or external scorer adapter. Existing page-map sidecars are a different, implemented contract. |
| E2 / S3b | Acquisition implemented; labels/calibration pending | Public corpus pins, acquisition tooling and tests exist. OCR crop labels do not constitute reviewed whole-page extraction ground truth. |
| E3 / S3a | Experiments partly done; changes planned | `docs/conversion-text-decisions.md` records header/footer and normalization measurements that retained defaults. New cleanup, mappings and alternative extractor comparisons remain. |
| Packaging maintenance | Implemented, unreleased | `pyproject.toml` sdist exclusions and `tests/test_packaging.py`; listed in changelog, previously missing from the roadmap's unreleased summary. |

No planned feature above should be marked fully implemented merely because its benchmark
interface, supporting metadata or design documentation exists. Conversely, S1a and M5 should
not be labelled simply "planned": their remaining work is evaluation.

## Approved structure and rationale

The roadmap was reduced from roughly 400 lines before archiving to 114 lines. It should answer what exists,
what comes next and what depends on what. Detailed implementation choices belong in linked
plans and issues. Use four workstreams, displayed in pipeline order:

| Workstream | Existing task IDs retained | Ownership |
|---|---|---|
| Extraction and reference quality | E1–E4 (S3a/S3b remain aliases) | Reviewed artifact schema, corpus baseline, extraction/cleanup comparisons and reference parsing. |
| Chunking and retrieval | S1a, remaining S4, S4a, S5, S6–S7 | Frozen-text evaluation, source hierarchy, lexical features, context assembly, optional embeddings and citation graph queries. |
| Zotero compatibility and evidence | Z1–Z5 | Supported-version tests, synced-text investigation, normalization, annotations and local-API adoption. |
| Optional writes and user workflows | M3–M5; M2 release follow-up | Opt-in curation, sidecar comments, OCR validation and unreleased maintenance follow-up. |

Retire Track 2 from the active list. Keep its completed/discarded disposition in history.
Move M1, S1, S2 and S3 out of the open-work tables. Existing IDs remain searchable in the
completed summary; do not recycle them. If heading names change, update inbound links in
`docs/chunking-research-proposal.md` and `docs/workflow-test-corpus.md` in the same edit.

The condensed file would contain:

1. **Current capability and unreleased changes:** one paragraph, linked to changelog and architecture.
2. **Next milestones:** E1 pilot/schema and E2 baseline; S1a reviewed retrieval evaluation on
   frozen Markdown; Z1 compatibility checks independently. Extraction evidence gates promotion
   of defaults, not all chunker prototyping.
3. **Open work:** four compact tables with `ID | Deliverable | Status | Needs | Issue`.
   Each task appears once. Use `planned`, `partial`, `validation pending`, or `implemented,
   unreleased`; shipped work is summarized separately.
4. **Completed and dropped work:** short capability summary linked to changelog/history.
5. **Decision links:** extraction plan, chunking proposal, corpus plan and operations docs.

## Reorganize task scope before shortening rows

- Split S4's remaining checklist within its row/linked issues into **source structure**
  (blocks, parent links, section tags and token budgets) and **lexical features** (abstract/tag
  columns, stemming and prefix/proximity). These can ship separately; lexical changes do not
  all need a source hierarchy. Do not reopen completed #79/#80 work.
- Put block-aware/table-aware source spans under S4 and context expansion under S4a/#108.
  Repeated table headers and embedding prefixes are derived context with original locators.
- Keep E4 parsing and S7 graph queries distinct. S5's structured reference-entry search must
  explicitly depend on reference extraction (E4) as well as section/source structure; a section
  filter alone does not supply parsed entries.
- Make Z5 a preferred sequencing dependency of M3; remove completed M1 from the active
  dependency chain. M4 needs stable source locators; semantic discovery is not technically
  required to store comments. Reassess the existing S6 dependency rather than blocking comments
  on embeddings by default.
- Keep Z3 normalization separate from E3 transcription repair: matching normalization must not
  silently change authoritative scientific text. Coordinate rebuilds when useful.
- Give M5 a validation checklist/issue if it becomes scheduled; it currently has no issue.
  Do not open a new issue merely to rename a track.

## Where the removed detail goes

| Current roadmap material | Destination |
|---|---|
| Introductory architecture and safety explanation | Existing `docs/architecture.md` and `AGENTS.md`; retain a short link in roadmap. |
| Release-by-release lists and long History table | `CHANGELOG.md`; preserve unique package/step dispositions in a small roadmap-history document if absent from existing plans. |
| Chunking protocol and seven-project implementation review | Existing `docs/chunking-research-proposal.md` and `docs/search-quality.md`. |
| E1 contract, external scorer/model strategy, parser/extractor/GROBID dependency decisions | One extraction-quality design document, linked from E1–E4 and existing corpus docs. Move text intact before condensing. |
| Track 2 provenance/status cautions and recovery instructions | Existing operations/ingestion docs after checking for gaps; historical motivation belongs in history. |

Preserve the decisions already made: internal schema rather than evidence-ledger adoption;
separate expectations/observations/results; strict scientific fidelity checks alongside tolerant
metrics; human-reviewed baseline and injected defects; isolated external scorer pilot; model
review as calibrated triage; optional GROBID; no mandatory RAGFlow or restored Marker tree;
shared authoritative source spans with derived embedding inputs. Compression changes navigation,
not these decisions or acceptance gates.

## Verification for this audit

Focused local tests: pages/chunks, metadata quality, passage benchmark, canonical publication,
pre-write checks and packaging: **149 passed, 183 subtests passed**. This supports the reviewed
implemented foundations; it does not substitute for private-corpus validation, live multi-version
Zotero testing, dependency-alert verification or extraction-ground-truth review.
