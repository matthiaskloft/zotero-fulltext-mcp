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

Run it with `benchmarks` on `PYTHONPATH` (as pytest does). `--pdf` is hashed and must match the
expectation's `pdf_sha256`, so checks cannot be scored against the wrong file.

| Expectation element | olmOCR-bench check |
| --- | --- |
| `heading`, `paragraph`, `list_item`, `caption`, `footnote`, `reference_entry` text | `present` |
| Consecutive text blocks of one page (by `reading_order`) | `order` |
| Table cell grid | `table`: one check per cell with its up/down/left/right neighbours and nearest header cells |
| `equation.latex` | `math` |

Everything is exported strict: `max_diffs` 0, case-sensitive. olmOCR still normalises both sides
first (NFC, whitespace, Markdown emphasis, `<br>`, dash and quote variants), so "strict" means
strict within that normalisation. Elements that cannot be expressed are listed in the report's
`skipped` list with a reason, and table relations omitted from an otherwise emitted check in
`partial`:

- page furniture (`header`, `footer`, `page_number`): the artifact does not say an extractor must
  omit it, so no `absent` check is invented;
- figures, `other` blocks, blocks without text or with only punctuation, equations without LaTeX,
  blocks marked high uncertainty;
- table cells that are spanned or have no text, and neighbours that are spanned or empty;
- order pairs across pages or with identical text on both sides;
- page body anchors, reference entries and citation markers.

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
The dependency versions below are what resolved on the run date; pin them to reproduce.

```bash
WORK=/path/to/empty/scratch/directory
PY="$WORK/olmocr-venv/Scripts/python.exe"      # bin/python on macOS/Linux

python -m uv venv "$WORK/olmocr-venv" --python 3.11
python -m uv pip install --python "$PY" --no-deps \
  "git+https://github.com/allenai/olmocr.git@1e139a5ea61f1668164e6b63357d7284a8391615"
python -m uv pip install --python "$PY" numpy beautifulsoup4 fuzzysearch rapidfuzz tqdm pypdf pillow playwright lxml
PLAYWRIGHT_BROWSERS_PATH="$WORK/pw-browsers" "$PY" -I -m playwright install chromium
```

Resolved: numpy 2.4.6, beautifulsoup4 4.15.0, fuzzysearch 0.8.1, rapidfuzz 3.14.6, tqdm 4.70.1,
pypdf 6.21.4, pillow 13.0.0, playwright 1.64.1 (Chromium headless shell 156.0.8078.4), lxml 6.1.3.
`playwright` and Chromium are needed only for `math` checks; `lxml` only to compare rendered
math. Without them a JSONL containing a `math` line cannot even be loaded, because
`MathTest.__post_init__` renders the equation.

Benchmark layout (`$WORK/bench`) and run, executed from outside the downloaded sources:

```text
bench/pdfs/corpus.pdf                        copy of tests/fixtures/ocr_corpus/corpus.pdf
bench/ocr_corpus.jsonl                       exporter output
bench/<candidate>/corpus_pg<N>_repeat1.md    one Markdown file per page and candidate
```

```bash
cd "$WORK"
PLAYWRIGHT_BROWSERS_PATH="$WORK/pw-browsers" "$PY" -I -X utf8 -m olmocr.bench.benchmark \
  --dir "$WORK/bench" --force --bootstrap_samples 100
```

`-X utf8` is needed on Windows (`-I` ignores `PYTHONIOENCODING`, and the default cp1252 console
encoding crashed the run on a non-ASCII symbol). `--force` is needed because the PDF has pages
without checks; olmOCR also adds one `baseline` check on page 1 of each PDF. Candidate Markdown
came from the repository's own extraction entry point
(`python -m zotero_pdf_text._extract_markdown corpus.pdf out.md`, default options, no image
output), split into pages with the `.pages.json` it writes.

## Run

Input: `tests/fixtures/ocr_corpus/corpus.pdf`, pages 1 and 3, with the expectation committed as
`benchmarks/extraction/examples/ocr_corpus_olmocr_feasibility.expectation.json`. It is a
**hand-written feasibility fixture** (text taken from `corpus.tex` and the PDF text layer; bounding
boxes approximate; one equation with its LaTeX from the source). Its `reviewed` status is nominal
so the exporter accepts it; it is not a human-reviewed baseline and must not be reused as one.

Export: 25 checks (7 present, 5 order, 12 table, 1 math), 4 skipped (two page numbers, one figure,
one cross-page order pair), 0 partial table relations.

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
- **Chunk coverage.** It scores one page's Markdown, not passage or chunk boundaries.
- **Cross-page order.** Order checks relate two texts on a single page.
- **Absence of extra or invented text.** Only `absent` checks test this, and none is exported.
- **References and figures.** No reference parsing, identifier recovery or citation-marker links;
  figures are not checked beyond their caption text.
- **Table checks are existential.** A cell passes if any cell in any table of the page satisfies
  the relations.

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
| Ongoing | the adapter (under 300 lines, tested without olmOCR) and one isolated environment to rebuild when the pin moves |

## Decisions and open points

- The expectation is the only answer key. The JSONL and report are regenerable and not committed.
- Conservative skips (furniture, spanned or empty cells, high-uncertainty blocks) over guessed
  semantics. Revisit furniture `absent` checks when the artifact records whether it must be omitted.
- One table check per cell is verbose (12 for a 4 by 3 table). Sampling or per-row checks would cut
  it; not done because the choice should follow real tables.
- Whole-block text is exported rather than 1 to 3 sentence snippets, as the olmOCR README
  recommends. Long blocks interrupted by hyphenation or footnotes in real PDFs may need splitting.
- OmniDocBench is untouched; it remains a later option for richer table and formula metrics.
