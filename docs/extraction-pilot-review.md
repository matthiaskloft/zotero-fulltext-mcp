# E2 pilot: selecting pages and reviewing drafts

Maintainer guide for the E2 reviewed baseline ([#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115),
[plan](extraction-quality-plan.md)). The tooling here selects documents and prepares draft
proposals. It writes **no labels**: nothing it produces is ground truth until a person reviews it.

## What is committed

| File | Role |
| --- | --- |
| `benchmarks/extraction/pilot_manifest.json` | Pilot and held-out documents per stratum, licence exclusions, PDF SHA-256 pins |
| `benchmarks/extraction/pilot_manifest.sha256` | SHA-256 of the manifest (line endings normalized) |
| `tools/build_pilot_manifest.py` | Regenerate (`--write`) or verify (`--check`) the manifest; metadata only, no PDFs read |
| `tools/draft_pilot_pages.py` | Opt-in host step: render pages from cached PDFs and write draft proposals |

The selection is layout-first (two-column, tables, equations, scans, non-English, references are
filled before fields are spread), seeded (`seed` in the manifest) and independent of input order.
The pilot targets 20 pages from 16 documents plus 3 whole papers; the held-out set is 11 documents
plus 2 whole papers, disjoint from the pilot, and must not be used for tuning, calibration or the
design of injected defects. Changing `sources.json` selection fields, the seed or the quotas changes
the manifest and its pin, so the change is a visible diff; the test suite and `--check` fail until
both files are regenerated and committed together.

Licence gate: an entry's expected text may be committed only if its licence is CC BY (any version),
CC0 or public domain. Anything mentioning NC, ND, SA or publisher terms, or with no verifiable
licence, is listed under `excluded` with its reason and never selected. Excluded entries need a
maintainer decision before they can re-enter.

## Known limits of the selection

`sources.json` has no page-level layout. Only `styles` and `language` are inspection metadata, and
nearly every entry says just "native text". Each selected document is marked `basis: metadata`
(a verified document-level property) or `basis: assumed` (inferred from its `field`, unverified).
The host step checks each assumed stratum page by page with crude heuristics and reports a
selection it cannot support (`stratum_unsupported` / `stratum_partial`) instead of picking a page
anyway. If an assumption fails, change the entry or the assumption table in
`benchmarks/extraction/pilot.py`, regenerate and review the manifest diff.

## Preparing drafts (host, opt-in)

```console
uv run python tools/fetch_public_pdf_corpus.py --id <id>      # explicit, as before
uv run python tools/draft_pilot_pages.py --out <dir outside the repo>
```

The tool never downloads and never touches a Zotero library. A PDF that is missing from the cache
or does not match its pinned hash is reported in `selection_report.json` and on the console, and
the run exits non-zero. `--out` must be outside the repository (the default is a cache directory
under your home directory): page images and extracted text are copyrighted and must never be
committed. Chosen pages are decided from the seed plus the cached PDFs; the report lists them per
document, so you can commit that list as a follow-up if you want the page choice pinned.

Output: `images/<id>-pNNNN.png` (rendered at 150 dpi), `drafts/pilot-<id>.expectation.draft.json`
and `selection_report.json`. Each page records its renderer, version, dpi and image SHA-256.
Geometry is declared `frame: displayed` (rotation removed, as pymupdf4llm does), with `rotation`
read from a separately opened copy of the PDF.

## Reviewing a draft

A draft is an `expectation` record with `provenance.review.status: "draft"` and `proposed_by`
naming the tool. Its blocks come only from the PDF's own text layer, are typed `other` and carry
high uncertainty; scanned pages have none. They are a convenience, not a conclusion. Judge the page
image, not the proposal:

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
placeholder uncertainty note, keep `proposed_by`, and keep `render.image_sha256` on every page.
Then confirm the strict loader accepts it:

```console
uv run python -c "import sys; sys.path.insert(0, 'benchmarks'); from extraction import load_reviewed_expectation as l; l(sys.argv[1])" <file>
```

Changing the status alone fails: the schema demands reviewer, timestamp, basis and the hashes above.
The status is still an attestation by the reviewer, not something the schema can prove.

## Where reviewed records go

Reviewed pilot records, with the `.draft` marker dropped from the name, go to
`benchmarks/extraction/expectations/pilot/<record_id>.expectation.json`. They contain expected
text, so only documents that passed the licence gate (every document in the manifest does) may be
committed, and only after review. Held-out records go to `.../expectations/held_out/` and are
labelled only after the pilot and scorer are stable. Drafts, page images and the selection report
stay out of the repository.
