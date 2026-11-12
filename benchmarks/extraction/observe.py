"""Export the CURRENT production extraction as an ``observation`` record (roadmap E1/E2).

Benchmark-side only: nothing here ships in the wheel and no behaviour of the production conversion
changes. The extractor is the one the converter runs (``zotero_pdf_text._extract_markdown``, in the
same subprocess, with the same options), and its per-page lengths are read back with the
converter's own ``_read_page_texts``, so the page of every block is *measured* by the package's page
map, never estimated. The optional crop OCR reuses ``image_ocr.plan_crops`` / ``_run_ocr`` /
``render_replacement`` / ``splice`` page by page.

``observe_pdf`` returns the schema-valid record together with the exact Markdown its ``output_span``
offsets refer to. Markdown structure becomes blocks (headings, paragraphs, list items, pipe tables,
``$$`` equations, captions, picture placeholders, a trailing bare page number, and reference-list
entries under a References/Bibliography heading). Nothing the extractor did not measure is invented:
no bounding boxes, no render identity, no LaTeX numbers, no table spans, no citation markers. See
``docs/extraction-quality-plan.md`` ("Observation exporter") for what is and is not detected.

When ``pages`` is given the whole document is still extracted (as in production) and the requested
pages' Markdown is sliced out by the page map, so page text is identical to a whole-document run.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .records import options_sha256, sha256_bytes, sha256_text, validate_record

OBSERVER_NAME = "extraction-observe"
OBSERVER_VERSION = "1"
DEFAULT_TIMEOUT_S = 600
_ROMAN_FOLIO = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,7}|[IVXLCDM]{1,7})$")


class ExtractionFailed(Exception):
    """The extractor could not produce Markdown for this PDF (message: the underlying error)."""


@dataclass(frozen=True)
class ObservationResult:
    observation: dict[str, Any]
    markdown: str
    extractor: dict[str, Any]
    elapsed_s: float


# --- Markdown -> blocks ----------------------------------------------------------------------

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+\S")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]+\S")
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$")
_IMAGE_LINE = re.compile(r"^[ \t]*!\[[^\]]*\]\([^)]*\)[ \t]*$")
_RULE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_PICTURE_START = "<!-- Start of picture text -->"
_PICTURE_END = "<!-- End of picture text -->"
# Captions: the label is emphasised ("**Fig. 1** ..."), or followed by a separator ("Table 2. ...").
# A sentence that merely opens with a label ("Table 3 reports ...") is a cross-reference, not a caption.
_LABEL = r"(?:figure|fig\.?|table|tab\.?|abbildung|abb\.?|tabelle)"
_CAPTION_EMPHASISED = re.compile(r"^[\s#>]*(?:\*\*|__|\*|_)\s*" + _LABEL + r"\s*\d+[A-Za-z]?\s*[.:]?\s*(?:\*\*|__|\*|_)", re.I)
_CAPTION_SEPARATED = re.compile(r"^[\s#>]*" + _LABEL + r"\s*\d+[A-Za-z]?\s*[.:]\s", re.I)
_TABLE_LABEL = re.compile(r"^[\s#>*_]*(?:table|tab\.?|tabelle)", re.I)
_FIGURE_LABEL = re.compile(r"^[\s#>*_]*(?:figure|fig\.?|abbildung|abb\.?)", re.I)
_REFERENCES_HEADING = re.compile(
    r"^[\s#>*_]*(?:\d+\.?\s*|[ivx]+\.\s*)?(?:references?|bibliography|literature(?: cited)?|works cited|literatur(?:verzeichnis)?)[\s*_.:]*$",
    re.I,
)
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"'|]+", re.I)
_REFERENCE_START = re.compile(r"^[ \t]*(?:\[\d{1,3}\]|\d{1,3}[.)])[ \t]+\S")


@dataclass
class _Raw:
    """One block found in a page's Markdown; ``start``/``end`` are offsets into the page text."""

    type: str
    start: int
    end: int
    lines: list[str]


def _lines_with_offsets(text: str) -> Iterator[tuple[int, str]]:
    offset = 0
    for line in text.split("\n"):
        yield offset, line
        offset += len(line) + 1


