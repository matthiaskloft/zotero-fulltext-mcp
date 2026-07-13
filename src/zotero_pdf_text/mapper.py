from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import ProjectConfig
from .identity import classify_identity, normalize_doi, normalize_text, safe_folder_id
from .pdf_probe import extract_early_text
from .zotero_db import AttachmentRecord, load_attachment_records, snapshot_database

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - exercised only if dependency is missing
    fuzz = None


@dataclass
class SourceFile:
    path: Path
    suffix: str
    size: int
    modified: str
    sha256: str


@dataclass
class MappingRow:
    classification: str
    source_kind: str
    source_path: str
    source_name: str
    source_size: int
    source_mtime: str
    sha256: str
    logical_id: str
    safe_folder_id: str
    zotero_parent_key: str
    zotero_attachment_key: str
    item_type: str
    title: str
    creators: str
    year: str
    doi: str
    citation_key: str
    venue: str
    content_type: str
    zotero_path: str
    mapping_method: str
    candidate_count: int
    metadata_match_score: int
    identity_status: str
    identity_rule: str
    page_count: int
    title_score: int
    author_evidence: bool
    year_evidence: bool
    observed_dois: str
    error: str


def run_dry_run(config: ProjectConfig) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = config.output_root / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)
    _configure_logging(run_dir / "run.log")
    logging.info("Starting dry-run mapper")
    logging.info("Linked attachments: %s", config.linked_attachments)

    db_snapshot = snapshot_database(config.zotero_sqlite, run_dir)
    records = load_attachment_records(db_snapshot)
    pdf_sources = _list_sources(config.linked_attachments, ".pdf")
    pdf_sources = _add_absolute_record_sources(pdf_sources, records, ".pdf")
    epub_sources = _list_sources(config.linked_attachments, ".epub")
    logging.info("Found %s PDFs and %s EPUBs", len(pdf_sources), len(epub_sources))

    rows = build_mapping_rows(config, records, pdf_sources, epub_sources)
    _write_reports(run_dir, rows, config, records)
    logging.info("Finished dry-run mapper")
    return run_dir


def build_mapping_rows(
    config: ProjectConfig,
    records: list[AttachmentRecord],
    pdf_sources: list[SourceFile],
    epub_sources: list[SourceFile],
) -> list[MappingRow]:
    source_by_norm = {_norm_path(source.path): source for source in pdf_sources}
    basename_index: dict[str, list[SourceFile]] = {}
    for source in pdf_sources:
        basename_index.setdefault(source.path.name.casefold(), []).append(source)

    candidates_by_source: dict[Path, list[tuple[AttachmentRecord, str]]] = {}
    for record in records:
        if not _is_pdf_record(record):
            continue
        for path, method in _candidate_paths(record, config.linked_attachments):
            source = source_by_norm.get(_norm_path(path))
            if source is not None:
                candidates_by_source.setdefault(source.path, []).append((record, method))
                break
        else:
            basename = _record_basename(record)
            matches = basename_index.get(basename.casefold(), []) if basename else []
            if len(matches) == 1:
                candidates_by_source.setdefault(matches[0].path, []).append((record, "basename_fallback"))

    rows: list[MappingRow] = []
    for source in sorted(pdf_sources, key=lambda item: item.path.name.casefold()):
        candidates = candidates_by_source.get(source.path, [])
        if not candidates:
            metadata_candidates = _metadata_candidates(source, records)
            if metadata_candidates:
                record, score, count = metadata_candidates
                rows.append(_metadata_candidate_row(source, record, score, count, config))
            else:
                rows.append(_orphan_row(source))
            continue
        record, method = _choose_candidate(candidates)
        rows.append(_mapped_row(source, record, method, len(candidates), config))

    for source in sorted(epub_sources, key=lambda item: item.path.name.casefold()):
        rows.append(_unsupported_row(source))

    return rows


def _list_sources(root: Path, suffix: str) -> list[SourceFile]:
    sources: list[SourceFile] = []
    for path in root.rglob(f"*{suffix}"):
        if not path.is_file() or "_maintenance" in path.parts:
            continue
        source = _source_file(path, suffix)
        if source is not None:
            sources.append(source)
    return sources


