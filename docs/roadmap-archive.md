# Roadmap archive

Completed and dropped work moved from `ROADMAP.md` on 2026-11-05.
Task IDs and original PR/issue links are retained for lookup. See the
[active roadmap](../ROADMAP.md) for remaining work.

## Released capability summary

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


## Track 2: Hardening leftovers

This track is complete in its retained scope; migration and its dedicated tests/guide were dropped.
These are the dispositions of [`plan-mcp-server-hardening.md`](../plan-mcp-server-hardening.md). Its
package and step numbers are kept as-is because merged branches and commits refer to them.

**Package 5, step 6: performance baselines.** Done: recorded in
[`docs/performance-baselines.md`](performance-baselines.md), re-runnable with
`benchmarks/latency.py`. They exposed a full-table scan on every passage fetch, now fixed with
an index, and searches on very common terms that still take seconds.

**Package 5, step 3: end-to-end fixture tests.** Done for the retained scope; the migration case
was dropped with migration. `tests/test_end_to_end.py` runs `convert-new` and `rebuild-index` on synthetic PDFs and
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


## History

These are the completed items, in the order they shipped. The source plans are
[`plan-mcp-server-hardening.md`](../plan-mcp-server-hardening.md),
[`plan-public-release-readiness.md`](../plan-public-release-readiness.md) and
[`plan-mcp-instruction-and-contract-robustness.md`](../plan-mcp-instruction-and-contract-robustness.md).

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

## Completed task rows

These rows retain the original task IDs; references to later steps describe the historical
sequence, not additional implementation work for these completed tasks.

| Step | What it does | Shipped / still open | Issues | Risk | Needs | Why here |
|---|---|---|---|---|---|---|
| **S1** (done) | Scores search quality (recall@k, MRR) on a personal question set that is never committed | Shipped v0.11.0 — [PR #98](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/98). No implementation work remaining. | [#78](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/78) | none | — | Every later step is judged by it. |
| **S2** (done) | Adds `author`, `title`, `citation_key` and year-range filters to `search_fulltext` | Author/title/citation-key/year filters shipped v0.11.0 — [PR #99](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/99). Other #77 features are tracked under S4/S5. | [#77](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/77) A + B | none | — | Query code only, with no schema change or rebuild. Fixes the most visible gap: an author search today also matches every reference list that cites that author. |
| **S3** (done, v0.12.0) | Stores previously omitted Zotero fields (abstract, tags, venue, item type, creator roles, …), and scores each paper's extraction quality | Metadata, detected language and extraction-quality scoring shipped v0.12.0 — [PR #114](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/114). Calibration remains S3b; searchable abstract/tag columns remain S4. | [#76](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/76), [#82](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/82) | contained | — | Needs a rebuild but no reconversion. The abstracts and tags are what S4's new columns and S6 build on. The quality score explains papers that are missing because extraction failed. |
| **M1** (done) | Fixes the duplicate check in `check-pdf` and `import-doi`. Live debug-bridge reads with a verified-copy fallback replace unsafe live `immutable=1` reads; writes refuse to proceed when neither check works. | Shipped v0.11.0 — [PR #97](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/97). WAL-aware mapper snapshots shipped v0.14.0 — [PR #151](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/151). | [#91](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/91) | none | — | A correctness bug in shipped write commands, so it comes first. It changes no derived data. |
