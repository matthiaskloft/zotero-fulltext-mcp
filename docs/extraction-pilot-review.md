# E2 pilot: selecting pages and reviewing drafts

Maintainer guide for the E2 reviewed baseline ([#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115),
[plan](extraction-quality-plan.md)). The tooling here selects documents and prepares draft
proposals. It writes **no labels**: nothing it produces is ground truth until a person reviews it.

## What is committed

| File | Role |
| --- | --- |
| `benchmarks/extraction/pilot_manifest.json` | Pilot and held-out documents per stratum, candidate pages, licence exclusions, page-check rejections, PDF SHA-256 pins |
| `benchmarks/extraction/pilot_manifest.sha256` | SHA-256 of the manifest (line endings normalized) |
| `tools/build_pilot_manifest.py` | Regenerate (`--write --cache <dir>`) or verify (`--check`, no PDFs needed) the manifest |
| `tools/draft_pilot_pages.py` | Opt-in host step: render the manifest's candidate pages from cached PDFs and write drafts |
| `benchmarks/extraction/pilot_records.py` | `load_pilot_expectation`: the load path for reviewed pilot records |

The selection is layout-first (two-column, tables, equations, scans, non-English, references are
filled before fields are spread), seeded (`seed` in the manifest) and independent of input order.
The pilot targets 20 pages from the strata plus 3 whole papers; the held-out set is disjoint from
the pilot (by id, PDF hash, DOI and normalized title) and must not be used for tuning, calibration
or the design of injected defects. Candidates are ranked by seed, and each is accepted only if the
page-level check below supports it; a failed candidate is listed under `page_check_rejected` and the
next one by rank takes its place. The manifest records the confirmed `candidate_pages` of each pilot
document, so page choices are pinned. If a stratum cannot deliver its pages, `unfilled` states
exactly how many are short and the build exits non-zero. `--check` replays the recorded page
checks, so the committed manifest verifies without PDFs; changing selection fields, the seed or the
quotas changes the manifest and its pin, a visible diff, and the test suite fails until both files
are regenerated and committed together. To regenerate: put the full corpus in the PDF cache and
run `python tools/build_pilot_manifest.py --write --cache <dir>`.

Licence gate: only exact allowlisted forms pass (CC BY any version, CC0, public domain, optional
parenthetical note, case-insensitive). Anything else, or a note mentioning NC, ND, SA or publisher
terms, is listed under `excluded` with its reason and never selected; it needs a maintainer
decision to re-enter. `johd-conlit-dataset` is excluded because its licence note mentions an NC
dataset.

## Known limits of the selection

`sources.json` has no page-level layout. Only `styles` and `language` are inspection metadata.
Each selected document is marked `basis: metadata` (a verified document-level property) or
`basis: assumed` (inferred from its `field`, unverified). Candidate pages passed crude heuristics in
`benchmarks/extraction/page_checks.py`; they are candidates that need a human look, not confirmed
layouts. The SHA-256 of that file is stored in the manifest, so changing a heuristic fails `--check`
until the manifest is rebuilt. The checks:

- **tables:** a printed table caption at the start of a text block ("Table 2.", "Tabelle 3:"; a
  cross-reference such as "Table 3 shows" is not a caption) on a page that is not mostly image. This
  is deliberately independent of any grid detector, so it is not biased toward the tables an
  extractor can already find. Ruled versus unruled geometry (a full rule grid, 3+ horizontal and 3+ vertical rules read from the
  page's drawing commands; booktabs-style rules and white-space tables count as unruled) is reported per page as `page_geometry`, and selection prefers
  to include both kinds in the pilot and in the held-out set. A caption does not prove the page holds
  a table (a caption over a figure, or a table that continues on the next page, can slip through).
- **equations:** at least two display-math rows (short rows with operators or math symbols and few
  words, numbered at the right or set apart by white space and indented), so pseudocode and inline
  math in prose do not count. Private font encodings that hide math glyphs are missed, so some
  equation-heavy papers drop out.
- **two-column, scans, non-English, references:** column blocks, a full-page image, text length, and
  pages from the last reference-list heading on with year or numbered lines.

Pages of rotated documents are judged as displayed; rotated pages with a CropBox different from the
MediaBox are refused. Whole papers are accepted only if a reference-list heading was found. Held-out
documents get one seeded page number each, pinned in the manifest (no text, no images), and the
loader enforces that a held-out record stays within it.

## Preparing drafts (host, opt-in)

```console
uv run python tools/fetch_public_pdf_corpus.py --id <id>      # explicit, as before
uv run python tools/draft_pilot_pages.py --out <dir outside every git checkout>
uv run python tools/draft_pilot_pages.py --held-out-check     # report-only counts; writes nothing
```

The tool never downloads and never touches a Zotero library. A PDF that is missing from the cache
or does not match its pinned hash is reported and the run exits non-zero. It verifies the manifest
and its pin first, refuses an unknown `--id`, refuses `--out` inside any git checkout (page images
and extracted text are copyrighted), will not overwrite an existing draft (it may hold your review
edits; `--overwrite` replaces it) and writes a new timestamped `selection_report-*.json` per run.
Pages come from the manifest; the tool re-runs the page check on them and flags any that no longer
pass.

Output: `images/<id>-pNNNN.png` (150 dpi) and `drafts/pilot-<id>.expectation.draft.json`. Each page
records renderer, version, dpi and image SHA-256. Geometry is `frame: displayed` (rotation removed,
as pymupdf4llm does), `box: cropbox`, with `rotation` read from a separately opened copy of the PDF.
By default a draft has no blocks, so nothing anchors you. `--with-text-proposals` adds the PDF's own
text-layer blocks (type `other`, high uncertainty) as a convenience.

## Reviewing a draft

A draft is an `expectation` record with `provenance.review.status: "draft"` and `proposed_by`
naming the tool. Judge the page image, not any proposal:

1. Open the page image next to the draft. Correct block boundaries, `type`, `reading_order`
   (document-wide, unique) and `bbox` (page points, top-left origin, within the page).
2. Set each block's exact `text` as printed (signs, decimals, operators, case). Add `text_sha256`
   (`extraction.sha256_text`) to every block with text. Tables need every grid cell (empty cells as
   `""`), equations a `latex` value or explicit `null`, figures a `graphic` payload; link captions
   with `caption_for` and cross-page content with `continues_from`.
3. Mark `body_start` / `body_end` anchors per page, and set `printed_label` where a label is printed.
4. For reference pages add `references.entries` (raw as printed, `raw_sha256`, printed DOI status;
   `absent_in_source` when none is printed) and citation `markers`.
5. Record real doubt as `uncertainty` on the block or entry rather than guessing.

## Becoming `reviewed`

Edit `provenance.review` to `status: "reviewed"` with `reviewer` (a pseudonymous id, not a name or
address, and not the proposing tool), `reviewed_at` (UTC timestamp) and `basis`
(`page_image_inspection` or `pdf_inspection`; extractor output is not a basis). Remove the draft's
placeholder uncertainty note and any text-proposal notes, keep `proposed_by`, and keep `render.image_sha256` on every page.
Then confirm the pilot loader accepts it (it wraps `load_reviewed_expectation`):

```console
uv run python -c "import sys, json, pathlib; sys.path.insert(0, 'benchmarks'); from extraction.pilot_records import load_pilot_expectation as l; l(sys.argv[1], json.load(open('benchmarks/extraction/pilot_manifest.json')), pathlib.Path(sys.argv[2]))" <file> benchmarks/public_pdfs/.cache
```

Pass the PDF cache (as above) whenever you can; without it the hash checks cannot run.
`load_pilot_expectation` additionally rejects a record that still carries the draft wording or a
text-proposal note, and a record with no blocks or only `other` blocks. There is one narrow
exception for a genuinely empty scan page: `provenance.uncertainty.note` starting with
`reviewed: scoped pages contain no content blocks`, with no blocks at all, accepted only for a
scan-stratum record, only with the cache, and only if the scoped pages have no text layer (it cannot
see whether the image is blank, so that remains your attestation). It is refused for every other
stratum and for whole papers. The loader also rejects a document hash, page count or scope that
differs from the manifest's pinned pages, and, with the cache, a PDF whose hash differs, page
geometry (size, rotation) that differs from the PDF, a render hash that does not reproduce, and a
render that cannot be reproduced here at all (another renderer or PyMuPDF version is reported as a
finding, not skipped).

## Where reviewed records go

Reviewed pilot records, with the `.draft` marker dropped from the name, go to
`benchmarks/extraction/expectations/pilot/<record_id>.expectation.json`. They contain expected
text, so only documents that passed the licence gate (every document in the manifest does) may be
committed, and only after review. Held-out records go to `.../expectations/held_out/` and are
labelled only after the pilot and scorer are stable. Drafts, page images and the selection report
stay out of the repository.

## Status of the committed pilot labels (agent review)

The records under `benchmarks/extraction/expectations/pilot/` were reviewed by an AI agent
(`reviewer: agent-claude-sonnet-5-5`, Claude Sonnet 5.5), **not by a human**; the maintainer chose
agent review for this pass. Any baseline reported against them must say so. The agent started from
the tool's text-layer proposals, compared every page against its rendered image and corrected
wrong glyphs (for example dropped ligatures, minus signs, relational operators and Greek letters
that the text layer mangles), but a language model reading 150 dpi images can miss errors, and
`load_pilot_expectation` cannot detect them.

Covered: the 20 pilot stratum pages (13 documents) and two of the three whole papers
(`natcomm-metascape`, `ap-high-q-nanocavity`). **Not labelled:** the whole paper
`jlmr-onet-content-model`; its four appendix pages are raster images of survey items and it holds
about 240 table rows, so it was left out rather than done hastily. Held-out documents are untouched.

Conventions the agent applied (decide once, state plainly):

- Text is whitespace-normalised by the scorer, so line breaks are joined with a space; ligatures
  are spelled out (`fi`, `fl`).
- A hyphen that exists only because a word was split at a line break is layout, not transcription,
  and is not recorded: the word is joined (`criti- cal` is `critical`, `rectan- gular` is
  `rectangular`, `modi- fication` is `modification`). Real hyphens are kept, including real
  hyphens that happen to fall at a line end (`high-` at the end of a line before `quality` is
  `high-quality`; `Q-factor`, `two-dimensional`, `bound-state-in-the-continuum`). A suspended
  hyphen in a coordination keeps its space (`single- and multiple-list`, `Meta- and Orthogonal`,
  `pre- and post-study`). Each break was checked against the PDF text layer (line numbers of the
  words) or, on pages with no text layer, against the page image; soft versus real was decided from
  the same word elsewhere in the document, the reference title in the record's own `fields`, and
  otherwise from standard spelling. Joined and kept pairs are enforced by
  `tests/test_extraction_pilot_labels.py`, which rejects any other `letter- lowercase` sequence.
  Residual doubt remains for compounds that appear only once, at a line end (for example
  `photo-induced`, `multi-platform`, `over-represented`).
- Sub- and superscripts are recorded as plain characters in reading order (`m2p2`, `k2x`), without
  markup, and font variants (bold, calligraphic, blackboard) as the plain letter. Paragraphs with
  such inline math carry `uncertainty: medium`; inline stacked fractions and products with no
  unambiguous linear reading carry `high`.
- Text that exists only inside figure images or vector figure labels is not recorded; figures are
  `figure` blocks with `text: null`. Decorative dotted leaders, logos and rules are not labelled
  (the PLoS dotted leaders are `figure`/`decoration` blocks), so an extractor that emits them is
  charged for invented text.
- Printed e-mail addresses are withheld (the block is `text: null`, `high` uncertainty), because the
  repository personal-data guard rejects e-mail addresses; this removes text-precision scoring on
  those four pages.
- Citation markers are not recorded (`markers: []`); the scorer does not score them. Reference
  fields hold authors, title, year and venue; titles broken across lines were re-joined using a
  word list, which can mis-resolve a hyphenated compound.
- A page-scoped record marks `truncated` only where a sentence visibly continues across the scope
  boundary.

Pages with real doubt (also marked in the records): `naca-lifting-surfaces-scan` pages 5 and 7 are
typewritten scans (one word and one equation number are smudged; the inline fraction on page 7 has
no linear reading); `stam-iron-pnictides` pages 6 and 8 are about 600 table cells of subscripted
formulas with no text layer (sign glyph in subscripts, one middle dot); `gaussian-coresets` and
`pmet-srmr-covariates` carry flattened inline math.
