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
import hashlib
import json
import re
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
    PageChecks,
    check_manifest,
)
from .records import options_sha256, sha256_bytes, validate_record

TOOL_NAME = "extraction-pilot-drafts"
TOOL_VERSION = "2"
DEFAULT_DPI = 150
DEFAULT_CACHE = REPO_ROOT / "benchmarks" / "public_pdfs" / ".cache"
DEFAULT_OUT = Path.home() / ".cache" / "zotero-fulltext-extraction-pilot"

_HEADING = re.compile(
    r"^[\d\W]*(references|bibliography|literature|literatur|literaturverzeichnis|r[ée]f[ée]rences|"
    r"bibliographie|referencias|bibliograf[ií]a|references cited)\W*$", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:19|20)\d\d[a-z]?\b")
_NUMBERED = re.compile(r"^\s*\[?\d{1,3}[\].)]\s+\S")
_MATH = re.compile("[Ͱ-Ͽ∀-⋿←-⇿\U0001d400-\U0001d7ff±×÷∞]")


class PdfProblem(RuntimeError):
    """A pinned PDF is missing from the cache or does not match its hash."""


# --- cache verification ----------------------------------------------------------------------


def verify_cached(doc: dict[str, Any], cache: Path) -> tuple[str, Path, str | None]:
    """Return (status, path, detail). Status is ``ok``, ``missing`` or ``hash_mismatch``. Never downloads."""
    path = cache / f"{doc['id']}.pdf"
    if not path.is_file():
        return "missing", path, f"not in cache; fetch it with tools/fetch_public_pdf_corpus.py --id {doc['id']}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pinned = doc.get("pdf_sha256") or doc.get("sha256")
    if digest != pinned:
        return "hash_mismatch", path, f"sha256 {digest} != pinned {pinned}"
    return "ok", path, None


# --- page checks (heuristics; candidates, never labels) --------------------------------------


def geometry_refusal(page: pymupdf.Page) -> str | None:
    """A rotated page with a CropBox smaller than its MediaBox is refused rather than guessed at."""
    if page.rotation and tuple(page.cropbox) != tuple(page.mediabox):
        return "rotated page with a CropBox different from its MediaBox"
    return None


def _text_blocks(page: pymupdf.Page) -> list[tuple[float, float, float, float, str]]:
    return [(b[0], b[1], b[2], b[3], b[4]) for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]


def _image_share(page: pymupdf.Page) -> float:
    covered = 0.0
    for image in page.get_image_info():
        x0, y0, x1, y1 = image["bbox"]
        covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return covered / max(page.rect.width * page.rect.height, 1.0)


def _has_real_table(page: pymupdf.Page) -> bool:
    """A grid of at least 3 x 3 cells, holding text in over a third of its cells, on a page that is not mostly image."""
    if _image_share(page) >= 0.5:
        return False
    try:
        tables = page.find_tables().tables
    except Exception:  # noqa: BLE001 - PyMuPDF raises assorted errors on malformed pages
        return False
    for table in tables:
        if table.row_count < 3 or table.col_count < 3:
            continue
        cells = [cell for row in table.extract() for cell in row]
        filled = sum(1 for cell in cells if cell and cell.strip())
        if cells and filled / len(cells) >= 0.35:
            return True
    return False


def page_support(stratum: str, page: pymupdf.Page) -> bool:
    """Whether this page passes the crude check for ``stratum``: a candidate, not a label."""
    page.remove_rotation()  # in memory only: judge the displayed page, as the draft's render does
    width, height = page.rect.width, page.rect.height
    blocks = _text_blocks(page)
    text = "".join(b[4] for b in blocks)
    if stratum == "two-column":
        left = sum(1 for b in blocks if b[2] <= 0.55 * width and b[3] - b[1] > 20)
        right = sum(1 for b in blocks if b[0] >= 0.45 * width and b[3] - b[1] > 20)
        return min(left, right) >= 3
    if stratum == "tables":
        return _has_real_table(page)
    if stratum == "equations":
        return len(_MATH.findall(text)) >= 15
    if stratum == "scans":
        return _image_share(page) >= 0.6
    if stratum == "non-english":
        return len(text) >= 1000
    if stratum == "references":
        lines = [line.strip() for line in text.splitlines()]
        year_lines = sum(1 for line in lines if _YEAR.search(line))
        numbered = sum(1 for line in lines if _NUMBERED.match(line))
        return year_lines >= 3 or numbered >= 5
    raise ValueError(f"unknown stratum {stratum!r} ({width}x{height})")


def _reference_start(doc: pymupdf.Document) -> int:
    """First page of the reference list: the last page with a reference-list heading, else beyond the end."""
    headings = [n for n in range(1, doc.page_count + 1)
                if any(_HEADING.match(line.strip()) for line in doc[n - 1].get_text().splitlines())]
    return headings[-1] if headings else doc.page_count + 1


def choose_pages(doc: pymupdf.Document, stratum: str, wanted: int, seed: str, doc_id: str) -> tuple[list[int], int]:
    """Pick ``wanted`` candidate pages by seeded hash rank; return (pages, number of supported pages).

    Year-bearing lines also occur in body text, so reference pages count only from the last
    reference-list heading onward; a document with no such heading supports nothing.
    """
    first = _reference_start(doc) if stratum == "references" else 1
    supported = [
        n for n in range(first, doc.page_count + 1)
        if geometry_refusal(doc[n - 1]) is None and page_support(stratum, doc[n - 1])
    ]
    supported.sort(key=lambda n: hashlib.sha256(f"{seed}|pages|{doc_id}|{stratum}|{n}".encode()).hexdigest())
    return sorted(supported[:wanted]), len(supported)


class CachedPageChecks(PageChecks):
    """Page checks against hash-verified cached PDFs; a missing or changed PDF raises ``PdfProblem``."""

    def __init__(self, cache: Path, seed: str):
        self.cache, self.seed = cache, seed

    def _path(self, entry: dict[str, Any]) -> Path:
        status, path, detail = verify_cached(entry, self.cache)
        if status != "ok":
            raise PdfProblem(f"{entry['id']}: {status}: {detail}")
        return path

    def pages(self, entry: dict[str, Any], stratum: str, wanted: int) -> tuple[list[int], int]:
        with pymupdf.open(self._path(entry)) as doc:
            return choose_pages(doc, stratum, wanted, self.seed, entry["id"])

    def reference_list_found(self, entry: dict[str, Any]) -> bool:
        with pymupdf.open(self._path(entry)) as doc:
            return choose_pages(doc, "references", 1, self.seed, entry["id"])[1] > 0


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
                for x0, y0, x1, y1, text in _text_blocks(work_page):
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
        row: dict[str, Any] = {"id": entry["id"], "stratum": entry["stratum"]}
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


__all__ = ["CachedPageChecks", "build_draft", "choose_pages", "held_out_check", "main", "page_support", "run", "verify_cached"]
