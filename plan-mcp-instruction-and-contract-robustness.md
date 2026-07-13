# Plan: MCP Instruction and Contract Robustness

**Created**: 2026-07-13
**Author**: Codex

## Status

| Phase | Status | Date | Notes |
|-------|--------|------|-------|
| Spec | DONE | 2026-07-13 | Follow-up to the completed safe-read-surface work and the 2026-07-13 MCP instruction review. |
| Plan | DONE | 2026-07-13 | Three sequential, independently shippable phases. |
| Phase 1: Capability Guidance and Opt-In Mutation | MERGED | 2026-07-13 | [PR #8](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/8) merged; default is read-only, and reconversion is opt-in, annotated, preflighted, and rollback-protected. |
| Phase 2: Evidence and Retrieval Contract | IMPLEMENTED | 2026-07-13 | Added content-bound locators, matched-field evidence, exact-chunk navigation/truncation semantics, reliability warnings, and strict context-key validation; full suite passes. |
| Phase 3: Protocol-Native Schemas and Errors | TODO | | |
| Ship | IN_PROGRESS | 2026-07-13 | Preparing Phase 2 for review; Phase 1 was merged in [PR #8](https://github.com/matthiaskloft/zotero-fulltext-mcp/pull/8). |

## Spec

### Summary

**Motivation**: The MCP server has a bounded, path-free surface and labels returned scholarship as
untrusted, but its short server instruction does not explain the intended search-to-evidence
workflow. The current default surface also exposes a converted-output rewrite tool behind a
literal argument that an agent can supply itself. Search results do not distinguish body matches
from metadata-only matches, source locators are not content-bound, passage navigation is weak, and
the generic dictionary return annotations do not give MCP clients precise output schemas or
protocol-native tool errors.

**Outcome**: MCP clients will receive concise cross-tool guidance, accurate tool-specific usage
descriptions, explicit risk annotations, and a read-only default tool set. Search and retrieval
responses will distinguish discovery from evidence, bind locators to the converted content, expose
bounded navigation and reliability warnings, and use protocol-native schemas and error signaling.

### Requirements

- Keep the default MCP tool set read-only with respect to both Zotero and the converted-text
  sidecar. `reconvert_with_math_ocr` must be absent unless explicitly enabled at server startup.
- Preserve the optional interactive reconversion workflow for users who deliberately enable it;
  retain its exact confirmation literal, rate limiter, single-attachment scope, and pipeline lock.
- Keep the server instruction concise and limited to stable cross-tool behavior: offline/stale
  scope, untrusted-content handling, search-to-retrieval workflow, evidence attribution, absence
  of page locators, and explicit approval for writes.
- Put parameter semantics and operational details in the individual tool descriptions rather than
  duplicating the README in the server instruction.
- Add MCP safety annotations for every registered tool. Treat them as client hints, not as the
  enforcement boundary.
- Distinguish body-text matches from title, creator, and citation-key matches. A metadata-only hit
  must never be presented as if its selected body chunk contains supporting evidence.
- Bind each search/passage source locator to the indexed Markdown content hash without exposing a
  local path. Do not claim generation-level stability beyond that content binding.
- Make exact-chunk traversal explicit and bounded. Preserve the existing behavior in which
  omitting `chunk_index` returns a leading passage, but make that behavior unambiguous.
- Require exactly one of `parent_key` and `attachment_key` for item-context lookup.
- Surface machine-usable extraction/identity warnings instead of requiring clients to interpret
  undocumented internal enum values unaided.
- Advertise precise success output schemas and report expected tool failures with MCP
  `isError=true`, stable public codes/messages, and no local diagnostics.
- Preserve Python 3.11, SQLite FTS5, the optional `[mcp]` dependency boundary, all existing CLI
  response fields, and existing installations except for the deliberate change that MCP
  reconversion becomes opt-in. New CLI JSON fields must be additive.

### Design Decisions

| Decision | Options | Chosen | Rationale |
|----------|---------|--------|-----------|
| Instruction size | Keep the current one-liner; embed a full manual; concise workflow-oriented instruction | Concise workflow-oriented instruction | The server instruction should establish trust and cross-tool behavior without spending context on limits and setup details that belong in schemas/docs. |
| Reconversion exposure | Keep default literal gate; remove from MCP; startup opt-in | Startup opt-in | This revises the earlier hardening-plan decision. It preserves the valued in-conversation repair workflow while making the default surface structurally read-only; the literal remains defense in depth, not proof of consent. |
| Mutation approval | Rely on wording; rely on annotations; structural opt-in plus explicit-user wording and annotations | Structural opt-in plus wording and annotations | Tool annotations and prompts are advisory. Registration-time capability separation is the enforceable boundary available in this local stdio design. |
| Search match provenance | Document possible metadata matches only; return body/non-body boolean; return matched fields | Return matched fields | `matched_fields` lets clients distinguish `text`, `title`, `creators`, and `citation_key`, including queries whose terms match across fields. |
| Locator binding | Wait for managed generations; add Markdown hash; invent page mapping | Add Markdown hash | `markdown_sha256` already exists in FTS metadata and is refreshed by reconversion. Including it makes a locator content-specific without reviving the deferred generation architecture or fabricating pages. |
| Passage navigation | Keep implicit chunk indices; add exact-chunk navigation metadata; redesign retrieval around arbitrary character windows | Add exact-chunk navigation metadata | `chunk_count`, previous/next indices, and `has_more` solve normal traversal additively. Omitting `chunk_index` remains a leading-preview convenience, not a pagination cursor. |
| Reliability signaling | Explain internal enums in the prompt; add normalized warnings; hide provenance | Add normalized warnings while retaining provenance | Stable warnings such as `identity_unverified` and `math_extraction_may_be_lossy` are easier for clients to act on while the detailed provenance remains available. |
| MCP result shape | Keep `dict[str, object]`; use typed success schemas and ordinary error dictionaries; use typed success schemas plus protocol-native tool errors | Typed success schemas plus protocol-native tool errors | Clients can validate and understand successful results, while `isError=true` correctly distinguishes expected failure from successful data. |
| MCP SDK compatibility | Leave `mcp>=1.9` unbounded; adopt v2 immediately; pin the tested v1 API range | Pin the tested v1 API range | The locked implementation is MCP 1.28.1 and the v2 API is a separate migration. Use `mcp>=1.28,<2` for this work and plan v2 separately. |

### Scope

#### In Scope

- Server instruction and all MCP tool descriptions.
- Startup flags and generated registrations for opt-in reconversion.
- MCP tool annotations, success schemas, and tool-level error signaling.
- Search match-field provenance, content-bound locators, passage navigation metadata, exact-one
  context-key validation, and normalized reliability warnings.
- Focused unit/protocol tests, README/tool-contract documentation, architecture/operations/data
  dictionary updates, dependency lock refresh, and an Unreleased changelog entry.

#### Out of Scope

- Live Zotero collections, tags, notes, or Zotero URI access; these remain the official Zotero MCP
  server's responsibility.
- A managed index-generation pointer, generation history, or generation IDs.
- Verified PDF page-to-text mapping or page-number citations.
- Semantic/vector search, query rewriting, reranking models, or search-quality evaluation beyond
  the explicit lexical contract.
- Bulk math OCR, arbitrary MCP write tools, Zotero writes, or changes to the CLI write-plan model.
- MCP v2 migration; this plan deliberately stabilizes on the tested v1 API first.

### Architecture Overview

```text
MCP initialization
  -> concise server instructions (scope, trust, workflow, attribution)
  -> registered tools + annotations
       -> default: search / retrieve / context (read-only)
       -> optional: BibTeX read bridge
       -> optional: reconvert one attachment (side-effecting)

search_fulltext
  -> FTS match across title / creators / text / citation_key
  -> matched_fields + snippet + content-bound source_locator + warnings
  -> get_fulltext_chunk(attachment_key, source_locator.chunk_index)
  -> exact passage + navigation + same content-bound locator + warnings
  -> get_item_context(exactly one key) for bibliography/extraction context

tool execution
  -> typed success data advertised through outputSchema
  -> expected failure mapped to isError=true with public code/message
  -> unexpected failure mapped to generic isError=true without paths/traceback
```

The existing `fts.py` layer remains the local query/retrieval implementation. `mcp_contract.py`
continues to own the public boundary: capability registration, annotations, validation,
serialization, warnings, and error redaction. The CLI retains path-bearing maintenance DTOs and
does not import the optional MCP runtime merely to run non-MCP commands.

### Constraints

- Never expose or modify real Zotero data during automated tests; use temporary fixtures only.
- Keep the MCP package optional. Imports needed only for actual server construction must remain
  lazy so `pip install .` without `[mcp]` still supports the conversion/CLI package.
- Keep normal MCP responses free of `source_path`, `markdown_path`, config paths, and endpoint
  details.
- Preserve existing response fields where practical; new evidence/navigation fields are additive.
  The exactly-one context-key rule and opt-in reconversion are intentional contract tightenings and
  must be documented in the changelog.
- Do not describe character offsets as PDF page positions. Adjacent stored chunks may overlap, so
  navigation metadata must not imply disjoint passages.
- Do not add a new configuration system. Reuse explicit CLI flags and the existing
  `install-mcp` registration generation.
- Any `pyproject.toml` dependency change requires a matching `uv.lock` refresh.

### Open Questions

None blocking. The plan chooses `--enable-reconvert` as the startup flag and
`content_sha256` as the public locator field name; implementation review may refine naming only if
it finds a collision with an existing public contract.

## Implementation Plan

### Phase 1: Capability Guidance and Opt-In Mutation

**Goal**: Make the default server structurally read-only, and give clients concise, accurate usage
guidance and risk metadata.

**Files to create:**

- None.

**Files to modify:**

- `src/zotero_pdf_text/mcp_contract.py`
- `src/zotero_pdf_text/mcp_server.py`
- `src/zotero_pdf_text/cli.py`
- `src/zotero_pdf_text/math_ocr.py`
- `tests/test_mcp_server.py`
- `tests/test_cli.py`
- `tests/test_math_ocr.py`
- `pyproject.toml`
- `uv.lock`
- `README.md`
- `docs/architecture.md`
- `docs/operations.md`
- `AGENTS.md`
- `CHANGELOG.md`

**Steps:**

1. Replace `MCP_INSTRUCTIONS` with a roughly 120-180 word instruction covering: offline and
   potentially stale scope; untrusted titles/metadata/text/BibTeX; `all_terms` -> targeted chunk
   retrieval -> context workflow; `any_terms` and `phrase` guidance; human-readable bibliography
   plus attachment/locator traceability; no invented page numbers; and explicit user approval for
   any converted-output rewrite. Keep numeric bounds, install guidance, Marker details, and
   cooldown duration out of this string.
2. Expand each tool description with only its local semantics. In particular, state that search
   covers metadata and body text, that `chunk_index` should normally come from a search hit and
   omission returns a leading passage, that context accepts exactly one key, that BibTeX reads a
   local optional integration, and that reconversion overwrites one converted Markdown/image/index
   record without writing Zotero.
3. Add `enable_reconvert: bool = False` to `create_server`. Register
   `reconvert_with_math_ocr` only when true. Split the tool-name constants into the read-only
   default tuple plus explicit optional BibTeX and reconversion names. Enabling reconversion must
   require an explicitly supplied, valid `--config` at server startup; reject
   `--enable-reconvert` plus DB-only startup with a structured startup error rather than
   advertising a tool whose every call would return `config_required`.
4. Add `--enable-reconvert` to `zotero-fulltext-mcp` and `zotero-pdf-text install-mcp`. Propagate
   it through generated Claude/Codex registrations exactly as `--enable-bibtex` is propagated.
   Loading a config may continue when needed to derive `--db`, but merely supplying `--config`
   must not expose reconversion without the flag.
5. Add `ToolAnnotations` to every tool using the locked v1 SDK API. Mark search, passage, and
   context `readOnlyHint=True`, `destructiveHint=False`, and `openWorldHint=False`; omit
   `idempotentHint` for read-only tools because MCP defines it in terms of repeated environmental
   effects and it is meaningful only for non-read-only operations. Give the loopback-only BibTeX
   bridge the same read-only/non-destructive/closed-world annotations. Mark reconversion
   non-read-only, destructive, non-idempotent, and closed-world. Treat these values as hints only;
   the startup flag remains the enforcement boundary.
6. Keep MCP imports lazy. Update `FakeFastMCP` to capture decorator metadata without weakening the
   no-`mcp` import path used by ordinary CLI/package operation.
7. Change the MCP extra to `mcp>=1.28,<2`, regenerate `uv.lock`, and document that the project is
   intentionally on the tested v1 API pending a separate v2 migration.
8. Update README, architecture, operations, AGENTS, and the Unreleased changelog so they no longer
   describe reconversion as part of the safe default surface or the confirmation literal as user
   approval.

**Depends on:** None.

**Acceptance criteria:**

- Default `create_server` exposes only `search_fulltext`, `get_fulltext_chunk`, and
  `get_item_context`; optional tools appear only under their explicit flags.
- Generated registrations include `--enable-reconvert` only when requested.
- `--enable-reconvert` without an explicit valid config fails at startup; an enabled tool is
  callable rather than predictably unavailable.
- Every listed tool has the intended MCP annotations and an accurate, workflow-oriented
  description.
- The instruction contains the trust, scope, retrieval, attribution, and mutation rules without
  operational recipes or local facts.
- Non-MCP CLI commands still import and run without the `[mcp]` extra.

### Phase 2: Evidence and Retrieval Contract

**Goal**: Make search results honest about why they matched and make retrieved evidence stable,
navigable, and easy for an agent to assess.

**Files to create:**

- None.

**Files to modify:**

- `src/zotero_pdf_text/fts.py`
- `src/zotero_pdf_text/mcp_contract.py`
- `tests/test_fts.py`
- `tests/test_mcp_server.py`
- `tests/test_cli.py`
- `README.md`
- `docs/architecture.md`
- `docs/operations.md`
- `docs/data-dictionary.md`
- `CHANGELOG.md`

**Steps:**

1. Extend `SearchResult` and the FTS query with `markdown_sha256` and `matched_fields`. Use the
   actual FTS5 column mapping (`title=0`, `creators=1`, `text=2`, `citation_key=3`) to select a
   highlighted value for each indexed field, then compare each highlighted value with its original
   value to determine whether FTS inserted markers. Do not infer matches by merely scanning for a
   sentinel that could already occur in scholarship. Preserve body-match-first representative-
   chunk ranking and deterministic record deduplication, and retain a collision fixture proving
   that original marker-like/control content does not create a false match.
2. Return `matched_fields` through the MCP serializer. A metadata-only result still has a chunk
   locator for retrieval, but the contract must explicitly say that the located chunk is a
   navigation starting point, not proof that the query occurs in body text.
3. Add the record's `markdown_sha256` to `FullTextResult` and include it as
   `content_sha256` in every search and passage `source_locator`. Keep attachment key, chunk index,
   and trimmed-text character offsets unchanged. Update documentation to call this
   content-bound, not generation-bound or page-bound. Search locators describe the complete stored
   chunk. Exact retrieval locators describe only the returned span: add `truncated`,
   `stored_chunk_char_start`, and `stored_chunk_char_end` so a caller can distinguish a complete
   chunk from a `max_chars` prefix. For an untruncated exact retrieval the search and passage
   locators are equal; for a truncated retrieval they intentionally share attachment/hash/chunk
   identity but have different returned end offsets. For a leading preview (`chunk_index=null`),
   set both stored-chunk span fields to `null` because the response may combine multiple chunks;
   set `truncated` to whether the returned document span ends before `total_chars`. Encode the
   exact-versus-preview nullability in the TypedDict schema and tests.
4. Query and return `chunk_count` for passage retrieval. For an exact `chunk_index`, return
   `previous_chunk_index`, `next_chunk_index`, and `has_more`; use `null` at boundaries. When
   `chunk_index` is omitted, return a leading preview with navigation fields explicitly `null`
   rather than pretending its potentially multi-chunk/truncated window is an exact cursor.
   If an exact index is outside `0 <= chunk_index < chunk_count`, raise a dedicated internal
   exception that the MCP boundary maps to `chunk_not_found`; preserve the existing empty leading
   preview for a legitimate zero-chunk record.
5. Add `has_math` to MCP search and passage responses. Centralize a normalized warning mapping in
   `mcp_contract.py`: emit `identity_unverified` unless `identity_status` is one of `verified`,
   `manual_accepted`, or `fulltext_verified`; emit `attachment_match_unverified` unless
   `classification == "mapped_verified"`; and emit `math_extraction_may_be_lossy` when
   `has_math` is true and `extraction_tool != "marker"`. Treat unknown future values
   conservatively as unverified/lossy, define every trigger in the data dictionary, and retain the
   underlying provenance fields.
6. Validate that exactly one of `parent_key` and `attachment_key` is supplied at both the MCP
   boundary and the FTS function boundary. Return `invalid_context_key` for neither or both at
   MCP level; update internal callers/tests to pass one key.
7. Add regression fixtures for body-only, title-only, creator-only, citation-key-only, and
   cross-field `all_terms` matches; marker collisions in scholarly text; locator hash changes
   after reconversion/rebuild; untruncated and `max_chars`-truncated exact locators; custom stored
   chunks larger than the MCP retrieval maximum; first/middle/last chunk navigation;
   leading-preview semantics; out-of-range exact chunks and zero-chunk records; every
   known/unknown warning state; and both/neither context keys.
8. Update the public tool contract and Unreleased changelog. Include an example workflow that
   retrieves the `source_locator.chunk_index` from a search result and attributes the result with
   bibliography plus locator, without presenting internal attachment keys as the bibliography.
   Adding `markdown_sha256` and `matched_fields` to the internal `SearchResult` also adds them to
   `search-fts --json` via `SearchResult.to_dict()`; accept and document this as an intentional
   additive CLI contract change, and cover it in `tests/test_cli.py`.

**Depends on:** Phase 1, so the descriptions introduced there can be updated alongside the final
response fields without conflicting default-tool assumptions.

**Acceptance criteria:**

- A title-only match reports `matched_fields=["title"]` and is never described as a body match.
- Search and untruncated exact-passage results for the same stored chunk return the same
  content-bound locator. Truncated retrievals set `truncated=true`, retain the stored chunk span,
  and report the smaller returned span without claiming locator equality.
- Reconversion, or an index rebuild after replacing converted Markdown, changes
  `content_sha256` whenever the resulting Markdown content changes and preserves it when the
  content is byte-identical. Comparing the new hash with a previously returned locator therefore
  detects content changes rather than merely detecting that an operation ran.
- Exact chunk retrieval exposes correct bounded navigation; leading preview behavior is explicit.
- Reliability warnings are deterministic and documented.
- Supplying zero or two item-context keys returns the dedicated public error.
- An out-of-range exact chunk returns `chunk_not_found`; a zero-chunk record still supports an
  empty leading preview.

### Phase 3: Protocol-Native Schemas and Errors

**Goal**: Let MCP clients discover precise success shapes and reliably distinguish failed tool
calls from successful data without exposing diagnostics.

**Files to create:**

- `tests/test_mcp_protocol.py` — real SDK-level inspection/call tests for tool schemas,
  annotations, structured results, and `isError` behavior.

**Files to modify:**

- `src/zotero_pdf_text/mcp_contract.py`
- `tests/test_mcp_server.py`
- `README.md`
- `docs/data-dictionary.md`
- `CHANGELOG.md`

**Steps:**

1. Define stdlib `TypedDict` success models for provenance, source locators, warnings, search,
   passage, context, BibTeX export, and reconversion. Give each registered tool its concrete
   success return annotation instead of `dict[str, object]`, allowing FastMCP 1.28 to advertise a
   useful `outputSchema` without making Pydantic/MCP a top-level dependency of non-MCP code.
2. Refactor `_public_call` into a boundary that returns only typed success data. Map expected
   failures to `PublicMcpError` whose string representation contains a stable machine-readable
   public code plus its safe message, then let the FastMCP tool layer produce a tool error
   (`isError=true`). Catch and redact every unexpected exception before it reaches FastMCP, since
   FastMCP 1.28 wraps the exception string into `ToolError` content. Never include exception reprs,
   database/config paths, endpoints, or tracebacks.
3. Before changing all tools, prove the chosen FastMCP 1.28 error path in one SDK-level test:
   inspect `list_tools()` for `outputSchema`, call a tool successfully for structured content,
   and invoke the low-level registered protocol handler for invalid input to observe the actual
   `CallToolResult.isError=true`. Do not use `FastMCP.call_tool()` as the assertion surface because
   it returns converted tool content rather than the protocol `CallToolResult`. If the SDK prefixes
   tool-error text, assert the public code remains unambiguous rather than parsing an exact whole
   string.
4. Convert the remaining tools only after that proof passes. Keep startup errors in their existing
   structured stderr JSON contract; they are process-start failures, not MCP tool results.
5. Retain lightweight fake-server unit tests for registration and internal serialization, but add
   real FastMCP tests so annotations, generated schemas, structured content, and tool-error flags
   cannot regress behind the fake decorator.
6. Document the success/error envelopes and note that error responses intentionally do not carry
   success structured content. Disabled optional capabilities are verified as absent through
   `list_tools` and therefore use the SDK's unknown-tool behavior if a client nevertheless calls
   them; project-defined error codes apply only after a tool is registered. Add the compatibility-
   tightening entry to the Unreleased changelog.

**Depends on:** Phase 2, because output models should be introduced once the final evidence fields
are known rather than rewritten twice.

**Acceptance criteria:**

- `list_tools` exposes non-generic output schemas for every enabled tool.
- Successful SDK-level calls provide structured content conforming to those schemas.
- Invalid input, unavailable index, missing attachment, enabled-but-unavailable integration, rate
  limit, and reconversion failure produce `isError=true` with stable public codes and no paths or
  tracebacks. Disabled BibTeX/reconversion capabilities are absent from `list_tools`; the plan does
  not promise a project-defined code for calling an unregistered tool.
- Existing startup error JSON remains unchanged.
- Package import and non-MCP CLI tests still pass without eagerly importing the MCP runtime.

## Verification & Validation

- **Focused automated checks per phase**:
  - Phase 1: `uv run pytest tests/test_mcp_server.py tests/test_cli.py tests/test_packaging.py -q`
  - Phase 2: `uv run pytest tests/test_fts.py tests/test_mcp_server.py tests/test_math_ocr.py -q`
  - Phase 3: `uv run pytest tests/test_mcp_protocol.py tests/test_mcp_server.py -q`
- **Full automated check**: `uv run pytest -q` under the locked `mcp` and `test` extras. Confirm the
  existing Windows/macOS/Linux CI matrix remains green.
- **Schema inspection**: use the real FastMCP server object or MCP Inspector to list tools and
  inspect instructions, descriptions, annotations, input schemas, and output schemas for default,
  BibTeX-enabled, reconvert-enabled, and both-enabled configurations.
- **Manual local smoke test**: against a real non-committed `--db`/`--config`, search for a body
  term and a title-only term, retrieve the returned exact chunk, inspect context/warnings, traverse
  one adjacent chunk, and confirm no local paths appear. Do not invoke reconversion during smoke
  testing unless the user separately approves rewriting a disposable converted attachment.
- **Registration smoke test**: run `install-mcp` with no optional flags and with each opt-in flag;
  inspect the printed Claude/Codex registration and verify the resulting tool lists.
- **Failure smoke test**: call with invalid query, invalid context-key combination, unknown
  attachment, and unavailable DB; verify client-visible tool errors retain codes and redact paths.

## Dependencies

- Phase 1 changes the MCP extra to the tested v1 range `mcp>=1.28,<2` and refreshes `uv.lock`.
- Phase 2 depends on Phase 1; Phase 3 depends on Phase 2.
- No new runtime service, database schema migration, vector store, or Zotero permission is needed.
- MCP v2 migration is explicitly deferred to a separate plan after its stable release and a
  compatibility review.

## Notes

- This is a focused follow-up to `plan-mcp-server-hardening.md`, not a revival of its deferred
  managed-generation packages. It deliberately revises the earlier choice to expose reconversion
  by default because a documented literal is a capability check, not user consent.
- `content_sha256` detects that a locator belongs to different converted text; it does not prove
  which index generation produced the result. Documentation must preserve that distinction.
- The repository has no active TODO-file convention available at the referenced skill path and no
  project TODO file. Deferred generation IDs, page mapping, semantic search, and MCP v2 are
  therefore recorded in this plan's Out of Scope section rather than creating a new TODO artifact.
- No source code or runtime configuration is changed by creating this plan.
- Phase 1 implementation review extended the file list with `math_ocr.py` and its tests. It stages
  Marker output and image assets, preflights the sidecar/key, and rolls derived assets back on an
  ordinary commit failure so the newly opt-in mutation does not report failure after a partial
  write.
- Phase 2 preserves body-first representative-chunk selection while deriving `matched_fields` by
  comparing FTS5-highlighted values with their originals, so marker-like source content cannot
  create false matches. Search and passage locators now bind to converted Markdown content and
  exact retrieval distinguishes returned spans from stored spans; leading previews remain
  explicitly non-cursor responses. Reliability warnings are conservative for unknown provenance
  values, and both MCP and FTS boundaries require exactly one context key.
- Phase 2 verification: `197 passed, 5 subtests passed` with the repository virtual environment.
  No real local config was present beyond `config.example.json`, so the optional live-library
  smoke test was not run and no researcher data was accessed.

## Review Feedback

Plan reviewed in 3 iterations by an independent architecture reviewer. The first review found one
blocker, five warnings, and one suggestion; all were addressed in the plan:

- Tool-error handling now requires the public code in the exception string, redacts unexpected
  errors before FastMCP sees them, and tests the low-level protocol result rather than
  `FastMCP.call_tool()`.
- Phase 2 now covers the additive `search-fts --json` DTO change in `tests/test_cli.py`.
- Read-only tools omit the semantically irrelevant idempotence hint, and BibTeX is explicitly
  closed-world because its endpoint is constrained to the local Zotero loopback port.
- Reliability warning triggers and conservative unknown-value behavior are explicit.
- Exact out-of-range chunks have a `chunk_not_found` contract while zero-chunk leading previews
  remain valid.
- Locator refresh language now requires reconversion or rebuilding after Markdown replacement.
- Matched-field detection compares FTS-highlighted values to originals using the explicit column
  mapping, avoiding false positives from sentinel-like source content.

Iteration 2 found two blockers and two warnings, all addressed:

- Exact passage responses now distinguish returned spans from stored chunk spans, expose a
  `truncated` flag, and require locator equality only when retrieval is untruncated.
- Disabled optional tools are tested through `list_tools`; stable project codes are required only
  for registered tools.
- Enabling reconversion now requires an explicit valid config at startup.
- The compatibility requirement now preserves existing CLI fields while allowing the documented
  additive JSON fields.

Iteration 3 found no blockers and two warnings, both addressed in the final plan:

- Hash behavior is now content-based: an operation changes `content_sha256` only when its resulting
  Markdown bytes change.
- Leading previews use `null` stored-chunk spans and define `truncated` against `total_chars`, with
  the nullability included in schemas and tests.
