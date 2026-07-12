# Plan: Public Release Readiness for External Researchers

**Created**: 2026-07-12
**Author**: Claude

## Status

| Phase | Status | Date | Notes |
|-------|--------|------|-------|
| Spec | DONE | 2026-07-12 | |
| Plan | DONE | 2026-07-12 | |
| Phase 1: Cross-Platform CI and Dependency Reproducibility | IMPLEMENTED_WITH_CONCERNS | 2026-07-12 | CI workflow authored and locally verified on Windows only (`uv sync --extra mcp --extra test --locked && uv run pytest -q`, `uv build`); the macOS/Linux legs of the matrix have not yet run on GitHub Actions since nothing has been pushed. See Notes. |
| Phase 2: Crash-Safe Index Publication | IMPLEMENTED | 2026-07-12 | Extended beyond the original file list during implementation/review: `build_text_index` (the first-build entry point) was also made atomic, `os.replace` gained retry-with-backoff for a reproduced Windows transient-`PermissionError` race, cleanup-failure exception masking was fixed, and `reconvert-math` now takes `pipeline_write_lock`. See Notes. |
| Phase 3: Preflight Check and Onboarding Polish | IMPLEMENTED | 2026-07-12 | `zotero_root` check and a `TypeError` config-parsing gap were both caught and fixed during review before this was marked done. |
| Phase 4: Tagged Releases and Changelog | IMPLEMENTED_WITH_CONCERNS | 2026-07-12 | Changelog, version bump, release convention, and README pinned-tag instructions are done. The actual `git tag`/push step is deliberately deferred until after this branch merges to `master` with green CI, per this phase's own dependency note. |
| Ship | TODO | | |

## Spec

### Summary

**Motivation**: The repository is already public on GitHub, has a working single-machine
install/config flow, and shipped Package 1 (safe MCP read surface) and Package 4A (bounded
lexical retrieval) from `plan-mcp-server-hardening.md`. But the project has never been installed
or used by anyone other than its author. Three concrete gaps stand between "this works for me"
and "a researcher I've never talked to can install this and point it at their own Zotero
library": (1) macOS/Linux support is explicitly documented as untested guesswork, so telling a
non-Windows researcher to install this is currently dishonest; (2) `build_fts_index` deletes the
existing search index before rebuilding it, so a crash or interrupted run during any rebuild —
not just the first one — can leave a first-time user with no working index and, unlike the
author, no git history or prior experience to recover from it; (3) there's no automated way for a
new user to tell "my config/environment is broken" apart from "conversion is slow" before they've
sunk time into a large conversion run, and no pinned/tagged version to install for stability.

**Outcome**: A researcher who has never seen this codebase can follow the README on Windows,
macOS, or Linux, get a fast preflight check that confirms their config and environment are sound
before committing to a long conversion run, get a search index that survives an interrupted
rebuild, and install a specific tagged version instead of a floating `HEAD`.

### Requirements

- The test suite must run in CI on Windows, macOS, and Linux, so the README's platform claims are
  verified rather than asserted.
- Dependency installation must be reproducible: a lockfile pins exact versions used in CI and
  recommended for install, while `pyproject.toml` keeps its existing minimum-version contract for
  library consumers.
- `build_fts_index` and any other in-place index writer must never destroy a previously working,
  queryable SQLite database as a side effect of starting a new build. If a build fails or is
  interrupted, the previous index must remain intact and queryable.
- A new CLI command must let a user validate their config, required paths, and Python/dependency
  versions in seconds, before running a potentially long conversion.
- Releases must be tagged (`vX.Y.Z`) with a changelog entry, so the README can recommend
  installing a specific tag instead of `HEAD` for anyone who isn't actively developing this
  project.
- None of the above may change existing CLI command names/behavior, MCP tool surface, or index
  schema in a way that breaks the already-shipped Package 1/4A contract.

### Design Decisions

