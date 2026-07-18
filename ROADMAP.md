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

## Order

| Rank | Package | Source | Status | Why here |
|------|---------|--------|--------|----------|
| 1 | Package 1: Safe MCP Read Surface | hardening plan | DONE | Shipped as PR #4. Prerequisite for exposing this server to any client at all. |
| 2 | Package 4A: Retrieval Foundation | hardening plan | DONE | Shipped as PR #5. Prerequisite for Package 4B; makes search predictable and bounded. |
| 3 | **Public Release Readiness (Phases 1-4)** | release-readiness plan | DONE | Shipped as PR #7 and tagged v0.2.0. CI matrix (Windows/macOS/Linux), `uv.lock`, crash-safe index publication, `check-setup`, tagged releases. |
| 4 | Package 2: Transactional Derived Artifacts (reduced scope) | hardening plan | DONE | Implemented 2026-07-17: immutable index generations + atomic `current.json` pointer + publish journal, managed `rebuild-index`/`update-index` command family, exclusive-create lock hardening, duplicate-key rejection, schema detection, output-root containment. |
| 5 | Package 3: Canonical Library and Reconciliation | hardening plan | OPTIONAL | Explicitly the highest-risk, most speculative package (whole-library migration). Depends on Package 2 (now shipped). Start only if the timestamped-run layout becomes an actual practical pain point. |
| 6 | Package 4B: Generation-Aware Retrieval and Library Status | hardening plan | BLOCKED | Depends on Packages 2 (done) and 3 (optional). Cannot start until Package 3 ships. |
| — | Package 5 steps 2-6 (schema-compat tests, fixture tests, upgrade guide) | hardening plan | FOLDED IN | Superseded in scope by release-readiness Phase 1 (CI) and Phase 2 (crash-safety tests); remaining schema-compat/fixture/upgrade-guide work is deferred alongside Packages 2-3, since it documents behavior those packages would add. |

## Rationale for this ordering

- **Ship what unblocks other humans before shipping what hardens internal operation further.**
  Packages 1 and 4A made the server *safe* and *predictable* to expose — necessary before anyone
  else installs it. Release readiness makes it *installable and diagnosable* by someone who isn't
  the author. Both are now more valuable than Packages 2/3/4B, which hardens a production
  operation mode (multi-generation history, cross-machine migration) nobody has asked for yet.
- **Release readiness was designed to not require Packages 2/3.** It intentionally borrows only
  the narrow slice of Package 2 that matters for a first-time user (atomic index replace) rather
  than waiting on the full generation system — see the Design Decisions table in
  `plan-public-release-readiness.md`. That is what makes it possible to rank it ahead of Package 2
  without a dependency conflict.
- **Packages 2, 3, and 4B stay exactly as speculative as the hardening plan already marked them.**
  This roadmap doesn't pre-approve them; each still needs its own go/no-go once a concrete need
  shows up, per that plan's Revision Notes.

## Next action

Ranks 1-4 are done. The remaining items (Package 3, and Package 4B behind it) are optional and
explicitly need-driven: start Package 3 only if the timestamped-run layout becomes an actual
practical pain point, per the hardening plan's own status notes. With Package 2's Unreleased
changelog backlog plus PRs #8-#18, cutting a v0.3.0 release (per AGENTS.md's release convention)
is the more immediate next step than either optional package.
