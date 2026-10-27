"""Physical PDF page offsets for converted text.

A page map is a list of ``(page, start_char, end_char)`` triples: ``page`` is the physical,
1-based PDF page index (never a printed journal page number), and the half-open range addresses
the stored text an index record holds (the converted Markdown body without front matter). The
ranges are contiguous and in order, so concatenating their slices reconstructs that text exactly.
Pages that produced no text have no range.

A map is only ever measured, never estimated: text whose page boundaries were not recorded at
conversion time has no map, and readers say so instead of guessing.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

from ._atomic import atomic_write_text

PageMap = list[tuple[int, int, int]]

PAGE_MAP_SIDECAR_SUFFIX = ".pages.json"
PAGE_MAP_SIDECAR_VERSION = 1
# The plain-text fallback extractor writes "## Page N" before each page's text.
_FALLBACK_PAGE_MARKER_RE = re.compile(r"^## Page (\d+)$", re.MULTILINE)


def page_map_sidecar_path(markdown_path: Path) -> Path:
    return markdown_path.with_suffix(PAGE_MAP_SIDECAR_SUFFIX)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_map_for_body(pages: Iterable[tuple[int, str]], body: str) -> PageMap | None:
    """Map per-page extractor output onto ``body``, the stored text derived from it.

    ``body`` must be the concatenation of the page texts with only leading and trailing
    whitespace removed (what front matter assembly and front-matter stripping do); anything else
    means the text was rewritten after extraction and the page boundaries are unknown, so no map.
    """
    pages = list(pages)
    joined = "".join(text for _page, text in pages)
    if joined.strip() != body:
        return None
    lead = len(joined) - len(joined.lstrip())
    result: PageMap = []
    offset = -lead
    for page, text in pages:
        start, end = max(offset, 0), min(offset + len(text), len(body))
        offset += len(text)
        if start < end:
            result.append((page, start, end))
    return result


def page_map_from_fallback_markers(text: str) -> PageMap | None:
    """Recover a page map from the fallback extractor's ``## Page N`` markers.

    Each page's range runs from its marker to the next marker, so the separator between pages
    belongs to the page before it. Returns None unless the text starts with a marker and the page
    numbers strictly increase, because only then is every character attributable to a page.
    """
    markers = list(_FALLBACK_PAGE_MARKER_RE.finditer(text))
    if not markers or markers[0].start() != 0:
        return None
    pages = [int(match.group(1)) for match in markers]
    if any(later <= earlier for earlier, later in zip(pages, pages[1:])):
        return None
    starts = [match.start() for match in markers] + [len(text)]
    return [(page, starts[i], starts[i + 1]) for i, page in enumerate(pages)]


def write_page_map_sidecar(markdown_path: Path, body: str, page_map: PageMap) -> None:
    payload = {
        "version": PAGE_MAP_SIDECAR_VERSION,
        "body_sha256": text_sha256(body),
        "pages": [list(entry) for entry in page_map],
    }
    atomic_write_text(page_map_sidecar_path(markdown_path), json.dumps(payload))


def read_page_map_sidecar(markdown_path: Path, body: str) -> PageMap | None:
    """Return the sidecar's page map if it was recorded for exactly this ``body``.

    The sidecar carries the hash of the text it describes, so a map left behind by an earlier
    conversion, or by text rewritten since (equation OCR, figure enrichment), is ignored.
    """
    try:
        payload = json.loads(page_map_sidecar_path(markdown_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("body_sha256") != text_sha256(body):
        return None
    return validate_page_map(payload.get("pages"), len(body))


def validate_page_map(value: object, text_len: int) -> PageMap | None:
    """Return ``value`` as a page map if it is well formed for text of ``text_len``, else None."""
    if not isinstance(value, list):
        return None
    result: PageMap = []
    previous_end = 0
    previous_page = 0
    for entry in value:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 3 and all(type(v) is int for v in entry)):
            return None
        page, start, end = entry
        if page <= previous_page or start != previous_end or end <= start:
            return None
        result.append((page, start, end))
        previous_end, previous_page = end, page
    if result and previous_end != text_len:
        return None
    return result


def page_range(page_map: PageMap, start_char: int, end_char: int) -> tuple[int, int] | None:
    """Return the first and last physical page that ``[start_char, end_char)`` touches."""
    if not page_map or end_char <= start_char:
        return None
    starts = [start for _page, start, _end in page_map]
    first = max(bisect.bisect_right(starts, start_char) - 1, 0)
    last = max(bisect.bisect_right(starts, end_char - 1) - 1, 0)
    return page_map[first][0], page_map[last][0]