def _add_absolute_record_sources(
    sources: list[SourceFile],
    records: list[AttachmentRecord],
    suffix: str,
) -> list[SourceFile]:
    seen = {_norm_path(source.path) for source in sources}
    combined = list(sources)
    for record in records:
        if not _is_pdf_record(record):
            continue
        for path, method in _candidate_paths(record, Path()):
            if method != "absolute_path" or path.suffix.casefold() != suffix:
                continue
            norm = _norm_path(path)
            if norm in seen:
                continue
            source = _source_file(path, suffix)
            if source is None:
                logging.warning("Skipping unreadable Zotero linked file: %s", path)
                continue
            combined.append(source)
            seen.add(norm)
    return combined


def _source_file(path: Path, suffix: str) -> SourceFile | None:
    try:
        if not path.is_file() or "_maintenance" in path.parts:
            return None
        stat = path.stat()
        return SourceFile(
            path=path,
            suffix=suffix,
            size=int(stat.st_size),
            modified=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            sha256=_sha256(path),
        )
    except OSError as exc:
        logging.warning("Unable to read source file %s: %s", path, exc)
        return None


def _mapped_row(
    source: SourceFile,
    record: AttachmentRecord,
    method: str,
    candidate_count: int,
    config: ProjectConfig,
) -> MappingRow:
    logical_id = _logical_id(record, source)
    try:
        text, page_count = extract_early_text(
            source.path,
            pages=config.early_pages,
            max_page_chars=config.max_page_chars,
        )
        evidence = classify_identity(
            title=record.title,
            doi=record.doi,
            year=record.year,
            author_surnames=record.creator_surnames,
            item_type=record.item_type,
            text=text,
        )
        manually_accepted = _is_manually_accepted(record, source, config)
        if evidence.status == "verified" or manually_accepted:
            classification = "mapped_verified"
        elif evidence.status == "possible_mismatch":
            classification = "possible_mismatch"
        else:
            classification = "mapped_unverified"
        return _row(
            classification=classification,
            source=source,
            logical_id=logical_id,
            record=record,
            mapping_method=method,
            candidate_count=candidate_count,
            identity_status="manual_accepted" if manually_accepted else evidence.status,
            identity_rule=_manual_identity_rule(evidence.rule) if manually_accepted else evidence.rule,
            page_count=page_count,
            title_score=evidence.title_score,
            author_evidence=evidence.author_evidence,
            year_evidence=evidence.year_evidence,
            observed_dois=";".join(evidence.observed_dois),
        )
    except Exception as exc:
        return _row(
            classification="error",
            source=source,
            logical_id=logical_id,
            record=record,
            mapping_method=method,
            candidate_count=candidate_count,
            identity_status="error",
            identity_rule="pdf_probe_failed",
            error=f"{type(exc).__name__}: {exc}",
        )


def _metadata_candidate_row(
    source: SourceFile,
    record: AttachmentRecord,
    metadata_match_score: int,
    candidate_count: int,
    config: ProjectConfig,
) -> MappingRow:
    logical_id = _logical_id(record, source)
    try:
        text, page_count = extract_early_text(
            source.path,
            pages=config.early_pages,
            max_page_chars=config.max_page_chars,
        )
        evidence = classify_identity(
            title=record.title,
            doi=record.doi,
            year=record.year,
            author_surnames=record.creator_surnames,
            item_type=record.item_type,
            text=text,
        )
        manually_accepted = _is_manually_accepted(record, source, config)
        if manually_accepted:
            classification = "mapped_verified"
        elif evidence.status == "possible_mismatch":
            classification = "possible_mismatch"
        else:
            classification = "mapped_unverified"
        return _row(
            classification=classification,
            source=source,
            logical_id=logical_id,
            record=record,
            mapping_method="filename_metadata_candidate",
            candidate_count=candidate_count,
            metadata_match_score=metadata_match_score,
            identity_status="manual_accepted" if manually_accepted else "candidate",
            identity_rule=_manual_identity_rule(f"metadata_candidate_{evidence.rule}")
            if manually_accepted
            else f"metadata_candidate_{evidence.rule}",
            page_count=page_count,
            title_score=evidence.title_score,
            author_evidence=evidence.author_evidence,
            year_evidence=evidence.year_evidence,
            observed_dois=";".join(evidence.observed_dois),
        )
    except Exception as exc:
        return _row(
            classification="error",
            source=source,
            logical_id=logical_id,
            record=record,
            mapping_method="filename_metadata_candidate",
            candidate_count=candidate_count,
            metadata_match_score=metadata_match_score,
            identity_status="error",
            identity_rule="pdf_probe_failed",
            error=f"{type(exc).__name__}: {exc}",
        )