| Decision | Options | Chosen | Rationale |
|----------|---------|--------|-----------|
| Distribution mechanism | Publish to PyPI; keep git-clone + `pip install -e .[mcp]` | Keep git-clone install | The index schema and MCP tool surface are still evolving (Packages 2-4B in the hardening plan remain undecided), and the target audience (researchers comfortable running Python virtualenvs to use an MCP server) is unaffected by the extra `git clone` step. Publishing to PyPI adds a build/publish pipeline and an implicit versioning promise this project isn't ready to make. Revisit once the schema is stable and there's a real request for it. |
| Dependency reproducibility | No lockfile (status quo); `pip-compile`/pinned `requirements.txt`; `uv.lock` | `uv.lock` | Matches the mechanism already named as the intended fix in `plan-mcp-server-hardening.md` Package 5, avoiding a second, later migration. `uv sync --extra mcp --extra test --locked` becomes the recommended install path in CI and docs; `pyproject.toml`'s `>=` bounds are untouched so the package still composes normally for anyone installing it as a dependency. |
| CI scope | Full Package 5 property/adversarial/fixture test suite; a minimal cross-platform pytest matrix | Minimal 3-OS pytest matrix (Windows, macOS, Linux) on push/PR | The specific claim blocking honest recommendation to external researchers is "macOS/Linux untested" — a matrix run of the existing suite directly retires that claim. Deeper adversarial/fixture testing (tampered pointers, lock races, injected mid-write crashes) is real but is Package 2/5 scope tied to the artifact-generation system this plan does not build; it stays deferred. |
| Crash-safety scope for index publication | Full Package 2 immutable-generation system with a pointer file and retained history; a narrower atomic single-file replace | Narrower atomic replace: build to a temp file in the same directory, validate, `os.replace` into place only on success | Package 2's generation/pointer/recovery-journal design solves ongoing multi-writer, multi-artifact production operation (JSONL + SQLite + manifest kept in lockstep across machines). The actual new-user risk here is simpler and narrower: one process, one file, don't destroy the old one until the new one is proven good. `os.replace` is atomic on both POSIX and Windows for same-volume renames, which covers this without adopting generation IDs, a current-pointer, or a recovery journal. |
| Preflight validation | No dedicated command (status quo, errors surface mid-run); a `check-setup` CLI command | Add `check-setup` | A first-time external user has no one to ask when `dry-run` fails 40 minutes in because of a bad path or a missing optional dependency. A fast, read-only command that validates config paths, Python version, and required/optional dependency availability up front turns a confusing mid-run failure into an actionable message before any time is spent. |
| Release process | Floating `HEAD` only (status quo); tagged releases with a changelog | Tagged releases (`vX.Y.Z`) + `CHANGELOG.md`, no PyPI publish | Tags/changelog are near-zero overhead (a `git tag` and a markdown entry) and let the README recommend `pip install "git+https://github.com/matthiaskloft/zotero-fulltext-mcp@v0.2.0#egg=zotero-fulltext-mcp[mcp]"` instead of an unpinned `HEAD` clone, without committing to the PyPI publish pipeline decided against above. |

### Scope

#### In Scope

- GitHub Actions CI running `pytest` on Windows, macOS, and Linux for every push/PR.
- `uv.lock`, with README/AGENTS.md install instructions updated to the `uv`-based path alongside
  the existing `pip install -e` path (both remain valid; `uv` becomes recommended).
- Atomic, crash-safe replacement for `build_fts_index`'s SQLite output (and the equivalent JSONL
  write path if it has the same destroy-before-write pattern) so an interrupted rebuild cannot
  leave a user with zero working index.
- A new `check-setup` CLI command: validates config resolution, required paths exist and are
  readable/writable as appropriate, Python version, and presence of optional extras relevant to
  requested operations (`mcp`, `marker`, `zotero-write`).
- `CHANGELOG.md` plus a tagging convention documented in `AGENTS.md`, and a first tag cut at the
  end of this plan's Phase 4.
- README/docs updates reflecting all of the above (install instructions, new command, platform
  support claim correction once CI is green).

#### Out of Scope

- Publishing to PyPI (explicit design decision above; revisit later).
- Package 2's full immutable-generation/pointer/recovery-journal system.
- Package 3 (canonical library layout, legacy migration tooling) — irrelevant to a new user who
  has no legacy timestamped runs to migrate.
