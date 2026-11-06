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
`basis: assumed` (inferred from its `field`, unverified). Candidate pages passed crude heuristics
(column blocks, a ruled grid of at least 3 x 3 cells with text in over a third of them and little
image area, math characters, a full-page image, text length, reference-list pages from the last
heading on, year or numbered lines). They are candidates that need a human look, not confirmed
layouts. Known blind spots: tables without ruling lines are not detected, so such papers drop out of
the tables stratum; equations set in private font encodings are missed. Pages of rotated documents
are judged as displayed, and rotated pages with a CropBox different from the MediaBox are refused.
Whole papers are accepted only if a reference-list heading was found; the draft report says
whether one was found.

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
uv run python -c "import sys, json; sys.path.insert(0, 'benchmarks'); from extraction.pilot_records import load_pilot_expectation as l; l(sys.argv[1], json.load(open('benchmarks/extraction/pilot_manifest.json')))" <file>
```

`load_pilot_expectation` additionally rejects a record that still carries the draft wording, that
has no blocks or only `other` blocks (unless `provenance.uncertainty.note` starts with the explicit
attestation `reviewed: scoped pages contain no content blocks`, for genuinely empty pages), whose
document hash, page count or scope differs from the manifest, and, given the PDF cache, whose PDF
or render hashes do not reproduce.

Changing the status alone fails: the schema demands reviewer, timestamp, basis and the hashes above.
The status is still an attestation by the reviewer, not something the schema can prove.

## Where reviewed records go

Reviewed pilot records, with the `.draft` marker dropped from the name, go to
`benchmarks/extraction/expectations/pilot/<record_id>.expectation.json`. They contain expected
text, so only documents that passed the licence gate (every document in the manifest does) may be
committed, and only after review. Held-out records go to `.../expectations/held_out/` and are
labelled only after the pilot and scorer are stable. Drafts, page images and the selection report
stay out of the repository.