def _row(
    *,
    classification: str,
    source: SourceFile,
    logical_id: str,
    record: AttachmentRecord | None = None,
    source_kind: str = "source_pdf",
    mapping_method: str = "",
    candidate_count: int = 0,
    metadata_match_score: int = 0,
    identity_status: str = "",
    identity_rule: str = "",
    page_count: int = 0,
    title_score: int = 0,
    author_evidence: bool = False,
    year_evidence: bool = False,
    observed_dois: str = "",
    error: str = "",
) -> MappingRow:
    return MappingRow(
        classification=classification,
        source_kind=source_kind,
        source_path=str(source.path),
        source_name=source.path.name,
        source_size=source.size,
        source_mtime=source.modified,
        sha256=source.sha256,
        logical_id=logical_id,
        safe_folder_id=safe_folder_id(logical_id),
        zotero_parent_key=(record.parent_key if record else "") or "",
        zotero_attachment_key=(record.attachment_key if record else "") or "",
        item_type=(record.item_type if record else "") or "",
        title=(record.title if record else "") or "",
        creators="; ".join(record.creators) if record else "",
        year=(record.year if record else "") or "",
        doi=(record.doi if record else "") or "",
        citation_key=(record.citation_key if record else "") or "",
        venue=(record.venue if record else "") or "",
        content_type=(record.content_type if record else "") or "",
        zotero_path=(record.zotero_path if record else "") or "",
        mapping_method=mapping_method,
        candidate_count=candidate_count,
        metadata_match_score=metadata_match_score,
        identity_status=identity_status,
        identity_rule=identity_rule,
        page_count=page_count,
        title_score=title_score,
        author_evidence=author_evidence,
        year_evidence=year_evidence,
        observed_dois=observed_dois,
        error=error,
    )


def _orphan_row(source: SourceFile) -> MappingRow:
    return _row(classification="orphan_pdf", source=source, logical_id=f"sha256:{source.sha256}")


def _unsupported_row(source: SourceFile) -> MappingRow:
    return _row(
        classification="unsupported",
        source=source,
        source_kind="unsupported_epub",
        logical_id=f"sha256:{source.sha256}",
        identity_status="unsupported",
        identity_rule="epub_excluded_v1",
    )


def _is_manually_accepted(record: AttachmentRecord, source: SourceFile, config: ProjectConfig) -> bool:
    if record.attachment_key in config.manually_accepted_attachment_keys:
        return True
    return (record.attachment_key, source.path.name) in config.manually_accepted_mappings


def _manual_identity_rule(rule: str) -> str:
    return f"manual_accept:{rule}"


def _logical_id(record: AttachmentRecord, source: SourceFile) -> str:
    doi = normalize_doi(record.doi)
    if doi:
        return f"doi:{doi}"
    if record.parent_key:
        return f"zotero:{record.parent_key}"
    return f"sha256:{source.sha256}"


def _choose_candidate(candidates: list[tuple[AttachmentRecord, str]]) -> tuple[AttachmentRecord, str]:
    priority = {"zotero_attachments_path": 0, "absolute_path": 1, "basename_fallback": 2}
    return sorted(candidates, key=lambda item: priority.get(item[1], 9))[0]


