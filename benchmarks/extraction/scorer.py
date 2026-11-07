"""Score an extraction observation against a reviewed expectation (roadmap E1, package B).

Benchmark-side only: not shipped, no dependency beyond package A. Every check is evaluated twice:

- ``strict``: exact characters. Signs, decimal points, operators, case and page numbers must
  survive, and tables must match cell by cell at the same grid position. Strict *transcription*
  does treat line wraps and runs of whitespace as equal, ignores block boundaries, and strips
  Markdown markup from the *observed* text only (a heading marker at block start, bullet markers,
  paired ``**``/``__`` emphasis). Expected text, table cells, LaTeX and raw references are never
  stripped. Block segmentation is scored separately (``text_segmentation``).
- ``tolerant``: the same check after NFKC and case folding as well (DOIs also lose their
  ``doi:``/URL prefix; table header flags are ignored). Labelled as such, reported as separate
  metrics, and never able to turn a strict failure into a pass.

The verdict is tri-state and decided by strict checks alone: ``failed`` when a deciding strict check
fails; else ``incomplete`` when any expected item or family went unscored (listed in ``not_scored``)
or no strict check ran; else ``passed``. ``text_span_hash`` (byte-exact hash of a block) and
``text_segmentation`` (block boundaries) are reported but never decide the verdict, and unscored
items of those two metrics do not make it incomplete.

Check families: text presence, precision (invented content) and segmentation, reading order,
tables (cells, exactness, order, extras), equations, captions, figures, and references
(segmentation, ordinals, field parsing, identifier recovery, scored separately). Library
resolution and citation-marker links are out of scope (markers are listed as not scored).

Public API: ``score_records``, ``score_files``, ``ScoringError``, ``METRIC_VERSIONS``,
``score_to_result``, ``failure_result``.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .records import (
    RecordError,
    load_record,
    load_reviewed_expectation,
    sha256_text,
    validate_record,
    verify_output_spans,
)

SCORER_VERSION = "3"
_METRICS = (
    "text_presence", "text_precision", "text_segmentation", "text_span_hash", "reading_order",
    "table_cell_match", "table_exact", "table_order", "table_precision", "equation_match",
    "equation_precision", "caption_match", "caption_precision", "figure_match",
    "reference_segmentation_recall", "reference_segmentation_precision", "reference_ordinal",
    "reference_field_accuracy", "reference_doi_recovery", "reference_doi_absent_correct",
    "output_span_roundtrip",
)
METRIC_VERSIONS: dict[str, str] = {name: SCORER_VERSION for name in _METRICS}

# Reported but never decide the verdict: layout, and the byte-exact hash.
_INFORMATIONAL = frozenset({"text_segmentation", "text_span_hash"})

# Running headers/footers are not body text and extractors legitimately drop them; captions,
# tables, equations and reference entries have their own families.
_TEXT_TYPES = frozenset({"heading", "paragraph", "list_item", "footnote", "other"})

# Separators an extractor may legitimately put between the fragments of a merged block.
_JOINERS = ("", " ", "\n", "\n\n")

_HEADING_START = re.compile(r"\A\s*#{1,6}[ \t]+")
_BULLET = re.compile(r"^[ \t]*[-*+\u2022][ \t]+")
# Paired strong emphasis only. The markers must not touch a word character on the outside and must
# hug non-space text on the inside, so ``x**2 + y**2``, ``a ** b`` and ``a * b`` are left alone.
_STRONG = re.compile(r"(?<![\w*])(\*\*|__)(?=[^\s*_])(.+?)(?<=[^\s*_])\1(?![\w*])", re.S)

Norm = Callable[[str], str]
Pair = tuple[int, int]


class ScoringError(ValueError):
    """The inputs cannot be scored (wrong record kind, unreviewed truth, or a PDF mismatch)."""


def _strict(text: str) -> str:
    """Strict transcription view: exact characters, runs of whitespace (and line wraps) equal."""
    return " ".join(text.split())


def _loose(text: str) -> str:
    return unicodedata.normalize("NFKC", _strict(text)).casefold()


def _loose_doi(text: str) -> str:
    value = _loose(text)
    value = re.sub(r"^(doi:\s*|https?://(dx\.)?doi\.org/)", "", value)
    return value.rstrip(".,;")


def _strip_markup(text: str) -> str:
    """Markdown markup an extractor adds to *observed* block text: a heading marker at the start of
    the block, bullet markers (``-``, ``*``, ``+``, bullet) when the block's first line is a list
    item, and paired ``**``/``__`` emphasis. Ordered-list numbers are kept (they are printed text,
    like section numbers), single ``*``/``_`` are kept, and expected text is never passed through
    here. An expected text that itself starts with ``#`` or a bullet therefore cannot match."""
    heading = _HEADING_START.sub("", text, count=1)
    if heading != text:
        text = heading
    else:
        lines = text.split("\n")
        if _BULLET.match(lines[0]):
            text = "\n".join(_BULLET.sub("", line, count=1) for line in lines)
    return _STRONG.sub(r"\2", text)


_MODES: tuple[tuple[str, Norm], ...] = (("strict", _strict), ("tolerant", _loose))
# The same normalizations for observed block text, after markup stripping.
def _observed(norm: Norm) -> Norm:
    def view(text: str) -> str:
        return norm(_strip_markup(text))

    return view


_OBS_NORMS: dict[str, Norm] = {mode: _observed(norm) for mode, norm in _MODES}


class _Checks:
    """Collects checks and unscored items, and derives metrics and the verdict from them."""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.not_scored: list[dict[str, str]] = []

    def add_mode(self, name: str, mode: str, subject: str, pair: Pair, detail: str = "") -> None:
        matched, total = pair
        check: dict[str, Any] = {
            "name": name, "mode": mode, "subject": subject, "passed": matched == total,
            "matched": matched, "total": total, "detail": detail,
            "decides": mode == "strict" and name not in _INFORMATIONAL,
        }
        self.checks.append(check)

    def add(self, name: str, subject: str, strict: Pair, tolerant: Pair, detail: str = "", *,
            strict_only: bool = False) -> None:
        """Both modes; tolerant is never below strict."""
        if tolerant[0] < strict[0]:
            tolerant = strict
        self.add_mode(name, "strict", subject, strict, detail)
        if not strict_only:
            self.add_mode(name, "tolerant", subject, tolerant, detail)

    def mark_tolerant_only(self) -> None:
        """Flag tolerant passes whose strict twin failed: matches that establish no fidelity."""
        strict_failed = {(c["name"], c["subject"]) for c in self.checks if c["mode"] == "strict" and not c["passed"]}
        for check in self.checks:
            if check["mode"] == "tolerant":
                check["tolerant_only"] = check["passed"] and (check["name"], check["subject"]) in strict_failed

    def skip(self, metric: str, subject: str, reason: str) -> None:
        self.not_scored.append({"metric": metric, "subject": subject, "reason": reason})

    def metrics(self) -> list[dict[str, Any]]:
        sums: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        for check in self.checks:
            acc = sums[(check["name"], check["mode"])]
            acc[0] += check["matched"]
            acc[1] += check["total"]
        return [
            {"name": name, "version": METRIC_VERSIONS[name], "mode": mode,
             "value": matched / total, "n": total}
            for (name, mode), (matched, total) in sorted(sums.items()) if total
        ]


