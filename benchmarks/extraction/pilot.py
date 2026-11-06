"""Deterministic pilot / held-out document selection for the E2 extraction baseline.

Pure metadata logic: reads ``benchmarks/public_pdfs/sources.json`` entries, applies the corpus
licence gate, assigns difficulty strata and selects documents. It never reads a PDF; page choices
that need PDFs are made by the opt-in host step in ``drafts.py``.

Selection is layout-first (difficulty strata are filled before fields are spread), seeded and
order-independent: candidates are ranked by SHA-256 of ``seed|stratum|id``, never by Python's
``random`` or by input order, so the same sources and seed always give the same manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
DEFAULT_SEED = "e2-pilot-2026-11"

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_PATH = REPO_ROOT / "benchmarks" / "public_pdfs" / "sources.json"
MANIFEST_PATH = Path(__file__).resolve().parent / "pilot_manifest.json"
PIN_PATH = Path(__file__).resolve().parent / "pilot_manifest.sha256"

STRATA = ("two-column", "tables", "equations", "scans", "non-english", "references")

# Pilot: documents per stratum and the total pages to be drawn from them (sum = 20 pages).
PILOT_QUOTAS: dict[str, dict[str, int]] = {
    "two-column": {"documents": 3, "pages": 4},
    "tables": {"documents": 3, "pages": 4},
    "equations": {"documents": 3, "pages": 4},
    "scans": {"documents": 1, "pages": 2},
    "non-english": {"documents": 3, "pages": 3},
    "references": {"documents": 3, "pages": 3},
}
HELD_OUT_QUOTAS: dict[str, int] = {
    "two-column": 2, "tables": 2, "equations": 2, "scans": 1, "non-english": 2, "references": 2,
}
PILOT_WHOLE_PAPERS = 3
HELD_OUT_WHOLE_PAPERS = 2
WHOLE_PAPER_PAGES = (8, 30)  # annotating a whole paper is costly: keep them short but with references

# --- stratum assignment ----------------------------------------------------------------------
# sources.json has no page-level layout data. Only `styles` and `language` are verified
# inspection metadata, and almost every entry says just "native text". Everything below the
# metadata rules is an UNVERIFIED ASSUMPTION about the field's typical layout; the host step
# checks each assumed stratum against the actual pages and reports what it cannot support.
SCAN_STYLES = frozenset({"scanned pages", "noisy existing OCR", "no text layer"})
ASSUMED_FIELDS: dict[str, tuple[str, ...]] = {
    "two-column": ("physics", "chemistry", "materials-engineering", "computer-science"),
    "tables": ("clinical-medicine", "ecology-biology", "psychology", "economics", "education",
               "sociology-politics", "psychometrics", "statistics"),
    "equations": ("physics", "statistics", "psychometrics", "computer-science",
                  "materials-engineering", "economics"),
    "references": ("clinical-medicine", "physics", "chemistry", "ecology-biology", "statistics",
                   "psychometrics", "computer-science", "economics", "materials-engineering",
                   "psychology", "education", "sociology-politics", "law", "humanities-linguistics"),
}
ASSUMED_MIN_PAGES = {"references": 8}
# `styles` entries that verify a stratum at document level (gaussian-coresets: equations; data-sharing: tables, two columns).
STYLE_STRATA = {"two columns": "two-column", "tables": "tables", "equations": "equations", "references": "references"}
PAGE_CHECK_VERSION = 2

# Placeholder wording written into drafts; a reviewed record must not still carry it.
DRAFT_NOTE = "draft proposal; becomes ground truth only through human review (docs/extraction-pilot-review.md)"
PROPOSAL_BLOCK_NOTE = "unreviewed PDF text-layer proposal; type, boundaries and text are not checked"
JOURNAL_FIELDS = frozenset(f for fields in ASSUMED_FIELDS.values() for f in fields)

ASSUMPTIONS = [
    "Stratum 'metadata' basis: derived from sources.json `styles` (two columns, scanned pages, "
    "noisy existing OCR, no text layer, references) or `language` (non-English). Verified at "
    "corpus inspection time, but only at document level.",
    "Stratum 'assumed' basis: UNVERIFIED. sources.json carries no page-level layout, so tables, "
    "equations, two-column layout and a reference list are assumed from the entry's `field` "
    "(and a minimum page count for references). The host step checks the assumption per page and "
    "reports unsupported choices instead of picking an arbitrary page.",
    "Candidate pages are the output of crude heuristic checks on the cached PDFs (see drafts.py), "
    "not confirmed layouts; the reviewer decides whether a page serves its stratum.",
    "Non-English entries are taken from `language` only; individual pages may still be mostly "
    "figures or references.",
    "Entries without `field` and `language` (the four original sources) can only qualify through "
    "`styles`; their language is treated as unknown, not English.",
]

# --- licence gate ----------------------------------------------------------------------------
# Case-insensitive allowlist of exact forms (as recorded in sources.json). Anything else, however
# close, is excluded and goes to maintainer review: "CC BY 4.0, copyright Elsevier" or "CC BY-nd"
# must never slip through a prefix match.
_BASE = (r"(?:CC[ -]BY(?:[ -]\d\.\d)?|Creative Commons Attribution(?: \d\.\d)?|CC0(?: 1\.0)?"
         r"|Public domain(?: in the United States)?)")
_LICENCE_FORM = re.compile(
    _BASE + r"(?: \(([^()]*)\))?(?:; (?:see rights source for jurisdiction details|NTRS: public use permitted))?",
    re.IGNORECASE,
)
_NOTE_REJECT = re.compile(
    r"(?<![A-Za-z])(?:NC|ND|SA)(?![A-Za-z])|non[ -]?commercial|no[ -]?deriv|share[ -]?alike|all rights reserved|publisher",
    re.IGNORECASE,
)


def license_gate(license_text: Any) -> tuple[bool, str]:
    """Corpus rule: CC BY (any version), CC0, public domain pass; NC/ND/SA/publisher/unverifiable fail.

    Only exact allowlisted forms pass (case-insensitive, optional parenthetical note). A note that
    mentions NC/ND/SA/publisher terms (for example a described dataset) is rejected as ambiguous.
    """
    if not isinstance(license_text, str) or not license_text.strip():
        return False, "no licence recorded (unverifiable)"
    text = " ".join(license_text.split())
    match = _LICENCE_FORM.fullmatch(text)
    if match is None:
        return False, "licence form is not on the allowlist (unverifiable); maintainer review required"
    note = match.group(1) or ""
    hit = _NOTE_REJECT.search(note)
    if hit:
        return False, f"licence note mentions {hit.group(0)!r}; NC/ND/SA/publisher terms are rejected (needs maintainer confirmation)"
    return True, "ok"


# --- helpers ---------------------------------------------------------------------------------


def canonical_json(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rank(seed: str, *parts: str) -> str:
    return hashlib.sha256("|".join((seed, *parts)).encode("utf-8")).hexdigest()


def selection_inputs_sha256(sources: list[dict[str, Any]]) -> str:
    """Hash of exactly the source fields selection depends on (label edits do not change it)."""
    projection = [
        {k: s.get(k) for k in ("id", "license", "language", "field", "styles", "pages", "sha256")}
        for s in sorted(sources, key=lambda s: s["id"])
    ]
    return sha256_bytes(canonical_json(projection))


def candidate_strata(entry: dict[str, Any]) -> dict[str, str]:
    """Strata an entry can serve, mapped to their basis (``metadata`` or ``assumed``)."""
    styles = set(entry.get("styles") or [])
    field, language, pages = entry.get("field"), entry.get("language"), entry.get("pages", 0)
    found: dict[str, str] = {}
    if styles & SCAN_STYLES:
        found["scans"] = "metadata"
    if language not in (None, "en"):
        found["non-english"] = "metadata"
    for style, stratum in STYLE_STRATA.items():
        if style in styles:
            found[stratum] = "metadata"
    if not styles & SCAN_STYLES:
        for stratum, fields in ASSUMED_FIELDS.items():
            if stratum not in found and field in fields and pages >= ASSUMED_MIN_PAGES.get(stratum, 1):
                found[stratum] = "assumed"
    return found


def normalize_title(title: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def identity_keys(entry: dict[str, Any]) -> set[tuple[str, str]]:
    """Keys under which two entries are the same work: PDF hash, DOI, normalized title."""
    keys = {("sha256", str(entry.get("sha256", "")).lower())}
    if entry.get("doi"):
        keys.add(("doi", str(entry["doi"]).strip().lower()))
    if normalize_title(entry.get("title")):
        keys.add(("title", normalize_title(entry["title"])))
    return keys


def _doc(entry: dict[str, Any], role: str, stratum: str | None, basis: str | None, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": entry["id"], "role": role, "stratum": stratum, "basis": basis,
        "field": entry.get("field"), "language": entry.get("language"),
        "license": entry["license"], "pdf_pages": entry["pages"], "pdf_sha256": entry["sha256"],
    }
    doc.update(extra)
    return doc


class PageChecks:
    """Page-level checks the manifest builder needs; the real one reads cached PDFs (drafts.py)."""

    def pages(self, entry: dict[str, Any], stratum: str, wanted: int) -> tuple[list[int], int]:
        """Up to ``wanted`` candidate pages for ``stratum`` and the number of supported pages."""
        raise NotImplementedError

    def reference_list_found(self, entry: dict[str, Any]) -> bool:
        raise NotImplementedError


class ReplayMiss(LookupError):
    """The committed manifest holds no page-check result for a document selection asks about."""


class ReplayChecks(PageChecks):
    """Replays the page-check results recorded in a committed manifest, so it can be regenerated without PDFs."""

    def __init__(self, manifest: dict[str, Any]):
        self.pages_by_key: dict[tuple[str, str], tuple[list[int], int]] = {}
        self.whole: dict[str, bool] = {}
        for name in ("pilot", "held_out"):
            for doc in manifest[name]:
                if doc["role"] == "stratum_pages":
                    # Held-out documents record only a count; a placeholder page stands in for "supported".
                    pages = list(doc.get("candidate_pages") or ([0] if doc["supported_pages"] else []))
                    self.pages_by_key[(doc["id"], doc["stratum"])] = (pages, doc["supported_pages"])
                else:
                    self.whole[doc["id"]] = True
        for item in manifest.get("page_check_rejected", []):
            if item["stratum"] == "whole_paper":
                self.whole[item["id"]] = False
            else:
                self.pages_by_key[(item["id"], item["stratum"])] = ([], 0)

    def pages(self, entry: dict[str, Any], stratum: str, wanted: int) -> tuple[list[int], int]:
        try:
            pages, supported = self.pages_by_key[(entry["id"], stratum)]
        except KeyError:
            raise ReplayMiss(f"{entry['id']!r} / {stratum!r} has no recorded page check") from None
        return pages[:wanted], supported

    def reference_list_found(self, entry: dict[str, Any]) -> bool:
        try:
            return self.whole[entry["id"]]
        except KeyError:
            raise ReplayMiss(f"{entry['id']!r} has no recorded whole-paper check") from None


# --- manifest --------------------------------------------------------------------------------


def build_manifest(sources: list[dict[str, Any]], seed: str = DEFAULT_SEED, checks: PageChecks | None = None) -> dict[str, Any]:
    """Select pilot and held-out documents. Deterministic in (sources, seed, page checks); input order is irrelevant.

    With ``checks``, a candidate is accepted only if the page-level check supports it; failures are
    recorded under ``page_check_rejected`` and the next candidate by rank takes the slot. Pilot
    documents then carry ``candidate_pages``: pages that passed a crude check, to be looked at, not
    confirmed layouts.
    """
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for entry in sorted(sources, key=lambda s: s["id"]):
        ok, reason = license_gate(entry.get("license"))
        if ok:
            eligible.append(entry)
        else:
            excluded.append({"id": entry["id"], "license": str(entry.get("license", "")), "reason": reason})

    candidates = {e["id"]: candidate_strata(e) for e in eligible}
    by_id = {e["id"]: e for e in eligible}
    scarcity = {s: sum(1 for c in candidates.values() if s in c) for s in STRATA}
    order = sorted(STRATA, key=lambda s: (scarcity[s], STRATA.index(s)))  # fill the scarcest stratum first

    used_keys: set[tuple[str, str]] = set()
    counts: dict[str, dict[str, int]] = {"pilot": {}, "held_out": {}}
    picked: dict[str, dict[str, list[dict[str, Any]]]] = {"pilot": {}, "held_out": {}}
    unfilled: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []

    def take(subset: str, entry: dict[str, Any]) -> None:
        used_keys.update(identity_keys(entry))
        for key in (f"field:{entry.get('field')}", f"lang:{entry.get('language')}"):
            counts[subset][key] = counts[subset].get(key, 0) + 1

    def free(entry_id: str) -> bool:
        return not identity_keys(by_id[entry_id]) & used_keys

    def pick(subset: str, stratum: str) -> list[dict[str, Any]]:
        pilot_set = subset == "pilot"
        wanted_docs = PILOT_QUOTAS[stratum]["documents"] if pilot_set else HELD_OUT_QUOTAS[stratum]
        remaining = PILOT_QUOTAS[stratum]["pages"] if pilot_set else 0
        cap = -(-PILOT_QUOTAS[stratum]["pages"] // PILOT_QUOTAS[stratum]["documents"])
        chosen: list[dict[str, Any]] = []
        skipped: set[str] = set()
        # Pilot: keep taking documents until the stratum's pages are covered (with page checks a
        # document may support fewer pages than its share); without checks stop at the document quota.
        while (remaining > 0 and (checks is not None or len(chosen) < wanted_docs)) if pilot_set else len(chosen) < wanted_docs:
            pool = [i for i, c in candidates.items() if stratum in c and i not in skipped and free(i)]
            if not pool:
                break

            def rank(i: str) -> tuple[int, int, str]:
                entry = by_id[i]
                spread = counts[subset].get(f"field:{entry.get('field')}", 0)
                if stratum == "non-english":
                    spread += counts[subset].get(f"lang:{entry.get('language')}", 0)
                return (0 if candidates[i][stratum] == "metadata" else 1, spread, _rank(seed, subset, stratum, i))

            best = min(pool, key=rank)
            share = min(cap, remaining) if pilot_set else 1
            extra: dict[str, Any] = {}
            if checks is not None:
                pages, supported = checks.pages(by_id[best], stratum, share)
                if not pages:
                    rejected.append({"set": subset, "stratum": stratum, "id": best, "reason": "page check supports no page for this stratum"})
                    skipped.add(best)
                    continue
                if pilot_set:
                    share = len(pages)
                    extra = {"candidate_pages": pages, "supported_pages": supported}
                else:
                    extra = {"supported_pages": supported}
            take(subset, by_id[best])
            if pilot_set:
                extra["page_quota"] = share
            chosen.append(_doc(by_id[best], "stratum_pages", stratum, candidates[best][stratum], **extra))
            remaining -= share
        if pilot_set:
            found = sum(d["page_quota"] for d in chosen)
            if found < PILOT_QUOTAS[stratum]["pages"]:
                unfilled.append({"set": subset, "stratum": stratum, "unit": "pages", "wanted": PILOT_QUOTAS[stratum]["pages"], "found": found})
        elif len(chosen) < wanted_docs:
            unfilled.append({"set": subset, "stratum": stratum, "unit": "documents", "wanted": wanted_docs, "found": len(chosen)})
        return chosen

    for stratum in order:
        picked["pilot"][stratum] = pick("pilot", stratum)
        picked["held_out"][stratum] = pick("held_out", stratum)

    def whole(subset: str, wanted: int) -> list[dict[str, Any]]:
        lo, hi = WHOLE_PAPER_PAGES
        chosen: list[dict[str, Any]] = []
        skipped: set[str] = set()
        while len(chosen) < wanted:
            pool = [
                e for e in eligible
                if e["id"] not in skipped and free(e["id"]) and e.get("field") in JOURNAL_FIELDS and e.get("language") == "en"
                and lo <= e["pages"] <= hi and not set(e.get("styles") or []) & SCAN_STYLES
            ]
            if not pool:
                break
            best = min(pool, key=lambda e: (counts[subset].get(f"field:{e.get('field')}", 0), _rank(seed, subset, "whole", e["id"])))
            extra: dict[str, Any] = {}
            if checks is not None:
                if not checks.reference_list_found(best):
                    rejected.append({"set": subset, "stratum": "whole_paper", "id": best["id"], "reason": "no reference list found"})
                    skipped.add(best["id"])
                    continue
                extra = {"reference_list_found": True}
            take(subset, best)
            chosen.append(_doc(best, "whole_paper", None, None, **extra))
        if len(chosen) < wanted:
            unfilled.append({"set": subset, "stratum": "whole_paper", "unit": "documents", "wanted": wanted, "found": len(chosen)})
        return chosen

    pilot_whole = whole("pilot", PILOT_WHOLE_PAPERS)
    held_whole = whole("held_out", HELD_OUT_WHOLE_PAPERS)

    def flat(subset: str, extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [d for s in STRATA for d in picked[subset][s]] + extra

    pilot_docs, held_docs = flat("pilot", pilot_whole), flat("held_out", held_whole)
    return {
        "manifest_version": MANIFEST_VERSION,
        "seed": seed,
        "selection_inputs_sha256": selection_inputs_sha256(sources),
        "page_check_version": PAGE_CHECK_VERSION if checks is not None else None,
        "note": "Document-level selection plus crude page-level checks; no labels. candidate_pages are "
                "pages that passed a heuristic check and still need a human look, not confirmed layouts. "
                "The held-out set is fixed now and must not be used for tuning, threshold calibration or "
                "the design of injected defects.",
        "strata": list(STRATA),
        "pilot_page_target": sum(q["pages"] for q in PILOT_QUOTAS.values()),
        "assumptions": ASSUMPTIONS,
        "pilot": pilot_docs,
        "held_out": held_docs,
        "excluded": excluded,
        "page_check_rejected": rejected,
        "unfilled": unfilled,
    }


def render_manifest(manifest: dict[str, Any]) -> bytes:
    return canonical_json(manifest)


def manifest_pin(manifest_bytes: bytes) -> str:
    """SHA-256 of the manifest with line endings normalized, so a CRLF checkout still verifies."""
    return sha256_bytes(manifest_bytes.replace(b"\r\n", b"\n"))


def load_sources(path: Path = SOURCES_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["sources"]  # type: ignore[no-any-return]


def validate_manifest(manifest: dict[str, Any], sources: list[dict[str, Any]]) -> list[str]:
    """Invariants a manifest must keep: disjoint sets (by id, hash, DOI, title), licence-clean, pins match."""
    problems: list[str] = []
    by_id = {s["id"]: s for s in sources}
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        problems.append(f"unknown manifest_version {manifest.get('manifest_version')!r}")
    sets = {name: [d["id"] for d in manifest[name]] for name in ("pilot", "held_out")}
    for name, ids in sets.items():
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            problems.append(f"{name}: document {dup!r} listed more than once")
    for shared in sorted(set(sets["pilot"]) & set(sets["held_out"])):
        problems.append(f"document {shared!r} is in both the pilot and the held-out set")
    keys: dict[str, dict[tuple[str, str], str]] = {"pilot": {}, "held_out": {}}
    for name in ("pilot", "held_out"):
        for doc in manifest[name]:
            entry = by_id.get(doc["id"])
            if entry is None:
                problems.append(f"{name}: {doc['id']!r} is not in sources.json")
                continue
            for key in identity_keys(entry):
                keys[name].setdefault(key, doc["id"])
            ok, reason = license_gate(entry.get("license"))
            if not ok:
                problems.append(f"{name}: {doc['id']!r} fails the licence gate: {reason}")
            if entry["sha256"] != doc["pdf_sha256"]:
                problems.append(f"{name}: {doc['id']!r} pdf_sha256 differs from sources.json")
    for key in sorted(set(keys["pilot"]) & set(keys["held_out"])):
        if keys["pilot"][key] != keys["held_out"][key]:
            problems.append(
                f"pilot {keys['pilot'][key]!r} and held-out {keys['held_out'][key]!r} are the same work (shared {key[0]})")
    excluded = {e["id"] for e in manifest["excluded"]}
    for shared in sorted(excluded & (set(sets["pilot"]) | set(sets["held_out"]))):
        problems.append(f"document {shared!r} is both excluded and selected")
    return problems


def check_manifest(
    manifest_path: Path = MANIFEST_PATH,
    pin_path: Path = PIN_PATH,
    sources_path: Path = SOURCES_PATH,
) -> list[str]:
    """Verify the committed manifest: pin matches, selection inputs unchanged, replay regenerates it identically.

    Page-check results are replayed from the manifest itself, so no PDFs are needed; a selection
    that would ask for a check the manifest never recorded is reported.
    """
    problems: list[str] = []
    committed = manifest_path.read_bytes()
    pin = pin_path.read_text(encoding="utf-8").strip() if pin_path.exists() else ""
    if manifest_pin(committed) != pin:
        problems.append("manifest SHA-256 does not match its pin file")
    manifest = json.loads(committed)
    sources = load_sources(sources_path)
    problems.extend(validate_manifest(manifest, sources))
    if manifest.get("selection_inputs_sha256") != selection_inputs_sha256(sources):
        problems.append("sources.json selection inputs changed since the manifest was generated; regenerate and review the diff")
    replay = ReplayChecks(manifest) if manifest.get("page_check_version") is not None else None
    try:
        regenerated = render_manifest(build_manifest(sources, manifest.get("seed", DEFAULT_SEED), replay))
    except ReplayMiss as miss:
        problems.append(f"selection changed: {miss}")
    else:
        if regenerated != committed.replace(b"\r\n", b"\n"):
            problems.append("manifest differs from a fresh regeneration with its seed")
    return problems


def summary_lines(manifest: dict[str, Any]) -> list[str]:
    lines = [f"seed {manifest['seed']}; pilot page target {manifest['pilot_page_target']}"]
    for name in ("pilot", "held_out"):
        lines.append(f"{name}:")
        for stratum in (*STRATA, None):
            ids = []
            for d in manifest[name]:
                if d["stratum"] != stratum:
                    continue
                label = f"{d['id']}[{d['basis'] or 'whole'}]"
                if d.get("candidate_pages"):
                    label += f" pages {d['candidate_pages']}"
                elif d.get("page_quota"):
                    label += f" x{d['page_quota']}p"
                ids.append(label)
            if ids:
                lines.append(f"  {stratum or 'whole papers'}: " + ", ".join(ids))
    for item in manifest["excluded"]:
        lines.append(f"excluded {item['id']}: {item['reason']}")
    for item in manifest["page_check_rejected"]:
        lines.append(f"page check rejected {item['set']}/{item['stratum']} {item['id']}: {item['reason']}")
    for item in manifest["unfilled"]:
        lines.append(f"SHORT {item['set']}/{item['stratum']}: wanted {item['wanted']} {item['unit']}, found {item['found']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate or verify the E2 pilot/held-out manifest.")
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--pin", type=Path, default=PIN_PATH)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--cache", type=Path, default=None,
                        help="corpus PDF cache: run the page-level checks (required with --write); read-only, no downloads")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the manifest and its SHA-256 pin")
    mode.add_argument("--check", action="store_true", help="verify the committed manifest and pin; exit 1 on drift")
    args = parser.parse_args(argv)

    if args.check:
        problems = check_manifest(args.manifest, args.pin, args.sources)
        for problem in problems:
            print(f"PROBLEM {problem}")
        print("manifest OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if args.write and args.cache is None:
        parser.error("--write needs --cache: page choices are confirmed against the cached, hash-verified PDFs before pinning")

    sources = load_sources(args.sources)
    if args.cache is not None:
        from .drafts import CachedPageChecks, PdfProblem

        try:
            manifest = build_manifest(sources, args.seed, CachedPageChecks(args.cache, seed=args.seed))
        except PdfProblem as pdf_problem:
            print(f"PROBLEM {pdf_problem}")
            return 1
    else:
        manifest = build_manifest(sources, args.seed)
    problems = validate_manifest(manifest, sources)
    if problems:
        for problem in problems:
            print(f"PROBLEM {problem}")
        return 1
    data = render_manifest(manifest)
    if args.write:
        args.manifest.write_bytes(data)
        args.pin.write_bytes((manifest_pin(data) + "\n").encode("ascii"))
        print(f"wrote {args.manifest} and {args.pin}")
    for line in summary_lines(manifest):
        print(line)
    return 1 if manifest["unfilled"] else 0