- Package 4B (generation-bound retrieval locators, `library_status`) — depends on the full
  Package 2/3 systems this plan does not build.
- Any new MCP tool, changed tool signature, or changed index schema.
- Verified, tested macOS/Linux Zotero-executable auto-detection paths beyond what the CI matrix's
  synthetic/mocked tests can exercise — CI proves the Python package behaves correctly
  cross-platform, not that Zotero process-detection heuristics are correct on a real macOS/Linux
  Zotero install. That remains a documented best-effort caveat until someone runs it against a
  real non-Windows Zotero installation.

### Architecture Overview

```text
CI (GitHub Actions)
  matrix: windows-latest, macos-latest, ubuntu-latest
  uv sync --extra mcp --extra test --locked
  pytest -q
             |
             v
Index build (fts.py: build_fts_index)
  write to <output>.tmp-<pid>        (new)
  validate: PRAGMA integrity_check, expected record/chunk counts   (new)
  os.replace(tmp, output)            only on success — old file untouched until here
             |
             v
New user flow
  check-setup --config config.json   (new: fast, read-only, no conversion work)
  dry-run / convert-new              (existing, now safe to interrupt)
  install-mcp                        (existing)
```

### Constraints

- Keep Python 3.11 compatibility and SQLite FTS5; no new service dependency.
- Do not change `pyproject.toml`'s `>=` minimum-version contract; `uv.lock` is additive.
- Do not touch the MCP tool contract, index schema, or CLI command names shipped in Package 1/4A.
- `os.replace` atomicity assumption requires the temp file to be created on the same filesystem/
  volume as the final output; `build_fts_index` must write its temp file into `output.parent`,
  never a system temp directory, to preserve that guarantee.
- CI must not require real Zotero data, a GPU, or `marker-pdf`; keep fixtures synthetic as the
  existing test suite already does.

### Open Questions

None blocking. Whether to eventually publish to PyPI is explicitly deferred, not open — it is a
design decision above, not an unresolved question.

## Implementation Plan

### Phase 1: Cross-Platform CI and Dependency Reproducibility

**Goal**: Make the "works on Windows, macOS, Linux" claim verified rather than asserted, and make
installs reproducible.

**Files to create:**

- `.github/workflows/ci.yml` — matrix CI: `windows-latest`, `macos-latest`, `ubuntu-latest`;
  `uv sync --extra mcp --extra test --locked`; `pytest -q`.
- `uv.lock` — generated via `uv lock` against current `pyproject.toml` extras.

**Files to modify:**

- `pyproject.toml` — no dependency changes; confirm `[tool.uv]` section isn't needed beyond
  defaults, add one only if `uv lock` requires explicit extra grouping.
- `README.md` — add a `uv`-based install alternative alongside the existing venv/`pip install -e`
  path; note CI now covers all three platforms.
- `AGENTS.md` — update Development Commands section to mention `uv sync --extra mcp --extra
  test --locked` as the reproducible install path and `uv lock` as the update command.
- `docs/troubleshooting.md` — add a short "CI matrix" note pointing readers to the workflow badge
  as the source of truth for platform support, replacing informal claims.

**Steps:**

1. Run `uv lock` locally against the existing `pyproject.toml` extras (`mcp`, `zotero-write`,
   `marker`, `test`) to produce `uv.lock`. Confirm `marker-pdf` stays an optional, not
   CI-installed, extra (GPU-bound, not needed for the base test suite per existing test-mocking
   conventions).
2. Add the GitHub Actions workflow with the three-OS matrix, using `astral-sh/setup-uv` and
   `uv sync --extra mcp --extra test --locked`, then `uv run pytest -q`.
3. Push a throwaway branch/PR to confirm the matrix actually goes green on all three OSes; fix
   any platform-specific test failures surfaced (expect possible path-separator or
   `platform.node()`-related assumptions in existing tests — inspect and fix in place, do not
   skip tests to force green).
4. Update README/AGENTS.md/troubleshooting docs per the file list above once CI is verified green.

