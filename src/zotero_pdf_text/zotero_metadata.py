"""Read the bibliographic fields the index used to drop, from a read-only Zotero snapshot.

`load_parent_metadata` copies `zotero.sqlite` through `snapshot_for_reading` (the same verified,
non-writing path the audit uses) and never touches the original, so it is safe to run while Zotero
is open. `with_zotero_metadata` wraps a staging writer so a rebuild can merge these fields into the
JSONL it is about to index -- no reconversion, no change to Markdown, PDFs or Zotero.

Nothing here is guessed: a field Zotero does not hold stays empty. Collections, `extra` (beyond the
citation-key parse already done elsewhere), notes and annotations are deliberately not read.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._atomic import replace_with_retry
from .zotero_db import snapshot_for_reading

# Scalar index fields -> the Zotero fieldName that feeds them. `venue` has its own fallback chain.
_SCALAR_FIELDS: dict[str, str | tuple[str, ...]] = {
    "abstract": "abstractNote",
    "journal_abbreviation": "journalAbbreviation",
    "volume": "volume",
    "issue": "issue",
    "pages": "pages",
    "date": "date",
    "publisher": ("publisher", "university", "institution", "company", "studio", "network", "label"),
    "place": "place",
    "isbn": "ISBN",
    "issn": "ISSN",
    "url": "url",
    "language": "language",
}
# Zotero's type-specific fields that map to a base field (university, institution -> publisher;
# websiteTitle, blogTitle, ... -> publicationTitle) are listed explicitly, in preference order,
# rather than read from baseFieldMappingsCombined, so a library without that table still works.
_VENUE_FIELDS = (
    "publicationTitle",
    "proceedingsTitle",
    "bookTitle",
    "conferenceName",
    "websiteTitle",
    "blogTitle",
    "forumTitle",
    "encyclopediaTitle",
    "dictionaryTitle",
    "programTitle",
    "publisher",
    "university",
    "institution",
)

# Every key this module adds to a JSONL record. Readers treat all of them as optional.
ZOTERO_METADATA_KEYS: tuple[str, ...] = (
    "item_type",
    "venue",
    *_SCALAR_FIELDS,
    "tags",
    "creators_structured",
)

_ID_BATCH = 500


@dataclass
class MetadataMergeStats:
    matched: int = 0
    unmatched: int = 0


def load_parent_metadata(zotero_sqlite: Path) -> dict[str, dict[str, Any]]:
    """Return {parent_key: fields} for every non-deleted top-level item in the library."""
    with snapshot_for_reading(Path(zotero_sqlite)) as snapshot:
        con = sqlite3.connect(snapshot)
        con.row_factory = sqlite3.Row
        try:
            return _load(con)
        finally:
            con.close()


def _load(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    items = con.execute(
        """
        SELECT i.itemID AS item_id, i.key AS key, it.typeName AS item_type
        FROM items i
        LEFT JOIN itemTypesCombined it ON it.itemTypeID = i.itemTypeID
        WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
          AND (it.typeName IS NULL OR it.typeName NOT IN ('attachment', 'note', 'annotation'))
        """
    ).fetchall()
    ids = [int(row["item_id"]) for row in items]
    fields: dict[int, dict[str, str]] = {}
    creators: dict[int, list[dict[str, Any]]] = {}
    tags: dict[int, list[dict[str, Any]]] = {}
    for start in range(0, len(ids), _ID_BATCH):
        batch = ids[start : start + _ID_BATCH]
        marks = ",".join("?" for _ in batch)
        for row in con.execute(
            f"""
            SELECT d.itemID AS item_id, f.fieldName AS field, v.value AS value
            FROM itemData d
            JOIN fieldsCombined f ON f.fieldID = d.fieldID
            JOIN itemDataValues v ON v.valueID = d.valueID
            WHERE d.itemID IN ({marks})
            """,
            batch,
        ):
            fields.setdefault(int(row["item_id"]), {})[row["field"]] = str(row["value"] or "")
        for row in con.execute(
            f"""
            SELECT ic.itemID AS item_id, ct.creatorType AS role, c.firstName AS first,
                   c.lastName AS last, ic.orderIndex AS pos
            FROM itemCreators ic
            JOIN creators c ON c.creatorID = ic.creatorID
            LEFT JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
            WHERE ic.itemID IN ({marks})
            ORDER BY ic.itemID, ic.orderIndex
            """,
            batch,
        ):
            creators.setdefault(int(row["item_id"]), []).append(
                {
                    "role": row["role"] or "",
                    "first": row["first"] or "",
                    "last": row["last"] or "",
                    "order": int(row["pos"] or 0),
                }
            )
        for row in con.execute(
            f"""
            SELECT it.itemID AS item_id, t.name AS name, it.type AS type
            FROM itemTags it
            JOIN tags t ON t.tagID = it.tagID
            WHERE it.itemID IN ({marks})
            ORDER BY it.itemID, t.name
            """,
            batch,
        ):
            tags.setdefault(int(row["item_id"]), []).append(
                {"name": row["name"] or "", "type": "automatic" if row["type"] == 1 else "manual"}
            )

    result: dict[str, dict[str, Any]] = {}
    for row in items:
        item_id = int(row["item_id"])
        item_fields = fields.get(item_id, {})
        record: dict[str, Any] = {"item_type": row["item_type"] or ""}
        record["venue"] = next((item_fields[f] for f in _VENUE_FIELDS if item_fields.get(f)), "")
        for key, names in _SCALAR_FIELDS.items():
            candidates = (names,) if isinstance(names, str) else names
            record[key] = next((item_fields[n] for n in candidates if item_fields.get(n)), "")
        record["tags"] = tags.get(item_id, [])
        record["creators_structured"] = creators.get(item_id, [])
        result[str(row["key"])] = record
    return result


def merge_zotero_metadata(record: dict[str, Any], metadata: dict[str, Any] | None) -> bool:
    """Overlay `metadata` onto one JSONL record in place. Returns False when there was none."""
    if metadata is None:
        return False
    for key in ZOTERO_METADATA_KEYS:
        record[key] = metadata.get(key, [] if key in ("tags", "creators_structured") else "")
    return True


def with_zotero_metadata(
    writer: Callable[[Path], None], zotero_sqlite: Path
) -> tuple[Callable[[Path], None], MetadataMergeStats]:
    """Wrap a `stage_generation` writer so the staged JSONL also carries current Zotero fields.

    The snapshot is read when the writer runs (inside the generation staging, under the pipeline
    lock) and a failure there aborts the staging, so the published generation is never replaced
    by a half-enriched one. A record whose parent Zotero no longer lists keeps whatever fields it
    already had.
    """
    stats = MetadataMergeStats()

    def _write(jsonl_path: Path) -> None:
        writer(jsonl_path)
        metadata = load_parent_metadata(zotero_sqlite)
        staged = jsonl_path.with_name(jsonl_path.name + ".enriched")
        try:
            with jsonl_path.open("r", encoding="utf-8") as source, staged.open(
                "w", encoding="utf-8", newline="\n"
            ) as target:
                for line in source:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if isinstance(record, dict):
                        if merge_zotero_metadata(record, metadata.get(str(record.get("zotero_parent_key") or ""))):
                            stats.matched += 1
                        else:
                            stats.unmatched += 1
                        target.write(json.dumps(record, ensure_ascii=False) + "\n")
                    else:
                        target.write(line.rstrip("\n") + "\n")
            replace_with_retry(staged, jsonl_path)
        finally:
            staged.unlink(missing_ok=True)

    return _write, stats