def _metadata_candidates(source: SourceFile, records: list[AttachmentRecord]) -> tuple[AttachmentRecord, int, int] | None:
    scored: list[tuple[int, bool, bool, AttachmentRecord]] = []
    filename = normalize_text(source.path.stem)
    if not filename:
        return None

    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not _is_pdf_record(record) or not record.title:
            continue
        identity = (record.parent_key or "", normalize_doi(record.doi), normalize_text(record.title))
        if identity in seen:
            continue
        seen.add(identity)

        title = normalize_text(record.title)
        if not title:
            continue
        score = _filename_title_score(filename, title)
        author_hit = any(normalize_text(name) in filename for name in record.creator_surnames if name)
        year_hit = bool(record.year and record.year in source.path.stem)

        if score >= 97 or (score >= 90 and (author_hit or year_hit)):
            scored.append((score, author_hit, year_hit, record))

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    top_score, _, _, top_record = scored[0]
    if len(scored) > 1 and scored[1][0] >= top_score - 2:
        return None
    return top_record, top_score, len(scored)


def _filename_title_score(filename: str, title: str) -> int:
    if not filename or not title:
        return 0
    if fuzz is not None:
        return int(fuzz.token_set_ratio(filename, title))
    title_words = set(title.split())
    filename_words = set(filename.split())
    if not title_words:
        return 0
    return int(100 * len(title_words & filename_words) / len(title_words))


def _candidate_paths(record: AttachmentRecord, linked_root: Path) -> list[tuple[Path, str]]:
    zotero_path = record.zotero_path or ""
    if zotero_path.startswith("attachments:"):
        relative = zotero_path.split(":", 1)[1].replace("/", os.sep).replace("\\", os.sep)
        return [(linked_root / relative, "zotero_attachments_path")]
    # zotero_path reflects whatever OS Zotero itself ran on when the attachment was linked, not
    # the OS this tool happens to run on -- recognize both a Windows drive-letter path and a
    # POSIX absolute path as "absolute_path" regardless of the current platform.
    if re.match(r"^[A-Za-z]:[\\/]", zotero_path) or zotero_path.startswith("/"):
        return [(Path(zotero_path), "absolute_path")]
    return []


def _record_basename(record: AttachmentRecord) -> str:
    zotero_path = record.zotero_path or ""
    if zotero_path.startswith("attachments:") or zotero_path.startswith("storage:"):
        return Path(zotero_path.split(":", 1)[1]).name
    return Path(zotero_path).name if zotero_path else ""


def _is_pdf_record(record: AttachmentRecord) -> bool:
    return (record.content_type or "").casefold() == "application/pdf" or _record_basename(record).casefold().endswith(".pdf")


def _norm_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_reports(
    run_dir: Path,
    rows: list[MappingRow],
    config: ProjectConfig,
    records: list[AttachmentRecord],
) -> None:
    csv_path = run_dir / "mapping_report.csv"
    jsonl_path = run_dir / "mapping_report.jsonl"
    fieldnames = list(asdict(rows[0]).keys()) if rows else [field.name for field in MappingRow.__dataclass_fields__.values()]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
    _write_summary(run_dir / "summary.md", rows, config, records)


def _write_summary(path: Path, rows: list[MappingRow], config: ProjectConfig, records: list[AttachmentRecord]) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.classification] = counts.get(row.classification, 0) + 1
    pdf_count = sum(1 for row in rows if row.source_kind == "source_pdf")
    epub_count = sum(1 for row in rows if row.source_kind == "unsupported_epub")
    lines = [
        "# Zotero PDF Dry-Run Summary",
        "",
        f"- Run created: {datetime.now().isoformat(timespec='seconds')}",
        f"- Linked attachments: `{config.linked_attachments}`",
        f"- Output run folder: `{path.parent}`",
        f"- Source PDFs seen: {pdf_count}",
        f"- EPUBs excluded as unsupported: {epub_count}",
        f"- Zotero attachment records inspected: {len(records)}",
        "",
        "## Classification Counts",
        "",
    ]
    for name in sorted(counts):
        lines.append(f"- `{name}`: {counts[name]}")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Source PDFs were opened read-only for hashing and early text probing.",
            "- The live Zotero database was copied to this run folder before querying.",
            "- No Zotero paths or attachment files were modified by the mapper.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _configure_logging(path: Path) -> None:
    logging.basicConfig(
        filename=path,
        filemode="w",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
