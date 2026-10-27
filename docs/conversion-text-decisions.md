# Conversion text decisions

Decisions about what the primary extractor's text contains, and what the index does to it, made
for issue #79 and backed by a measurement on a real library. Only aggregate counts are recorded
here; no paper text, keys or paths.

## Page offsets

`pymupdf4llm.to_markdown(..., page_chunks=True)` returns one string per page. Without
`page_chunks`, the output is exactly those strings concatenated with no separator, so the stored
Markdown is unchanged and the page boundaries cost nothing. `tests/test_extract_markdown.py`
checks this on the committed OCR corpus PDF, and checks that the per-page lengths add up to the
whole output. pymupdf4llm 1.28.0 is the version this was verified on; a pymupdf4llm upgrade
that breaks it fails that test.

Pages are physical, 1-based indices into the PDF file. Printed journal page numbers are a
separate concern (the `pages` metadata field).

## Header and footer removal: unchanged

pymupdf4llm's defaults (`header=True, footer=True`) keep running headers and footers. A sample of
12 randomly chosen papers from a library of about 2,000, converted with and without
`header=False, footer=False` (11 converted; one failed for an unrelated console-encoding reason
in the measurement script):

- Every paper lost lines, 2 to 956 per paper. No line was ever added.
- Most removed lines are page furniture: page numbers, running heads (journal name and issue,
  author names, book or chapter titles), copyright lines, and repository download banners
  repeated on every page.
- Some removed lines are real content: a bold appendix heading, a footnote, and a full body
  sentence at the top of a book chapter page.

Because removal deletes real text in a small sample, the default is not switched. Page furniture
adds noise to search but does not hide anything; deleting a sentence hides it permanently, and
nothing tells the reader it is gone. To revisit, compare a larger sample and check whether the
lost content follows a pattern that a narrower rule could keep.

## Normalization for indexing: not added

Measured on the full text of the same library's current index (2,033 records):

| Problem | Records affected | Occurrences |
| --- | ---: | ---: |
| Ligature characters (U+FB00 to U+FB06) | 2 | 5,255 |
| Lowercase, `-`, line break, lowercase | 7 | 1,726 |
| Lowercase, `-`, space, lowercase | 1,019 | 31,833 |

- **Ligatures** are concentrated in 2 records. Too rare to justify an indexing transform.
- **Hyphen at a line break** barely occurs, because pymupdf4llm joins the lines of a paragraph
  with a space, not a newline. The matches it does find are mostly code listings.
- **Hyphen then space** is the form a line-break split takes in this extractor's output, and it
  is common. But in a random sample of 153 matches almost all are legitimate: suspended compounds
  (`Denk- und`, `short- and`, `self- or`; the most frequent following words are `und`, `oder`,
  `and`, `bzw`, `or`) and real hyphenated compounds broken at the line end (`self- report`,
  `cross- sectional`). Joining them would produce non-words (`selfreport`, `Denkund`). Without a
  dictionary, a rule cannot tell `be- yond` from `self- report`, and the real word splits are a
  small minority. The FTS tokenizer already splits `self- report` and `self-report` into the same
  two tokens, so leaving the text alone loses nothing for real compounds.

So the indexed text stays identical to the stored text, apart from the existing image-link
blanking. Revisit this if embeddings (#1) prove sensitive to word splits, ideally with a
dictionary-backed rule.