**Depends on:** None.

**Acceptance criteria:**

- A fresh checkout on Windows, macOS, and Linux each pass `uv sync --extra mcp --extra test
  --locked && uv run pytest -q` with no manual intervention.
- `uv.lock` is committed and pins exact resolved versions for every extra used in CI
  (`mcp`, `test`); `marker` stays excluded from the CI-installed extras (GPU-bound, not needed
  for the base suite).
- The GitHub Actions workflow runs on every push and PR against `master` and fails the check if
  any of the three OS jobs fails.
- No existing test is skipped, marked `xfail`, or deleted solely to make the matrix pass; any
  genuine platform-specific bug surfaced by the matrix is fixed in the source it exposes.
- README and `docs/troubleshooting.md` no longer state that macOS/Linux are "untested guesses"
  for the parts CI now verifies (package install, import, and the full test suite); the
  Zotero-executable-detection caveat for real (non-CI, non-synthetic) macOS/Linux installs
  remains, since CI cannot verify that.

### Phase 2: Crash-Safe Index Publication

**Goal**: An interrupted or failed index rebuild must never leave a user with a missing or
corrupt search index.

**Files to modify:**

- `src/zotero_pdf_text/fts.py` — `build_fts_index`: write to a temp path inside `output.parent`,
  validate (`PRAGMA integrity_check` plus the existing record/chunk count bookkeeping already
  computed during the build), then `os.replace` into `output` only on success; leave the prior
  `output` file untouched on any failure.
- `src/zotero_pdf_text/indexer.py` — `append_text_index()` (currently opens `existing_jsonl`/
  `output` with `.open("w", ...)`, truncating before writing, called from `cli.py` `append-index`
  and `convert-new`) and `replace_text_index_record()` (same truncate-then-write pattern) both
  need the same temp-then-replace treatment as `build_fts_index`; these are concrete existing
  destroy-before-write sites for the JSONL sidecar, not hypothetical.
- `src/zotero_pdf_text/math_ocr.py` — `reconvert_with_marker` calls into
  `replace_text_index_record()` above for the JSONL/index side, so it inherits the fix from
  `indexer.py`. Its separate `markdown_path.write_text(...)` overwrite of the converted Markdown
  itself stays intentionally non-atomic and out of scope: AGENTS.md already documents
  `reconvert_with_math_ocr` as the one deliberate write path, and a failed Markdown overwrite
  affects only that one paper rather than the whole index.
