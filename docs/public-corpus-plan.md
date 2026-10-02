# Public PDF corpus plan (S3b)

Tracks [#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115). Extends
`benchmarks/public_pdfs/sources.json` and follows the storage rules in
[`workflow-test-corpus.md`](workflow-test-corpus.md): metadata and SHA-256 pins only, no PDFs
committed, downloads are explicit host preparation.

## Purpose

1. Calibrate S3's extraction-quality thresholds (`quality.py`) against reviewed labels.
2. Check `detected_language` on non-English items.
3. Decide whether `convert-new` should refresh Zotero metadata automatically.
4. Provide a committable, public retrieval question set (positive and negative questions) that
   complements the private S1/S1a set.

## Size and fields

Target: about 100 PDFs. Fields were approved by the maintainer.

| Group | Field | Journals | Articles per journal | Items |
| --- | --- | ---: | ---: | ---: |
| Natural and life sciences | Clinical medicine | 3 | 2 | 6 |
| | Physics | 3 | 2 | 6 |
| | Chemistry | 3 | 2 | 6 |
| | Ecology/biology | 3 | 2 | 6 |
| Formal and quantitative | Statistics | 3 | 2 | 6 |
| | Psychometrics | 3 | 2 | 6 |
| | Computer science | 3 | 2 | 6 |
| | Economics | 3 | 2 | 6 |
| | Materials/engineering | 3 | 2 | 6 |
| Social sciences and humanities | Psychology | 3 | 2 | 6 |
| | Education (incl. NEPS Survey Papers) | 3 | 2 | 6 |
| | Sociology/political science | 3 | 2 | 6 |
| | Law | 3 | 2 | 6 |
| | Humanities/linguistics | 3 | 2 | 6 |
| Extra formats | German, French, Spanish papers | — | — | 6 (2 each) |
| | Image-only or poor-OCR scans | — | — | 2 |
| | Slide decks and grey-literature reports | — | — | 2 |
| | Long monograph or review (100+ pages) | — | — | 1 |
| | Already pinned sources | — | — | 4 |
| **Total** | | | | **~99** |

Non-English papers may also count toward a field when they fit one.

## Selection method

Hierarchical, by web search only, no downloads during selection:

1. **Journal.** Fully open access (not hybrid), well regarded in the field, articles under a
   verifiable Creative Commons licence or public domain. Prefer journals with stable PDF URLs.
2. **Article.** Popular (highly cited or widely read) within the journal, published since 2000
   unless chosen as a historical case. Within each field, vary layout across articles:
   two-column vs single-column, table- or equation-heavy, short report vs long article.
3. **Topic spread.** Avoid two articles on the same topic across the whole corpus, so negative
   questions (below) can be written for gaps that are known to exist.

### Licence rules

- Verify on the article or record page, not the journal's general policy page. Record the
  licence and version (`CC-BY-4.0`, ...).
- Flag NC, ND and SA variants in the record; they are acceptable for metadata-only pinning but
  restrict any derived fixture.
- Reject items without a verifiable open licence.

## Record format

Each item is added to `benchmarks/public_pdfs/sources.json` with the existing fields (`id`,
`title`, `url`, `rights_source`, `license`, `sha256`, `pages`, `styles`) plus:

| Field | Meaning |
| --- | --- |
| `field` | One of the approved fields above, or `extra` |
| `language` | ISO 639-1 code from inspection |
| `doi` | When one exists |
| `expected_quality` | `good`, `degraded` or `unusable`, from inspecting the pages, never from extractor output |
| `quality_notes` | What drove the label (e.g. broken ligatures, scanned pages, garbled columns) |

Candidates are first recorded in a review table in this document; only downloaded and pinned
items enter `sources.json`.

## Question set

Written after the corpus is pinned and labelled, by reading the papers, never from search
results. Stored as a committed fixture (public papers only), in the format the S1a harness reads.

| Type | Share | Expected result |
| --- | ---: | --- |
| Positive, single paper | ~60% | The paper, with marked evidence spans |
| Positive, multi-paper | ~10% | Several papers across fields |
| Near-miss negative | ~15% | No paper: shares vocabulary with a corpus paper but asks about something it does not cover |
| Plain negative | ~15% | No paper: plausible research topic absent from the corpus |

Target about 150 questions, with the same held-out split as S1a. Negative questions need a
metric the harness does not have yet: whether top results are flagged as weak / below a score
threshold. Adding it is part of this work.

## Workflow

1. Candidate journals per field → maintainer review.
2. Candidate articles with verified licences → maintainer review.
3. Host download, SHA-256 pinning, page inspection and quality labels.
4. Opt-in host test comparing scores to labels; threshold margins reported.
5. Question set and negative-result metric.
6. Threshold adjustments in `quality.py`, confirmed on a real library.
