# Markdown and reference extraction quality

Decision record, 2026-11-05. Implementation is planned under E1–E4 in the
[active roadmap](../ROADMAP.md#extraction-and-reference-quality). Existing page maps, heuristic
quality scores and synthetic OCR checks are implemented; the reviewed artifact benchmark is not.

### Internal artifact and evaluation contract

Use a small internal JSON Schema, not evidence-ledger-schema or evidence-ledger-mcp now.
Keep reviewed expectations, automatically proposed observations, and run/results records separate.
Record PDF hash/title/physical page count; page dimensions and render identity; stable block IDs,
types, bounding boxes with explicit units/origin, reading order, meaningful body start/end anchors,
text/table cells, equations, graphics and caption links. Distinguish physical pages from printed
labels. Record annotation tool/model/version, human review, uncertainty and run configurations,
input/output hashes, metric versions, failures/timeouts, latency and resource costs.

Reference expectations use the same envelope: raw entry, ordinal/page, reviewed bibliographic
fields and citation-marker links. Score segmentation, field parsing, printed identifier recovery,
and reference-to-library resolution separately; missing source DOIs are not parsing failures.
Strict checks preserve signs, decimals, operators, case, exact spans and hashes; tolerant checks
are separate. A normalized quotation hash cannot establish scientific transcription fidelity.

Start with 20 diverse public-corpus pages and a few complete papers for cross-page/reference
behavior. Establish expectations by PDF inspection. Include injected missing signs, swapped table
values, omitted text and reordered columns to verify evaluator sensitivity. Keep the existing
synthetic corpus for cheap deterministic CI; real PDFs/annotations follow corpus rights rules.

Evaluate [olmOCR-bench](https://github.com/allenai/olmocr/blob/main/olmocr/bench/README.md)
first in a pinned isolated environment, exporting compatible presence/order/table/math checks
from the internal artifact. Do not duplicate answer keys or adopt its OCR model merely to use its
scorer. Its tolerant page-level checks do not verify exact location, our hashes or chunk coverage;
retain those internally. Consider [OmniDocBench](https://github.com/opendatalab/OmniDocBench#evaluation)
later for richer table/formula metrics; its full formula evaluation adds system dependencies.
Neither evaluator has been installed or validated here, so adapter feasibility is an E1 deliverable.

A cheap visual model may compare page images with corresponding Markdown and propose localized
omission/addition/order/table/equation/caption findings or abstain. Calibrate false passes and
false alarms against human labels and injected defects; pin prompt/model/version and record cost.
Model grades are triage, never ground truth. No model provider is selected and no uploads authorized.

### Reuse and dependency decisions

Benchmark current pymupdf4llm plus existing opt-in crop OCR first. Compare Docling as the first
alternative extractor; OpenDataLoader PDF is a second candidate with a Java runtime and distinct
hybrid path. Keep structured output/provenance alongside Markdown. Selective Unicode repairs must
preserve original mappings; do not add automatic generative rewriting or blanket dehyphenation.
No mandatory RAGFlow service or return of Marker's removed dependency tree. Optional local services
remain separate from ordinary conversion/search and require measured benefit before adoption.

For Markdown structure, evaluate markdown-it-py with built-in tables and selected mdit-py-plugins
math rules. Its block line ranges need verified conversion to original character spans. Use the
chosen embedding model's tokenizer (Hugging Face tokenizers where compatible; tiktoken for matching
encodings), not word counts, for complete input budgets. Tokenization measures size; it does not
require independent lexical and embedding source boundaries. Tree-sitter is a later option if
finer ranges justify byte-to-character mapping and packaging complexity.

Manuscript Verification System already supplies GROBID/TEI integration, raw-reference preservation,
citation-marker links, deterministic fallback ideas and separate parsing/DOI metrics. Inspect its
current provenance/license constraints before code reuse; do not copy private manuscripts, results
or paths here. Its recorded limited-domain comparison favored GROBID over the tested ensemble,
not a universal superiority claim over the deterministic parser. Prefer an explicitly configured
GROBID service on the embedding machine, cache by PDF hash plus service/model/options identity,
and retain engine/error provenance. Deterministic fallback remains a candidate, not an ensemble
requirement. GROBID TEI paragraph offsets are not Markdown offsets: align and verify before making
passage locators; ambiguous links remain unresolved. Core FTS works without GROBID or embeddings.

### Order and cross-track boundaries

E1 schema/pilot plus E2 reviewed baseline precede extraction-default changes. Compare extractor
settings, engine changes and cleanup separately. Then freeze/pin the resulting Markdown for S1a
boundary/context sweeps. Chunker prototyping can proceed in parallel; promoting production defaults
requires a reviewed extraction baseline and downstream held-out retrieval evidence.

Store authors/year/venue/DOI as exact metadata/filter fields. Paper embeddings start from
title/abstract/keywords; passage title/heading prefixes are ablations. Reference links belong to
S7's explicit graph, not wholesale bibliography prefixes on every passage. Distinguish shared
authorship, shared references (bibliographic coupling), and being cited together (co-citation);
future graph-assisted ranking must be separately measured and return verified source links.

## Artifact schema contract (E1, package A)

Implemented under `benchmarks/extraction/` (benchmark-side only: not in the wheel, no CLI command;
`jsonschema` is a `test` extra). Three JSON Schemas (draft 2020-12, `schema_version: 1`) in
`benchmarks/extraction/schemas/` share definitions from `common.schema.json`. A change to any
required field bumps `schema_version`; the schemas reject versions they do not know.

| Record kind | Schema | Holds | Authority |
| --- | --- | --- | --- |
| `expectation` | `expectation.schema.json` | Reviewed layout/block and reference ground truth | Ground truth only when `provenance.review.status` is `reviewed` with reviewer, timestamp and basis (`page_image_inspection` or `pdf_inspection`) |
| `observation` | `observation.schema.json` | Same layout/reference shape, produced by an extractor, model or heuristic | Never; carries `produced_by` and no review block |
| `run` | `run.schema.json` | Configurations, inputs, per-result hashes, status, latency/resources, metrics | Holds hashes only, no content or truth |

The kinds are kept structurally apart: `record_kind` is a constant per schema, the `review` block
exists only in expectations, and the DOI vocabularies do not overlap, so an observation never
validates as an expectation by accident. This is not tamper-proof: a person can still hand-edit
`status` to `reviewed`. Review is an attestation by the named reviewer, not something a schema can
prove. Promoting an observation means a human writing a new expectation record.

Reviewed expectations carry extra requirements that drafts and observations do not: a pseudonymous
reviewer id (not the proposing tool or model), a valid `reviewed_at`, a `basis`, a
`render.image_sha256` on every page when the basis is `page_image_inspection`, `text_sha256` on
non-null block text and `raw_sha256` on reference entries, an explicit `text` key (value or null)
on text-bearing blocks, and the table/graphic/equation payload matching the block type (a table
needs at least one cell and must cover its whole grid, spans included, with empty cells as `""`;
an equation states `latex` as a value or explicit null). Expectations also require `bbox` and `render`; observations from
Markdown-only extractors may omit `bbox`, `page.render` and the entry `page`.

Shared envelope (expectation and observation): `document` (PDF SHA-256, title, physical page count),
`scope` (physical pages covered, or `complete_document`), `pages`, `blocks` and optional
`references`.

- **Pages** use 1-based *physical* page numbers; `printed_label` is separate (string or null).
  Each page declares its `geometry` (`unit`: `pt`, `px` or `normalized`; `origin`: `top-left` or
  `bottom-left`; `frame`: `unrotated` or `displayed`; `box`: `cropbox` or `mediabox`; `rotation`:
  the PDF's stored `/Rotate` value 0/90/180/270; width/height), its `render` identity (renderer,
  version, DPI, image hash) and optional `body_start`/`body_end` anchors naming blocks on that page.
  Frames: `unrotated` is the named box before `/Rotate` (PyMuPDF `get_text` on a page whose rotation
  was not removed; its `page.rect` is the rotated box). `displayed` is the page as shown. pymupdf4llm
  yields `displayed` coordinates, because `to_markdown` calls `page.remove_rotation()` and the open
  page then reports rotation 0, so read `rotation`/box geometry from a separately opened document
  or declare `displayed`. `px` is always `displayed` (and then page `render` with `dpi` is required);
  `normalized` requires width = height = 1. Bbox bounds are strict for expectations. Observations are
  kept recordable: they may overshoot the page by up to `BBOX_MARGIN_FRACTION` (half a page
  dimension) and may have inverted corners, so extractor quirks are scorable; only absurd boxes are
  rejected. Observations still need `geometry`, block `page` and marker `page` (page attribution);
  adapters for Markdown-only tools derive it from page maps. Only `bbox`, the page `render` (unless
  `px`) and the entry `page` may be omitted.
- **Blocks** have stable IDs, a type, `page`, a bounding box `{x0,y0,x1,y1}` in the page's
  coordinate system, a document-wide `reading_order`, exact `text` plus optional `text_sha256`
  (UTF-8, unnormalized), and typed payloads: table cells with row/col spans, equations (LaTeX,
  printed number, display flag) and graphics. Caption blocks link with `caption_for`. A block lives
  on one page; content that crosses a page break is a chain of blocks linked by `continues_from`
  (same type, later in reading order, same or later page, one continuation per block; only valid
  links are recorded, so chains are acyclic). Table row indices restart at 0 in each
  fragment, the logical table is the fragments' rows concatenated in chain order, `n_cols` must
  agree, and a repeated header row is marked `is_header`. Observations may add `output_span`
  (Markdown hash plus half-open character offsets and an optional slice hash) per block;
  `verify_output_spans` checks the round trip against the Markdown text.
- **References**: `entries` (ordinal, page, raw text as printed, reviewed `fields`, `doi`) and
  `markers` linking printed citation markers to entry IDs, with `target_status`: `resolved`,
  `no_target` (the list has no matching entry) or `out_of_scope` (target on a page outside a
  page-scoped record). An entry crossing a page break gives its first block, `page` and, when the chain ends on a different page, `end_page` (never
  without `page`). In an expectation the entry's block must be a `reference_entry` chain head used
  by one entry. `truncated` (`start`, `end`, `both`) on blocks and entries records content cut off
  at the boundary of a page-scoped record; it is rejected in `complete_document` records.
  Reviewed fields include `year` and the as-printed `year_raw`. Reference-to-library resolution
  (matching entries to Zotero items) is deliberately deferred: no field exists for it yet and it
  will be added with the scorer. In an
  expectation the DOI status is `present`, `absent_in_source` or `not_assessed`; in an observation
  `present`, `not_found` or `parse_failed`. A source that prints no DOI is therefore never scored as
  a parse failure.
- **Runs**: `metric_versions` pins every metric; each result reports `ok`, `failed`, `timeout`,
  `missing_backend` or `skipped`. Every (configuration, input) pair must have a result; pairs that
  were not run are recorded as `skipped`, never omitted. `ok` requires an output hash; every other
  status requires an error and carries no metrics. Strict and tolerant metrics are separate entries
  (`mode`), each reported at most once per result.

The loader (`benchmarks/extraction/records.py`: `load_record`, `load_reviewed_expectation`,
`validate_record`) adds checks JSON Schema cannot express: unique block IDs and reading order,
caption links resolving to table/figure blocks, page numbers within `page_count` and `scope`,
bounding boxes inside the page geometry (with the tolerance above), continuation chains, table cells inside and not overlapping in the grid, anchors
on their own page, marker and entry links resolving, text/raw/options hash round-trips, and run
cross-references and metric version pins. Scorers should load truth through
`load_reviewed_expectation`, which refuses drafts. No external-benchmark adapter or sampling tool
exists yet.

## Extraction scorer (E1, package B)

`benchmarks/extraction/scorer.py` (`score_records`, `score_files`; benchmark-side, no new
dependency) compares a reviewed expectation with an observation. It refuses a mismatched
`pdf_sha256`, an unreviewed expectation and wrongly typed records before scoring anything.

**Strict semantics (maintainer decision): transcription is separate from layout.** Strict
transcription compares exact characters: signs, decimal points, operators and case must survive,
tables must match cell by cell at the same position, pages must agree. It treats line wraps and
runs of whitespace as equal, ignores block boundaries (text is searched in the page's concatenated
observed text) and ignores Markdown heading markers. Block segmentation is therefore its own
reported metric (`text_segmentation`: does one observed block hold exactly the expected text), and
the byte-exact UTF-8 hash check (`text_span_hash`) is a separate metric. Neither decides the
verdict. The `tolerant` mode additionally applies NFKC and case folding (DOIs lose `doi:`/URL
prefixes, LaTeX loses whitespace, table header flags are ignored); it is reported as separate
metrics, a check that only tolerant accepts is flagged `tolerant_only` and counted, and a
normalized match is never evidence of transcription fidelity.

**Verdict.** Tri-state, from strict deciding checks only: `failed` if any fails; otherwise
`incomplete` if any expected item or family went unscored (every such item is listed in
`not_scored` with a reason: truncated at the scope boundary, high uncertainty, not comparable, an
entry not segmented in that mode, spans without Markdown text) or no strict check ran; otherwise
`passed`. `strict_passed` is true only for `passed`. A missing `references` section counts as an
empty list wherever the expectation has references, so the entries are scored as not recovered.

- **Text.** Presence per body block (fragment) on its own physical page, in reading order. Reading
  order searches forward from the previous match, so repeated text and interleaved chains do not
  cause false failures; a block found only before its predecessor is displaced and fails, while a
  block that is not found at all is listed, not charged again as an order failure. `text_precision`
  measures invented content: observed body text with no counterpart in any expected text (pages
  with truncated or uncertain expected text are not assessed).
- **Tables.** Cells at the same grid position with the same spans and text (`table_cell_match`),
  the whole grid (`table_exact`), and the relative order of matched tables (`table_order`).
  Expected and observed tables are paired by an optimal assignment, not greedily; the header flag
  is compared in strict mode only.
- **Equations and captions.** LaTeX, printed number and display flag, each observed equation used
  once. A caption needs exact text, the same page and a link to the corresponding target: tables use
  the table pairing, so an extra earlier table cannot break the link; other targets need the same
  type and an overlapping page, and the same position when both records hold the same number of
  them. Captions that span pages are not compared.
- **References.** Segmentation recall/precision (raw text and start page; an observed `end_page`
  must agree), printed ordinals, field accuracy (title, authors, year, `year_raw`, venue) and DOI
  recovery, and `reference_doi_absent_correct` for entries whose source prints no DOI (a missing DOI
  is correct, an invented one fails). Observed entries that match a truncated or uncertain expected
  entry leave the precision denominator. Field and DOI checks cover an entry only in the modes where
  it was segmented, and each other (entry, mode) is listed in `not_scored`, so those metrics never
  describe only the survivors without saying so. Citation markers and library resolution are not
  scored.
- **Cross-page chains.** Blocks linked by `continues_from` (also same-page continuations) are one
  logical item, so a correct merged extraction passes as well as a fragment-per-page one. A split
  fragment must be on its own page; only an observed block that merges it with its chain
  predecessor may sit on another page. A table chain is compared as the fragments' rows
  concatenated in chain order, after dropping a header row repeated on a continuation page (exact
  match with the first fragment's header, so keeping it is a strict failure). Expected items marked
  `truncated`, and chains with an unscored fragment, are not compared.
- **Markdown-only observations.** Missing bounding boxes and renders are fine; an entry with no page
  can match only in tolerant mode, so it cannot pass strict. With `markdown=` / `markdown_path=` the
  observation's `output_span` hashes are verified (`output_span_roundtrip`, strict only).
- **Output.** `score["metrics"]` entries (`name`, `version`, `mode`, `value`, `n`) fit the run
  schema; `METRIC_VERSIONS` pins them (scorer version 2). `score_to_result` copies the verdict and
  the unscored count into the run result through two optional run-schema fields, `verdict` and
  `unscored_items`, so a partial score cannot read as a pass; `failure_result` builds entries for
  failures, timeouts and missing backends, which carry an error and no metrics.
- **Sensitivity.** `benchmarks/extraction/defects.py` injects missing numeric signs, swapped table
  values, omitted text, reordered columns and swapped reading order. The injectors do not repair
  cross-references (see its docstring). `tests/test_extraction_scorer.py` proves each makes the
  relevant deciding strict check fail on synthetic records, including across page breaks, while the
  clean case passes.
