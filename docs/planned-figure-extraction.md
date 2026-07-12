# Planned: Figure And Graphics Extraction

Status: **not started** — design only, moved here from the old workspace `TODO.md` on 2026-07-11.

Goal: add a graphics artifact layer for each Zotero PDF so LLMs can inspect article figures,
plots, diagrams, screenshots, and page renderings alongside full text.

This should be implemented as a separate layer from Markdown/full-text conversion. Text remains
in the existing full-text index. Graphics get their own extracted files, metadata, and MCP
access.

## Proposed Artifact Layout

```text
converted_text/
  figures/
    <attachment_key>/
      0001_page-003_fig-001.png
      0002_page-007_fig-002.png
      manifest.jsonl
  index/
    zotero_figure_index.sqlite
```

Use four-digit or otherwise width-stable numeric prefixes where lists may grow.

## Metadata Fields

Each extracted figure record should include:

```yaml
zotero_key: ...
parent_key: ...
attachment_key: ...
title: ...
authors: ...
year: ...
doi: ...
page: ...
figure_label: ...
caption: ...
image_path: ...
thumbnail_path: ...
source_pdf_path: ...
extraction_tool: ...
extraction_method: embedded_image | rendered_crop | page_render
confidence: high | medium | low | needs_review
created_at: ...
```

## Extraction Strategy

1. Embedded image extraction
   - Use PyMuPDF to extract raster images directly embedded in each PDF.
   - This is fast and useful for photos, scans, screenshots, and some bitmap figures.
   - It will miss many scientific plots because they are often vector drawings.

2. Rendered page and figure crop extraction
   - Render pages as images with PyMuPDF.
   - Detect/crop figure regions from rendered pages where possible.
   - Associate crops with nearby captions such as `Figure`, `Fig.`, `Abb.`, and variants.
   - Mark uncertain crops as `needs_review`.

3. Conservative fallback
   - If reliable figure regions cannot be detected, store page renders for pages with detected
     figure captions.
   - This gives LLMs visual access without pretending the crop is precise.

## Indexing

Add a figure metadata index:

- Build JSONL manifests per attachment.
- Build a sidecar SQLite index for figure metadata.
- Include searchable captions, labels, titles, authors, DOI, Zotero keys, attachment keys, and
  image paths.
- Keep extraction method and confidence fields so LLMs can reason about reliability.

## MCP Access

Extend the read-only `zotero-fulltext` MCP server with graphics tools:

- `search_figures`
- `get_article_figures`
- `get_figure_context`
- `get_figure_image`
- `get_page_image`

The existing Zotero MCP remains responsible for live Zotero metadata, collections, tags, notes,
and Zotero URIs.

## Tests

- Unit-test embedded image extraction on a tiny PDF fixture.
- Unit-test caption detection for `Figure`, `Fig.`, and `Abb.` labels.
- Unit-test manifest generation and SQLite indexing.
- Smoke-test extraction on a small sample of real Zotero PDFs.
- Verify returned image paths exist and are readable.

## Open Design Questions

- Whether to store full page renders for every page or only pages with detected figure captions.
- Whether to add thumbnails immediately or generate them lazily.
- Whether tables should be handled by the graphics layer or kept as a separate future artifact
  type.
- Which layout detection tool, if any, should be added after the first PyMuPDF-only version.

## Precedent: why equation extraction shipped differently than originally planned

An earlier equation-extraction design (dedicated per-equation JSONL/SQLite artifacts with
provenance and confidence fields — structurally similar to what's proposed above for figures) was
never built as specified. Instead, a simpler just-in-time approach shipped 2026-07-06/07:

- `math_detection.py` auto-detects likely math content per PDF during normal conversion
  (math-font substrings + Unicode math-symbol density) and stores it as a `has_math` boolean in
  the Markdown front matter, JSONL sidecar, and SQLite metadata (`by_has_math` in
  `coverage-report`).
- `reconvert-math --key <ATTACHMENT_KEY>` re-extracts **one paper at a time** with marker-pdf
  (LaTeX-aware equation/figure handling), overwriting that paper's Markdown in place. Deliberately
  just-in-time only, never bulk — marker-pdf runs at roughly 27s/page, so reconverting a whole
  math-heavy subset in bulk would take on the order of weeks.
- No separate equation index, no per-equation confidence/provenance records, and no dedicated
  equation MCP tools were built — `has_math` is just a flag telling an LLM (or a human) "this one
  might be worth `reconvert-math`-ing before trusting the notation."

Worth revisiting before committing to the full figure-index design above: a similarly
just-in-time approach (flag pages/PDFs likely to have extractable figures, extract on demand via
an MCP tool) may cover most real usage with much less machinery than a bulk figure-extraction
pipeline and dedicated SQLite index.
