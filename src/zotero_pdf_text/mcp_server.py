from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .bibtex import DEFAULT_BBT_ENDPOINT, DEFAULT_BBT_TRANSLATOR, export_bibtex_entries
from .config import load_config, resolve_config_path, validate_config
from .fts import coverage_report as coverage_report_fn, get_fulltext, get_item_context as get_item_context_fn, search_fts
from .runtime import DEFAULT_ZOTERO_EXE, ensure_zotero_running as ensure_runtime_zotero


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zotero-fulltext-mcp")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite FTS database path. Default: ZOTERO_FULLTEXT_DB env var, or <output_root>/index/zotero_text_index.sqlite from the resolved config.",
    )
    parser.add_argument("--zotero-exe", type=Path, default=DEFAULT_ZOTERO_EXE, help="Path to zotero.exe.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to project config JSON. Default: ZOTERO_PDF_TEXT_CONFIG env var, or this "
            "machine's config.<hostname>.json/config.json next to the server's working directory."
        ),
    )
    args = parser.parse_args(argv)

    config_path = args.config if args.config is not None else resolve_config_path()
    if not config_path.exists():
        raise SystemExit(
            f"No project config found at {config_path}. Set ZOTERO_PDF_TEXT_CONFIG, pass --config "
            "explicitly, or create config.json/config.<hostname>.json for this machine."
        )
    config = load_config(config_path)
    validate_config(config)

    db_path = args.db if args.db is not None else Path(
        os.environ.get("ZOTERO_FULLTEXT_DB", str(config.output_root / "index" / "zotero_text_index.sqlite"))
    )

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise SystemExit(
            "The optional 'mcp' package is not installed. Install project MCP dependencies before running "
            "zotero-fulltext-mcp."
        ) from exc

    logging.getLogger("mcp").setLevel(logging.WARNING)

    mcp = FastMCP(
        "zotero-fulltext",
        instructions="""
## Zotero fulltext MCP — usage guide

This server provides **read-only fulltext search** over a Zotero library whose PDFs have been
converted to Markdown sidecar files. It operates independently of the Zotero application
and connector — all data is read from a pre-built SQLite FTS index.

### Tools at a glance

| Tool | When to use |
|---|---|
| `search_fulltext(query)` | Find papers by content — returns ranked snippets |
| `get_fulltext_chunk(attachment_key)` | Retrieve the full converted text for one paper |
| `get_item_context(parent_key)` | Look up metadata (title, authors, DOI, citation key) for an item |
| `coverage_report()` | Check how many items are indexed vs. total |
| `export_bibtex_entries_by_key(citation_keys)` | Export BibTeX for one or more citation keys |
| `ensure_zotero_running()` | Check / start the Zotero application |
| `reconvert_with_math_ocr(attachment_key)` | Re-extract one paper with marker-pdf when equations/figures look garbled or missing — blocking, takes minutes |

### Finding a DOI (if you don't already have one)

Before concluding a paper has no DOI, check **OpenAlex** — it indexes PsyArXiv/OSF and other
preprint servers that arXiv and CrossRef search miss:
```
https://api.openalex.org/works?search=<title>
```
Only treat a paper as genuinely DOI-less (needs a manual Zotero item, see below) after checking
OpenAlex too, not just arXiv/CrossRef.

### Workflow for adding a new paper

Run these CLI commands from this machine's `zotero_fulltext_mcp` checkout, using its own venv and
config (e.g. `config.<hostname>.json` if one exists, otherwise `config.json`):

```powershell
# 1. Import by DOI — fetches metadata from CrossRef/DataCite, posts to Zotero connector.
#    No plugins required. Returns JSON with status, title, item_type, and key.
.\.venv\Scripts\python.exe -m zotero_pdf_text import-doi --doi "10.xxxx/yyyy" --config .\config.json

# 2. Check whether Zotero found a PDF (reads local SQLite — works even if Zotero is closed)
.\.venv\Scripts\python.exe -m zotero_pdf_text check-pdf --key <ZOTERO_KEY> --config .\config.json

# 3. If no PDF: trigger Zotero's own "Find Available PDF" search (OA repositories, publisher
#    pages, etc.) before resorting to a manual attach. Requires the debug-bridge plugin.
.\.venv\Scripts\python.exe -m zotero_pdf_text find-pdf --key <ZOTERO_KEY> --config .\config.json

# 3b. Still nothing, but you already have the PDF on disk somewhere (e.g. a sibling project's
#     folder)? Link it with link-pdf — it also invokes the ZotMoov plugin to copy + rename the
#     file into the managed linked_attachments folder in one step. Requires debug-bridge AND
#     ZotMoov, both active.
.\.venv\Scripts\python.exe -m zotero_pdf_text link-pdf --key <ZOTERO_KEY> --file "C:\path\to\paper.pdf" --config .\config.json

# 4. Convert new PDFs and update the fulltext index (incremental — skips already-indexed items)
.\.venv\Scripts\python.exe -m zotero_pdf_text convert-new --config .\config.json
```

### find-pdf output

```json
{"ok": true, "key": "ABCD1234", "found": true, "attachment_key": "WXYZ5678", "error": "", "endpoint": "..."}
{"ok": true, "key": "ABCD1234", "found": false, "attachment_key": "", "error": "", "endpoint": "..."}
```

`found: false` means Zotero's own PDF search came up empty — try `link-pdf` next if you have a local copy.

### link-pdf output

```json
{"ok": true, "key": "NEWKEY01", "attachment_key": "NEWKEY01", "path": "C:\\...\\zotero_linked_attachments\\Author - Year - Title.pdf", "moved": true, "warning": "", "error": "", "endpoint": "..."}
```

`moved: true` means ZotMoov copied the file into `linked_attachments` and renamed it — the returned
`key`/`path` are for the *new* attachment (ZotMoov replaces the original attachment item with a
renamed clone, so the key changes). `moved: false` with a `warning` means the file stayed at its
original path (ZotMoov or its `dst_dir` pref wasn't available) — in that case, copy the file into
`config.linked_attachments` yourself before trusting the link long-term.

### import-doi output

```json
{"status": "imported", "doi": "...", "title": "...", "item_type": "journalArticle", "key": "ABCD1234"}
{"status": "already_in_library", "doi": "...", "key": "ABCD1234"}
```

Use the `key` field for subsequent `check-pdf` calls.

### Fixing garbled equations or missing figures

Default conversion uses pymupdf4llm, which has no semantic understanding of PDF math fonts
and drops figures entirely. Two options, in order of preference:

**1. Look at it yourself first (usually cheapest).** Figures/equations extracted as raster
images during conversion live under `images/<paper-stem>/` next to the paper's markdown
file (see `markdown_path` from `get_item_context`/`get_fulltext_chunk`) -- some papers embed
their equations as images already, in which case one already exists and you can just read it
directly. If it doesn't, or you need a specific page, render that one page from the source
PDF (`source_path`) to an image yourself and read it -- you have vision, no OCR pipeline
needed for a single equation lookup mid-conversation.

**2. `reconvert_with_math_ocr(attachment_key)` -- only for a real, reusable index fix.**
This runs marker-pdf on the *entire* document (all pages, not just math-flagged ones) and
persists the result to the markdown file + search index, so it's worth it when you want the
improved text to stick around for future searches, not just to answer one question right now.
Measured cost on one deployment: ~27s/page (e.g. 36 pages -> ~16 minutes), GPU-bound -- your
own hardware will differ, so treat this as a rough order of magnitude rather than a guarantee.
It can stall indefinitely if the GPU is under contention from other work at the same time --
ask before running if you're not sure the GPU is free. It overwrites the paper's markdown file
in place; call `get_fulltext_chunk` again afterward to see the improved text.

**Do not use this for bulk/batch reconversion.** Call `coverage_report()` first and check the
`has_math` count against the per-page rate above -- for any library-sized batch this adds up to
many hours or days of continuous GPU time, which is out of scope for this tool regardless of the
exact count. Reconversion here is strictly just-in-time, one paper at a time, triggered by an
actual need in the current conversation. A dedicated bulk run (if ever wanted) would happen
out-of-band on different hardware, not through this tool.

### Notes

- `import-doi` requires Zotero to be running (uses the connector on port 23119). For arXiv
  DOIs (`10.48550/arXiv.*`) metadata comes from DataCite; for journals from CrossRef.
- `mcp__zotero__*` tools require the Zotero connector to be responding.
  Use `ensure_zotero_running()` to check. If the connector is down, all `mcp__zotero__*`
  calls will fail — but `zotero-fulltext` tools remain fully operational.
- Better BibTeX is only needed for `export_bibtex_entries_by_key`; `import-doi` no longer
  requires it.
- For a genuinely DOI-less item (old books, unindexed items) there's no CLI command yet —
  create it directly via debug-bridge JS: `new Zotero.Item('book')` (or other type),
  `item.setField(...)`, `item.setCreators([...])`, `item.libraryID = Zotero.Libraries.userLibraryID`,
  `await item.saveTx()`. Verify the title/authors against OpenAlex or another authoritative
  source first — don't guess metadata.
- **Never link a PDF from an external project folder (e.g. a sibling git repo) via a raw
  `Zotero.Attachments.linkFromFile()` call — use `link-pdf` instead.** The ZotMoov plugin
  normally relocates linked files into the managed `linked_attachments` folder automatically,
  but it only hooks Zotero's UI-driven "link to file" action — it does NOT fire for a bare
  `linkFromFile()` call made via debug-bridge JS. `link-pdf` closes this gap by calling
  ZotMoov's own move logic (`Zotero.ZotMoov.Menus._zotmoov.move(...)`) right after linking, so
  the file is always copied and renamed into `config.linked_attachments` before the workflow
  continues. Skipping straight to `linkFromFile()` and leaving the file at its original path is
  how a prior session's imports ended up as dangling attachments once that external folder was
  later cleaned up.
""",
    )
    zotero_exe = args.zotero_exe

    @mcp.tool()
    def ensure_zotero_running(wait_seconds: int = 15) -> dict[str, object]:
        """Ensure Zotero is running and report connector health."""
        return ensure_runtime_zotero(zotero_exe=zotero_exe, wait_seconds=wait_seconds).to_dict()

    @mcp.tool()
    def search_fulltext(query: str, limit: int = 10) -> list[dict[str, object]]:
        """Search converted Zotero full text and return bounded snippets."""
        return [result.to_dict() for result in search_fts(db_path, query, limit=limit)]

    @mcp.tool()
    def get_fulltext_chunk(attachment_key: str, max_chars: int = 12000, chunk_index: int | None = None) -> dict[str, object]:
        """Return a bounded full-text chunk for one Zotero attachment key."""
        return get_fulltext(
            db_path,
            attachment_key=attachment_key,
            max_chars=max_chars,
            chunk_index=chunk_index,
        ).to_dict()

    @mcp.tool()
    def get_item_context(parent_key: str | None = None, attachment_key: str | None = None) -> dict[str, object]:
        """Return sidecar metadata for a Zotero parent or attachment key."""
        return get_item_context_fn(db_path, parent_key=parent_key, attachment_key=attachment_key)

    @mcp.tool()
    def coverage_report() -> dict[str, object]:
        """Return sidecar full-text coverage counts."""
        return coverage_report_fn(db_path)

    @mcp.tool()
    def export_bibtex_entries_by_key(
        citation_keys: list[str],
        translator: str = DEFAULT_BBT_TRANSLATOR,
        endpoint: str = DEFAULT_BBT_ENDPOINT,
    ) -> dict[str, object]:
        """Return Better BibTeX/BibLaTeX entries for citation keys without writing files."""
        return export_bibtex_entries(citation_keys, translator=translator, endpoint=endpoint).to_dict()

    @mcp.tool()
    def reconvert_with_math_ocr(attachment_key: str) -> dict[str, object]:
        """Re-extract one paper with marker-pdf for LaTeX-aware equations and figures.

        Synchronous and blocking (tens of seconds to minutes; longer on first use while
        marker downloads ~1GB of model weights). Overwrites the existing markdown file in
        place; front matter records previous_extraction_tool for traceability. Call this
        reactively when get_fulltext_chunk output shows garbled or missing equations for a
        paper -- it does not require any prior math-detection flag.
        """
        from .math_ocr import reconvert_with_marker

        jsonl_path = config.output_root / "index" / "zotero_text_index.jsonl"
        return reconvert_with_marker(
            attachment_key,
            db_path=db_path,
            jsonl_path=jsonl_path,
            fts_db_path=db_path,
        ).to_dict()

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
