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
olmOCR-bench was installed in an isolated environment and run once through the exporter in
`benchmarks/extraction/olmocr_export.py`; see [olmocr-bench-feasibility.md](olmocr-bench-feasibility.md)
for the pin, commands, results and limits. OmniDocBench has not been installed or validated.

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
`load_reviewed_expectation`, which refuses drafts. The olmOCR adapter
([olmocr-bench-feasibility.md](olmocr-bench-feasibility.md)) and the pilot selection/draft tooling
([extraction-pilot-review.md](extraction-pilot-review.md)) now exist.

## Extraction scorer (E1, package B)

`benchmarks/extraction/scorer.py` (`score_records`, `score_files`; benchmark-side, no new
dependency) compares a reviewed expectation with an observation. It refuses a mismatched
`pdf_sha256`, an unreviewed expectation and wrongly typed records before scoring anything.

**Strict semantics (maintainer decision): transcription is separate from layout.** Strict
transcription compares exact characters: signs, decimal points, operators and case must survive,
tables must match cell by cell at the same position, pages must agree. It treats line wraps and
runs of whitespace as equal and ignores block boundaries (text is searched in the page's
concatenated observed text). Block segmentation is therefore its own reported metric
(`text_segmentation`: does one observed block hold exactly the expected text), and the byte-exact
UTF-8 hash check (`text_span_hash`) is a separate metric. Neither decides the verdict, and unscored
items of those two metrics do not make it incomplete.

**Markdown markup (extension of the "heading markup" decision).** Real extractor output such as
`## **Alpha Heading**`, `- first item` or `The **effect** is` must not fail strict. A narrow
stripping step therefore runs on *observed block text only*: a heading marker at the start of the
block, bullet markers (`-`, `*`, `+`, bullet) when the block's first line is a list item, and paired
`**`/`__` emphasis. The emphasis markers must not touch a word character on the outside and must
hug non-space text inside, so `a * b`, `a ** b` and `x**2 + y**2` are untouched; single `*`/`_`
(italics) and ordered-list numbers (`1. Introduction` is printed text) are kept. Expected text,
table cells, LaTeX and raw references are never stripped, which means an expected text that itself
begins with `#` or a bullet cannot match (record such a block as uncertain). The `tolerant` mode
additionally applies NFKC and case folding (DOIs lose `doi:`/URL prefixes, LaTeX loses whitespace,
table header flags are ignored); it is reported as separate metrics, a check that only tolerant
accepts is flagged `tolerant_only` and counted, and a normalized match is never evidence of
transcription fidelity.

**Verdict.** Tri-state, from strict deciding checks only: `failed` if any fails; otherwise
`incomplete` if any expected item or family went unscored or no strict check ran; otherwise
`passed`. Every unscored item is listed in `not_scored` with a reason: truncated at the scope
boundary, high uncertainty, an explicit `text: null`, not comparable, an entry not segmented in that
mode, citation markers (listed when the expectation has any: marker links are not scored), spans
without Markdown text. Deliberate exemptions: DOI status `not_assessed` and library resolution are
not scored and not listed. `strict_passed` is true only for `passed`, and a missing `references`
section counts as an empty list wherever the expectation has references.

