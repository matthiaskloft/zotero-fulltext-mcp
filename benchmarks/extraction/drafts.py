"""Opt-in host step: page checks on cached corpus PDFs and DRAFT expectation proposals.

Host-only and benchmark-side: not shipped, no Zotero access, no network. PDFs must already be in
the local cache and match the SHA-256 pinned in the manifest; a missing or mismatching file is
reported, never skipped silently and never downloaded. Output goes to a directory outside any git
checkout (page images and text-layer text are copyrighted material and must not be committed).

Drafts are ``expectation`` records with ``review.status == "draft"`` and ``proposed_by``
provenance. They are NOT ground truth: ``load_reviewed_expectation`` refuses them, and
``pilot_records.load_pilot_expectation`` additionally refuses a record that still carries the draft
wording. By default a draft holds page geometry and render identity only (no blocks), so the
reviewer is not anchored by proposals; ``--with-text-proposals`` adds the PDF's own text-layer
blocks (typed ``other``, high uncertainty). Nothing here is extractor or model output.

The page checks below are crude heuristics. A page that passes is a *candidate* that still needs
a human look, never a confirmed layout; a failed check means the heuristic failed, which says
little about the page itself.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf

from .pilot import (
    DRAFT_NOTE,
    MANIFEST_PATH,
    PIN_PATH,
    PROPOSAL_BLOCK_NOTE,
    REPO_ROOT,
    check_manifest,
)
from .page_checks import (  # noqa: F401 - re-exported for callers and tests
    CachedPageChecks,
    PdfProblem,
    choose_pages,
    geometry_refusal,
    page_support,
    text_blocks,
    verify_cached,
)
from .records import options_sha256, sha256_bytes, validate_record

TOOL_NAME = "extraction-pilot-drafts"
TOOL_VERSION = "3"
DEFAULT_DPI = 150
DEFAULT_CACHE = REPO_ROOT / "benchmarks" / "public_pdfs" / ".cache"
DEFAULT_OUT = Path.home() / ".cache" / "zotero-fulltext-extraction-pilot"


# --- draft construction ----------------------------------------------------------------------


def _page_entry(meta_page: pymupdf.Page, work_page: pymupdf.Page, image_hash: str, dpi: int) -> dict[str, Any]:
    # Rotation comes from the separately opened document; the working page has had its rotation
    # removed, so its rect, text boxes and render are all in the displayed frame. page.rect is the
    # CropBox clipped to the MediaBox, so "cropbox" is the box actually used.
    label = meta_page.get_label() or None
    return {
        "physical_page": meta_page.number + 1,
        "printed_label": label,
        "geometry": {
            "unit": "pt", "origin": "top-left", "frame": "displayed", "box": "cropbox",
            "rotation": meta_page.rotation, "width": round(work_page.rect.width, 3), "height": round(work_page.rect.height, 3),
        },
        "render": {"renderer": "pymupdf", "renderer_version": pymupdf.VersionBind, "dpi": dpi, "image_sha256": image_hash},
    }


def build_draft(
    doc_entry: dict[str, Any], pdf_path: Path, title: str, pages: list[int] | None, out_images: Path,
    dpi: int = DEFAULT_DPI, text_proposals: bool = False,
) -> dict[str, Any]:
    """Render ``pages`` (all pages when None) and return a draft expectation record."""
    meta = pymupdf.open(pdf_path)
    work = pymupdf.open(pdf_path)
    try:
        page_numbers = pages or list(range(1, meta.page_count + 1))
        for number in page_numbers:
            refusal = geometry_refusal(meta[number - 1])
            if refusal:
                raise ValueError(f"page {number}: {refusal}; refusing to guess its frame")
        entries: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        out_images.mkdir(parents=True, exist_ok=True)
        for number in page_numbers:
            work_page = work[number - 1]
            work_page.remove_rotation()
            png = work_page.get_pixmap(dpi=dpi, alpha=False).tobytes("png")
            (out_images / f"{doc_entry['id']}-p{number:04d}.png").write_bytes(png)
            entries.append(_page_entry(meta[number - 1], work_page, sha256_bytes(png), dpi))
            if text_proposals:
                width, height = work_page.rect.width, work_page.rect.height
                for x0, y0, x1, y1, text in text_blocks(work_page):
                    box = {"x0": max(0.0, x0), "y0": max(0.0, y0), "x1": min(width, x1), "y1": min(height, y1)}
                    if box["x1"] <= box["x0"] or box["y1"] <= box["y0"]:
                        continue
                    order = len(blocks) + 1
                    blocks.append({
                        "id": f"p{number}-b{order}", "type": "other", "page": number, "reading_order": order,
                        "bbox": {k: round(v, 3) for k, v in box.items()}, "text": text.strip(),
                        "uncertainty": {"level": "high", "note": PROPOSAL_BLOCK_NOTE},
                    })
        page_count = meta.page_count
    finally:
        meta.close()
        work.close()
    whole = pages is None
    return {
        "schema_version": 1,
        "record_kind": "expectation",
        "record_id": f"pilot-{doc_entry['id']}" + ("-whole" if whole else ""),
        "document": {"pdf_sha256": doc_entry["pdf_sha256"], "title": title, "page_count": page_count, "corpus_id": doc_entry["id"]},
        "scope": {"kind": "complete_document" if whole else "pages", "physical_pages": page_numbers},
        "pages": entries,
        "blocks": blocks,
        "provenance": {
            "proposed_by": {
                "tool": TOOL_NAME, "version": TOOL_VERSION,
                "options_sha256": options_sha256({"dpi": dpi, "text_proposals": text_proposals, "pymupdf": pymupdf.VersionBind}),
            },
            "review": {"status": "draft"},
            "uncertainty": {"level": "high", "note": DRAFT_NOTE},
        },
    }


# --- orchestration ---------------------------------------------------------------------------


def run(
    manifest: dict[str, Any], cache: Path, out: Path, *, only: list[str] | None = None,
    dpi: int = DEFAULT_DPI, text_proposals: bool = False, titles: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Draft every pilot document from the manifest's candidate pages. Problems are listed, never skipped silently."""
    known = {d["id"] for d in manifest["pilot"]}
    if only and set(only) - known:
        raise ValueError(f"unknown pilot document id(s): {sorted(set(only) - known)}")
    report: dict[str, Any] = {"seed": manifest["seed"], "documents": [], "problems": []}
    titles = titles or {}
    drafts_dir, images_dir = out / "drafts", out / "images"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    checks = CachedPageChecks(cache, manifest["seed"])
    for entry in manifest["pilot"]:
        if only and entry["id"] not in only:
            continue
        item: dict[str, Any] = {"id": entry["id"], "role": entry["role"], "stratum": entry["stratum"], "basis": entry["basis"]}
        report["documents"].append(item)
        status, path, detail = verify_cached(entry, cache)
        item["status"] = status
        if status != "ok":
            item["detail"] = detail
            report["problems"].append(f"{entry['id']}: {status}: {detail}")
            continue
        pages: list[int] | None = None
        if entry["role"] == "stratum_pages":
            pages = list(entry.get("candidate_pages") or [])
            item["pages"] = pages
            if not pages or any(not 1 <= n <= entry["pdf_pages"] for n in pages):
                item["status"] = "no_candidate_pages"
                report["problems"].append(f"{entry['id']}: manifest has no usable candidate pages; rebuild it with --cache")
                continue
            with pymupdf.open(path) as probe:
                failed = [n for n in pages if geometry_refusal(probe[n - 1]) is not None or not page_support(entry["stratum"], probe[n - 1])]
            if failed:
                item["check_failed_pages"] = failed
                report["problems"].append(
                    f"{entry['id']}: check failed on page(s) {failed} for stratum {entry['stratum']!r}; "
                    "the heuristic no longer passes them (library version drift?) - look before reviewing")
        else:
            item["reference_list_found"] = checks.reference_list_found(entry)
            if not item["reference_list_found"]:
                report["problems"].append(f"{entry['id']}: check failed: no reference list found in this whole paper")
        record_id = f"pilot-{entry['id']}" + ("-whole" if pages is None else "")
        target = drafts_dir / f"{record_id}.expectation.draft.json"
        if target.exists() and not overwrite:
            item["status"] = "exists"
            report["problems"].append(f"{entry['id']}: {target.name} already exists; it may hold review edits (use --overwrite to replace it)")
            continue
        try:
            record = build_draft(entry, path, titles.get(entry["id"], entry["id"]), pages, images_dir, dpi, text_proposals)
        except ValueError as error:
            item["status"] = "refused"
            report["problems"].append(f"{entry['id']}: {error}")
            continue
        validate_record(record)
        target.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        item["draft"] = target.name
    name = f"selection_report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    (out / name).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report["report_file"] = name
    return report


