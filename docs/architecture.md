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
- Full-text CLI/MCP: read-only search over converted Markdown and conversion
  metadata. It does not modify Zotero.
- Better BibTeX CLI/MCP bridge: returns full `.bib` entries by citation key.
  The MCP tool is read-only; the CLI can append missing entries to an explicit
  LaTeX project `references.bib`.
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

## Deferred Memory Layer

Obsidian-style memory is out of scope for this implementation. If added later,
it should summarize and cite Zotero/full-text evidence, not duplicate all PDF
text.
