"""Score an extraction observation against a reviewed expectation (roadmap E1, package B).

Benchmark-side only: not shipped, no dependency beyond package A. Every check is evaluated twice:

- ``strict``: exact comparison. Signs, decimal points, operators, case, page numbers, table
  positions and hashes must all survive. Only strict results can establish transcription fidelity.
- ``tolerant``: the same check after NFKC, case folding and whitespace collapsing (DOIs also lose
  their ``doi:``/URL prefix). Labelled as such, reported as separate metrics, and never able to
  turn a strict failure into a pass: ``strict_passed`` is computed from strict checks alone, and
  a check that only the tolerant mode accepts is counted in ``tolerant_only_matches``.

Check families: text presence (+ span hash), reading order, table cells, equations, captions, and
references (segmentation, field parsing, identifier recovery, scored separately). Library
resolution is out of scope. Items an expectation marks as high-uncertainty, or families the
observation cannot be compared on, are listed in ``not_scored`` instead of being dropped or
scored as zero.

Public API: ``score_records``, ``score_files``, ``ScoringError``, ``METRIC_VERSIONS``,
``score_to_result``, ``failure_result``.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
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

SCORER_VERSION = "1"
_METRICS = (
    "text_presence", "text_span_hash", "reading_order", "table_cell_match", "table_exact",
    "equation_match", "caption_match", "reference_segmentation_recall",
    "reference_segmentation_precision", "reference_field_accuracy", "reference_doi_recovery",
    "reference_doi_absent_correct", "output_span_roundtrip",
)
METRIC_VERSIONS: dict[str, str] = {name: SCORER_VERSION for name in _METRICS}

# Running headers/footers are not body text and extractors legitimately drop them; captions,
# tables, equations and reference entries have their own families.
_TEXT_TYPES = frozenset({"heading", "paragraph", "list_item", "footnote", "other"})

Norm = Callable[[str], str]


class ScoringError(ValueError):
    """The inputs cannot be scored (wrong record kind, unreviewed truth, or a PDF mismatch)."""


def _strict(text: str) -> str:
    return text


def _loose(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _loose_doi(text: str) -> str:
    value = _loose(text)
    value = re.sub(r"^(doi:\s*|https?://(dx\.)?doi\.org/)", "", value)
    return value.rstrip(".,;")


class _Checks:
    """Collects checks for both modes and derives metrics from them."""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.not_scored: list[dict[str, str]] = []

    def add(self, name: str, subject: str, strict: tuple[int, int], tolerant: tuple[int, int],
            detail: str = "", *, strict_only: bool = False) -> None:
        """``strict``/``tolerant`` are (matched, total). Tolerant is never below strict."""
        if tolerant[0] < strict[0]:
            tolerant = strict
        modes = (("strict", strict),) if strict_only else (("strict", strict), ("tolerant", tolerant))
        for mode, (matched, total) in modes:
            check: dict[str, Any] = {
                "name": name, "mode": mode, "subject": subject,
                "passed": matched == total, "matched": matched, "total": total, "detail": detail,
            }
            if mode == "tolerant":
                check["tolerant_only"] = matched == total and strict[0] != strict[1]
            self.checks.append(check)

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


def _ok(matched: bool) -> tuple[int, int]:
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


# --- text, order -------------------------------------------------------------------------------


def _page_blocks(record: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    pages: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for block in sorted(record["blocks"], key=lambda b: b["reading_order"]):
        pages[block["page"]].append(block)
    return pages


def _chains(record: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Blocks grouped into ``continues_from`` chains in chain order (singletons included), ordered
    by the reading order of each chain's first block. A block lives on one page; content that
    crosses a page break is a chain, and an extractor may emit it as fragments or merged."""
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


def _page_text(blocks: list[dict[str, Any]], norm: Norm) -> str:
    return norm("\n".join(b["text"] for b in blocks if isinstance(b.get("text"), str)))


def _locate(text: str, pages: set[int], observed: dict[int, list[dict[str, Any]]], norm: Norm) -> tuple[int, int] | None:
    """(page, offset) of the first occurrence of ``text`` on the earliest of ``pages`` that has it.

    ``pages`` is the page set of the expected block's chain, so a paragraph that the extractor
    merged across a page break is still found, on the page where the merged block lives."""
    for page in sorted(pages):
        offset = _page_text(observed.get(page, []), norm).find(norm(text))
        if offset >= 0:
            return (page, offset)
    return None


# Separators an extractor may legitimately put between the fragments of a merged block.
_JOINERS = ("", " ", "\n", "\n\n")


