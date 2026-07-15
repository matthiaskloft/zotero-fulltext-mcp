# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Conversion timeouts now scale with page count and a cheap vector-drawing-density scan (long or
  diagram-dense books no longer lose structure/images to the plain-text fallback needlessly), and
  every genuine primary-extractor timeout is recorded as a "timeout candidate" (per-run
  `timeout_candidates.csv`/`.jsonl` plus a persistent, deduped master file) instead of only a manifest
  `error` note.
- New `retry-timeout` CLI command resolves a pending timeout candidate: `--skip` permanently routes
  that attachment straight to the plain-text fallback (recorded in `timeout_skip_list.json`, not
  hardcoded in source), or `--retry` reconverts it with a longer budget and promotes a successful
  result into the live manifest/index without ever overwriting the originally converted Markdown
  file in place.
- New MCP tools: `list_timeout_candidates` (read-only, always available) and, opt-in via
  `--enable-retry-timeout`, `skip_timeout_extraction`/`retry_timeout_extraction` (each gated behind
  its own literal `confirm` string and independently rate-limited from math-OCR reconversion).

### Changed

- The default MCP surface is now entirely read-only. Single-attachment math OCR is registered only
  with `--enable-reconvert` plus an explicit valid config; its confirmation literal remains defense
  in depth rather than a substitute for user approval. Reconversion preflights its JSONL sidecar
  and rolls back derived Markdown/image/index state if a later commit step fails.
- MCP server/tool instructions now explain offline/stale scope, untrusted-content handling,
  search-to-passage workflow, bibliographic attribution, and the absence of PDF page locators.
- MCP tools now advertise read-only, destructive, and closed-world safety hints to compatible
  clients, with reconversion explicitly marked non-idempotent. The optional MCP dependency is
  constrained to the tested v1 API (`mcp>=1.28,<2`) pending a separate v2 migration.
- Search now reports the fields that actually matched and content-bound locator hashes. Passage
  retrieval distinguishes complete and truncated stored chunks, exposes bounded chunk navigation,
  and returns deterministic reliability warnings for identity, attachment mapping, and math
  extraction concerns. Item context now requires exactly one parent or attachment key.
- `install-mcp --enable-reconvert` now rejects registration up front when the optional `[marker]`
  extra is not importable, instead of generating a registration that only fails once the server
  itself starts.
- Enabled MCP tools now advertise concrete success output schemas. Expected failures are native
  MCP `isError` results with stable, path-free public codes rather than success-shaped error
  dictionaries; disabled optional tools remain absent from the advertised surface.

## [0.2.0] - 2026-07-12

First tagged release. Covers everything merged since the initial import, aimed at making the
project safe to expose to an LLM client and installable by researchers outside the original
author's own machine.

### Added

- Safe default MCP read surface: search, bounded passage retrieval, and item-context tools are
  read-only and path-free by default; `reconvert_with_math_ocr` requires an exact confirmation
  literal and is rate-limited (PR #4).
- Explicit, bounded lexical search modes (`all_terms`, `any_terms`, `phrase`) with deterministic
  ranking and exact trimmed-text citation offsets (PR #5).
- Cross-platform CI (Windows/macOS/Linux) running the full test suite and a wheel build on every
  push/PR.
- `uv.lock` for reproducible installs; `uv sync --extra mcp --extra test --locked` is now the
  recommended install path alongside the existing `pip install -e .[mcp]`.
- `check-setup` CLI command: fast, read-only validation of config paths, `output_root`
  writability, Python version, and optional-extra availability before running a conversion.
- Crash-safe index publication: `build-index`, `append-index`, and `build-fts` now build to a
  same-directory temp file (with a `PRAGMA integrity_check` gate for the SQLite build) and only
  replace the previous file via an atomic rename on success. An interrupted or failed rebuild
  leaves the previous index intact and queryable instead of destroying it.
- `reconvert-math` now acquires the same cross-process write lock as every other index writer,
  closing a last-writer-wins race with concurrent index rebuilds.

### Changed

- `pyproject.toml` gained an explicit `[build-system]` (hatchling) declaration so `uv sync`
  installs the project itself, not just its dependencies.

## 0.1.0 - 2026-07-10

Initial import of the Zotero full-text conversion pipeline, CLI, and MCP server. Not tagged.

[Unreleased]: https://github.com/matthiaskloft/zotero-fulltext-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/matthiaskloft/zotero-fulltext-mcp/releases/tag/v0.2.0
