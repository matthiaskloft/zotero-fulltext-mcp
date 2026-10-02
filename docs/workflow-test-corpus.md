# Public PDF corpus and workflow tests

## Current coverage

The committed `tests/fixtures/ocr_corpus/corpus.pdf` is a synthetic, reviewed math/crop
classification fixture. The four preprints in `benchmarks/preprints/sources.json` support
image-classification benchmarks; permission to redistribute their crops should not be
interpreted as a license to redistribute entire PDFs. The live container currently seeds
two small synthetic PDFs, so it checks integration rather than realistic extraction quality.

`benchmarks/public_pdfs/sources.json` holds four downloaded and inspected public sources,
with SHA-256 pins, rights references and layout descriptions, plus the S3b calibration corpus
described in [`public-corpus-articles.md`](public-corpus-articles.md). The four original sources:

| Source | Pages | Main purpose |
| --- | ---: | --- |
| Crowdsourcing survey, JMLR | 46 | Long prose, references, single-column reading order |
| Data sharing, PLOS ONE | 5 | Two columns, tables, plots, repeated headers |
| Gaussian mixture coresets, JMLR | 25 | Equations, theorems, symbols, mixed figures |
| Origin of species, historical scan | 532 | Scanning artifacts and noisy existing OCR; opt-in stress case |

The JMLR PDFs contain CC BY 4.0 notices. The PLOS article has an Attribution notice
without a version in the inspected notice. The scan's rights page identifies US public-domain
status. Consult the individual rights sources before redistributing derivatives; the repository
stores metadata only. Source URLs may change: checksum failures require review, never automatic
acceptance of replacement bytes.

Fetch on the host, outside Docker:

```console
uv run python tools/fetch_public_pdf_corpus.py --id data-sharing
uv run python tools/fetch_public_pdf_corpus.py
```

The default cache is gitignored. Downloads are explicit preparation, not part of ordinary CI.
Some publishers refuse scripted downloads; the tool reports those sources, continues with the rest
and exits non-zero. Their `note` says to save the PDF from `url` in a browser as
`.cache/<id>.pdf`; the next run verifies its checksum.
The long scan is about 43 MB. Public originals are not included in the Docker build context.
Later real-paper Zotero tests can copy selected cached files into an isolated container as test
inputs; they must not mount a private library.

## OCR cases still needed

The historical scan already has an imperfect text layer. It tests handling of existing OCR,
not whether an image-only page triggers OCR. Add controlled raster-only derivatives of reviewed
source pages at several resolutions, then rotation, skew and noise variants. These are planned,
not generated fixtures. Record parent hash, page numbers, rendering settings and transformation
seed. Establish expected text by inspecting the page; do not use the extractor's own output as
ground truth. Score prose OCR separately from mathematical transcription and crop classification.

Default extraction's OCR availability depends on installed OCR engines/language data; the plain
text fallback does not OCR. The container has not been validated for page OCR. A missing backend
or empty extraction must be an explicit outcome, not evidence that a scan converted successfully.
Multilingual papers, slides, forms and genuinely damaged PDFs remain coverage gaps.

## User stories operationalized as workflow tests

All tests that can run without Zotero belong in the host suite. Only real Zotero integration
tests belong in the Zotero container. Reuse the host-tested conversion/search pipeline inside
integration tests, with assertions focused on the Zotero boundary.

| User story | Observable acceptance criteria | Run location | Status |
| --- | --- | --- | --- |
| Import and link a paper | Real item and attachment appear; ZotMoov moves PDF to the isolated linked folder; CLI reports bridge as source | Zotero container | Implemented with synthetic PDFs |
| Avoid importing the same paper twice | Repeated import detects duplicate through the live bridge and creates no new item | Zotero container | Implemented |
| Convert my library and find a passage | Linked attachment is discovered, converted and indexed; MCP returns known text and a valid passage hash | Zotero container | Implemented with synthetic PDFs |
| Filter search by author/year | Reference-list names do not count as item authors; filters match seeded metadata | Host; container for real metadata boundary | Implemented synthetic coverage |
| Resume without repeating work | Second conversion skips unchanged input; existing passage locator still resolves | Host; container for linked-path discovery | Implemented synthetic coverage |
| Read a two-column article correctly | Reviewed passages preserve column order; tables keep row/value associations; headers do not pollute passage text | Host, cached PLOS PDF | Proposed |
| Search a mathematical paper | Reviewed prose/formula anchors survive conversion; math OCR is opt-in and its changes refresh the index | Host, coresets + synthetic math fixture | Existing crop tests; full-paper workflow proposed |
| Search a scanned paper | Image-only pages trigger a configured OCR backend; reviewed text is searchable; absent OCR produces a clear failure/outcome | Host, controlled scan variants | Proposed; variants and backend validation needed |
| Handle a scan with existing OCR | Noisy text layer is recognized; quality is measured against reviewed transcription, not merely nonempty output | Host, historical scan pages | Proposed |
| Recover after interrupted conversion | Completed papers are reused; incomplete work is retried; index/manifest stay consistent | Host | Extend with corpus workflow |
| Resolve or acquire a paper online | DOI produces expected item; downloaded attachment is actually linked and usable | Zotero container with network | Existing opt-in live tests; excluded from deterministic CI |
| Preserve library isolation | Test profile and outputs stay separate from normal config; no private paths, account or library enter image | Host config guards; container actual data-directory assertion | Implemented |

Keep full-paper quality benchmarks opt-in on the host, with reviewed expectations and a pinned
local cache. Small deterministic extraction fixtures can enter ordinary CI once their rights and
ground truth are reviewed. The regular three-OS CI excludes both live markers; the Docker runner
selects only tests requiring a running Zotero instance. The container's installed dependencies
support those integration workflows; it does not run the general unit or extraction suite.