def _check_text(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> None:
    observed = _page_blocks(obs)
    texts: list[tuple[dict[str, Any], set[int]]] = []
    for chain in _chains(exp):
        if chain[0]["type"] not in _TEXT_TYPES:
            continue
        pages = _chain_pages(chain)
        scored = []
        for block in chain:
            if not isinstance(block.get("text"), str) or not block["text"]:
                continue
            reason = _skip_reason(block, "block")
            if reason:
                out.skip("text_presence", block["id"], reason)
                continue
            scored.append(block)
            texts.append((block, pages))
            found = {n: _locate(block["text"], pages, observed, f) for n, f in (("strict", _strict), ("tolerant", _loose))}
            where = [p for p, bs in observed.items() if p not in pages
                     and _strict(block["text"]) in _page_text(bs, _strict)]
            detail = "" if found["strict"] else (
                f"found only on page(s) {sorted(where)}" if where else "not found on the expected page")
            out.add("text_presence", block["id"], _ok(bool(found["strict"])), _ok(bool(found["tolerant"])), detail)
        if scored and any("text_sha256" in b for b in scored):
            hashes = {sha256_text(b["text"]) for p in pages for b in observed.get(p, []) if isinstance(b.get("text"), str)}
            fragments_ok = all(sha256_text(b["text"]) in hashes for b in scored)
            merged_ok = len(scored) == len(chain) > 1 and any(
                sha256_text(joiner.join(b["text"] for b in scored)) in hashes for joiner in _JOINERS)
            hit = fragments_ok or merged_ok
            out.add("text_span_hash", _chain_id(chain), _ok(hit), _ok(hit),
                    "" if hit else "no observed block on the page(s) round-trips to the expected hash "
                    "(for a cross-page chain: each fragment, or the fragments joined by '', ' ', newline or blank line)",
                    strict_only=True)
    positions = {n: [_locate(b["text"], pages, observed, f) for b, pages in texts]
                 for n, f in (("strict", _strict), ("tolerant", _loose))}
    for i in range(len(texts) - 1):
        results = {}
        for mode in ("strict", "tolerant"):
            first, second = positions[mode][i], positions[mode][i + 1]
            results[mode] = _ok(first is not None and second is not None and first < second)
        out.add("reading_order", f"{texts[i][0]['id']}->{texts[i + 1][0]['id']}", results["strict"], results["tolerant"],
                "pair counts as failed when either block is not found")


# --- tables, equations, captions ----------------------------------------------------------------


def _cells(table: dict[str, Any], norm: Norm) -> dict[tuple[int, int], tuple[int, int, str, bool]]:
    return {(c["row"], c["col"]): (c.get("rowspan", 1), c.get("colspan", 1), norm(c["text"]), bool(c.get("is_header")))
            for c in table["cells"]}


def _table_match(exp_table: dict[str, Any], obs_table: dict[str, Any], norm: Norm) -> int:
    obs = _cells(obs_table, norm)
    return sum(1 for pos, cell in _cells(exp_table, norm).items() if obs.get(pos) == cell)


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


def _check_tables(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> None:
    candidates = _table_chains(obs)
    claimed: dict[str, set[str]] = {"strict": set(), "tolerant": set()}
    for ident, chain, pages, table in _table_chains(exp):
        reasons = [r for r in (_skip_reason(b, "table") for b in chain) if r]
        if reasons:
            out.skip("table_cell_match", ident, reasons[0])
            continue
        total = len(table["cells"])
        results: dict[str, tuple[int, int, bool, str]] = {}
        for mode, norm in (("strict", _strict), ("tolerant", _loose)):
            best: tuple[int, str, dict[str, Any]] | None = None
            for cand_id, _, cand_pages, cand_table in candidates:
                if cand_pages & pages and cand_id not in claimed[mode]:
                    score = _table_match(table, cand_table, norm)
                    if best is None or score > best[0]:
                        best = (score, cand_id, cand_table)
            if best is None:
                results[mode] = (0, total, False, "no unclaimed observed table on the page(s)")
                continue
            claimed[mode].add(best[1])
            same_grid = (best[2]["n_rows"], best[2]["n_cols"], len(best[2]["cells"])) == (
                table["n_rows"], table["n_cols"], total)
            results[mode] = (best[0], total, best[0] == total and same_grid, f"matched observed table {best[1]!r}")
        s, t = results["strict"], results["tolerant"]
        out.add("table_cell_match", ident, (s[0], s[1]), (t[0], t[1]), s[3])
        out.add("table_exact", ident, _ok(s[2]), _ok(t[2]), "grid and every cell identical")


def _norm_latex(norm: Norm, latex: str) -> str:
    return "".join(norm(latex).split()) if norm is _loose else latex


def _check_equations(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> None:
    observed = _page_blocks(obs)
    for block in sorted(exp["blocks"], key=lambda b: b["reading_order"]):
        want = block.get("equation")
        if want is None:
            continue
        reason = _skip_reason(block, "equation")
        if reason:
            out.skip("equation_match", block["id"], reason)
            continue
        if want.get("latex") is None and want.get("number") is None:
            out.skip("equation_match", block["id"], "expectation records neither LaTeX nor a number")
            continue
        results = {}
        for mode, norm in (("strict", _strict), ("tolerant", _loose)):
            hit = False
            for cand in observed.get(block["page"], []):
                got = cand.get("equation")
                if got is None:
                    continue
                if want.get("latex") is not None and (
                        got.get("latex") is None or _norm_latex(norm, got["latex"]) != _norm_latex(norm, want["latex"])):
                    continue
                if want.get("number") is not None and got.get("number") != want["number"]:
                    continue
                if want.get("display") is not None and got.get("display") != want["display"]:
                    continue
                hit = True
                break
            results[mode] = _ok(hit)
        out.add("equation_match", block["id"], results["strict"], results["tolerant"],
                "LaTeX, printed number and display flag must agree")


def _target_key(chains: list[list[dict[str, Any]]], target: dict[str, Any]) -> tuple[str, int, set[int]]:
    """(type, ordinal among same-type chains starting on the same page, pages of the target's chain)."""
    chain = next(c for c in chains if any(b["id"] == target["id"] for b in c))
    peers = [c for c in chains if c[0]["type"] == chain[0]["type"] and c[0]["page"] == chain[0]["page"]]
    return chain[0]["type"], peers.index(chain), _chain_pages(chain)


def _check_captions(exp: dict[str, Any], obs: dict[str, Any], out: _Checks) -> None:
    exp_by_id = {b["id"]: b for b in exp["blocks"]}
    obs_by_id = {b["id"]: b for b in obs["blocks"]}
    exp_chains, obs_chains = _chains(exp), _chains(obs)
    continued = {b["continues_from"] for b in exp["blocks"] if "continues_from" in b}
    for block in sorted(exp["blocks"], key=lambda b: b["reading_order"]):
        if block["type"] != "caption":
            continue
        reason = _skip_reason(block, "caption") or (
            None if isinstance(block.get("text"), str) else "caption has no text")
        if reason:
            out.skip("caption_match", block["id"], reason)
            continue
        if "continues_from" in block or block["id"] in continued:
            out.skip("caption_match", block["id"], "caption itself spans pages; not compared")
            continue
        target = exp_by_id.get(block.get("caption_for", ""))
        results = {}
        for mode, norm in (("strict", _strict), ("tolerant", _loose)):
            hit = False
            for cand in obs["blocks"]:
                if cand["type"] != "caption" or cand["page"] != block["page"] or not isinstance(cand.get("text"), str):
                    continue
                if norm(cand["text"]) != norm(block["text"]):
                    continue
                linked = obs_by_id.get(cand.get("caption_for", ""))
                if target is not None:
                    if linked is None:
                        continue
                    want, got = _target_key(exp_chains, target), _target_key(obs_chains, linked)
                    if got[0] != want[0] or got[1] != want[1] or not got[2] & want[2]:
                        continue
                hit = True
                break
            results[mode] = _ok(hit)
        out.add("caption_match", block["id"], results["strict"], results["tolerant"],
                "caption text exact and linked to the same table/figure (by type, page, order)")


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
    for key in ("title", "venue"):
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
    if "references" not in obs:
        out.skip("reference_*", "<all>", "observation has no references section; not comparable, not scored as zero")
        return
    exp_entries = []
    for entry in exp["references"]["entries"]:
        reason = _skip_reason(entry, "entry")
        if reason:
            out.skip("reference_segmentation_recall", entry["id"], reason)
        else:
            exp_entries.append(entry)
    obs_entries = [e for e in obs["references"]["entries"] if e.get("page") is None or e["page"] in scope]
    aligned = {m: _align(exp_entries, obs_entries, n, m == "tolerant")
               for m, n in (("strict", _strict), ("tolerant", _loose))}

    for entry in exp_entries:
        s, t = (aligned[m].get(entry["id"]) is not None for m in ("strict", "tolerant"))
        out.add("reference_segmentation_recall", entry["id"], _ok(s), _ok(t), "raw text and start/end page identical")
    n_obs = len(obs_entries)
    if n_obs:
        out.add("reference_segmentation_precision", "<all>",
                (len(aligned["strict"]), n_obs), (len(aligned["tolerant"]), n_obs),
                "observed entries that align with an expected entry")

    # Parsing and identifier checks cover only entries segmented in that mode: a missed entry is
    # already a segmentation failure and is not double-counted here.
    for entry in exp_entries:
        if "fields" in entry:
            hits = {m: (_field_hits(entry["fields"], aligned[m][entry["id"]].get("fields", {}),
                                    _strict if m == "strict" else _loose)
                        if entry["id"] in aligned[m] else None) for m in ("strict", "tolerant")}
            pairs = {m: ((sum(h.values()), len(h)) if h is not None else None) for m, h in hits.items()}
            n_fields = len(entry["fields"])
            if pairs["strict"] is not None and n_fields:
                out.add("reference_field_accuracy", entry["id"], pairs["strict"], pairs["tolerant"] or pairs["strict"],
                        f"fields: {sorted(k for k, ok in (hits['strict'] or {}).items() if not ok) or 'all exact'}")
        status = entry["doi"]["status"]
        if status == "not_assessed":
            continue
        ids = {m: aligned[m].get(entry["id"]) for m in ("strict", "tolerant")}
        if ids["strict"] is None:
            continue
        got = {m: (e["doi"] if e is not None else None) for m, e in ids.items()}
        if status == "present":
            res = {}
            for m, norm in (("strict", _strict), ("tolerant", _loose_doi)):
                d = got[m]
                res[m] = _ok(d is not None and d["status"] == "present" and norm(d["value"]) == norm(entry["doi"]["value"]))
            out.add("reference_doi_recovery", entry["id"], res["strict"], res["tolerant"],
                    f"observed DOI status: {got['strict']['status'] if got['strict'] else 'unaligned'}")
        else:  # absent_in_source: a missing DOI is correct; an invented one is the failure
            clean = {m: _ok(d is not None and d["status"] != "present") for m, d in got.items()}
            out.add("reference_doi_absent_correct", entry["id"], clean["strict"], clean["tolerant"],
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


def _score_validated(exp: dict[str, Any], obs: dict[str, Any], markdown: str | None = None) -> dict[str, Any]:
    if exp["document"]["pdf_sha256"] != obs["document"]["pdf_sha256"]:
        raise ScoringError("pdf_sha256 mismatch: the observation is of a different PDF than the expectation")
    out = _Checks()
    for family in (_check_text, _check_tables, _check_equations, _check_captions, _check_references):
        family(exp, obs, out)
    _check_spans(obs, markdown, out)
    strict = [c for c in out.checks if c["mode"] == "strict"]
    failed = [c for c in strict if not c["passed"]]
    tolerant_only = sum(1 for c in out.checks if c.get("tolerant_only"))
    return {
        "scorer_version": SCORER_VERSION,
        "pdf_sha256": exp["document"]["pdf_sha256"],
        "expectation_id": exp["record_id"],
        "observation_id": obs["record_id"],
        "strict_passed": bool(strict) and not failed,
        "strict_failures": len(failed),
        "tolerant_only_matches": tolerant_only,
        "checks": out.checks,
        "metrics": out.metrics(),
        "not_scored": out.not_scored,
    }


def score_records(expectation: dict[str, Any], observation: dict[str, Any], *,
                  markdown: str | None = None) -> dict[str, Any]:
    """Score ``observation`` against ``expectation``. Both must be valid records of the right kind
    and the expectation must be reviewed. A PDF hash mismatch is refused before any scoring.

    ``strict_passed`` depends on strict checks only. Pass the extractor's ``markdown`` text to have
    the observation's ``output_span`` hashes round-trip checked (strict only).
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
    markdown = Path(markdown_path).read_text(encoding="utf-8") if markdown_path is not None else None
    return _score_validated(exp, obs, markdown)


# --- run-schema output ---------------------------------------------------------------------------


def score_to_result(configuration_id: str, input_id: str, score: dict[str, Any], output_sha256: str,
                    **extra: Any) -> dict[str, Any]:
    """A run ``results`` entry (status ``ok``) carrying the score's versioned metrics.

    ``extra`` passes through ``observation_id``, ``latency_seconds`` or ``resources``.
    """
    return {"configuration_id": configuration_id, "input_id": input_id, "status": "ok",
            "output_sha256": output_sha256, "metrics": score["metrics"], **extra}


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
