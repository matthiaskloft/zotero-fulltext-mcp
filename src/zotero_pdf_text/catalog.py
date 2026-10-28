"""Human-facing guides to an output root: ``library/catalog.*`` and the top-level ``README.md``.

Library folders are named by Zotero attachment key, which keeps paths stable but says nothing
about which paper is which. The catalog lists every indexed paper by citation key, year, authors
and title, with a link *relative to the catalog itself*, so it opens the right file on any
machine the output root is synced to -- the absolute paths the index records name only the
machine that converted the paper.

Both files are derived from the published index and rewritten after every publication. Nothing
reads them back.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from urllib.parse import quote

from ._atomic import atomic_write_text

CATALOG_CSV_FILENAME = "catalog.csv"
CATALOG_MD_FILENAME = "catalog.md"
README_FILENAME = "README.md"

_COLUMNS = ("citation_key", "year", "creators", "title", "doi", "attachment_key", "file")

README_TEXT = """\
# Converted Zotero full text

Written by zotero-fulltext-mcp. Everything here is derived from your Zotero library and can be
rebuilt from it; nothing in this folder is read back into Zotero.

| Folder | What it holds | Edit by hand? |
|---|---|---|
| `library/` | One folder per Zotero PDF attachment. Initial conversions use `<key>/<key>.md` and `<key>/images/`; accepted replacements use `<key>/versions/<generation>/` with Markdown and images together. Published revisions are retained. Start with `library/catalog.md` (clickable) or `library/catalog.csv` (spreadsheet) to find the current conversion. | No |
| `index/` | The search index. `current.json` names the live version in `versions/`; the previous version is kept for rollback. | No |
| `runs/` | One folder per command run, named `<date>_<time>_<command>`: logs, manifests, mapping reports and plans. Safe to delete old ones once nothing refers to them. | No |
| `review/` | Lists waiting for your decision: PDFs whose conversion timed out, PDFs with no Zotero item, and the extraction skip list. Change them through the `zotero-pdf-text` commands. | Through commands |

Never rename, move or symlink folders in here. The index records where each file is, and a
renamed folder disconnects search from the text.
"""


def write_output_guides(output_root: Path, index_jsonl: Path) -> None:
    """Rewrite the catalog from ``index_jsonl`` and the README; unchanged files are left alone."""
    library_dir = output_root / "library"
    rows = sorted(_catalog_rows(index_jsonl, library_dir), key=_sort_key)
    library_dir.mkdir(parents=True, exist_ok=True)
    _write_if_changed(library_dir / CATALOG_CSV_FILENAME, _render_csv(rows))
    _write_if_changed(library_dir / CATALOG_MD_FILENAME, _render_markdown(rows))
    _write_if_changed(output_root / README_FILENAME, README_TEXT)


def _catalog_rows(index_jsonl: Path, library_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with index_jsonl.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            rows.append(
                {
                    "citation_key": str(record.get("citation_key") or ""),
                    "year": str(record.get("year") or ""),
                    "creators": str(record.get("creators") or ""),
                    "title": str(record.get("title") or ""),
                    "doi": str(record.get("doi") or ""),
                    "attachment_key": str(record.get("zotero_attachment_key") or ""),
                    "file": _relative_link(str(record.get("markdown_path") or ""), library_dir),
                }
            )
    return rows


def _relative_link(markdown_path: str, library_dir: Path) -> str:
    """The Markdown file's path from ``library/``, with forward slashes so it works everywhere.

    A file on another drive has no relative form; it keeps its absolute path.
    """
    if not markdown_path:
        return ""
    try:
        return Path(os.path.relpath(Path(markdown_path).resolve(), library_dir.resolve())).as_posix()
    except ValueError:
        return Path(markdown_path).as_posix()


def _sort_key(row: dict[str, str]) -> tuple[str, str]:
    return (row["citation_key"] or row["title"]).casefold(), row["attachment_key"]


def _render_csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    # A byte-order mark so Excel opens umlauts and other non-ASCII titles correctly.
    return "﻿" + buffer.getvalue()


def _render_markdown(rows: list[dict[str, str]]) -> str:
    lines = [
        "# Library catalog",
        "",
        f"{len(rows)} papers. Links are relative to this file, so they work wherever the folder is synced.",
        "",
        "| Citation key | Year | Authors | Title |",
        "|---|---|---|---|",
    ]
    for row in rows:
        title = _cell(row["title"]) or row["attachment_key"]
        # An absolute fallback (another drive) is no portable link; show the title alone.
        relative = row["file"] and not Path(row["file"]).is_absolute() and ":" not in row["file"]
        link = f"[{title}]({quote(row['file'])})" if relative else title
        lines.append(f"| {_cell(row['citation_key'])} | {_cell(row['year'])} | {_cell(row['creators'])} | {link} |")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def _write_if_changed(path: Path, content: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    atomic_write_text(path, content)