- `src/zotero_pdf_text/lock.py` — no code change; document in a comment or docstring near
  `pipeline_write_lock` that it serializes writers across processes/machines but does not by
  itself protect a concurrent reader (e.g. a running MCP server's `search_fts`) from observing a
  half-written file — that guarantee comes from the temp-then-`os.replace` pattern added in this
  phase, not the lock. Note as a pre-existing, out-of-scope gap that `reconvert-math` is not
  currently wrapped in `pipeline_write_lock` at all, unlike every other write command; not fixed
  here.
- `src/zotero_pdf_text/cli.py` — no behavior change expected; update any docstring/help text that
  currently implies in-place destructive rebuild.
- `tests/test_fts.py` — add tests: interrupted build (mock a failure partway through row
  insertion) leaves the previous valid database file byte-for-byte in place and still queryable;
  a successful rebuild fully replaces it; the temp file is cleaned up on both success and failure.
- `tests/test_indexer.py` — mirror the same interruption tests for `append_text_index()` and
  `replace_text_index_record()`: a failure partway through leaves the previous JSONL file intact
  and parseable.
- `docs/operations.md` — document the new crash-safety guarantee for `build-index`/`append-index`/
  `rebuild` commands, and note the lock-vs-atomic-replace division of responsibility above.

**Steps:**

1. Change `build_fts_index` to compute a unique temp path (`output.with_name(f".{output.name}.tmp-{os.getpid()}")`)
   inside the same directory as `output`. Build and populate the SQLite database at the temp path
   exactly as today, but do not `unlink()` the existing `output` up front.
2. After `con.commit()` and `con.close()`, run `PRAGMA integrity_check` against the temp database
   and confirm it returns `ok`; raise a clear error (and clean up the temp file) if it doesn't.
3. On successful validation, `os.replace(temp_path, output)` — atomic on both POSIX and Windows
   for same-volume renames, so a reader never observes a half-written file at `output`. On
   Windows, `os.replace` can transiently raise `PermissionError` if a reader has the destination
   open at the exact instant of replace; treat that as a safe build failure (the previous file is
   still untouched, since replace itself didn't happen) and let it propagate as the build's error
   rather than silently retrying into a masked failure — a short bounded retry (e.g. 3 attempts
   with a brief backoff) is acceptable but not required.
4. Wrap steps 1-3 in a `try/finally` that removes the temp file on any exception path, so failed
   builds don't accumulate stray `.tmp-*` files.
5. Apply the identical temp-then-replace pattern to `append_text_index()` and
   `replace_text_index_record()` in `indexer.py`, per the file list above.
6. Add the interruption tests described in the file list for both `fts.py` and `indexer.py`:
   patch the write loop to raise partway through, assert the pre-existing output file is
   unchanged (same mtime/hash) and still usable afterward (queryable for the SQLite case,
   parseable for the JSONL case).

**Depends on:** None (independent of Phase 1; can ship in parallel).

### Phase 3: Preflight Check and Onboarding Polish

**Goal**: A first-time user can confirm their setup is sound in seconds, before starting a
long conversion run.

**Files to create:**

- `tests/test_check_setup.py` — tests for each validation branch (missing config, unreadable
  Zotero data directory, missing linked-attachments folder, non-writable output root, missing
  optional extra when an operation that needs it is implied, Python version below 3.11 simulated
  via a mocked version check).

**Files to modify:**

- `src/zotero_pdf_text/cli.py` — add `check-setup` subcommand: resolves config the same way
  every other command does (`resolve_config_path`/`--config`), then reports pass/fail for each
  checked item (config found and parseable, `zotero_data_directory` readable, `linked_attachments`
  readable, `output_root` exists-or-creatable and writable, Python >= 3.11, `mcp` extra installed
  if `--check-mcp` or by default, `pyzotero`/`marker-pdf` presence noted as informational since
  they're optional). Read-only: performs no conversion work and does not write to `output_root`
  beyond a creatability check that itself doesn't leave stray files.
- `README.md` — add `check-setup` to the "Configure" section as the recommended first command to
  run after writing `config.json`, before `ensure-zotero`/`dry-run`.
- `docs/operations.md` — full command reference entry for `check-setup`.
- `docs/troubleshooting.md` — cross-reference `check-setup` from existing troubleshooting entries
  where the root cause is a bad path or missing extra.

**Steps:**

1. Design the check list as a small ordered sequence of `(name, check_fn) -> CheckResult(status,
   detail)` pairs. Reuse `validate_config()` from `config.py` for the checks it already covers
   (`zotero_root`, `zotero_data_directory`, `linked_attachments`, `zotero_sqlite`), but note that
   it does not check `output_root` at all — the `output_root` exists-or-creatable-and-writable
   check is new logic `check-setup` must implement itself, not delegated reuse.
2. Implement the subcommand to print a concise pass/fail table and exit non-zero if any required
   (non-optional) check fails, matching the existing CLI's error-reporting conventions (no raw
   tracebacks for expected failure modes).
3. Add the optional-extra checks (`mcp`, `zotero-write`, `marker`) as informational rather than
   failing by default, since a user may intentionally not have them installed; add a
   `--require-mcp` flag (or similar) for anyone who wants a hard failure before MCP registration.
4. Write the tests in the file list, covering each failure branch plus the all-pass case.
5. Update README/docs per the file list.

**Depends on:** None (independent; can ship in parallel with Phases 1-2). Should land before
Phase 4's release cut so the first tagged release includes it.

### Phase 4: Tagged Releases and Changelog

**Goal**: A researcher can install a specific, stable version instead of a floating `HEAD`.

**Files to create:**

- `CHANGELOG.md` — Keep a Changelog-style format, starting with an `[Unreleased]` section and a
  first dated entry once tagged.

**Files to modify:**

- `AGENTS.md` — document the release convention: update `CHANGELOG.md`, bump
  `pyproject.toml`'s `version`, tag `vX.Y.Z`, push the tag.
- `pyproject.toml` — bump `version` from `0.1.0` to the first tagged version (e.g. `0.2.0`,
  reflecting the Package 1/4A/this-plan's changes since `0.1.0`).
- `README.md` — change the install instructions to recommend
  `pip install "git+https://github.com/matthiaskloft/zotero-fulltext-mcp@v<version>#egg=zotero-fulltext-mcp[mcp]"`
  (pinned tag) as the primary path for anyone not actively developing this project, keeping the
  existing `git clone` + editable-install path documented for contributors.

**Steps:**

1. Populate `CHANGELOG.md` retroactively with entries for the Package 1 and Package 4A merges
   (PR #4, PR #5) plus this plan's Phases 1-3, using existing merge-commit messages and plan
   documents as the source of truth — do not invent unverified detail.
2. Bump `pyproject.toml` version.
3. Document the release convention in `AGENTS.md`.
4. Tag the release (`git tag v0.2.0 && git push origin v0.2.0`) after Phases 1-3 are merged to
   `master` and CI is green on the tagged commit.
5. Update README's install section to the pinned-tag command.

**Depends on:** Phases 1-3 (the tag should represent a release that already has CI, crash-safe
indexing, and the preflight check — cutting a tag before those land would immediately need a
follow-up patch release).

## Verification & Validation

- **Automated**: `uv run pytest -q` locally and in the new three-OS CI matrix. New tests for
  Phase 2 (interrupted-build safety) and Phase 3 (`check-setup` branches) must pass on all three
  platforms, not just the developer's machine.
- **Manual**: On a non-committed local config, run `check-setup` against a deliberately broken
  config (bad path, missing extra) and confirm the failure message is actionable; run it against
  a valid config and confirm all-pass; interrupt a real `rebuild-index`/`build-index` run (e.g.
  `Ctrl+C` or a forced process kill partway through) against a copy of derived output and confirm
  the previous index is still queryable afterward.
- **Release checks**: Confirm the tagged install command in the README actually installs and runs
  `zotero-fulltext-mcp --help` in a clean venv with no other local state.

## Dependencies

- Phase 4 depends on Phases 1-3 landing first (see above).
- Phases 1, 2, and 3 have no dependencies on each other and may be implemented/shipped in any
  order or in parallel.
- No new database service, embedding provider, or Zotero write permission is required.

## Notes

- This plan deliberately does not resurrect Packages 2, 3, or 4B from
  `plan-mcp-server-hardening.md` in full. It borrows the narrow, concrete slice of Package 2
  (don't destroy a working index on a failed rebuild) that directly matters for a first-time
  external user, and leaves the rest — multi-generation history, canonical library migration,
  generation-bound retrieval locators — deferred exactly as that plan already states, pending a
  confirmed real need.
- PyPI publication is a deferred decision, not a rejected one; revisit once the index
  schema/MCP tool surface is stable enough to make a versioning promise to strangers.
- Real macOS/Linux Zotero-installation verification (as opposed to CI running the Python test
  suite on those OSes) remains an open, explicitly documented caveat this plan does not close —
  it requires an actual non-Windows Zotero installation to test against, which the author does
  not currently have.

## Implementation Notes (2026-07-12)

All four phases were implemented in one pass on `feat/public-release-readiness`, rebased onto
`master` at `1e6486c` (post-PR-#6) before this note was written. 167 tests pass locally via
`uv run pytest -q`.

**Deviations from the original phase file lists, all discovered during implementation or the
review loop and judged in-scope because they directly serve the same phase's stated goal:**

- **Phase 1**: `pyproject.toml` needed a `[build-system]` (hatchling) table and
  `[tool.hatch.build.targets.wheel] packages = ["src/zotero_pdf_text"]`, not anticipated in the
  plan's file list. Without it, `uv sync` installed only dependencies, not the project itself —
  the documented `uv run zotero-fulltext-mcp --help` instruction would have failed. Also added a
  `uv build` CI step and `tests/test_packaging.py` (console-script registration check) after
  review flagged that the build-system config was otherwise never exercised by CI, and that
  `pytest`'s `pythonpath = ["src"]` override means the rest of the suite never imports the
  actually-installed package.
- **Phase 2**: extended the atomic temp-then-replace treatment to `build_text_index` (the
  first-full-build entry point, not in the original file list) after review identified it as an
  untested, unprotected destroy-before-write site serving the exact risk this phase targets.
  Added `_replace_with_retry` (short retry with backoff around `os.replace`) after independent
  review reproduced a genuine Windows `PermissionError` when a reader has the destination file
  open at the instant of rename. Fixed a `finally`-block cleanup-exception-masking bug (a failed
  temp-file `unlink` after a real build failure would otherwise replace the original exception).
  Added `pipeline_write_lock` to `reconvert-math` (`math_ocr.py`), which previously wrote the
  shared JSONL/FTS files without the lock every sibling write command already takes — closing a
  latent last-writer-wins race, not one newly introduced by this phase's changes.
- **Phase 3**: initial implementation omitted the `zotero_root` check (`validate_config()` checks
  four paths; the first draft only ported three) — caught by spec-compliance review and fixed,
  with the masking test corrected to actually create `zotero_root` on disk. Also caught: `TypeError`
  from structurally-malformed-but-syntactically-valid config JSON (e.g. a JSON array instead of an
  object) was not in the caught-exception tuple and would have crashed the command with a raw
  traceback — exactly the failure mode this command exists to prevent. Both fixed and tested.
- **Phase 4**: no functional deviations. `git tag`/push intentionally not performed yet — see the
  Status table.

**Not fixed, explicitly deferred as pre-existing/out-of-scope:**

- `reconvert-math` is still not wrapped for CLI-vs-MCP-tool duplicate-invocation races beyond the
  lock now added; this closes the specific gap review found without expanding into Package 2's
  full generation system.
- Orphaned temp files from a hard process kill (not a normal exception) are not swept or reported;
  accepted as an inherent limitation of PID-named temp-then-replace, consistent with this phase's
  narrower scope versus Package 2's full recovery-journal design.

## Review Feedback

Plan reviewed in 1 iteration by an independent `feature-dev:code-architect` review. Result: 0
blockers, 4 warnings (all addressed by revision), 4 suggestions.

- Addressed: `lock.py`'s `pipeline_write_lock` serializes writers but does not protect a
  concurrent reader from a half-written file; Phase 2 now documents this division of
  responsibility explicitly rather than silently composing with it.
- Addressed: `indexer.py`'s `append_text_index()` and `replace_text_index_record()` are concrete,
  already-in-production destroy-before-write sites for the JSONL sidecar (truncate-then-write via
  `.open("w", ...)`), not merely a hypothetical "check if this happens" — Phase 2's file list now
  names them explicitly and adds matching interruption tests.
- Addressed: Phase 3's reliance on `validate_config()` was overstated — it does not check
  `output_root`; the plan now states that check is new logic, not reuse.
- Addressed: noted `os.replace`'s narrow Windows transient-`PermissionError` failure mode as a
  safe-but-possible build failure, distinct from the crash-safety guarantee itself.
- Noted, not addressed (accepted as out of scope per reviewer's own recommendation):
  `reconvert-math` is not currently wrapped in `pipeline_write_lock`, unlike every other write
  command — a pre-existing gap, called out in Phase 2's file list so it isn't mistaken for
  already-fixed, but not remediated by this plan.
- Noted, not addressed (accepted as out of scope): `math_ocr.py`'s Markdown overwrite itself
  (as opposed to the index/JSONL side, which is fixed) stays intentionally non-atomic, matching
  its existing documented status in AGENTS.md as the one deliberate write path; a failure there
  affects only one paper, not the whole index.
- Confirmed no risk to Package 1's safe-MCP-read-surface guarantees or Package 4A's search
  contract — no shared code path is touched by this plan's changes.