def _ok(matched: bool) -> Pair:
    return (1, 1) if matched else (0, 1)


def _skip_reason(item: dict[str, Any], noun: str) -> str | None:
    """Why an expected item is not compared, or None. A truncated item (cut off at the scope
    boundary of a page-scoped record) is never compared, not even on its visible part: whether the
    observation cut at the same place is not something the expectation vouches for."""
    if item.get("uncertainty", {}).get("level") == "high":
        return f"expected {noun} marked high uncertainty"
    if "truncated" in item:
        return f"expected {noun} truncated at the scope boundary ({item['truncated']}); not compared"
    return None


# --- blocks, chains, observed text stream -----------------------------------------------------


def _page_blocks(record: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    pages: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for block in sorted(record["blocks"], key=lambda b: b["reading_order"]):
        pages[block["page"]].append(block)
    return pages


def _chains(record: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Blocks grouped into ``continues_from`` chains in chain order (singletons included), ordered
    by the reading order of each chain's first block. Content that crosses a page or column break is
    a chain, and an extractor may emit it as fragments or merged."""
    following = {b["continues_from"]: b for b in record["blocks"] if "continues_from" in b}
    ids = {b["id"] for b in record["blocks"]}
    chains = []
    for block in sorted(record["blocks"], key=lambda b: b["reading_order"]):
        if block.get("continues_from") in ids:
            continue
        chain = [block]
        while chain[-1]["id"] in following:
            chain.append(following[chain[-1]["id"]])
        chains.append(chain)
    return chains


def _chain_pages(chain: list[dict[str, Any]]) -> set[int]:
    return {b["page"] for b in chain}


def _chain_id(chain: list[dict[str, Any]]) -> str:
    return "+".join(b["id"] for b in chain)


def _bounded(hay: str, needle: str, i: int) -> bool:
    """A match may not start or end inside a word: if the match edge is alphanumeric, the character
    next to it must not be (so ``5`` is not found in ``51`` and ``cat`` not in ``concat``)."""
    j = i + len(needle)
    before = i == 0 or not (needle[0].isalnum() and hay[i - 1].isalnum())
    after = j >= len(hay) or not (needle[-1].isalnum() and hay[j].isalnum())
    return before and after


def _search(hay: str, needle: str, start: int = 0, end: int | None = None,
            taken: list[tuple[int, int]] | None = None, pages: Callable[[int, int], bool] | None = None) -> int | None:
    """First word-bounded occurrence of ``needle`` in ``hay[start:end]`` that overlaps no ``taken``
    interval (and satisfies ``pages``, if given)."""
    if not needle:
        return None
    limit = len(hay) if end is None else end
    i = hay.find(needle, start)
    while i >= 0 and i + len(needle) <= limit:
        if _bounded(hay, needle, i) and not any(i < b and i + len(needle) > a for a, b in taken or ()):
            if pages is None or pages(i, i + len(needle)):
                return i
        i = hay.find(needle, i + 1)
    return None


def _occurrences(hay: str, needle: str, start: int, end: int,
                 taken: list[tuple[int, int]] | None = None) -> list[int]:
    """Every word-bounded, unconsumed occurrence of ``needle`` in ``hay[start:end]``."""
    found: list[int] = []
    pos = start
    while (i := _search(hay, needle, pos, end, taken)) is not None:
        found.append(i)
        pos = i + 1
    return found


class _Stream:
    """The observed text of all pages in reading order under one normalization: blocks are joined
    by a single space, so block boundaries and line wraps do not matter when searching."""

    def __init__(self, observed: dict[int, list[dict[str, Any]]], norm: Norm):
        self.spans: list[tuple[int, int, int, dict[str, Any]]] = []
        pieces = []
        pos = 0
        for page in sorted(observed):
            for block in observed[page]:
                if not isinstance(block.get("text"), str):
                    continue
                text = norm(block["text"])
                if text:
                    self.spans.append((pos, pos + len(text), page, block))
                    pieces.append(text)
                    pos += len(text) + 1
        self.text = " ".join(pieces)

    def _page_at(self, offset: int) -> int | None:
        for start, end, page, _ in self.spans:
            if start <= offset <= end:
                return page
        return None

    def span_of(self, offset: int) -> tuple[int, int] | None:
        """(start, end) of the observed block holding ``offset``."""
        for start, end, _, _ in self.spans:
            if start <= offset <= end:
                return (start, end)
        return None

    def find(self, text: str, start: int, pages: set[int], taken: list[tuple[int, int]]) -> int | None:
        """First unconsumed occurrence at or after ``start`` lying entirely on ``pages``."""
        return _search(self.text, text, start, None, taken,
                       lambda a, b: {self._page_at(a), self._page_at(b - 1)} <= pages)

    def find_merged(self, text: str, neighbour: str, pages: set[int], taken: list[tuple[int, int]],
                    before: bool) -> int | None:
        """Unconsumed occurrence of ``text`` inside an observed block (on ``pages``) that also holds
        the chain neighbour's text: the allowance for a block merged across a page or column break,
        wherever the extractor attributed it. Inside the block the fragments must keep their order
        (``before``: ``text`` must precede the neighbour, else follow it), so the allowance cannot
        hide a reordering."""
        for start, end, page, _ in self.spans:
            if page not in pages:
                continue
            others = _occurrences(self.text, neighbour, start, end)
            for found in _occurrences(self.text, text, start, end, taken):
                # A distinct, correctly ordered pair: identical adjacent fragments need two copies.
                if any(n >= found + len(text) if before else found >= n + len(neighbour) for n in others):
                    return found
        return None


def _linked_reference_blocks(exp: dict[str, Any], chains: list[list[dict[str, Any]]]) -> set[str]:
    """Ids of the blocks (whole chains) that a reference entry points at: their text is the entry's."""
    linked: set[str] = set()
    for entry in exp.get("references", {}).get("entries", []):
        if "block_id" in entry:
            linked |= {b["id"] for b in _chain_index(chains, entry["block_id"])}
    return linked


def _fragments(exp: dict[str, Any], out: _Checks) -> list[dict[str, Any]]:
    """Scorable expected text fragments in reading order, each with its chain neighbours. Body text
    types, plus reference-entry blocks that no entry covers (an expectation may record the
    bibliography as blocks only, and losing it must not pass)."""
    chains = _chains(exp)
    linked = _linked_reference_blocks(exp, chains)
    result = []
    for chain in chains:
        if chain[0]["type"] not in _TEXT_TYPES and not (
                chain[0]["type"] == "reference_entry" and chain[0]["id"] not in linked):
            continue
        last: dict[str, Any] | None = None
        for block in chain:
            reason = _skip_reason(block, "block")
            if not isinstance(block.get("text"), str) or not block["text"]:
                out.skip("text_presence", block["id"], reason or "expected block states no text (explicit null); not compared")
            elif reason:
                out.skip("text_presence", block["id"], reason)
            else:
                entry = {"block": block, "chain": chain, "previous": last["block"] if last else None, "next": None}
                if last is not None:
                    last["next"] = block
                result.append(entry)
                last = entry
                continue
            last = None
    return sorted(result, key=lambda f: f["block"]["reading_order"])


def _place(frag: dict[str, Any], stream: _Stream, norm: Norm, cursor: int,
           taken: list[tuple[int, int]], placed: dict[str, tuple[str, int | None]]) -> tuple[str, int, int | None]:
    """Locate one fragment: (status, new cursor, offset). Status is ``ok`` (found, and in order),
    ``displaced`` (found, but only before the previous match) or ``missing``. The fragment must lie
    on its own page; only a block that merges it with a chain neighbour may sit elsewhere. Each
    occurrence in the observed text is consumed by one fragment, so a duplicated expected
    paragraph needs two observed ones."""
    block = frag["block"]
    text, pages = norm(block["text"]), {block["page"]}
    forward = stream.find(text, cursor, pages, taken)
    if forward is not None:
        taken.append((forward, forward + len(text)))
        return "ok", forward + len(text), forward
    for neighbour, before in ((frag["previous"], False), (frag["next"], True)):
        if neighbour is None:
            continue
        merged = stream.find_merged(text, norm(neighbour["text"]), _chain_pages(frag["chain"]), taken, before)
        if merged is None:
            continue
        taken.append((merged, merged + len(text)))
        if merged >= cursor:
            return "ok", merged + len(text), merged
        # Before the cursor: legitimate only for a later fragment whose predecessor was itself placed
        # in this very block (the block holds the chain, and sits where the head sits, so unrelated
        # content such as a footnote may interleave in the expectation). Otherwise the merged block
        # is displaced relative to the surrounding content.
        previous_at = None if before else placed.get(neighbour["id"], ("", None))[1]
        if previous_at is not None and stream.span_of(previous_at) == stream.span_of(merged):
            return "ok", cursor, merged
        return "displaced", cursor, merged
    anywhere = stream.find(text, 0, pages, taken)
    if anywhere is not None:
        taken.append((anywhere, anywhere + len(text)))
        return "displaced", cursor, anywhere
    return "missing", cursor, None


def _check_text(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> None:
    observed = _page_blocks(obs)
    streams = {mode: _Stream(observed, _OBS_NORMS[mode]) for mode, _ in _MODES}
    frags = _fragments(exp, out)

    placements: dict[str, dict[str, tuple[str, int | None]]] = {mode: {} for mode, _ in _MODES}
    for mode, norm in _MODES:
        cursor = 0
        taken: list[tuple[int, int]] = []
        for frag in frags:
            status, cursor, offset = _place(frag, streams[mode], norm, cursor, taken, placements[mode])
            placements[mode][frag["block"]["id"]] = (status, offset)

    for frag in frags:
        block = frag["block"]
        bid = block["id"]
        found = {m: placements[m][bid][0] != "missing" for m, _ in _MODES}
        detail = ""
        if not found["strict"]:
            text = _strict(block["text"])
            elsewhere = sorted(p for p in observed if p != block["page"]
                               and streams["strict"].find(text, 0, {p}, []) is not None)
            detail = f"found only on page(s) {elsewhere}" if elsewhere else "not found on the expected page"
        out.add("text_presence", bid, _ok(found["strict"]), _ok(found["tolerant"]), detail)

    _check_segmentation(frags, observed, out)
    _check_hashes(exp, observed, out)
    _check_order(frags, placements, out)


def _check_segmentation(frags: list[dict[str, Any]], observed: dict[int, list[dict[str, Any]]], out: _Checks) -> None:
    """Block boundaries: does one observed block hold exactly the expected block's text (or, for a
    chain, exactly the fragment or the whole joined chain)? Layout, so informational."""
    for frag in frags:
        block, chain = frag["block"], frag["chain"]
        results = {}
        for mode, norm in _MODES:
            wanted = {norm(block["text"])}
            if len(chain) > 1 and all(isinstance(b.get("text"), str) for b in chain):
                wanted.add(norm(" ".join(b["text"] for b in chain)))
            pages = _chain_pages(chain)
            seen = _OBS_NORMS[mode]
            results[mode] = _ok(any(isinstance(b.get("text"), str) and seen(b["text"]) in wanted
                                    for p in pages for b in observed.get(p, [])))
        out.add("text_segmentation", block["id"], results["strict"], results["tolerant"],
                "an observed block holds exactly this text (boundaries differ otherwise)")


def _check_hashes(exp: dict[str, Any], observed: dict[int, list[dict[str, Any]]], out: _Checks) -> None:
    """Byte-exact: some observed block's UTF-8 hash equals the expected hash (or, for a chain, each
    fragment's, or the fragments joined by an ordinary separator). Informational."""
    for chain in _chains(exp):
        if chain[0]["type"] not in _TEXT_TYPES:
            continue
        texts = [b for b in chain if isinstance(b.get("text"), str) and b["text"]]
        if not texts or not any("text_sha256" in b for b in texts):
            continue
        if len(texts) != len(chain) or any(_skip_reason(b, "block") for b in chain):
            out.skip("text_span_hash", _chain_id(chain), "chain has unscored or textless fragments")
            continue
        pages = _chain_pages(chain)
        hashes = {sha256_text(b["text"]) for p in pages for b in observed.get(p, []) if isinstance(b.get("text"), str)}
        fragments_ok = all(sha256_text(b["text"]) in hashes for b in texts)
        merged_ok = any(
            sha256_text(joiner.join(b["text"] for b in texts)) in hashes for joiner in _JOINERS)
        hit = fragments_ok or merged_ok
        out.add("text_span_hash", _chain_id(chain), _ok(hit), _ok(hit),
                "" if hit else "no observed block round-trips byte-exactly to the expected hash "
                "(a chain: each fragment, or the fragments joined by '', ' ', newline or blank line)",
                strict_only=True)


def _check_order(frags: list[dict[str, Any]], placements: dict[str, dict[str, tuple[str, int | None]]],
                 out: _Checks) -> None:
    """Each located fragment after the first must be found after its predecessor's match (or inside
    a merged block). Displaced fragments fail; unlocated ones are listed, never charged here."""
    for mode, _ in _MODES:
        first = True
        for frag in frags:
            bid = frag["block"]["id"]
            status, _ = placements[mode][bid]
            if status == "missing":
                if mode == "strict":
                    out.skip("reading_order", bid, "block not found, order not assessed (see text_presence)")
                continue
            if first:
                first = False
                continue
            out.add_mode("reading_order", mode, bid, _ok(status == "ok"),
                         "" if status == "ok" else "found only before an earlier block's match")


_MD_TABLE_RULE = re.compile(r"(?m)^[\s|:\-]+$")


def _is_payload_text(block: dict[str, Any], mode: str, norm: Norm) -> bool:
    """Does an observed block's optional ``text`` merely serialize its structured payload? For an
    equation: the LaTeX, ignoring whitespace. For a table: only the cell texts, each used once, with
    separators and Markdown table markup (pipes, rule rows) tolerated. Anything else is content."""
    text = _OBS_NORMS[mode]
    squeeze = "".join
    if block.get("equation") is not None:
        latex = block["equation"].get("latex")
        return latex is not None and squeeze(text(block["text"]).split()) == squeeze(norm(latex).split())
    if block.get("table") is not None:
        rest = squeeze(text(_MD_TABLE_RULE.sub(" ", block["text"]).replace("|", " ")).split())
        for cell in sorted((squeeze(norm(c["text"]).split()) for c in block["table"]["cells"]), key=len, reverse=True):
            if cell:
                at = rest.find(cell)
                if at >= 0:
                    rest = rest[:at] + rest[at + len(cell):]
        return not rest
    return False


def _check_precision(exp: dict[str, Any], obs: dict[str, Any], out: _Checks, matched: dict[str, set[str]]) -> None:
    """Invented content: observed text, in every block that has text, that no expected text accounts
    for. Each expected text (block text and raw reference entries) covers one word-bounded occurrence,
    so extra copies, joined words and appended digits count as invented. Reviewed expectations must
    record every printed header, footer and page number on in-scope pages: there is no exemption for
    them here. Pages where the expectation has truncated or uncertain text are not assessed. The
    optional ``text`` of an observed table or equation block whose structured payload matched an
    expected item reproduces that item and is accounted for (``matched``: block ids per mode); the
    same text on an unmatched block is still invented."""
    observed = _page_blocks(obs)
    streams = {mode: _Stream(observed, _OBS_NORMS[mode]) for mode, _ in _MODES}
    scope = set(exp["scope"]["physical_pages"])
    unsure = {b["page"] for b in exp["blocks"] if isinstance(b.get("text"), str) and _skip_reason(b, "block")}
    # A reference recorded both as a block and as a linked entry is one printed text: the entry's raw
    # text covers it, and the block texts of its chain are the fallback if the raw text is not
    # contiguous in the observation. Counting both would pay for two occurrences.
    chains = _chains(exp)
    linked = _linked_reference_blocks(exp, chains)
    items: list[tuple[str, list[str]]] = []
    for entry in exp.get("references", {}).get("entries", []):
        fallback: list[str] = []
        if "block_id" in entry:
            fallback = [b["text"] for b in _chain_index(chains, entry["block_id"]) if isinstance(b.get("text"), str)]
        items.append((entry["raw"], fallback))
    items += [(b["text"], []) for b in exp["blocks"] if isinstance(b.get("text"), str) and b["id"] not in linked]
    covered: dict[str, dict[str, Pair]] = {}
    for mode, norm in _MODES:
        stream = streams[mode]
        mask = bytearray(len(stream.text))
        # Decide the payload exemption first and keep exempt text out of ordinary allocation, so a
        # structured block's serialized payload cannot use up a genuine paragraph's occurrence.
        exempt = {block["id"] for _, _, _, block in stream.spans
                  if block["id"] in matched[mode] and _is_payload_text(block, mode, norm)}
        taken: list[tuple[int, int]] = [(start, end) for start, end, _, block in stream.spans
                                        if block["id"] in exempt]

        whole = {(start, end) for start, end, _, _ in stream.spans}

        def fits(a: int, b: int, tier: int) -> bool:
            if tier == 1:  # exactly one observed block
                return (a, b) in whole
            if tier == 2:  # inside one observed block
                return any(start <= a and b <= end for start, end, _, _ in stream.spans)
            return True  # a span across blocks

        def cover(text: str, tier: int) -> bool:
            # Only in-scope observed text can be accounted for: an out-of-scope copy must not use
            # up the expected occurrence.
            i = _search(stream.text, text, 0, None, taken, lambda a, b: (
                {stream._page_at(a), stream._page_at(b - 1)} <= scope and fits(a, b, tier)))
            if i is None:
                return False
            mask[i:i + len(text)] = b"\x01" * len(text)
            taken.append((i, i + len(text)))
            return True

        # Whole-block matches are allocated first, then within-block, then spans across blocks, so a
        # cross-block coincidence (``A Beta`` + ``Gamma`` containing ``Beta Gamma``) cannot steal the
        # occurrence a real block needs. Longest first within a tier.
        pending = sorted(((norm(t), f) for t, f in items if t), key=lambda it: len(it[0]), reverse=True)
        for tier in (1, 2, 3):
            pending = [(text, fallback) for text, fallback in pending if not cover(text, tier)]
        for _, fallback in pending:
            for part in fallback:
                if part:
                    cover(norm(part), 3)
        for start, end, page, block in stream.spans:
            if page not in scope:
                continue
            letters = [k for k in range(start, end) if stream.text[k] != " "]
            accounted = block["id"] in exempt
            covered.setdefault(block["id"], {})[mode] = (
                len(letters) if accounted else sum(mask[k] for k in letters), len(letters))
    pages = {b["id"]: b["page"] for b in obs["blocks"]}
    for bid, modes in covered.items():
        if pages[bid] in unsure:
            out.skip("text_precision", bid, "page has truncated or uncertain expected text; not assessed")
            continue
        if modes["strict"][1]:
            out.add("text_precision", bid, modes["strict"], modes["tolerant"],
                    "observed text with no counterpart in the expected text (invented content)")


def _precision(name: str, observed: list[tuple[str, set[int]]], matched: dict[str, set[str]], scope: set[int],
               unsure: set[int], out: _Checks) -> None:
    """One check per observed item in scope that no expected item accounts for (extras fail)."""
    for ident, pages in observed:
        if not pages & scope:
            continue
        if pages & unsure:
            out.skip(name, ident, "page has an unscored expected item of this kind; not assessed")
            continue
        out.add(name, ident, _ok(ident in matched["strict"]), _ok(ident in matched["tolerant"]),
                "observed item with no counterpart in the expectation")


# --- tables, equations, captions ----------------------------------------------------------------


def _cells(table: dict[str, Any], norm: Norm, headers: bool) -> dict[tuple[int, int], tuple[Any, ...]]:
    out = {}
    for c in table["cells"]:
        cell: tuple[Any, ...] = (c.get("rowspan", 1), c.get("colspan", 1), norm(c["text"]))
        out[(c["row"], c["col"])] = cell + ((bool(c.get("is_header")),) if headers else ())
    return out


def _table_match(exp_table: dict[str, Any], obs_table: dict[str, Any], norm: Norm, headers: bool) -> int:
    obs = _cells(obs_table, norm, headers)
    return sum(1 for pos, cell in _cells(exp_table, norm, headers).items() if obs.get(pos) == cell)


def _header_rows(table: dict[str, Any]) -> list[tuple[tuple[int, str], ...]]:
    """Leading rows whose every cell is a header cell, as (col, text) tuples."""
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cell in table["cells"]:
        rows[cell["row"]].append(cell)
    lead = []
    for r in range(table["n_rows"]):
        if not rows[r] or not all(c.get("is_header") for c in rows[r]):
            break
        lead.append(tuple(sorted((c["col"], c["text"]) for c in rows[r])))
    return lead


def _logical_table(chain: list[dict[str, Any]]) -> dict[str, Any]:
    """One table from a chain: fragments' rows concatenated in chain order (row indices restart per
    fragment). A header row repeated on a continuation page (identical to the first fragment's
    header) is page furniture, not data, and is dropped so a correct merge compares equal."""
    first = chain[0]["table"]
    lead = _header_rows(first)
    cells: list[dict[str, Any]] = []
    offset = 0
    for index, block in enumerate(chain):
        table = block["table"]
        skip = len(lead) if index and lead and _header_rows(table)[:len(lead)] == lead else 0
        cells.extend({**c, "row": c["row"] - skip + offset} for c in table["cells"] if c["row"] >= skip)
        offset += table["n_rows"] - skip
    return {"n_rows": offset, "n_cols": first["n_cols"], "cells": cells}


def _table_chains(record: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]], set[int], dict[str, Any]]]:
    return [(_chain_id(c), c, _chain_pages(c), _logical_table(c)) for c in _chains(record)
            if c[0]["type"] == "table" and all("table" in b for b in c)]


