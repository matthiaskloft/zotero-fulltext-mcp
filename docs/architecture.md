# Architecture

## Sources Of Truth

- Zotero is the source of truth for bibliographic metadata, collections, tags,
  notes, and item relationships. Zotero's own account sync (zotero.org,
  metadata-only) is what reconciles this across machines if you use more than
  one. **The Zotero data directory (`zotero_data_directory` in your config)
  should always live locally on each machine — never inside a cloud-synced
  folder (Dropbox, Nextcloud, OneDrive, etc.).** `zotero.sqlite` is a live,
  actively-written database; syncing it as a plain file has been observed to
  produce sync-conflict corruption.
- `zotero_linked_attachments` is the source of truth for linked PDF files.
  These are plain, write-once files — safe to keep in a cloud-synced folder if
  you work from more than one machine.
- Converted Markdown and sidecar indexes are derived artifacts for LLM access.
  If shared across machines as a single index, writes should be serialized by
  a `.pipeline.lock` file (see `docs/operations.md`) so two machines never
  rebuild them at the same moment.

Generated Markdown and SQLite indexes can be rebuilt. They should not be treated
as primary library records.

## Data Flow

1. `dry-run` snapshots `zotero.sqlite`, reads Zotero attachment metadata, scans
   linked PDFs, and writes mapping reports.
2. `convert-verified` converts `mapped_verified` PDFs to Markdown with YAML
   front matter.
3. `build-index` reads a conversion manifest and creates a JSONL sidecar with
   metadata, paths, checksums, conversion metadata, and full text (full rebuild,
   for the first build or a manifest that already covers everything trusted).
   `append-index` adds a smaller manifest's new rows into an existing JSONL
   sidecar instead, skipping attachment keys already indexed; `convert-new` runs
   dry-run, conversion, and this append step together for new items.
4. `build-fts` reads the JSONL sidecar and creates a SQLite FTS5 database with
   chunked searchable text (`append-index` triggers this automatically).
5. LLM tools use Zotero MCP for live Zotero metadata and the full-text sidecar
   for bounded text retrieval.
6. Better BibTeX supplies authoritative BibTeX/BibLaTeX entries by
   `citation_key` through its local JSON-RPC endpoint.

## Access Layers

- Zotero MCP: live metadata, collections, tags, child attachments, notes, and
  Zotero URIs. Zotero must be running.
- Full-text MCP: the default surface is bounded, read-only search, passage retrieval, and item
  context over converted Markdown. It never launches Zotero or exposes local paths; returned
  library material is labelled untrusted. Search results identify the fields that matched, and
  search/passage locators bind attachment/chunk/character identity to the converted Markdown
  SHA-256. Exact passage reads expose bounded chunk navigation and distinguish the returned span
  from a larger stored chunk; leading previews are explicitly not exact chunk cursors. The
  optional math-OCR capability must be enabled at
  startup with an explicit valid config governing the selected database and with the Marker
  dependency installed. It requires an exact confirmation literal, overwrites one attachment's
  derived Markdown, image assets, and index record, and is rate-limited, but it never modifies
  Zotero.
- Full-text CLI: maintenance and operational commands, including Zotero process startup and
  unguarded `reconvert-math`, remain explicit local workflows.
- Better BibTeX CLI/MCP bridge: the CLI returns or appends full `.bib` entries by citation key.
  The MCP export bridge is disabled by default and, when explicitly enabled, can only call the
  configured credential-free loopback endpoint on Zotero's local port.

MCP tool annotations describe this split to compatible clients: index reads and the loopback
BibTeX bridge are read-only, non-destructive, closed-world operations; math reconversion is a
non-idempotent destructive update to derived content. These annotations are presentation/risk
hints, while startup-time capability registration is the enforcement boundary.
- Ingestion queue: dry-run dedupe for LLM literature-search imports.
- Zotero write CLI: approval-gated write plans that generate local Zotero
  JavaScript for creating items, linking local PDFs, updating exact-key metadata,
  triggering Zotero identifier/PDF lookup, or moving exact keys to trash. This
  is separate from MCP and never writes directly to `zotero.sqlite`.
- ZotMoov: optional Zotero-side file-management layer. It can move/rename PDFs
  found or stored by Zotero into the linked attachment folder; this project only
  records whether ZotMoov is expected and refreshes derived indexes afterward.

## Confidence Model

The trusted baseline is `mapped_verified`. Unverified mappings should not be
silently mixed into verified search results. If they are indexed later, they
must carry explicit `classification` and `identity_status` fields.

`classify_identity` (`identity.py`) strips Markdown image syntax (`![alt](path)`) from converted
text before scoring, so an embedded image filename that happens to echo a candidate's title can't
be mistaken for real article prose by `title_score`. A confidently-parsed DOI in the text that
conflicts with the expected one is treated as disqualifying evidence regardless of title score —
that check runs before, and takes precedence over, the title/author/year accept rule, so generic
topic-vocabulary overlap can never let a wrong-document mapping through just because a real,
different DOI was also present.

DOI, author-surname, and year evidence is scanned only within a leading `EVIDENCE_WINDOW_CHARS`
(6,000 characters) window of the converted text, not the whole document — a paper's own DOI stamp
and byline normally land within the first page or two, while reference lists and cited works can
run for many more pages after that. This keeps a document that merely cites a different-DOI work,
or a bibliography entry sharing a claimed author's surname, from being penalized (or credited) for
evidence that isn't actually about the document itself. Title matching is intentionally not
windowed, since `title_score`'s fuzzy partial-ratio matching can find a title anywhere in the text.

## Deferred Memory Layer

Obsidian-style memory is out of scope for this implementation. If added later,
it should summarize and cite Zotero/full-text evidence, not duplicate all PDF
text.