def held_out_check(manifest: dict[str, Any], cache: Path) -> list[dict[str, Any]]:
    """Report-only: per held-out document, how many pages pass the stratum check. Writes nothing."""
    checks = CachedPageChecks(cache, manifest["seed"])
    rows = []
    for entry in manifest["held_out"]:
        row: dict[str, Any] = {"id": entry["id"], "stratum": entry["stratum"], "candidate_pages": entry.get("candidate_pages")}
        try:
            if entry["role"] == "stratum_pages":
                row["supported_pages"] = checks.pages(entry, entry["stratum"], 1)[1]
            else:
                row["reference_list_found"] = checks.reference_list_found(entry)
        except PdfProblem as problem:
            row["problem"] = str(problem)
        rows.append(row)
    return rows


def _inside_git_checkout(path: Path) -> bool:
    """True if ``path`` lies in any git checkout, including this repository's main checkout and worktrees."""
    resolved = path.resolve()
    if any((p / ".git").exists() for p in (resolved, *resolved.parents)):
        return True
    try:
        common = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    roots = [REPO_ROOT.resolve()] + ([Path(common).resolve().parent] if common else [])
    return any(root == resolved or root in resolved.parents for root in roots)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--pin", type=Path, default=PIN_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="directory of <id>.pdf files (default: the gitignored corpus cache); read-only")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory; must be outside every git checkout")
    parser.add_argument("--id", action="append", help="limit to this manifest pilot document id (repeatable)")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--with-text-proposals", action="store_true",
                        help="also propose PDF text-layer blocks (off by default: they can anchor the reviewer)")
    parser.add_argument("--overwrite", action="store_true", help="replace existing drafts (they may hold review edits)")
    parser.add_argument("--held-out-check", action="store_true",
                        help="report-only: count pages passing each held-out stratum check; writes no text or images")
    args = parser.parse_args(argv)

    problems = check_manifest(args.manifest, args.pin)
    if problems:
        parser.error("manifest check failed, refusing to draft from a changed selection: " + "; ".join(problems))
    manifest = json.loads(args.manifest.read_bytes())
    if args.held_out_check:
        rows = held_out_check(manifest, args.cache)
        for row in rows:
            print(json.dumps(row))
        return 1 if any(row.get("problem") or row.get("supported_pages") == 0 or row.get("reference_list_found") is False for row in rows) else 0
    if _inside_git_checkout(args.out):
        parser.error("--out must be outside every git checkout: page images and extracted text are copyrighted and must not be committed")
    if args.id and set(args.id) - {d["id"] for d in manifest["pilot"]}:
        parser.error(f"unknown pilot document id(s): {sorted(set(args.id) - {d['id'] for d in manifest['pilot']})}")
    sources = {s["id"]: s["title"] for s in json.loads((REPO_ROOT / "benchmarks" / "public_pdfs" / "sources.json").read_text(encoding="utf-8"))["sources"]}
    report = run(manifest, args.cache, args.out, only=args.id, dpi=args.dpi,
                 text_proposals=args.with_text_proposals, titles=sources, overwrite=args.overwrite)
    for item in report["documents"]:
        print(f"{item['id']}: {item['status']}" + (f" pages {item['pages']}" if item.get("pages") else ""))
    for problem in report["problems"]:
        print(f"PROBLEM {problem}")
    print(f"report: {args.out / report['report_file']}")
    return 1 if report["problems"] else 0


__all__ = ["CachedPageChecks", "PdfProblem", "build_draft", "choose_pages", "held_out_check", "main", "page_support", "run", "verify_cached"]