def split_blocks(text: str) -> list[_Raw]:
    """Cut one page's Markdown into typed raw blocks (offsets relative to ``text``)."""
    blocks: list[_Raw] = []
    current: _Raw | None = None
    mode = ""  # "" | "para" | "list" | "table" | "math" | "fence" | "picture"

    def flush() -> None:
        nonlocal current, mode
        if current is not None:
            blocks.append(current)
        current, mode = None, ""

    def open_block(kind: str, new_mode: str, offset: int, line: str) -> None:
        nonlocal current, mode
        flush()
        current = _Raw(kind, offset, offset + len(line.rstrip()), [line])
        mode = new_mode

    def extend(offset: int, line: str) -> None:
        assert current is not None
        current.end = offset + len(line.rstrip())
        current.lines.append(line)

    for offset, line in _lines_with_offsets(text):
        stripped = line.strip()
        if mode == "fence":
            extend(offset, line)
            if _FENCE.match(line):
                flush()
            continue
        if mode == "math":
            extend(offset, line)
            if stripped.endswith("$$"):
                flush()
            continue
        if mode == "picture":
            extend(offset, line)
            if _PICTURE_END in line:
                flush()
            continue
        if not stripped:
            flush()
            continue
        if _FENCE.match(line):
            open_block("other", "fence", offset, line)
            continue
        if stripped.startswith("$$"):
            open_block("equation", "" if (len(stripped) > 2 and stripped.endswith("$$")) else "math", offset, line)
            if mode == "":
                flush()
            continue
        if stripped.startswith(_PICTURE_START):
            open_block("figure", "picture", offset, line)
            if _PICTURE_END in line:
                flush()
            continue
        if _IMAGE_LINE.match(line):
            open_block("figure", "", offset, line)
            flush()
            continue
        if _RULE.match(line) and mode != "para":
            flush()
            continue
        if _HEADING.match(line):
            open_block("heading", "", offset, line)
            flush()
            continue
        if _TABLE_ROW.match(line):
            if mode == "table":
                extend(offset, line)
            else:
                open_block("table", "table", offset, line)
            continue
        if mode == "table":
            flush()
        if _LIST_ITEM.match(line) and mode in ("", "list"):
            open_block("list_item", "list", offset, line)
            continue
        if mode in ("para", "list"):
            extend(offset, line)
            continue
        open_block("paragraph", "para", offset, line)
    flush()
    # A figure placeholder directly followed by its picture-text comment is one figure.
    merged: list[_Raw] = []
    for block in blocks:
        prev = merged[-1] if merged else None
        if (
            prev is not None and prev.type == "figure" and block.type == "figure"
            and block.lines[0].strip().startswith(_PICTURE_START)
            and text[prev.end:block.start].strip() == ""
            and text[prev.end:block.start].count("\n") <= 2
        ):
            prev.end, prev.lines = block.end, prev.lines + block.lines
        else:
            merged.append(block)
    return merged


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells, cell, i = [], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            cell.append("\\|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(ch)
        i += 1
    cells.append("".join(cell).strip())
    return cells


def parse_table(lines: Sequence[str]) -> dict[str, Any] | None:
    """Pipe-table lines to a schema ``table`` payload; None if no usable rows. Spans are unknown."""
    rows = [_split_row(line) for line in lines if not _TABLE_RULE.match(line)]
    header = len(lines) > 1 and bool(_TABLE_RULE.match(lines[1]))
    rows = [row for row in rows if row]
    if not rows:
        return None
    cells = []
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            cell: dict[str, Any] = {"row": r, "col": c, "text": text}
            if header and r == 0:
                cell["is_header"] = True
            cells.append(cell)
    return {"n_rows": len(rows), "n_cols": max(len(row) for row in rows), "cells": cells}


def parse_equation(raw: str) -> str:
    """The LaTeX between ``$$`` delimiters, whitespace-trimmed."""
    body = raw.strip()
    body = body[2:] if body.startswith("$$") else body
    body = body[:-2] if body.endswith("$$") else body
    return body.strip()


def _item_spans(raw: _Raw, text: str) -> list[tuple[int, int]]:
    """List items inside one list raw block: each marker line opens an item."""
    items: list[list[int]] = []
    offset = raw.start
    for line in raw.lines:
        if _LIST_ITEM.match(line):
            items.append([offset, offset + len(line.rstrip())])
        elif items and line.strip():
            items[-1][1] = offset + len(line.rstrip())
        offset += len(line) + 1
    return [(a, b) for a, b in items]


def _reference_spans(raw: _Raw) -> list[tuple[int, int]]:
    """Entries of one reference-list paragraph: one per numbered line when every non-blank line
    starts a numbered entry, else the whole block is one entry (wrapped lines stay together)."""
    starts = [(i, line) for i, line in enumerate(raw.lines) if _REFERENCE_START.match(line)]
    if len(starts) < 2:
        return [(raw.start, raw.end)]
    offsets, offset = [], raw.start
    for line in raw.lines:
        offsets.append(offset)
        offset += len(line) + 1
    spans = []
    for n, (i, _line) in enumerate(starts):
        last = (starts[n + 1][0] if n + 1 < len(starts) else len(raw.lines)) - 1
        spans.append((offsets[i], offsets[last] + len(raw.lines[last].rstrip())))
    return spans


def _is_caption(text: str) -> bool:
    first = text.lstrip()
    return bool(_CAPTION_EMPHASISED.match(first) or _CAPTION_SEPARATED.match(first))


def build_page_blocks(
    page: int, page_text: str, base: int, markdown_sha: str, state: dict[str, Any],
    full_markdown: str,
) -> list[dict[str, Any]]:
    """Typed schema blocks for one page. ``base`` is the page text's offset in the full Markdown;
    ``state`` carries ``order`` (next reading order) and ``in_references`` across pages."""
    raws = split_blocks(page_text)
    out: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = state["entries"]
    n = 0

    def new_block(btype: str, start: int, end: int) -> dict[str, Any]:
        nonlocal n
        n += 1
        block: dict[str, Any] = {
            "id": f"p{page}-b{n}", "type": btype, "page": page, "reading_order": state["order"],
            "output_span": {
                "markdown_sha256": markdown_sha, "start": base + start, "end": base + end,
                "span_sha256": sha256_text(full_markdown[base + start:base + end]),
            },
        }
        state["order"] += 1
        return block

    last_index = len(raws) - 1
    for index, raw in enumerate(raws):
        raw_text = page_text[raw.start:raw.end]
        if raw.type == "heading":
            state["in_references"] = bool(_REFERENCES_HEADING.match(raw_text))
            block = new_block("heading", raw.start, raw.end)
            block["text"] = raw_text
        elif raw.type == "table":
            table = parse_table(raw.lines)
            block = new_block("table", raw.start, raw.end)
            if table is not None:
                block["table"] = table
        elif raw.type == "equation":
            block = new_block("equation", raw.start, raw.end)
            block["equation"] = {"latex": parse_equation(raw_text), "display": True}
        elif raw.type == "figure":
            block = new_block("figure", raw.start, raw.end)
            block["graphic"] = {"kind": "figure"}
        elif raw.type == "list_item":
            for a, b in _item_spans(raw, page_text):
                item = new_block("reference_entry" if state["in_references"] else "list_item", a, b)
                item["text"] = page_text[a:b]
                out.append(item)
                if state["in_references"]:
                    _add_entry(entries, item, page, page_text[a:b])
            continue
        elif raw.type == "other":
            block = new_block("other", raw.start, raw.end)
            block["text"] = raw_text
        elif index == last_index and _ROMAN_FOLIO.match(raw_text.strip()):
            block = new_block("page_number", raw.start, raw.end)
            block["text"] = raw_text
        elif state["in_references"]:
            for a, b in _reference_spans(raw):
                entry_block = new_block("reference_entry", a, b)
                entry_block["text"] = page_text[a:b]
                out.append(entry_block)
                _add_entry(entries, entry_block, page, page_text[a:b])
            continue
        elif _is_caption(raw_text):
            block = new_block("caption", raw.start, raw.end)
            block["text"] = raw_text
        else:
            block = new_block("paragraph", raw.start, raw.end)
            block["text"] = raw_text
        if isinstance(block.get("text"), str):
            block["text_sha256"] = sha256_text(block["text"])
        out.append(block)
    _link_captions(out)
    return out


def _add_entry(entries: list[dict[str, Any]], block: dict[str, Any], page: int, raw: str) -> None:
    doi = _DOI.search(raw)
    doi_payload: dict[str, Any] = {"status": "not_found"}
    if doi:
        doi_payload = {"status": "present", "value": doi.group(0).rstrip(".,;)]}")}
    entries.append({
        "id": f"ref-{len(entries) + 1}", "ordinal": len(entries) + 1, "page": page, "block_id": block["id"],
        "raw": raw, "raw_sha256": sha256_text(raw), "doi": doi_payload,
    })


def _link_captions(blocks: list[dict[str, Any]]) -> None:
    """Heuristic ``caption_for``: a table/figure caption points at the adjacent block of the matching
    kind (the one before it first, else the one after), if that block is not already captioned."""
    taken: set[str] = set()
    for i, block in enumerate(blocks):
        if block["type"] != "caption":
            continue
        text = block.get("text", "")
        kind = "table" if _TABLE_LABEL.match(text) else "figure" if _FIGURE_LABEL.match(text) else None
        if kind is None:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(blocks) and blocks[j]["type"] == kind and blocks[j]["id"] not in taken:
                block["caption_for"] = blocks[j]["id"]
                taken.add(blocks[j]["id"])
                break


# --- extraction ------------------------------------------------------------------------------


@contextlib.contextmanager
def _package_importable() -> Iterator[None]:
    """The extractor subprocess must import the same ``zotero_pdf_text`` this process uses."""
    import zotero_pdf_text

    root = str(Path(zotero_pdf_text.__file__).resolve().parent.parent)
    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = root if not previous else root + os.pathsep + previous
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous


def _extract_pages(pdf_path: Path, workdir: Path, timeout_s: int, with_images: bool) -> tuple[list[tuple[int, str]], bool, Path]:
    """Run the production primary extractor; return (page_texts, has_math, images_dir)."""
    from zotero_pdf_text import converter

    raw_path = workdir / "raw.md"
    images_dir = workdir / "images"
    try:
        with _package_importable():
            converter._run_extractor(
                pdf_path, raw_path, converter.PRIMARY_EXTRACTION_TOOL, timeout_s,
                image_dir=images_dir if with_images else None,
            )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionFailed(f"TimeoutExpired: exceeded {timeout_s} seconds") from exc
    except subprocess.CalledProcessError as exc:
        raise ExtractionFailed(f"CalledProcessError: {converter._stderr_tail(exc)}") from exc
    except OSError as exc:
        raise ExtractionFailed(f"{type(exc).__name__}: {exc}") from exc
    raw = raw_path.read_text(encoding="utf-8")
    page_texts = converter._read_page_texts(raw_path.with_suffix(".pages.json"), raw)
    if not page_texts:
        raise ExtractionFailed("the extractor produced no measured page map; refusing to guess page boundaries")
    has_math = converter._read_math_sidecar(raw_path.with_suffix(".math.json"))
    return page_texts, has_math, images_dir


def _enrich_with_ocr(page_texts: list[tuple[int, str]], images_dir: Path, has_math: bool, settings: Mapping[str, Any]) -> tuple[list[tuple[int, str]], str]:
    """Crop OCR through the package's own planner/runner, splicing page by page (page boundaries
    survive because replacements never cross a page text)."""
    from zotero_pdf_text import image_ocr
    from zotero_pdf_text.config import ImageOcrSettings
    from zotero_pdf_text.library import relative_image_links

    config = ImageOcrSettings(**dict(settings))
    status = image_ocr.probe(config.base_url, config.model)
    if not status.ok:
        raise ExtractionFailed(f"image OCR unavailable: {status.detail}")
    virtual_markdown = Path("observe") / "observe.md"
    page_texts = [(page, relative_image_links(text, virtual_markdown)) for page, text in page_texts]
    plans = []
    spans = []
    for page, text in page_texts:
        page_plans = image_ocr.plan_crops(text, images_dir, has_math=has_math)
        plans.extend(page_plans)
        spans.append(len(page_plans))
    plans = image_ocr._run_ocr(plans, settings=config, images_dir=images_dir)
    enriched, cursor = [], 0
    for (page, text), count in zip(page_texts, spans):
        chunk = plans[cursor:cursor + count]
        cursor += count
        replacements = [
            (plan.ref.span, image_ocr.render_replacement(plan.crop_class, plan.ocr_text, plan.ref.markup))
            for plan in chunk if plan.ocr_text
        ]
        enriched.append((page, image_ocr.splice(text, replacements)))
    return enriched, config.model


def _page_entries(pdf_path: Path, pages: Sequence[int] | None) -> tuple[list[dict[str, Any]], int, str]:
    import pymupdf

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - pymupdf raises several unrelated types
        raise ExtractionFailed(f"cannot open PDF: {type(exc).__name__}: {exc}") from exc
    try:
        count = doc.page_count
        if count < 1:
            raise ExtractionFailed("PDF has no pages")
        pages = list(pages) if pages is not None else list(range(1, count + 1))
        for number in pages:
            if not 1 <= number <= count:
                raise ValueError(f"page {number} is outside 1..{count}")
        entries = []
        for number in pages:
            page = doc[number - 1]
            entries.append({
                "physical_page": number,
                "printed_label": page.get_label() or None,
                # page.rect is the displayed (rotation applied) CropBox clipped to the MediaBox.
                "geometry": {
                    "unit": "pt", "origin": "top-left", "frame": "displayed", "box": "cropbox",
                    "rotation": page.rotation, "width": round(page.rect.width, 3), "height": round(page.rect.height, 3),
                },
            })
        title = (doc.metadata or {}).get("title") or ""
        return entries, count, title
    finally:
        doc.close()


def observe_pdf(
    pdf_path: Path, *, pages: Sequence[int] | None = None, options: Mapping[str, Any] | None = None
) -> ObservationResult:
    """Run the current production extraction on ``pdf_path`` and return a schema-valid observation.

    ``pages`` are 1-based physical pages (None = whole document). ``options``: ``image_ocr`` (bool,
    default False: also read crops with the local OCR model), ``image_ocr_settings`` (mapping of
    ``ImageOcrSettings`` fields), ``timeout_s`` (default 600). Raises ``ExtractionFailed`` when the
    extractor fails and ``ValueError`` for pages outside the document.
    """
    import pymupdf
    import pymupdf4llm

    opts = dict(options or {})
    use_ocr = bool(opts.get("image_ocr", False))
    timeout_s = int(opts.get("timeout_s", DEFAULT_TIMEOUT_S))
    pdf_path = Path(pdf_path)
    started = time.perf_counter()
    try:
        pdf_bytes = pdf_path.read_bytes()
    except OSError as exc:
        raise ExtractionFailed(f"cannot read PDF: {exc}") from exc
    requested = sorted(set(pages)) if pages is not None else None
    # Opened first so an unreadable PDF or a bad page number fails before the extractor subprocess.
    page_entries, page_count, title = _page_entries(pdf_path, requested)
    selected = [entry["physical_page"] for entry in page_entries]

    ocr_model = ""
    with tempfile.TemporaryDirectory(prefix="observe-") as tmp:
        page_texts, has_math, images_dir = _extract_pages(pdf_path, Path(tmp), timeout_s, with_images=use_ocr)
        if use_ocr:
            try:
                page_texts, ocr_model = _enrich_with_ocr(page_texts, images_dir, has_math, opts.get("image_ocr_settings", {}))
            except ExtractionFailed:
                raise
            except Exception as exc:  # noqa: BLE001 - OCR planner/runner errors are extractor failures
                raise ExtractionFailed(f"image OCR failed: {type(exc).__name__}: {exc}") from exc
    by_page = dict(page_texts)
    missing = [number for number in selected if number not in by_page]
    if missing:
        raise ExtractionFailed(f"the extractor returned no output for physical pages {missing}")

    markdown = "".join(by_page[number] for number in selected)
    markdown_sha = sha256_text(markdown)
    state: dict[str, Any] = {"order": 0, "in_references": False, "entries": []}
    blocks: list[dict[str, Any]] = []
    base = 0
    for number in selected:
        text = by_page[number]
        blocks.extend(build_page_blocks(number, text, base, markdown_sha, state, markdown))
        base += len(text)

    extractor_options: dict[str, Any] = {
        "image_ocr": use_ocr,
        "pymupdf4llm": {"function": "to_markdown", "page_chunks": True, "header_footer": "package defaults"},
        "pymupdf4llm_version": pymupdf4llm.__version__,
        "pymupdf_version": pymupdf.VersionBind,
        "observer": f"{OBSERVER_NAME}/{OBSERVER_VERSION}",
    }
    if use_ocr:
        extractor_options["image_ocr_model"] = ocr_model
    from zotero_pdf_text import converter

    extractor = {
        "name": converter.PRIMARY_EXTRACTION_TOOL,
        "version": f"pymupdf4llm {pymupdf4llm.__version__} / pymupdf {pymupdf.VersionBind}",
        "options": extractor_options,
    }
    opt_hash = options_sha256(extractor_options)
    pdf_sha = sha256_bytes(pdf_bytes)
    complete = selected == list(range(1, page_count + 1))
    identity: dict[str, Any] = {"tool": extractor["name"], "version": extractor["version"], "options_sha256": opt_hash}
    if ocr_model:
        identity["model"] = ocr_model
    tag = "whole" if complete else "p" + "-".join(str(n) for n in selected[:8])
    observation: dict[str, Any] = {
        "schema_version": 1,
        "record_kind": "observation",
        "record_id": f"obs-{pdf_sha[:12]}-{opt_hash[:8]}-{tag}"[:128],
        "document": {"pdf_sha256": pdf_sha, "title": title, "page_count": page_count},
        "scope": {"kind": "complete_document" if complete else "pages", "physical_pages": selected},
        "pages": page_entries,
        "blocks": blocks,
        "provenance": {
            "produced_by": identity,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    }
    if state["entries"]:
        observation["references"] = {"entries": state["entries"], "markers": []}
    elapsed = time.perf_counter() - started
    validate_record(observation)
    return ObservationResult(observation=observation, markdown=markdown, extractor=extractor, elapsed_s=elapsed)
