from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .identity import extract_year, normalize_doi


@dataclass(frozen=True)
class AttachmentRecord:
    attachment_item_id: int
    attachment_key: str
    parent_item_id: int | None
    parent_key: str | None
    link_mode: int | None
    content_type: str | None
    zotero_path: str | None
    item_type: str | None
    title: str
    doi: str
    citation_key: str
    year: str
    venue: str
    creators: list[str]
    creator_surnames: list[str]


def find_item_by_doi(doi: str, zotero_sqlite: Path) -> str | None:
    """Return the Zotero parent key for an item with the given DOI, or None if not found.

    Opens the database in immutable read-only mode so that Zotero's WAL write locks
    are bypassed. The tradeoff is that items committed after the last WAL checkpoint
    may not appear; for dedup checks this is acceptable.
    """
    from .identity import normalize_doi
    needle = normalize_doi(doi)
    if not needle:
        return None
    uri = f"file:{zotero_sqlite.as_posix()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT i.key, iv.value AS doi_value
        FROM items i
        JOIN itemData id ON id.itemID = i.itemID
        JOIN itemDataValues iv ON iv.valueID = id.valueID
        JOIN fields f ON f.fieldID = id.fieldID
        WHERE f.fieldName = 'DOI'
          AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """,
    ).fetchall()
    con.close()
    for row in rows:
        if normalize_doi(str(row["doi_value"])) == needle:
            return str(row["key"])
    return None


def check_pdf_attachment(parent_key: str, zotero_sqlite: Path) -> dict[str, object]:
    """Return PDF attachment info for a Zotero item key, reading directly from SQLite."""
    uri = f"file:{Path(zotero_sqlite).as_posix()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT ai.key AS attachment_key,
               ia.path AS path,
               ia.contentType AS content_type
        FROM items pi
        JOIN itemAttachments ia ON ia.parentItemID = pi.itemID
        JOIN items ai ON ai.itemID = ia.itemID
        WHERE pi.key = ?
          AND (
            lower(coalesce(ia.contentType, '')) = 'application/pdf'
            OR lower(coalesce(ia.path, '')) LIKE '%.pdf'
          )
        """,
        (parent_key,),
    ).fetchall()
    con.close()
    attachments = [
        {"key": row["attachment_key"], "path": row["path"] or "", "content_type": row["content_type"] or ""}
        for row in rows
    ]
    return {"parent_key": parent_key, "found": len(attachments) > 0, "attachments": attachments}


def snapshot_database(source: Path, run_dir: Path) -> Path:
    destination = run_dir / "zotero.sqlite"
    shutil.copy2(source, destination)
    return destination


def load_attachment_records(db_path: Path) -> list[AttachmentRecord]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT
            ia.itemID AS attachment_item_id,
            ai.key AS attachment_key,
            ia.parentItemID AS parent_item_id,
            pi.key AS parent_key,
            ia.linkMode AS link_mode,
            ia.contentType AS content_type,
            ia.path AS zotero_path,
            it.typeName AS item_type
        FROM itemAttachments ia
        JOIN items ai ON ai.itemID = ia.itemID
        LEFT JOIN items pi ON pi.itemID = ia.parentItemID
        LEFT JOIN itemTypesCombined it ON it.itemTypeID = pi.itemTypeID
        WHERE (
            lower(coalesce(ia.contentType, '')) IN ('application/pdf', 'application/epub+zip')
            OR lower(coalesce(ia.path, '')) LIKE '%.pdf'
            OR lower(coalesce(ia.path, '')) LIKE '%.epub'
        )
          AND ai.itemID NOT IN (SELECT itemID FROM deletedItems)
          AND (ia.parentItemID IS NULL OR ia.parentItemID NOT IN (SELECT itemID FROM deletedItems))
        """
    ).fetchall()

    parent_ids = sorted({row["parent_item_id"] for row in rows if row["parent_item_id"] is not None})
    fields = _load_fields(cur, parent_ids)
    creators = _load_creators(cur, parent_ids)
    con.close()

    records: list[AttachmentRecord] = []
    for row in rows:
        parent_id = row["parent_item_id"]
        item_fields = fields.get(parent_id, {})
        item_creators = creators.get(parent_id, [])
        surnames = [creator.split()[-1] for creator in item_creators if creator.split()]
        date = item_fields.get("date", "")
        records.append(
            AttachmentRecord(
                attachment_item_id=int(row["attachment_item_id"]),
                attachment_key=row["attachment_key"] or "",
                parent_item_id=parent_id,
                parent_key=row["parent_key"],
                link_mode=row["link_mode"],
                content_type=row["content_type"],
                zotero_path=row["zotero_path"],
                item_type=row["item_type"],
                title=item_fields.get("title", ""),
                doi=normalize_doi(item_fields.get("DOI", "")),
                citation_key=_citation_key(item_fields),
                year=extract_year(date),
                venue=item_fields.get("publicationTitle", "")
                or item_fields.get("conferenceName", "")
                or item_fields.get("publisher", ""),
                creators=item_creators,
                creator_surnames=surnames,
            )
        )
    return records


def _load_fields(cur: sqlite3.Cursor, item_ids: list[int]) -> dict[int, dict[str, str]]:
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = cur.execute(
        f"""
        SELECT id.itemID, f.fieldName, v.value
        FROM itemData id
        JOIN fieldsCombined f ON f.fieldID = id.fieldID
        JOIN itemDataValues v ON v.valueID = id.valueID
        WHERE id.itemID IN ({placeholders})
        """,
        item_ids,
    ).fetchall()
    result: dict[int, dict[str, str]] = {}
    for row in rows:
        result.setdefault(int(row["itemID"]), {})[row["fieldName"]] = row["value"] or ""
    return result


def _citation_key(item_fields: dict[str, str]) -> str:
    citation_key = (item_fields.get("citationKey") or "").strip()
    if citation_key:
        return citation_key
    extra = item_fields.get("extra") or ""
    match = re.search(r"(?im)^\s*Citation Key:\s*(\S+)\s*$", extra)
    return match.group(1).strip() if match else ""


def _load_creators(cur: sqlite3.Cursor, item_ids: list[int]) -> dict[int, list[str]]:
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = cur.execute(
        f"""
        SELECT ic.itemID, c.firstName, c.lastName
        FROM itemCreators ic
        JOIN creators c ON c.creatorID = ic.creatorID
        WHERE ic.itemID IN ({placeholders})
        ORDER BY ic.itemID, ic.orderIndex
        """,
        item_ids,
    ).fetchall()
    result: dict[int, list[str]] = {}
    for row in rows:
        name = " ".join(part for part in [row["firstName"], row["lastName"]] if part).strip()
        if name:
            result.setdefault(int(row["itemID"]), []).append(name)
    return result
