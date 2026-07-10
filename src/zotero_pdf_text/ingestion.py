from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .identity import extract_year, normalize_doi, normalize_text


@dataclass(frozen=True)
class ImportCandidate:
    doi: str = ""
    title: str = ""
    authors: str = ""
    year: str = ""
    venue: str = ""
    url: str = ""
    pdf_url: str = ""
    pdf_path: str = ""
    pdf_strategy: str = ""
    metadata_strategy: str = ""
    zotmoov_expected: bool = False
    pdf_management_note: str = ""
    zotero_parent_key: str = ""
    source_query: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ExistingItem:
    zotero_parent_key: str
    title: str
    doi: str
    year: str
    url: str


@dataclass(frozen=True)
class IngestDecision:
    action: str
    reason: str
    candidate: ImportCandidate
    existing_zotero_parent_key: str = ""

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["candidate"] = asdict(self.candidate)
        return data


def load_candidates(path: Path) -> list[ImportCandidate]:
    if not path.exists():
        raise FileNotFoundError(path)
    candidates: list[ImportCandidate] = []
    with path.open("r", encoding="utf-8") as handle:
        first = handle.read(1)
        handle.seek(0)
        if first == "[":
            raw_items = json.load(handle)
            candidates.extend(_candidate_from_dict(item) for item in raw_items)
        else:
            for line in handle:
                if line.strip():
                    candidates.append(_candidate_from_dict(json.loads(line)))
    return candidates


def dry_run_ingest(candidates_path: Path, zotero_sqlite: Path, output: Path | None = None) -> list[IngestDecision]:
    candidates = load_candidates(candidates_path)
    existing = load_existing_items(zotero_sqlite)
    decisions = dedupe_candidates(candidates, existing)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            for decision in decisions:
                handle.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")
    return decisions


def dedupe_candidates(
    candidates: list[ImportCandidate],
    existing_items: list[ExistingItem],
) -> list[IngestDecision]:
    doi_index = {normalize_doi(item.doi): item for item in existing_items if normalize_doi(item.doi)}
    title_year_index: dict[tuple[str, str], ExistingItem] = {}
    title_index: dict[str, ExistingItem] = {}
    for item in existing_items:
        title_norm = normalize_text(item.title)
        if not title_norm:
            continue
        title_index.setdefault(title_norm, item)
        if item.year:
            title_year_index.setdefault((title_norm, item.year), item)

    decisions: list[IngestDecision] = []
    for candidate in candidates:
        candidate_doi = normalize_doi(candidate.doi)
        title_norm = normalize_text(candidate.title)
        year = candidate.year or extract_year(candidate.title)
        if candidate_doi and candidate_doi in doi_index:
            existing = doi_index[candidate_doi]
            decisions.append(
                IngestDecision("skip_existing", "doi_match", candidate, existing.zotero_parent_key)
            )
            continue
        if title_norm and year and (title_norm, year) in title_year_index:
            existing = title_year_index[(title_norm, year)]
            decisions.append(
                IngestDecision("skip_existing", "title_year_match", candidate, existing.zotero_parent_key)
            )
            continue
        if title_norm and title_norm in title_index:
            existing = title_index[title_norm]
            decisions.append(IngestDecision("needs_review", "title_match_year_unclear", candidate, existing.zotero_parent_key))
            continue
        if not candidate_doi and not title_norm:
            decisions.append(IngestDecision("needs_review", "missing_doi_and_title", candidate))
            continue
        decisions.append(IngestDecision("add_candidate", "no_duplicate_found", candidate))
    return decisions


def load_existing_items(db_path: Path) -> list[ExistingItem]:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT i.itemID, i.key
        FROM items i
        JOIN itemTypesCombined it ON it.itemTypeID = i.itemTypeID
        WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
          AND it.typeName != 'attachment'
        """
    ).fetchall()
    item_ids = [int(row["itemID"]) for row in rows]
    fields = _load_fields(cur, item_ids)
    con.close()

    result: list[ExistingItem] = []
    for row in rows:
        item_fields = fields.get(int(row["itemID"]), {})
        result.append(
            ExistingItem(
                zotero_parent_key=row["key"] or "",
                title=item_fields.get("title", ""),
                doi=normalize_doi(item_fields.get("DOI", "")),
                year=extract_year(item_fields.get("date", "")),
                url=item_fields.get("url", ""),
            )
        )
    return result


def ingest_approved(*args: object, **kwargs: object) -> None:
    raise NotImplementedError(
        "ingest-approved is deprecated. Use the guarded write workflow instead: "
        "zotero-write plan, zotero-write validate, then zotero-write apply --approve."
    )


def _candidate_from_dict(data: dict[str, object]) -> ImportCandidate:
    return ImportCandidate(
        doi=str(data.get("doi", "") or ""),
        title=str(data.get("title", "") or ""),
        authors=str(data.get("authors", data.get("creators", "")) or ""),
        year=str(data.get("year", "") or ""),
        venue=str(data.get("venue", "") or ""),
        url=str(data.get("url", "") or ""),
        pdf_url=str(data.get("pdf_url", "") or ""),
        pdf_path=str(data.get("pdf_path", "") or ""),
        pdf_strategy=str(data.get("pdf_strategy", "") or ""),
        metadata_strategy=str(data.get("metadata_strategy", "") or ""),
        zotmoov_expected=bool(data.get("zotmoov_expected", False)),
        pdf_management_note=str(data.get("pdf_management_note", "") or ""),
        zotero_parent_key=str(data.get("zotero_parent_key", "") or ""),
        source_query=str(data.get("source_query", "") or ""),
        reason=str(data.get("reason", "") or ""),
    )


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
