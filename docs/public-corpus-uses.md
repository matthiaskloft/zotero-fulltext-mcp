# Uses of the public corpus

Possible tests built on the S3b corpus (`benchmarks/public_pdfs/sources.json`), grouped by
ingestion stage. This is a backlog, not a commitment; see
[`workflow-test-corpus.md`](workflow-test-corpus.md) for what is implemented.

Environments: **host** (ordinary test suite, three-OS CI, or opt-in with the local cache),
**container** (isolated test Zotero in Docker), **network** (needs live internet).

## 1. Acquisition

1. **Pin integrity** (host, network, opt-in): fetch all sources, verify SHA-256 and page counts;
   catches moved or changed publisher files. Implemented by `tools/fetch_public_pdf_corpus.py`.
2. **Licence and metadata drift** (network, occasional): DOIs resolve, Crossref licence and title
   still match, URLs still serve a PDF. A maintenance audit rather than a test.
3. **Import by DOI** (container, network): `import-doi` on corpus DOIs; realistic coverage of
   Crossref/DataCite, many publishers, non-English metadata, item types, authors and years.
4. **Find a free PDF** (container, network): `find_available_pdf_for_item` should find the PDFs
   publishers serve freely and fail cleanly on the manual-download entries.
5. **Attach from URL** (container, network): `attach_pdf_from_url`; the pinned SHA-256 verifies the
   bytes after the ZotMoov move.
6. **Offline attach** (container): copy cached PDFs into the container and attach them as local
   files; a deterministic library of realistic papers.
7. **Duplicate detection** (container): same DOI twice, DOI plus PDF, and the same paper under
   several identifiers (PMC copy vs publisher version).

## 2. Discovery and conversion

8. **Linked-path discovery at scale** (container): about 100 linked attachments instead of 2.
9. **Text-layer extraction quality** (host, opt-in): scored against the maintainer's
   `expected_quality` labels per page and layout style (two columns, tables, equations, headers).
10. **Existing-OCR handling** (host): public-domain scans with noisy text layers.
11. **OCR triggering** (host): image-only derivatives of corpus pages at several resolutions, with
    rotation, skew and noise variants.
12. **Math OCR opt-in** (host): math-heavy entries (JMLR, EJS, LIPIcs, Bayesian Analysis) with
    `reconvert_with_math_ocr`.
13. **Crop and image classification** (host): figures and formulas extend the preprint crop
    benchmark, within the licences.
14. **Language handling** (host): German, French and Spanish entries for diacritics, hyphenation
    and OCR language data.
15. **Non-article formats** (host): slides, report and monograph for page sizes, long documents and
    chunking.
16. **Robustness and timeouts** (host): the 500-page scan and the monograph for timeout
    candidates, memory use and progress reporting.
17. **Resume and incremental runs** (host and container): convert half, interrupt, rerun; finished
    papers are skipped and the manifest stays consistent.

## 3. Indexing and retrieval

18. **Known-passage search** (host): reviewed anchor phrases per paper come back from
    `search_fulltext` with valid passage hashes.
19. **Ranking and recall benchmark** (host, opt-in): queries with known relevant papers across the
    fields, so neighbouring topics compete.
20. **Metadata filters** (container for real metadata): author and year filters; reference lists
    must not count as authors.
21. **Within-item search and chunk reading** (host): `search_within_fulltext` and
    `get_fulltext_chunk` locators on long documents.
22. **Citation keys and BibTeX export** (container): `lookup_citation_key` and
    `export_bibtex_entries_by_key` on varied metadata, including non-ASCII names.

## 4. End to end and regression

23. **Full user story** (container, network, nightly or manual): DOI → item → PDF → conversion →
    search finds a reviewed passage.
24. **Extractor and engine regression** (host, opt-in): rerun after dependency bumps; diff quality
    scores against the labels, never against earlier extractor output.
25. **Performance baseline** (host): pages per second and index size over the whole corpus.
26. **Small CI subset** (host, three-OS CI): a few short reviewed entries. Ordinary CI has no cache,
    so these stay opt-in until redistribution is decided, or are replaced by derived fixtures.

## By environment

| Environment | Uses |
| --- | --- |
| Ordinary CI | Synthetic fixtures only (26 once cleared) |
| Host, opt-in with cache | 1, 9–19, 21, 24, 25 |
| Container, offline | 6–8, 17, 20, 22 |
| Container with network | 3–5, 7, 23 |
| Maintenance | 2 |

Uses 3–5 and 23 make the corpus the input of the Zotero acquisition tests; the fetch tool stays for
host-side quality work, which should depend on neither Zotero nor the network.
