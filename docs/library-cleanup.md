# Library Cleanup

A guide, not a new command: this ties together commands that already exist elsewhere in
`docs/operations.md`, framed around one recurring task — getting a Zotero library's linked PDFs
into good enough shape for the fulltext index to trust, especially in the early stages of building
up a library (bulk imports, mixed sources, PDFs saved more than once). Each step links to the
section with the full option reference; this page is about *which command answers which question*
and *what order to run them in*, not the flags.

## The starting point: a dry run

```powershell
& $python -m zotero_pdf_text dry-run --config .\config.json
```

`dry-run`'s `mapping_report.csv` classifies every linked PDF and gives every cleanup step below its
input. Three classifications are the ones worth acting on before converting anything:

- `mapped_unverified` / `possible_mismatch` — the PDF is linked to a Zotero item, but the mapper
  isn't confident the metadata and the file actually match.
- `orphan_pdf` — a PDF with no confident metadata match, most often a generic
  publisher-filename (`1-s2.0-...-main.pdf`) that never gets a chance to match by filename alone.
- Two `mapped_verified` (or better) rows sharing the same `zotero_parent_key` — a duplicate
  attachment, not a mapping problem.

## "Does this PDF actually match the item it's linked to?"

Run **Unverified PDF Review** (`docs/operations.md`). `verify-unverified` compares Zotero's
metadata against the PDF's own text and gives you a deterministic accept/reject/manual-review
verdict per row, with cheap-subagent batches for the ambiguous ones. This is the first cleanup pass
to run, since a mismatch here means the file itself is wrong for that Zotero item — worth catching
before the file gets indexed under the wrong citation.

## "This PDF has no Zotero item — does it actually belong to one that's missing its file?"

Run **Orphan-Parent Discovery** (`docs/operations.md`). `find-orphan-parents` scores every
`orphan_pdf` row's own text content against every Zotero item that has no working PDF attachment —
the reverse of `dry-run`'s filename-only matching. Only high-confidence (verified) pairings are
reported; resolve one with `orphan-candidate --skip` (not the same paper) or `link-pdf` (attach it).

## "This item has 2+ PDFs — are any of them redundant?"

Run **Duplicate Attachment Cleanup** (`docs/operations.md`). `find-duplicate-attachments` finds
attachments on the same Zotero item that are byte-for-byte the same PDF (typically a re-saved copy
with a `2`/`3` suffix, or a second download from a different source) and writes a `trash_item`
write plan for the redundant copies. It only auto-resolves the unambiguous case — same hash, and
exactly one filename without a suffix; a genuinely different edition, or a group where the "keep"
file can't be picked automatically, is reported separately instead of guessed (see
`docs/troubleshooting.md` item 6 for why that line is drawn there). Removing anything still goes
through the same approval-gated `zotero-write approve`/`validate --require-approved`/
`apply --approve` flow as every other Zotero write in this project — nothing is trashed by running
the discovery command alone.

## After cleanup: rebuild or append the index

Promote verified/orphan-resolved rows into a manifest and run `update-index` (see "Unverified PDF
Review" and "Managed Index Generations" in `docs/operations.md`) rather than a full
`rebuild-index --manifest` rebuild, so cleanup doesn't cost a full reconversion of the library.

## Related, but not "cleanup" in this sense

**Timeout Candidates** (`docs/operations.md`) is about conversion performance (a PDF that timed out
during extraction), not about whether a PDF belongs where it's linked. It's tracked with the same
discover → report → resolve shape as the commands above, but it's a pipeline-health concern, not a
library-correctness one — listed here only so it isn't mistaken for a fourth cleanup step.
