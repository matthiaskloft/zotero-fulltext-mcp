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
| 3 | **Public Release Readiness (Phases 1-4)** | release-readiness plan | TODO — **next up** | Supersedes hardening plan's Package 5, whose own deferral condition ("unless the project becomes shared/public") is now true — the repo is public. This is the only package standing between "works for the author" and "installable by a researcher who has never seen this code." Nothing else in either plan blocks external use the way this does. |
| 4 | Package 2: Transactional Derived Artifacts (reduced scope) | hardening plan | OPTIONAL | Ranked below release readiness because its narrow, user-facing slice (don't destroy a working index on a failed rebuild) is already carved out and delivered by release-readiness Phase 2. The remaining scope — full immutable generations, a current-pointer, cross-machine recovery journal — solves ongoing production-operation problems this deployment has not confirmed it has. Revisit only if a real need appears. |
| 5 | Package 3: Canonical Library and Reconciliation | hardening plan | OPTIONAL | Explicitly the highest-risk, most speculative package (whole-library migration). Depends on Package 2. Start only if the timestamped-run layout becomes an actual practical pain point. |
| 6 | Package 4B: Generation-Aware Retrieval and Library Status | hardening plan | BLOCKED | Hard dependency on Packages 2 and 3 above. Cannot start until one of them ships. |
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

Rank 3 (Public Release Readiness) is the first not-done item. Its Phase 1 (Cross-Platform CI and
Dependency Reproducibility) is the first phase within it — see the elevated package-level plan
added to `plan-public-release-readiness.md`'s Phase 1 section (Goal, files, steps, dependencies,
and explicit acceptance criteria) for the ready-to-implement detail.