- **Text.** Presence per body block (fragment) on its own physical page, in reading order. Every
  occurrence in the observed text is consumed by one expected fragment and a match may not start or
  end inside a word, so a duplicated expected paragraph needs two observed ones and `5` is not found
  in `51`. Reading order searches forward from the previous match, so repeated text and interleaved
  chains do not cause false failures; a block found only before its predecessor is displaced and
  fails, while a block that is not found at all is listed, not charged again as an order failure.
  `text_precision` measures invented content in *every* observed block that has text: text no
  expected text accounts for (each expected text, including raw reference entries, covers one
  word-bounded occurrence), so extra copies, joined words and appended digits fail. Reviewed
  expectations must therefore record every printed header, footer and page number on in-scope pages;
  there is no scorer exemption. Coverage is allocated whole-block matches first, then inside one
  block, then across blocks, so a cross-block coincidence cannot steal a real block's text. The
  optional `text` of an observed table or equation block whose
  structured payload matched is accounted for only if it serializes that payload (an equation's LaTeX
  ignoring whitespace; a table's cell texts, each once, with Markdown table markup tolerated); any
  other text on it, and the same text on an unmatched block, is invented.
  Reference-entry blocks that no entry points at are compared like body text, so an expectation that
  records the bibliography as blocks only still fails when it is lost. Pages with truncated or
  uncertain expected text are not assessed.
- **Tables.** Cells at the same grid position with the same spans and text (`table_cell_match`),
  the whole grid (`table_exact`), the relative order of matched tables (`table_order`), and
  `table_precision` for observed tables no expected table accounts for. Observed table or equation
  blocks without their structured payload stay in that inventory: they fail precision unless their
  text is accounted for elsewhere, and are listed as not scored (verdict incomplete) when the
  expected items of that kind on their page cannot be compared. Expected and observed tables
  are paired by an optimal assignment (cell matches first, an identical grid as the tie-break), solved
  exhaustively per group of tables that share a page; the header flag is compared in strict mode
  only.
- **Equations, captions, figures.** LaTeX, printed number and display flag, each observed equation
  used once, plus `equation_precision` and `caption_precision` for extras. A caption needs exact text,
  the same page and a link to the corresponding target: tables use the table pairing, so an extra
  earlier table cannot break the link; other targets need the same type and an overlapping page, and
  the same position when both records hold the same number of them. Captions that span pages are not
  compared. `figure_match` is presence only: an observed figure block on the expected figure's page.
- **References.** Segmentation recall/precision (raw text and start page; an observed `end_page`
  must agree), field accuracy (title, authors, year, `year_raw`, venue), DOI recovery, and
  `reference_doi_absent_correct` for entries whose source prints no DOI (a missing DOI is correct, an
  invented one fails). `reference_ordinal` compares the *printed list position*: exactly in a
  complete-document record, and in a page-scoped record up to one constant observed-minus-expected
  offset (the most common one), because such a record may number from a different start. Observed
  entries that match a truncated or uncertain expected entry leave the precision denominator, and so
  do unmatched observed entries on that entry's pages (listed in `not_scored`: they may be the same
  entry with different text). Field and DOI checks cover an entry only in the modes where it was
  segmented, and each other (entry, mode) is listed in `not_scored`, so those metrics never describe
  only the survivors without saying so.
- **Cross-page chains.** Blocks linked by `continues_from` (also same-page continuations) are one
  logical item, so a correct merged extraction passes as well as a fragment-per-page one. A split
  fragment must be on its own page; only an observed block that merges it with a chain neighbour may
  sit on another page, including the later page. A merged block is still ordered against the content
  around it: the chain head must come after the preceding expected block, and later fragments may sit
  before the cursor (for example behind an interleaved footnote) only when their predecessor was
  placed in the same block. A table chain is compared as the fragments' rows
  concatenated in chain order, after dropping a header row repeated on a continuation page (exact
  match with the first fragment's header, so keeping it is a strict failure). Expected items marked
  `truncated`, and chains with an unscored fragment, are not compared.
- **Markdown-only observations.** Missing bounding boxes and renders are fine; an entry with no page
  can match only in tolerant mode, so it cannot pass strict. With `markdown=` / `markdown_path=` the
  observation's `output_span` hashes are verified (`output_span_roundtrip`, strict only); the file is
  read byte-exactly so CRLF is preserved.
- **Output.** `score["metrics"]` entries (`name`, `version`, `mode`, `value`, `n`) fit the run
  schema; `METRIC_VERSIONS` pins them (scorer version 3). `score_to_result` copies the verdict and
  the count of verdict-relevant unscored items into the run result through two optional run-schema
  fields, `verdict` and `unscored_items`, so a partial score cannot read as a pass; `failure_result`
  builds entries for failures, timeouts and missing backends, which carry an error and no metrics.
- **Sensitivity.** `benchmarks/extraction/defects.py` injects missing numeric signs, swapped table
  values, omitted text, reordered columns and swapped reading order. The injectors do not repair
  cross-references (see its docstring). `tests/test_extraction_scorer.py` proves each makes the
  relevant deciding strict check fail on synthetic records, including across page breaks, while the
  clean case passes.

## Pilot selection and draft tooling (E2, no labels)

Selection, draft proposals and the review procedure: [extraction-pilot-review.md](extraction-pilot-review.md).

## Observation exporter (E1/E2, step 3)

`benchmarks/extraction/observe.py` turns the *current production extraction* into a schema-valid
`observation` so the scorer can compare it with reviewed expectations. `observe_pdf(pdf_path, *,
pages=None, options=None)` returns a frozen `ObservationResult(observation, markdown, extractor,
elapsed_s)`; `pages` are 1-based physical pages (None = whole document), `options["image_ocr"]`
(default False) additionally reads crops with the package's local OCR model, and a failing extractor
raises `ExtractionFailed` with the underlying message. Benchmark-side only; no new dependency and no
change to production conversion.

It reuses the converter's own pieces: the same `_extract_markdown` subprocess (pymupdf4llm
`page_chunks`, package header/footer defaults), `_read_page_texts` for the measured per-page lengths,
and, for crop OCR, `image_ocr.plan_crops`/`_run_ocr`/`render_replacement`/`splice` page by page. The
whole document is always extracted and the requested pages are sliced by the page map, so page text
equals a whole-document run. The page of a block is the page whose measured range holds it; a missing
page map is a failure, never a guess. `markdown` is the exact text every block's `output_span` refers
to, so `output_span_roundtrip` passes. The record pins pymupdf4llm/pymupdf versions and the options
(hash in `provenance.produced_by.options_sha256`, OCR model in `model`).

