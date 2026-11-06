"""Opt-in host step: render pilot pages from cached corpus PDFs and write DRAFT expectation proposals.

Host-only and benchmark-side: not shipped, no Zotero access, no network. PDFs must already be in
the local cache and match the SHA-256 pinned in the manifest; a missing or mismatching file is
reported, never skipped silently and never downloaded. Output goes to a directory outside the
repository (page images and text-layer text are copyrighted material and must not be committed).

Drafts are ``expectation`` records with ``review.status == "draft"`` and ``proposed_by``
provenance. They are NOT ground truth: ``load_reviewed_expectation`` refuses them. The only
content they propose is what the PDF's own text layer contains (blocks typed ``other``, high
uncertainty); no extractor or model output is used, and nothing is labelled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pymupdf

from .pilot import MANIFEST_PATH, PIN_PATH, REPO_ROOT, manifest_pin
from .records import options_sha256, sha256_bytes, validate_record

TOOL_NAME = "extraction-pilot-drafts"
TOOL_VERSION = "1"
DEFAULT_DPI = 150
DEFAULT_CACHE = REPO_ROOT / "benchmarks" / "public_pdfs" / ".cache"
DEFAULT_OUT = Path.home() / ".cache" / "zotero-fulltext-extraction-pilot"

_HEADING = re.compile(
    r"^\W*(references|bibliography|literature|literatur|literaturverzeichnis|r[ée]f[ée]rences|"
    r"bibliographie|referencias|bibliograf[ií]a|references cited)\W*$", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:19|20)\d\d[a-z]?\b")
_MATH = re.compile("[Ͱ-Ͽ∀-⋿←-⇿\U0001d400-\U0001d7ff±×÷∞]")


# --- cache verification ----------------------------------------------------------------------


def verify_cached(doc: dict[str, Any], cache: Path) -> tuple[str, Path, str | None]:
    """Return (status, path, detail). Status is ``ok``, ``missing`` or ``hash_mismatch``. Never downloads."""
    path = cache / f"{doc['id']}.pdf"
    if not path.is_file():
        return "missing", path, f"not in cache; fetch it with tools/fetch_public_pdf_corpus.py --id {doc['id']}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != doc["pdf_sha256"]:
        return "hash_mismatch", path, f"sha256 {digest} != pinned {doc['pdf_sha256']}"
    return "ok", path, None


# --- layout checks (heuristics that verify assumed strata; never labels) ---------------------


def _text_blocks(page: pymupdf.Page) -> list[tuple[float, float, float, float, str]]:
    return [(b[0], b[1], b[2], b[3], b[4]) for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]


def page_support(stratum: str, page: pymupdf.Page) -> bool:
    """Whether this page plausibly belongs to ``stratum``. A crude check of the assumption, not a label."""
    width, height = page.rect.width, page.rect.height
    blocks = _text_blocks(page)
    text = "".join(b[4] for b in blocks)
    if stratum == "two-column":
        left = sum(1 for b in blocks if b[2] <= 0.55 * width and b[3] - b[1] > 20)
        right = sum(1 for b in blocks if b[0] >= 0.45 * width and b[3] - b[1] > 20)
        return min(left, right) >= 3
    if stratum == "tables":
        try:
            tables = page.find_tables().tables
        except Exception:  # noqa: BLE001 - PyMuPDF raises assorted errors on malformed pages
            return False
        return any(t.row_count >= 2 and t.col_count >= 2 for t in tables)
    if stratum == "equations":
        return len(_MATH.findall(text)) >= 15
    if stratum == "scans":
        covered = 0.0
        for image in page.get_image_info():
            x0, y0, x1, y1 = image["bbox"]
            covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
        return covered >= 0.6 * width * height
    if stratum == "non-english":
        return len(text) >= 1000
    if stratum == "references":
        lines = [line.strip() for line in text.splitlines()]
        year_lines = sum(1 for line in lines if _YEAR.search(line))
        return year_lines >= 3
    raise ValueError(f"unknown stratum {stratum!r}")


def choose_pages(doc: pymupdf.Document, stratum: str, wanted: int, seed: str, doc_id: str) -> tuple[list[int], int]:
    """Pick ``wanted`` supported pages by seeded hash rank; return (pages, number of supported pages)."""
    first = 1
    if stratum == "references":
        # Year-bearing lines also occur in body text, so only pages from the last reference-list
        # heading onward count; a document with no such heading supports nothing.
        headings = [n for n in range(1, doc.page_count + 1)
                    if any(_HEADING.match(line) for line in doc[n - 1].get_text().splitlines())]
        first = headings[-1] if headings else doc.page_count + 1
    supported = [n for n in range(first, doc.page_count + 1) if page_support(stratum, doc[n - 1])]
    supported.sort(key=lambda n: hashlib.sha256(f"{seed}|pages|{doc_id}|{stratum}|{n}".encode()).hexdigest())
    return sorted(supported[:wanted]), len(supported)


# --- draft construction ----------------------------------------------------------------------


def _page_entry(meta_page: pymupdf.Page, work_page: pymupdf.Page, image_hash: str, dpi: int) -> dict[str, Any]:
    # Rotation comes from the separately opened document; the working page has had its rotation
    # removed, so its rect, text boxes and render are all in the displayed frame.
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
    dpi: int = DEFAULT_DPI, text_blocks: bool = True,
) -> dict[str, Any]:
    """Render ``pages`` (all pages when None) and return a draft expectation record."""
    meta = pymupdf.open(pdf_path)
    work = pymupdf.open(pdf_path)
    try:
        page_numbers = pages or list(range(1, meta.page_count + 1))
        entries: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        out_images.mkdir(parents=True, exist_ok=True)
        for number in page_numbers:
            work_page = work[number - 1]
            work_page.remove_rotation()
            png = work_page.get_pixmap(dpi=dpi, alpha=False).tobytes("png")
            (out_images / f"{doc_entry['id']}-p{number:04d}.png").write_bytes(png)
            entries.append(_page_entry(meta[number - 1], work_page, sha256_bytes(png), dpi))
            if text_blocks:
                width, height = work_page.rect.width, work_page.rect.height
                for x0, y0, x1, y1, text in _text_blocks(work_page):
                    box = {"x0": max(0.0, x0), "y0": max(0.0, y0), "x1": min(width, x1), "y1": min(height, y1)}
                    if box["x1"] <= box["x0"] or box["y1"] <= box["y0"]:
                        continue
                    order = len(blocks) + 1
                    blocks.append({
                        "id": f"p{number}-b{order}", "type": "other", "page": number, "reading_order": order,
                        "bbox": {k: round(v, 3) for k, v in box.items()}, "text": text.strip(),
                        "uncertainty": {"level": "high", "note": "unreviewed PDF text-layer proposal; type, boundaries and text are not checked"},
                    })
        page_count = meta.page_count
    finally:
        meta.close()
        work.close()
    whole = pages is None
    record = {
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
                "options_sha256": options_sha256({"dpi": dpi, "text_blocks": text_blocks, "pymupdf": pymupdf.VersionBind}),
            },
            "review": {"status": "draft"},
            "uncertainty": {"level": "high", "note": "draft proposal; becomes ground truth only through human review (docs/extraction-pilot-review.md)"},
        },
    }
    return record


# --- orchestration ---------------------------------------------------------------------------


def run(
    manifest: dict[str, Any], cache: Path, out: Path, *, only: list[str] | None = None,
    dpi: int = DEFAULT_DPI, text_blocks: bool = True, titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Draft every pilot document. Returns a report; problems are listed, never skipped silently."""
    report: dict[str, Any] = {"seed": manifest["seed"], "documents": [], "problems": []}
    titles = titles or {}
    drafts_dir, images_dir = out / "drafts", out / "images"
    drafts_dir.mkdir(parents=True, exist_ok=True)
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
        title = titles.get(entry["id"], entry["id"])
        pages: list[int] | None = None
        if entry["role"] == "stratum_pages":
            with pymupdf.open(path) as probe:
                pages, supported = choose_pages(probe, entry["stratum"], entry["page_quota"], manifest["seed"], entry["id"])
            item["supported_pages"] = supported
            item["pages"] = pages
            if len(pages) < entry["page_quota"]:
                message = (f"{entry['id']}: stratum {entry['stratum']!r} ({entry['basis']}) supported on only "
                           f"{len(pages)} of {entry['page_quota']} wanted page(s); the layout assumption is not confirmed")
                item["status"] = "stratum_unsupported" if not pages else "stratum_partial"
                report["problems"].append(message)
                if not pages:
                    continue
        record = build_draft(entry, path, title, pages, images_dir, dpi, text_blocks)
        _validate_draft(record)
        target = drafts_dir / f"{record['record_id']}.expectation.draft.json"
        target.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        item["draft"] = target.name
    (out / "selection_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def _validate_draft(record: dict[str, Any]) -> None:
    """A draft must be a schema-valid expectation; fail loudly rather than write an invalid file."""
    validate_record(record)


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--pin", type=Path, default=PIN_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="directory of <id>.pdf files (default: the gitignored corpus cache)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory; must be outside the repository")
    parser.add_argument("--id", action="append", help="limit to this manifest document id (repeatable)")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--pages-only", action="store_true", help="propose no text-layer blocks, only page geometry and renders")
    args = parser.parse_args(argv)

    if _is_inside(args.out, REPO_ROOT):
        parser.error("--out must be outside the repository: page images and extracted text are copyrighted and must not be committed")
    data = args.manifest.read_bytes()
    if manifest_pin(data) != args.pin.read_text(encoding="utf-8").strip():
        parser.error("manifest does not match its SHA-256 pin; refusing to draft from a changed selection")
    manifest = json.loads(data)
    sources = {s["id"]: s["title"] for s in json.loads((REPO_ROOT / "benchmarks" / "public_pdfs" / "sources.json").read_text(encoding="utf-8"))["sources"]}
    report = run(manifest, args.cache, args.out, only=args.id, dpi=args.dpi, text_blocks=not args.pages_only, titles=sources)
    for item in report["documents"]:
        print(f"{item['id']}: {item['status']}" + (f" pages {item['pages']}" if item.get("pages") else ""))
    for problem in report["problems"]:
        print(f"PROBLEM {problem}")
    print(f"report: {args.out / 'selection_report.json'}")
    return 1 if report["problems"] else 0


__all__ = ["build_draft", "choose_pages", "main", "page_support", "run", "verify_cached"]
