"""Page-level heuristics that decide which pages are *candidates* for a pilot stratum.

Host-only (needs PyMuPDF and cached PDFs). Nothing here labels a page: a passing page still needs a
human look, and a failing page only means the heuristic failed. The SHA-256 of this file is stored
in the manifest (``page_check_source_sha256``), so any change to a heuristic makes ``--check`` fail
until the manifest is rebuilt and reviewed.

Table pages are chosen by a detector-independent signal, the printed table caption, so selection
is not biased towards tables the extractor's own grid detector finds. Ruled versus unruled geometry
is only reported as an attribute. Equation pages need display-math lines (isolated or numbered),
not just math characters.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from pathlib import Path
from typing import Any

import pymupdf

from .pilot import PageChecks

_HEADING = re.compile(
    r"^[\d\W]*(references|bibliography|literature|literatur|literaturverzeichnis|r[ée]f[ée]rences|"
    r"bibliographie|referencias|bibliograf[ií]a|references cited)\W*$", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:19|20)\d\d[a-z]?\b")
_NUMBERED = re.compile(r"^\s*\[?\d{1,3}[\].)]\s+\S")
_MATH = re.compile("[Ͱ-Ͽ∀-⋿←-⇿\U0001d400-\U0001d7ff±×÷∞]")
_OPERATOR = re.compile(r"[=<>+−±×≤≥≠≈]")
_EQ_NUMBER = re.compile(r"\(\s*[A-Z]?\d{1,3}(?:\.\d{1,2})?[a-z]?\s*\)\s*$")
_WORD = re.compile(r"[A-Za-zÀ-ɏ]{4,}")
# "Table 3." / "TABLE 3:" / "Tabelle 2" at the start of a block; a cross-reference such as
# "Table 3 shows ..." continues with a lowercase word and is not a caption.
_CAPTION = re.compile(
    r"^\s*(?:table|tab\.|tabelle|tableau|tabla|cuadro)\s+(?:S?\d{1,3}|[IVX]{1,5})(?P<rest>.*)", re.IGNORECASE | re.DOTALL)


class PdfProblem(RuntimeError):
    """A pinned PDF is missing from the cache or does not match its hash."""


def source_sha256() -> str:
    """Hash of this file (line endings normalized): the identity of the heuristics."""
    return hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


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


def geometry_refusal(page: pymupdf.Page) -> str | None:
    """A rotated page with a CropBox smaller than its MediaBox is refused rather than guessed at."""
    if page.rotation and tuple(page.cropbox) != tuple(page.mediabox):
        return "rotated page with a CropBox different from its MediaBox"
    return None


def text_blocks(page: pymupdf.Page) -> list[tuple[float, float, float, float, str]]:
    return [(b[0], b[1], b[2], b[3], b[4]) for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]


def image_share(page: pymupdf.Page) -> float:
    covered = 0.0
    for image in page.get_image_info():
        x0, y0, x1, y1 = image["bbox"]
        covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return covered / max(page.rect.width * page.rect.height, 1.0)


def has_table_caption(page: pymupdf.Page) -> bool:
    """A text block that starts like a table caption ("Table 2. ..."), not a running-text cross-reference."""
    for block in text_blocks(page):
        match = _CAPTION.match(block[4])
        if not match:
            continue
        rest = match.group("rest")
        if rest[:1] in {".", ":", "|", "—", "–", "-"} or rest[:1].isspace() and rest.strip()[:1].isupper():
            return True
        if not rest.strip():
            return True
    return False


def _levels(values: list[float], tolerance: float = 2.0) -> int:
    """Number of distinct positions among ``values``, merging those within ``tolerance`` points."""
    count, last = 0, None
    for value in sorted(values):
        if last is None or value - last > tolerance:
            count += 1
        last = value
    return count


def is_ruled(page: pymupdf.Page) -> bool:
    """Reported attribute only: the page draws a full rule grid (3+ horizontal and 3+ vertical rules).

    Read from the drawing commands, not from a table detector, so it does not depend on the same
    heuristics the extractor uses. Booktabs-style tables (horizontal rules only) and tables set with
    white space alone are "unruled".
    """
    if image_share(page) >= 0.5:
        return False
    horizontal: list[float] = []
    vertical: list[float] = []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                (x0, y0), (x1, y1) = tuple(item[1]), tuple(item[2])
                if abs(y0 - y1) < 1 and abs(x0 - x1) >= 60:
                    horizontal.append(y0)
                elif abs(x0 - x1) < 1 and abs(y0 - y1) >= 15:
                    vertical.append(x0)
            elif item[0] == "re":
                rect = item[1]
                if rect.width >= 60:
                    horizontal.extend((rect.y0, rect.y1))
                if rect.height >= 15:
                    vertical.extend((rect.x0, rect.x1))
    return _levels(horizontal) >= 3 and _levels(vertical) >= 3


def display_math_lines(page: pymupdf.Page) -> int:
    """Count isolated or numbered display-math lines.

    A display line is short, has an operator (or several math symbols) and few real words (so
    pseudocode and prose are excluded), and is either numbered at the right ("(3)") or set apart by
    white space above and below and indented from the text column. Inline math in running text never
    forms such a line.
    """
    lines: list[tuple[float, float, float, float, str]] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                x0, y0, x1, y1 = line["bbox"]
                lines.append((x0, y0, x1, y1, text))
    if not lines:
        return 0
    lines.sort(key=lambda item: (item[1], item[0]))
    body_height = statistics.median(item[3] - item[1] for item in lines)
    rows: list[tuple[float, float, float, float, str]] = []
    for x0, y0, x1, y1, text in lines:  # join fragments of one typeset row (sub/superscripts, split operators)
        if rows and abs(rows[-1][1] - y0) < body_height * 0.6:
            px0, py0, px1, py1, ptext = rows[-1]
            rows[-1] = (min(px0, x0), min(py0, y0), max(px1, x1), max(py1, y1), f"{ptext} {text}")
        else:
            rows.append((x0, y0, x1, y1, text))
    lines = rows
    margin = statistics.median(item[0] for item in lines)
    count = 0
    for i, (x0, y0, x1, y1, text) in enumerate(lines):
        if len(text) > 90 or len(_WORD.findall(text)) > 3:
            continue
        signals = len(_OPERATOR.findall(text)) + len(_MATH.findall(text))
        if signals < 2:
            continue
        numbered = bool(_EQ_NUMBER.search(text)) and x1 > page.rect.width * 0.6
        above = y0 - lines[i - 1][3] if i else 99.0
        below = lines[i + 1][1] - y1 if i + 1 < len(lines) else 99.0
        isolated = above > 0.5 * body_height and below > 0.5 * body_height and x0 > margin + 12
        if numbered or isolated:
            count += 1
    return count


def page_support(stratum: str, page: pymupdf.Page) -> bool:
    """Whether this page passes the crude check for ``stratum``: a candidate, not a label."""
    page.remove_rotation()  # in memory only: judge the displayed page, as the draft's render does
    width = page.rect.width
    blocks = text_blocks(page)
    text = "".join(b[4] for b in blocks)
    if stratum == "two-column":
        left = sum(1 for b in blocks if b[2] <= 0.55 * width and b[3] - b[1] > 20)
        right = sum(1 for b in blocks if b[0] >= 0.45 * width and b[3] - b[1] > 20)
        return min(left, right) >= 3
    if stratum == "tables":
        return has_table_caption(page) and image_share(page) < 0.5
    if stratum == "equations":
        return display_math_lines(page) >= 2
    if stratum == "scans":
        return image_share(page) >= 0.6
    if stratum == "non-english":
        return len(text) >= 1000
    if stratum == "references":
        lines = [line.strip() for line in text.splitlines()]
        year_lines = sum(1 for line in lines if _YEAR.search(line))
        numbered = sum(1 for line in lines if _NUMBERED.match(line))
        return year_lines >= 3 or numbered >= 5
    raise ValueError(f"unknown stratum {stratum!r}")


def _reference_start(doc: pymupdf.Document) -> int:
    """First page of the reference list: the last page with a reference-list heading, else beyond the end."""
    headings = [n for n in range(1, doc.page_count + 1)
                if any(_HEADING.match(line.strip()) for line in doc[n - 1].get_text().splitlines())]
    return headings[-1] if headings else doc.page_count + 1


def choose_pages(
    doc: pymupdf.Document, stratum: str, wanted: int, seed: str, doc_id: str, prefer: str | None = None,
) -> tuple[list[int], int, dict[int, str]]:
    """Pick ``wanted`` candidate pages by seeded hash rank.

    Returns (pages, number of supported pages, geometry attribute of the chosen pages). For tables
    the attribute is ``ruled`` or ``unruled`` and ``prefer`` moves pages with that attribute first.
    Reference pages count only from the last reference-list heading onward (year-bearing lines also
    occur in body text); a document with no such heading supports nothing.
    """
    first = _reference_start(doc) if stratum == "references" else 1
    supported = [
        n for n in range(first, doc.page_count + 1)
        if geometry_refusal(doc[n - 1]) is None and page_support(stratum, doc[n - 1])
    ]
    attrs: dict[int, str] = {}
    if stratum == "tables":
        attrs = {n: "ruled" if is_ruled(doc[n - 1]) else "unruled" for n in supported}
    supported.sort(key=lambda n: (
        0 if prefer and attrs.get(n) == prefer else 1,
        hashlib.sha256(f"{seed}|pages|{doc_id}|{stratum}|{n}".encode()).hexdigest(),
    ))
    chosen = sorted(supported[:wanted])
    return chosen, len(supported), {n: attrs[n] for n in chosen if n in attrs}


class CachedPageChecks(PageChecks):
    """Page checks against hash-verified cached PDFs; a missing or changed PDF raises ``PdfProblem``."""

    def __init__(self, cache: Path, seed: str):
        self.cache, self.seed = cache, seed

    def _path(self, entry: dict[str, Any]) -> Path:
        status, path, detail = verify_cached(entry, self.cache)
        if status != "ok":
            raise PdfProblem(f"{entry['id']}: {status}: {detail}")
        return path

    def pages(self, entry: dict[str, Any], stratum: str, wanted: int, prefer: str | None = None) -> tuple[list[int], int, dict[int, str]]:
        with pymupdf.open(self._path(entry)) as doc:
            return choose_pages(doc, stratum, wanted, self.seed, entry["id"], prefer)

    def reference_list_found(self, entry: dict[str, Any]) -> bool:
        with pymupdf.open(self._path(entry)) as doc:
            return choose_pages(doc, "references", 1, self.seed, entry["id"])[1] > 0
