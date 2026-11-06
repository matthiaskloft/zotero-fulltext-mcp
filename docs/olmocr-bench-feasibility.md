# olmOCR-bench scorer: adapter and feasibility run

Result of roadmap item E1, package C (issue #166), run 2026-11-06. Setup and adapter feasibility
**worked**: the pinned olmOCR-bench scorer installed into an isolated environment without its OCR
model, scored checks exported from our artifact, passed a correct Markdown and failed a defective
one. This is a feasibility result on one synthetic document, not an extraction baseline.

## What was built

`benchmarks/extraction/olmocr_export.py` (imported as `extraction.olmocr_export`) turns a *reviewed*
expectation into olmOCR-bench JSONL checks plus a sidecar report. The expectation stays the only
answer key; nothing in the JSONL is stored anywhere else, and drafts are refused.

```text
python -m extraction.olmocr_export EXPECTATION.json --pdf PDF --out checks.jsonl --report report.json
```

Run it with `benchmarks` on `PYTHONPATH` (as pytest does). A reviewed expectation is required; the
committed synthetic example is a draft and is accepted only with `--allow-synthetic-example` (and
only if its record id starts with `synthetic-example-` and it describes the committed corpus PDF).
Records whose own uncertainty is `high` are refused. `--report` is mandatory. `--pdf` is hashed and must match the
expectation's `pdf_sha256`, so checks cannot be scored against the wrong file.

| Expectation element | olmOCR-bench check |
| --- | --- |
| `heading`, `paragraph`, `list_item`, `caption`, `footnote`, `reference_entry` text | `present` |
| Consecutive text blocks of one page (by `reading_order`) | `order` |
| Table cell grid | `table`: one check per cell with its up/down/left/right neighbours and, where row 0 or column 0 holds an unspanned header, its `top_heading`/`left_heading` |
| `equation.latex` | `math` |

Everything is exported strict: `max_diffs` 0, case-sensitive. olmOCR still normalises both sides
first (NFC, whitespace, Markdown emphasis, `<br>`, dash and quote variants), so "strict" means
strict within that normalisation. Elements that cannot be expressed are listed in the report's
`skipped` list with a reason, and table relations omitted from an otherwise emitted check in
`partial`:

- page furniture (`header`, `footer`, `page_number`): the artifact does not say an extractor must
  omit it, so no `absent` check is invented;
- figures (a figure's own text is reported separately from a text-free graphic), `other` blocks,
  `text` on table and equation blocks (checked through cells and LaTeX instead), `caption_for` links
  and equation numbers (item-level entries), equations without LaTeX, blocks marked high uncertainty;
- text with fewer than 3 alphanumeric characters (table cells: fewer than 1), which would match
  almost anywhere on a page;
- text with an odd number of `_` or `*`: olmOCR strips emphasis page-wide with a lazy regex, so such
  markers can pair with markers elsewhere and fail correct Markdown (blocks and cells are skipped);
- table cells that are spanned or have no text, and neighbours that are spanned or empty;
- headings: upstream's Markdown parser treats only row 0 as the header row, so `top_heading` is
  asserted only for an unspanned header in row 0 and `left_heading` only for one in column 0; other
  header cells (second header row, spanned headers) are listed under `partial`;
- order pairs across pages, with identical text, or where one normalised text contains the other
  (olmOCR accepts any pair of matches, so such a check passes in either order);
- `continues_from` chains (same or earlier page): each fragment is checked on its own, the chain
  itself is reported as skipped;
- `truncated` blocks (cut at the scope boundary of a page-scoped record): skipped whole, because the
  expected text is only a fragment of the block;
- page body anchors, reference entries and citation markers (with their `target_status`).

A JSONL line may carry only fields of its olmOCR test class (the loader calls the dataclass with
`**data`), so links back to blocks live in the test `id` (`<record>.<block>.<kind>`) and the report.

## Pin

| | |
| --- | --- |
| Repository | `https://github.com/allenai/olmocr` |
| Release | `v0.4.27` (published 2026-03-12) |
| Commit | `1e139a5ea61f1668164e6b63357d7284a8391615` |
| Format read from | `olmocr/bench/README.md`, `tests.py`, `benchmark.py` at that commit |

The export field names and the per-class field whitelist in `tests/test_olmocr_export.py` come
from that `tests.py`. A different olmOCR version needs the format re-read before it is trusted.

## Exact commands

Everything lives in an empty directory outside the repository (`$WORK`); nothing touches
`pyproject.toml`, `uv.lock` or the project venv. The scorer was installed with `--no-deps` and only
what it imports was added: no torch, vLLM, transformers, model weights, API clients or GPU stack.
The dependency versions are pinned to what resolved on the run date.

```bash
WORK=/path/to/empty/scratch/directory
PY="$WORK/olmocr-venv/Scripts/python.exe"      # bin/python on macOS/Linux

python -m uv venv "$WORK/olmocr-venv" --python 3.11
python -m uv pip install --python "$PY" --no-deps \
  "git+https://github.com/allenai/olmocr.git@1e139a5ea61f1668164e6b63357d7284a8391615"
python -m uv pip install --python "$PY" numpy==2.4.6 beautifulsoup4==4.15.0 fuzzysearch==0.8.1 rapidfuzz==3.14.6 tqdm==4.70.1 \
  pypdf==6.21.4 pillow==13.0.0 playwright==1.64.1 lxml==6.1.3
PLAYWRIGHT_BROWSERS_PATH="$WORK/pw-browsers" "$PY" -I -m playwright install chromium
```

Resolved: numpy 2.4.6, beautifulsoup4 4.15.0, fuzzysearch 0.8.1, rapidfuzz 3.14.6, tqdm 4.70.1,
pypdf 6.21.4, pillow 13.0.0, playwright 1.64.1 (Chromium headless shell 156.0.8078.4), lxml 6.1.3.
`playwright` is always needed, because the scorer imports it at load time; the Chromium download
is needed only for `math` checks, and `lxml` only to compare rendered math. Without Chromium a
JSONL containing a `math` line cannot even be loaded, because `MathTest.__post_init__` renders the
equation.

Build the benchmark directory with the committed script (project environment, `benchmarks` on
`PYTHONPATH`), then score it, executed from outside the downloaded sources:

```bash
python -m extraction.olmocr_feasibility_inputs "$WORK/inputs"
```

```text
inputs/bench/pdfs/corpus.pdf                        copy of tests/fixtures/ocr_corpus/corpus.pdf
inputs/bench/ocr_corpus.jsonl                       exporter output
inputs/bench/<candidate>/corpus_pg<N>_repeat1.md    one Markdown file per page; candidates
                                                    pymupdf4llm, defective, mathcontrol
inputs/export_report.json                           counts, skipped items, check-to-block links
```

```bash
cd "$WORK"
PLAYWRIGHT_BROWSERS_PATH="$WORK/pw-browsers" "$PY" -I -X utf8 -m olmocr.bench.benchmark \
  --dir "$WORK/inputs/bench" --force --bootstrap_samples 100
```

`-X utf8` is needed on Windows for the scorer's own output (`-I` ignores `PYTHONIOENCODING`, and
the default cp1252 encoding crashed a math test on a non-ASCII symbol). The exported JSONL is
ASCII-escaped instead, because upstream opens it without an encoding. `--force` is needed because the PDF has pages
without checks; olmOCR also adds one `baseline` check on page 1 of each PDF. Candidate Markdown
comes from the repository's own extraction entry point
(`python -m zotero_pdf_text._extract_markdown corpus.pdf out.md`, default options, no image
output), split into pages with the `.pages.json` it writes; the script also injects the four defects.

## Run

Input: `tests/fixtures/ocr_corpus/corpus.pdf`, pages 1 and 3, with the expectation committed as
`benchmarks/extraction/examples/ocr_corpus_synthetic_example.draft_expectation.json`.

**This is not reviewed truth and not baseline data.** It is a synthetic example written by the
implementer from `corpus.tex` and the PDF text layer; no human inspected page images, and the
bounding boxes are approximate. Its review status is `draft`, its reviewer id is
`synthetic-example-not-a-reviewer`, no `basis` is stated (no schema value honestly describes it)
and its record id starts with `synthetic-example-`. The exporter accepts it only through the
explicit `--allow-synthetic-example` path, and every report carries
`expectation_review_status: draft`. It exists so the run is reproducible and for tests.

Export: 25 checks (7 present, 5 order, 12 table, 1 math), 6 skipped (two page numbers, one figure,
one cross-page order pair, one caption link, one equation number), 0 partial table relations.

| Candidate | Checks passed | By type |
| --- | --- | --- |
| Repo pymupdf4llm Markdown | 24 of 25, plus baseline | present 7/7, order 5/5, table 12/12, math 0/1 |
| Same plus the equation as `$$...$$` (positive control) | 25 of 25, plus baseline | math 1/1 |
| Deliberately defective Markdown | 12 of 25, plus baseline | present 6/7, order 3/5, table 3/12, math 0/1 |

- The only pymupdf4llm failure is the equation: its Markdown carries no LaTeX for it, so no check
  can match. That is the expected limit of the default path, and it is what the control isolates.
- The defective Markdown carried four injected defects: two table values swapped, a decimal point
  dropped (`1.640` to `1640`), one sentence omitted and two paragraphs swapped. All four were
  detected, and the failure messages name the offending cell relation or text, for example
  `Cell below '15.82' doesn't match expected '12.40'`.
- olmOCR's headline "Average Score" is the mean of per-JSONL scores, and the auto-added baseline
  counts as a file of its own (98.0% for one failure in 26). Use the per-check pass/fail and the
  failure list, not the headline.

## What the scorer can and cannot verify

Can: a text appears on the page (after normalisation); one text precedes another; a table cell
exists with the given neighbours or headings, in Markdown or HTML tables; a LaTeX equation appears
and renders to the same symbols in KaTeX. Failures are explained per check.

Cannot, so these stay as internal checks:

- **Exact location.** Everything is page-level and position-insensitive; bounding boxes are unused.
- **Hashes and exact spans.** `text_sha256` and span round-trips are meaningless after olmOCR's
  Unicode, whitespace, emphasis, dash and quote normalisation, and `present` is a substring match
  at similarity 1.0, so a text duplicated in the wrong place still passes.
- **Order is weaker than it looks.** olmOCR passes an order check if any match of `before` precedes
  any match of `after`, so it passes vacuously when one text contains the other (such pairs are
  skipped) and when the later text also occurs earlier on the page (not detected).
- **Emphasis characters.** Text with unbalanced `_`/`*` is skipped; balanced text is still
  stripped of emphasis markers on both sides, so such characters are not checked exactly.
- **Chunk coverage.** It scores one page's Markdown, not passage or chunk boundaries.
- **Cross-page order.** Order checks relate two texts on a single page.
- **Absence of extra or invented text.** Only `absent` checks test this, and none is exported.
- **References and figures.** No reference parsing, identifier recovery or citation-marker links;
  figures are not checked beyond their caption text.
- **Table checks are existential.** A cell passes if any cell in any table of the page satisfies
  the relations. Neighbour relations are ignored at table edges, so a missing edge neighbour is
  not detected.

The same list is written into every export report as `not_verifiable_by_olmocr`.

## Costs

| | |
| --- | --- |
| Install | about 30 s for venv, scorer and dependencies; about 35 s for the Chromium download (118 MiB) |
| Disk | about 180 MB venv, about 720 MB Chromium (math only) |
| Scoring | 2 to 4 s for 26 checks over three candidates, mostly Chromium start-up for math |
| Network | install only; scoring made no model or API calls |
| Model, GPU, keys | none |
| Side effect | the scorer writes an equation-render cache under `~/.cache/olmocr/bench/equations/` (12 KB); removed after this run |
| Ongoing | the adapter and input-builder modules (tested without olmOCR) and one isolated environment to rebuild when the pin moves |

## Decisions and open points

- The expectation is the only answer key. The JSONL and report are regenerable and not committed.
- Conservative skips (furniture, spanned or empty cells, high-uncertainty blocks) over guessed
  semantics. Revisit furniture `absent` checks when the artifact records whether it must be omitted.
- One table check per cell is verbose (12 for a 4 by 3 table). Sampling or per-row checks would cut
  it; not done because the choice should follow real tables.
- Whole-block text is exported rather than 1 to 3 sentence snippets, as the olmOCR README
  recommends. Long blocks interrupted by hyphenation or footnotes in real PDFs may need splitting.
- OmniDocBench is untouched; it remains a later option for richer table and formula metrics.