def _assign(scores: list[list[int | None]]) -> list[int | None]:
    """Assignment of expected rows to distinct candidate columns maximizing the total score
    (``None`` = not allowed). Exhaustive over candidate subsets (no dependency); greedy beyond 16
    candidates. Ties prefer assigning a candidate over leaving the row unmatched."""
    n, m = len(scores), len(scores[0]) if scores else 0
    if m > 16:
        taken: set[int] = set()
        result: list[int | None] = []
        for row in scores:
            options = [(s, j) for j, s in enumerate(row) if s is not None and j not in taken]
            pick = max(options, key=lambda o: (o[0], -o[1]))[1] if options else None
            if pick is not None:
                taken.add(pick)
            result.append(pick)
        return result
    memo: dict[tuple[int, int], tuple[int, tuple[int | None, ...]]] = {}

    def best(i: int, mask: int) -> tuple[int, tuple[int | None, ...]]:
        if i == n:
            return 0, ()
        if (i, mask) in memo:
            return memo[(i, mask)]
        top: tuple[int, tuple[int | None, ...]] | None = None
        for j in range(m):
            s = scores[i][j]
            if s is not None and not mask >> j & 1:
                rest = best(i + 1, mask | 1 << j)
                if top is None or s + rest[0] > top[0]:
                    top = (s + rest[0], (j, *rest[1]))
        rest = best(i + 1, mask)
        if top is None or rest[0] > top[0]:
            top = (rest[0], (None, *rest[1]))
        memo[(i, mask)] = top
        return top

    return list(best(0, 0)[1])