Detected: headings (`#`), paragraphs, list items (one block per item), pipe tables (cells with the
header row flagged; spans unknown), `$$` display equations (LaTeX, `display: true`, no printed number),
captions (label emphasised or followed by `.`/`:`, so "Table 3 reports ..." stays a paragraph) linked by
adjacency to a table or figure, figures (the `![](...)` crop placeholders production always leaves, typed `image`, and pymupdf4llm picture-text regions, no
text so their internals are not scored as prose), a trailing bare number as `page_number`, and
reference entries under a References/Bibliography heading (DOI `present`/`not_found` by pattern,
`raw` verbatim). Not detected: bounding boxes, page `render`, equation numbers, table spans and
cross-page `continues_from`/`truncated`, footnotes, running headers (pymupdf4llm drops them), reference
`fields`, citation markers (the scorer lists them as unscored), and display equations when crop OCR is
off (they stay crop placeholders, typed figure, so `equation_match` fails by design). Known scorer mismatches: pymupdf4llm often leaves the next
element's lead-in on a caption's line, so exact caption text fails; heading text carries `#`/`**`
markup (stripped by the scorer for transcription, so only the informational `text_span_hash` differs).
## Run harness (E1/E2, step 4)

`benchmarks/extraction/run_pilot.py`, run through `tools/run_extraction_pilot.py`, runs one extractor
over the reviewed pilot pages and scores it. Host-only and opt-in; no network, nothing shipped.

```console
uv run python tools/run_extraction_pilot.py --expectations <dir of reviewed records> \
    --out <dir outside every git checkout> [--image-ocr] [--option KEY=VALUE] [--timeout SECONDS] [--split pilot]
```

- **Inputs.** Every `*.json` in `--expectations` goes through `load_pilot_expectation`; a draft or any
  rejected record refuses the whole run, listing every finding. Each PDF in `--cache` is checked
  against the manifest's SHA-256 before it is extracted. `--out` defaults to a directory under the
  user's cache and is refused inside any git checkout (extracted text is copyrighted).
- **Splits.** `--split pilot` is the default and refuses held-out documents. `--split heldout` must be
  given explicitly, prints a warning and records `split: heldout` in the run; the held-out set is for
  measuring final results, never for tuning, threshold calibration or defect design.
- **Extraction.** Expectations are grouped by PDF; each PDF is extracted once for the union of the
  pages its expectations need (`pages=None` when that is the whole document), through an injectable
  callable with the signature of `extraction.observe.observe_pdf` (`pages`, `options`; returns
  `observation`, `markdown`, `extractor`, `elapsed_s`). `--timeout` applies per document and runs the
  extractor in a daemon thread, so a timed-out extraction is abandoned, not killed. Because the
  abandoned thread may still be running, nothing further is extracted after the first timeout: the
  remaining expectations are recorded as `skipped` with error kind `SkippedAfterTimeout`, and the summary
  says so. The library refuses an `out_dir` inside any git checkout. Each document's extractor identity
  goes into `details.json`; a run whose documents disagree is refused (it must describe one
  configuration), and a run where every document failed keeps tool/version `unknown`, noted in the summary.
  The summary states per document whether latency is the extractor's `elapsed_s` or the harness wall clock
  (failures, timeouts).
- **Scoring.** Each expectation is scored with `score_records(..., markdown=...)` so `output_span`
  hashes round-trip. An extractor exception, a timeout or an observation the scorer rejects becomes a
  `failure_result` (`failed` or `timeout`, with an error and no metrics) for every expectation of that
  PDF.
- **Outputs**, under `<out>/run-<UTC time>-<split>/`: `run.json` (validated against the run schema
  before it is written; one configuration, one input per expectation, versioned strict and tolerant
  metrics, verdicts), `summary.md` (per-page verdicts, strict and tolerant aggregates kept apart,
  counts of passed, failed, incomplete and errors, top failing strict checks), `details.json` (PDF,
  expectation, observation and Markdown hashes per input, which the run schema has no field for) and
  the `observations/` and `markdown/` files those hashes point at. `output_sha256` is the hash of the
  observation file as written. `latency_seconds` is the whole-PDF time, shared by that PDF's
  expectations.
