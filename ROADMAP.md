# Development Roadmap

This roadmap merges the two active plan documents into one ordered sequence. It does not rename
or renumber the packages/phases inside those documents (their names are referenced by merged PR
branches and commit history — `codex/mcp-safe-read-surface`, `codex/package-4-retrieval-
foundation`, etc.) — it only orders them and states why each sits where it does.

Source plans:
- [`plan-mcp-server-hardening.md`](plan-mcp-server-hardening.md) — internal safety/robustness
  work (MCP capability scoping, transactional artifacts, canonical library, retrieval).
- [`plan-public-release-readiness.md`](plan-public-release-readiness.md) — external
  installability work (CI, crash-safe indexing, preflight checks, releases).

## Ordering principle

Ranks are ordered by **risk to a researcher's existing data**, lowest first, then by whether an
item unblocks a decision about a riskier one.

The remaining work is ordered by *step* rather than by whole package. Packages 3, 4B and 5 each mix
steps that only read and report with steps that rewrite file locations across a whole library.
Ranking each package as a single unit forced the read-only steps to inherit the migration steps'
risk gate, which is why they sat blocked despite having nothing risky about them. Step numbers below
refer to the step lists in the source plans and are unchanged — nothing here is renamed or
renumbered.

Risk levels used below:

- **none** — reads and reports only; cannot modify converted output, the index, or source PDFs.
- **contained** — writes only through the shipped transactional artifact layer (staged generation
  plus atomic pointer), so a failure rolls back to the prior generation.
- **high** — rewrites file locations and image references across the whole converted library.

## Order

| Rank | Package | Source | Risk | Status | Why here |
|------|---------|--------|------|--------|----------|
| 1 | Package 1: Safe MCP Read Surface | hardening plan | none | DONE | Shipped as PR #4. Prerequisite for exposing this server to any client at all. |
| 2 | Package 4A: Retrieval Foundation | hardening plan | none | DONE | Shipped as PR #5. Prerequisite for Package 4B; makes search predictable and bounded. |
| 3 | **Public Release Readiness (Phases 1-4)** | release-readiness plan | contained | DONE | Shipped as PR #7 and tagged v0.2.0. CI matrix (Windows/macOS/Linux), `uv.lock`, crash-safe index publication, `check-setup`, tagged releases. |
| 4 | Package 2: Transactional Derived Artifacts (reduced scope) | hardening plan | contained | DONE | Implemented 2026-07-17: immutable index generations + atomic `current.json` pointer + publish journal, managed `rebuild-index`/`update-index` command family, exclusive-create lock hardening, duplicate-key rejection, schema detection, output-root containment. |
| 5 | Package 4B step 1 (remainder): locator staleness | hardening plan | none | DONE | Package 4A already ships a locator keyed on `content_sha256` — stronger than the plan's `generation_id`, since a content hash survives regeneration. What is missing is verification: `get_fulltext_chunk` never checks the caller's hash against what is stored, so a locator taken before a reconvert silently returns text from a different document version. Adding the `stale_locator` response closes a correctness gap in a citation path that already ships, touches read code only, and depends on nothing unbuilt. |
| 6 | Package 5 step 4: adversarial and containment tests | hardening plan | none | DONE | Tampered `current.json` generation identifiers, path escapes on MCP reads and maintenance writes, lock-ownership races, untrusted instruction text in titles and snippets. `resolve_generation_dir` already validates and contains; this proves it against hostile input. Covers shipped code, so nothing gates it. |
| 7 | Package 5 step 2: schema-compatibility tests | hardening plan | none | DONE | An index written by an older version must migrate through a documented command or fail with a precise recovery instruction, never a raw SQLite error. Covers Package 2, which shipped at rank 4. |
| 8 | Package 3 steps 4 + 7: `audit-library` and `library_status` | hardening plan | none | READY | The read-only half of Package 3. Compares represented attachments, source availability, canonical files, JSONL and FTS metadata, reporting `current`, `unindexed`, `stale_markdown`, `source_changed`, `metadata_changed`, `missing_source`, `missing_markdown`, `orphaned_index` and `duplicate_key` with per-item evidence. It moves no files. It is also the honest precondition for rank 10: the plan says to start migration only once the timestamped-run layout is an actual pain point, and this is the command that answers whether it is, instead of guessing. Requires a new `library.py` (none exists today) and the `is_canonical_eligible` predicate from step 3, used here in report-only form. |
| 9 | Package 4B step 2: SQL aggregates and truthful status | hardening plan | none | READY after 8 | Move aggregate reporting to SQL and expose rank 8's `library_status`. `coverage_report` currently does `SELECT *` over the whole metadata table and counts in Python, and reports index row counts under the name "coverage" — the exact overstatement this step exists to fix. Needs rank 8 first, because status can only distinguish indexed-snapshot statistics from source-library health once an audit can produce that comparison. |
| 10 | Package 3 steps 1, 2, 3, 5, 6: canonical layout, migration, reconciliation | hardening plan | **high** | OPTIONAL / GATED | The destructive half: `library/markdown` and `library/images` as canonical locations, publication through the artifact layer, `migrate-library-layout` dry-run and apply, and reconciliation-plan upserts replacing key-only incrementality. This is the package the plan calls its highest-risk item, and the gate is unchanged — start only if rank 8's audit shows the timestamped-run layout is an actual practical pain point. Take a manual filesystem backup of `output_root` before `--apply`, independent of the dry-run report. |
| 11 | Package 5 steps 3, 5, 6: fixture tests, upgrade guide, performance baselines | hardening plan | none | FOLLOWS 8/10 | Step 6 (index build time and size, audit time, p95 search latency) can be recorded once rank 8 exists. Steps 3 and 5 document and exercise migration end to end, so they follow rank 10 and only exist if it ships. |

## Rationale for this ordering

- **Do the work that cannot damage a library before the work that can.** Ranks 5-9 read, verify and
  report; none of them can modify converted output, the index or a source PDF. Rank 10 rewrites file
  locations across the whole library. Ordering by that distinction rather than by package number is
  the substance of this revision.
- **An audit is not a migration, and should not have waited behind one.** Package 3's steps 4 and 7
  were blocked only because they shared a package with steps 1-3, 5 and 6. Splitting them out is what
  lets the go/no-go on the risky half be made from evidence rather than from intuition about whether
  the layout hurts yet.
- **Ship what unblocks other humans before what hardens internal operation further.** Packages 1 and
  4A made the server *safe* and *predictable* to expose. Release readiness made it *installable and
  diagnosable* by someone who isn't the author. That principle is unchanged; it is why rank 10 still
  sits near the end rather than being pulled forward now that its read-only half has a slot.
- **Package 3's gate is preserved, not weakened.** Reordering does not pre-approve rank 10. It still
  needs its own go/no-go once a concrete need shows up, per the hardening plan's Revision Notes — the
  difference is that rank 8 now supplies the evidence for that decision.

## Next action

Ranks 1-7 are done. **Rank 8** (`audit-library` and `library_status`) is next, and is the first
remaining item that adds a command rather than tests. It is also the one that decides rank 10: the
plan gates migration on the timestamped-run layout being an actual pain point, and this is the
command that answers whether it is.

Image OCR (`ocr-images`) sits outside this roadmap — it is feature-complete in `[Unreleased]` but its
release is deferred by decision, not blocked by anything here.