def _assign_groups(scores: list[list[int | None]]) -> list[int | None]:
    """``_assign`` run separately on each group of expected tables and candidates linked by a
    shared page, so the exhaustive solver only ever sees a handful of candidates."""
    n, m = len(scores), len(scores[0]) if scores else 0
    parent = list(range(n + m))

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, row in enumerate(scores):
        for j, value in enumerate(row):
            if value is not None:
                parent[root(i)] = root(n + j)
    groups: dict[int, tuple[list[int], list[int]]] = defaultdict(lambda: ([], []))
    for i in range(n):
        groups[root(i)][0].append(i)
    for j in range(m):
        groups[root(n + j)][1].append(j)
    result: list[int | None] = [None] * n
    for rows, cols in groups.values():
        if rows and cols:
            for i, pick in zip(rows, _assign([[scores[i][j] for j in cols] for i in rows]), strict=True):
                result[i] = None if pick is None else cols[pick]
    return result


def _check_tables(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> dict[str, dict[str, str]]:
    """Score tables; return, per mode, the observed table chain id matched to each expected one."""
    candidates = _table_chains(obs)
    scope = set(exp["scope"]["physical_pages"])
    wanted = []
    unsure: set[int] = set()
    for ident, chain, pages, table in _table_chains(exp):
        reasons = [r for r in (_skip_reason(b, "table") for b in chain) if r]
        if reasons:
            out.skip("table_cell_match", ident, reasons[0])
            unsure |= pages
        else:
            wanted.append((ident, chain, pages, table))
    matched_ids: dict[str, dict[str, str]] = {"strict": {}, "tolerant": {}}
    results: dict[str, dict[str, tuple[int, bool, str]]] = {"strict": {}, "tolerant": {}}
    for mode, norm in _MODES:
        headers = mode == "strict"

        def rank(table: dict[str, Any], got: dict[str, Any]) -> int:
            """Cell matches first, an identical grid as the tie-break."""
            same = (got["n_rows"], got["n_cols"], len(got["cells"])) == (
                table["n_rows"], table["n_cols"], len(table["cells"]))
            return 2 * _table_match(table, got, norm, headers) + int(same)

        scores = [[rank(table, c[3]) if c[2] & pages else None for c in candidates]
                  for _, _, pages, table in wanted]
        for i, ((ident, _, _, table), pick) in enumerate(zip(wanted, _assign_groups(scores), strict=True)):
            if pick is None:
                results[mode][ident] = (0, False, "no unclaimed observed table on the page(s)")
                continue
            cand = candidates[pick]
            matched_ids[mode][ident] = cand[0]
            value = scores[i][pick] or 0
            cells = value // 2
            results[mode][ident] = (cells, cells == len(table["cells"]) and bool(value % 2),
                                    f"matched observed table {cand[0]!r}")
    for ident, _, _, table in wanted:
        total = len(table["cells"])
        s, t = results["strict"][ident], results["tolerant"][ident]
        out.add("table_cell_match", ident, (s[0], total), (t[0], total), s[2])
        out.add("table_exact", ident, _ok(s[1]), _ok(t[1]), "grid and every cell identical")
    _check_table_order(wanted, candidates, matched_ids, out)
    _precision("table_precision", [(c[0], c[2]) for c in candidates],
               {m: set(matched_ids[m].values()) for m in matched_ids}, scope, unsure, out)
    return matched_ids


def _check_table_order(wanted: list[Any], candidates: list[Any], matched: dict[str, dict[str, str]],
                       out: _Checks) -> None:
    """Matched observed tables must keep the expected tables' relative order."""
    rank = {c[0]: (c[1][0]["page"], c[1][0]["reading_order"]) for c in candidates}
    for mode, _ in _MODES:
        placed = [(ident, matched[mode][ident]) for ident, *_ in wanted if ident in matched[mode]]
        for (a_id, a_obs), (b_id, b_obs) in zip(placed, placed[1:], strict=False):
            out.add_mode("table_order", mode, f"{a_id}->{b_id}", _ok(rank[a_obs] < rank[b_obs]),
                         "observed tables appear in the expected order")


def _norm_latex(mode: str, latex: str) -> str:
    return "".join(_loose(latex).split()) if mode == "tolerant" else _strict(latex)


def _check_equations(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> dict[str, set[str]]:
    observed = _page_blocks(obs)
    used: dict[str, set[str]] = {"strict": set(), "tolerant": set()}
    unsure: set[int] = set()
    for block in sorted(exp["blocks"], key=lambda b: b["reading_order"]):
        want = block.get("equation")
        if want is None:
            continue
        reason = _skip_reason(block, "equation")
        if reason:
            out.skip("equation_match", block["id"], reason)
            unsure.add(block["page"])
            continue
        if want.get("latex") is None and want.get("number") is None:
            out.skip("equation_match", block["id"], "expectation records neither LaTeX nor a number")
            unsure.add(block["page"])
            continue
        results = {}
        for mode, _ in _MODES:
            hit = False
            for cand in observed.get(block["page"], []):
                got = cand.get("equation")
                if got is None or cand["id"] in used[mode]:
                    continue
                if want.get("latex") is not None and (
                        got.get("latex") is None or _norm_latex(mode, got["latex"]) != _norm_latex(mode, want["latex"])):
                    continue
                if want.get("number") is not None and got.get("number") != want["number"]:
                    continue
                if want.get("display") is not None and got.get("display") != want["display"]:
                    continue
                used[mode].add(cand["id"])
                hit = True
                break
            results[mode] = _ok(hit)
        out.add("equation_match", block["id"], results["strict"], results["tolerant"],
                "LaTeX, printed number and display flag must agree (each observed equation used once)")
    _precision("equation_precision",
               [(b["id"], {b["page"]}) for b in obs["blocks"] if b.get("equation") is not None],
               used, set(exp["scope"]["physical_pages"]), unsure, out)
    return used


def _chain_index(chains: list[list[dict[str, Any]]], block_id: str) -> list[dict[str, Any]]:
    return next(c for c in chains if any(b["id"] == block_id for b in c))


def _same_target(want_chain: list[dict[str, Any]], got_chain: list[dict[str, Any]], mode: str,
                 exp_chains: list[list[dict[str, Any]]], obs_chains: list[list[dict[str, Any]]],
                 table_match: dict[str, dict[str, str]]) -> bool:
    """Does the observed caption target correspond to the expected one? Tables use the table
    assignment, so an extra earlier table cannot break the link. Other targets need the same type
    and an overlapping page, and, when both records show the same number of such blocks on the
    page, the same position among them."""
    if want_chain[0]["type"] != got_chain[0]["type"] or not _chain_pages(want_chain) & _chain_pages(got_chain):
        return False
    want_id = _chain_id(want_chain)
    if want_id in table_match[mode]:
        return table_match[mode][want_id] == _chain_id(got_chain)

    def peers(chains: list[list[dict[str, Any]]], chain: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        return [c for c in chains if c[0]["type"] == chain[0]["type"] and c[0]["page"] == chain[0]["page"]]

    want_peers, got_peers = peers(exp_chains, want_chain), peers(obs_chains, got_chain)
    return len(want_peers) != len(got_peers) or want_peers.index(want_chain) == got_peers.index(got_chain)


def _check_captions(exp: dict[str, Any], obs: dict[str, Any], out: _Checks,
                    table_match: dict[str, dict[str, str]]) -> None:
    exp_chains, obs_chains = _chains(exp), _chains(obs)
    exp_by_id = {b["id"]: b for b in exp["blocks"]}
    obs_by_id = {b["id"]: b for b in obs["blocks"]}
    continued = {b["continues_from"] for b in exp["blocks"] if "continues_from" in b}
    used: dict[str, set[str]] = {"strict": set(), "tolerant": set()}
    unsure: set[int] = set()
    for block in sorted(exp["blocks"], key=lambda b: b["reading_order"]):
        if block["type"] != "caption":
            continue
        reason = _skip_reason(block, "caption") or (
            None if isinstance(block.get("text"), str) else "caption has no text")
        if reason:
            out.skip("caption_match", block["id"], reason)
            unsure.add(block["page"])
            continue
        if "continues_from" in block or block["id"] in continued:
            out.skip("caption_match", block["id"], "caption itself spans pages; not compared")
            unsure.add(block["page"])
            continue
        target = exp_by_id.get(block.get("caption_for", ""))
        results = {}
        for mode, norm in _MODES:
            hit = False
            for cand in obs["blocks"]:
                if cand["type"] != "caption" or cand["page"] != block["page"] or not isinstance(cand.get("text"), str):
                    continue
                if cand["id"] in used[mode] or _OBS_NORMS[mode](cand["text"]) != norm(block["text"]):
                    continue
                if target is not None:
                    linked = obs_by_id.get(cand.get("caption_for", ""))
                    if linked is None or not _same_target(
                            _chain_index(exp_chains, target["id"]), _chain_index(obs_chains, linked["id"]),
                            mode, exp_chains, obs_chains, table_match):
                        continue
                used[mode].add(cand["id"])
                hit = True
                break
            results[mode] = _ok(hit)
        out.add("caption_match", block["id"], results["strict"], results["tolerant"],
                "caption text exact, on the same page, and linked to the corresponding table/figure")
    _precision("caption_precision", [(b["id"], {b["page"]}) for b in obs["blocks"] if b["type"] == "caption"],
               used, set(exp["scope"]["physical_pages"]), unsure, out)


def _check_figures(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> None:
    """Presence only: each expected figure needs an observed figure block on its page (each used
    once). Content, placement and graphic kind are not compared."""
    taken: set[str] = set()
    for block in sorted(exp["blocks"], key=lambda b: b["reading_order"]):
        if block["type"] != "figure":
            continue
        reason = _skip_reason(block, "figure")
        if reason:
            out.skip("figure_match", block["id"], reason)
            continue
        cand = next((b for b in obs["blocks"] if b["type"] == "figure" and b["page"] == block["page"]
                     and b["id"] not in taken), None)
        if cand is not None:
            taken.add(cand["id"])
        out.add("figure_match", block["id"], _ok(cand is not None), _ok(cand is not None),
                "an observed figure block on the same page (presence only)")


# --- references ---------------------------------------------------------------------------------


def _position_ok(entry: dict[str, Any], cand: dict[str, Any], lenient: bool) -> bool:
    """Same start page; an observed ``end_page`` must equal the expected one. A merged entry has one
    block, so the record rules give it no ``end_page`` and it is judged on its start page and raw
    text. A Markdown-only observation may not know its pages; the tolerant mode then accepts it on
    raw text alone, strict never does."""
    if lenient and "page" not in cand:
        return True
    return cand.get("page") == entry["page"] and cand.get("end_page") in (None, entry.get("end_page"))


def _align(exp_entries: list[dict[str, Any]], obs_entries: list[dict[str, Any]], norm: Norm,
           lenient: bool) -> dict[str, dict[str, Any]]:
    """Expected entry id -> observed entry with the same raw text and page(s) (each used once)."""
    free: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in obs_entries:
        free[norm(entry["raw"])].append(entry)
    aligned = {}
    for entry in sorted(exp_entries, key=lambda e: e["ordinal"]):
        bucket = free.get(norm(entry["raw"]), [])
        for cand in bucket:
            if _position_ok(entry, cand, lenient):
                bucket.remove(cand)
                aligned[entry["id"]] = cand
                break
    return aligned


def _field_hits(want: dict[str, Any], got: dict[str, Any], norm: Norm) -> dict[str, bool]:
    def author(a: dict[str, Any]) -> tuple[str, str | None]:
        return (norm(a["family"]), norm(a["given"]) if "given" in a else None)

    hits: dict[str, bool] = {}
    for key in ("title", "venue", "year_raw"):
        if key in want:
            hits[key] = key in got and norm(got[key]) == norm(want[key])
    if "year" in want:
        hits["year"] = got.get("year") == want["year"]
    if "authors" in want:
        hits["authors"] = [author(a) for a in got.get("authors", [])] == [author(a) for a in want["authors"]]
    return hits


def _check_references(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> None:
    if "references" not in exp:
        return
    scope = set(exp["scope"]["physical_pages"])
    if exp["references"]["markers"]:
        out.skip("reference_markers", "<all>", "citation-marker links are not scored")
    # A missing observed section is an empty list: the expected entries were not recovered.
    observed_entries = obs.get("references", {}).get("entries", [])
    obs_entries = [e for e in observed_entries if e.get("page") is None or e["page"] in scope]
    expected = sorted(exp["references"]["entries"], key=lambda e: e["ordinal"])
    skipped = {}
    skipped_pages: set[int] = set()
    for entry in expected:
        reason = _skip_reason(entry, "entry")
        if reason:
            skipped[entry["id"]] = reason
            skipped_pages |= set(range(entry["page"], entry.get("end_page", entry["page"]) + 1))
            out.skip("reference_segmentation_recall", entry["id"], reason)
    scored = [e for e in expected if e["id"] not in skipped]
    # Align against every expected entry, skipped ones too, so a correct observed entry whose
    # expected twin is unscored is neither a hit nor an "extra" that lowers precision.
    aligned = {m: _align(expected, obs_entries, n, m == "tolerant") for m, n in _MODES}

    pairs: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {m: [] for m, _ in _MODES}
    for entry in scored:
        s_hit, t_hit = (aligned[m].get(entry["id"]) is not None for m in ("strict", "tolerant"))
        out.add("reference_segmentation_recall", entry["id"], _ok(s_hit), _ok(t_hit),
                "raw text and start/end page identical")
        for m, _ in _MODES:
            if aligned[m].get(entry["id"]) is not None:
                pairs[m].append((entry, aligned[m][entry["id"]]))
    # The ordinal is the printed list position. A complete-document record must reproduce it; a
    # page-scoped one may number from a different start, so it needs one constant offset.
    for m, _ in _MODES:
        offsets = [got["ordinal"] - entry["ordinal"] for entry, got in pairs[m]]
        base = Counter(offsets).most_common(1)[0][0] if offsets and exp["scope"]["kind"] == "pages" else 0
        for (entry, got), offset in zip(pairs[m], offsets, strict=True):
            out.add_mode("reference_ordinal", m, entry["id"], _ok(offset == base),
                         f"printed list position (offset {offset}, expected {base})")
    totals, hits_ = {}, {}
    for m, _ in _MODES:
        matched_obs = {id(e) for e in aligned[m].values()}
        to_skipped = sum(1 for e_id in skipped if e_id in aligned[m])
        # An observed entry matching nothing, on a page where an expected entry is unscored, may
        # be that very entry with different text: it leaves the denominator and is listed.
        excluded = [e for e in obs_entries if id(e) not in matched_obs and e.get("page") in skipped_pages]
        if m == "strict":
            for e in excluded:
                out.skip("reference_segmentation_precision", e["id"],
                         "observed entry on a page with an unscored expected entry; not counted as extra")
        totals[m] = len(obs_entries) - to_skipped - len(excluded)
        hits_[m] = sum(1 for e in scored if e["id"] in aligned[m])
    if totals["strict"] > 0:
        out.add("reference_segmentation_precision", "<all>", (hits_["strict"], totals["strict"]),
                (hits_["tolerant"], totals["tolerant"]),
                "observed entries that align with an expected entry (matches of unscored expected entries "
                "and unmatched entries on their pages excluded)")

    # Parsing and identifier checks cover an entry only in the modes where it was segmented. Every
    # other (entry, mode) is listed in not_scored, so these metrics never silently describe survivors.
    for entry in scored:
        if entry.get("fields"):
            for mode, norm in _MODES:
                seg = aligned[mode].get(entry["id"])
                if seg is None:
                    out.skip("reference_field_accuracy", entry["id"], f"{mode} mode: entry not segmented; fields not scored")
                    continue
                hits = _field_hits(entry["fields"], seg.get("fields", {}), norm)
                if hits:
                    bad = sorted(k for k, ok in hits.items() if not ok)
                    out.add_mode("reference_field_accuracy", mode, entry["id"], (sum(hits.values()), len(hits)),
                                 f"fields: {bad or 'all exact'}")
        status = entry["doi"]["status"]
        if status == "not_assessed":
            continue
        metric = "reference_doi_recovery" if status == "present" else "reference_doi_absent_correct"
        for mode, _ in _MODES:
            seg = aligned[mode].get(entry["id"])
            if seg is None:
                out.skip(metric, entry["id"], f"{mode} mode: entry not segmented; identifier not scored")
                continue
            doi = seg["doi"]
            if status == "present":
                canon = _loose_doi if mode == "tolerant" else str
                ok = doi["status"] == "present" and canon(doi["value"]) == canon(entry["doi"]["value"])
                out.add_mode(metric, mode, entry["id"], _ok(ok), f"observed DOI status: {doi['status']}")
            else:
                out.add_mode(metric, mode, entry["id"], _ok(doi["status"] != "present"),
                             "observation must not report a DOI the source does not print")


# --- entry points --------------------------------------------------------------------------------


def _check_spans(obs: dict[str, Any], markdown: str | None, out: _Checks) -> None:
    if not any("output_span" in b for b in obs["blocks"]):
        return
    if markdown is None:
        out.skip("output_span_roundtrip", "<all>", "observation has output spans but no Markdown text was supplied")
        return
    findings = verify_output_spans(obs, markdown)
    out.add("output_span_roundtrip", "<all>", _ok(not findings), _ok(not findings), "; ".join(findings), strict_only=True)


def _unscored(out: _Checks) -> list[dict[str, str]]:
    """Unscored items that bear on the verdict: those of informational metrics do not."""
    return [n for n in out.not_scored if n["metric"] not in _INFORMATIONAL]


def _verdict(out: _Checks) -> tuple[str, int, int]:
    deciding = [c for c in out.checks if c["decides"]]
    failures = sum(1 for c in deciding if not c["passed"])
    if failures:
        return "failed", failures, len(deciding)
    if not deciding or _unscored(out):
        return "incomplete", 0, len(deciding)
    return "passed", 0, len(deciding)


def _score_validated(exp: dict[str, Any], obs: dict[str, Any], markdown: str | None = None) -> dict[str, Any]:
    if exp["document"]["pdf_sha256"] != obs["document"]["pdf_sha256"]:
        raise ScoringError("pdf_sha256 mismatch: the observation is of a different PDF than the expectation")
    out = _Checks()
    _check_text(exp, obs, out)
    table_match = _check_tables(exp, obs, out)
    equations_used = _check_equations(exp, obs, out)
    _check_captions(exp, obs, out, table_match)
    matched_blocks = {mode: set(equations_used[mode]) | {
        block_id for chain_id in table_match[mode].values() for block_id in chain_id.split("+")}
        for mode, _ in _MODES}
    _check_precision(exp, obs, out, matched_blocks)
    _check_figures(exp, obs, out)
    _check_references(exp, obs, out)
    _check_spans(obs, markdown, out)
    out.mark_tolerant_only()
    verdict, failures, n_deciding = _verdict(out)
    return {
        "scorer_version": SCORER_VERSION,
        "pdf_sha256": exp["document"]["pdf_sha256"],
        "expectation_id": exp["record_id"],
        "observation_id": obs["record_id"],
        "verdict": verdict,
        "strict_passed": verdict == "passed",
        "strict_failures": failures,
        "strict_checks": n_deciding,
        "tolerant_only_matches": sum(1 for c in out.checks if c.get("tolerant_only")),
        "unscored_items": len(_unscored(out)),
        "checks": out.checks,
        "metrics": out.metrics(),
        "not_scored": out.not_scored,
    }


def score_records(expectation: dict[str, Any], observation: dict[str, Any], *,
                  markdown: str | None = None) -> dict[str, Any]:
    """Score ``observation`` against ``expectation``. Both must be valid records of the right kind
    and the expectation must be reviewed. A PDF hash mismatch is refused before any scoring.

    ``verdict`` is ``passed``, ``failed`` or ``incomplete`` and depends on strict checks only (see
    the module docstring); ``strict_passed`` is true only for ``passed``. Pass the extractor's
    ``markdown`` text to have the observation's ``output_span`` hashes round-trip checked.
    """
    try:
        kinds = (validate_record(expectation), validate_record(observation))
    except RecordError as err:
        raise ScoringError(f"invalid record: {err}") from err
    if kinds != ("expectation", "observation"):
        raise ScoringError(f"need an (expectation, observation) pair, got {kinds}")
    if expectation["document"]["pdf_sha256"] != observation["document"]["pdf_sha256"]:
        raise ScoringError("pdf_sha256 mismatch: the observation is of a different PDF than the expectation")
    if expectation["provenance"]["review"]["status"] != "reviewed":
        raise ScoringError("expectation is not reviewed; drafts are not ground truth")
    return _score_validated(expectation, observation, markdown)


def score_files(expectation_path: str | Path, observation_path: str | Path, *,
                markdown_path: str | Path | None = None) -> dict[str, Any]:
    """Load (reviewed expectation, observation) from disk and score them."""
    try:
        exp = load_reviewed_expectation(expectation_path)
        obs = load_record(observation_path, kind="observation")
    except RecordError as err:
        raise ScoringError(f"invalid record: {err}") from err
    # read_bytes keeps CRLF: the text is compared (and hashed) exactly as the extractor wrote it.
    markdown = Path(markdown_path).read_bytes().decode("utf-8") if markdown_path is not None else None
    return _score_validated(exp, obs, markdown)


# --- run-schema output ---------------------------------------------------------------------------


def score_to_result(configuration_id: str, input_id: str, score: dict[str, Any], output_sha256: str,
                    **extra: Any) -> dict[str, Any]:
    """A run ``results`` entry (status ``ok``) carrying the score's versioned metrics, the tri-state
    ``verdict`` and the number of ``unscored_items``, so an incomplete score cannot read as a pass.

    ``extra`` passes through ``observation_id``, ``latency_seconds`` or ``resources``.
    """
    return {"configuration_id": configuration_id, "input_id": input_id, "status": "ok",
            "output_sha256": output_sha256, "verdict": score["verdict"],
            "unscored_items": score["unscored_items"], "metrics": score["metrics"], **extra}


def failure_result(configuration_id: str, input_id: str, status: str, kind: str, message: str,
                   **extra: Any) -> dict[str, Any]:
    """A run ``results`` entry for a run that produced nothing to score.

    Failures, timeouts, missing backends and skips stay in the run with their error and costs; they
    carry no metrics, so they can neither pass nor read as a zero score.
    """
    if status == "ok":
        raise ValueError("use score_to_result for status 'ok'")
    return {"configuration_id": configuration_id, "input_id": input_id, "status": status,
            "error": {"kind": kind, "message": message}, **extra}
