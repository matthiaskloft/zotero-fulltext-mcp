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
JOURNAL_FIELDS = frozenset(f for fields in ASSUMED_FIELDS.values() for f in fields)

ASSUMPTIONS = [
    "Stratum 'metadata' basis: derived from sources.json `styles` (two columns, scanned pages, "
    "noisy existing OCR, no text layer, references) or `language` (non-English). Verified at "
    "corpus inspection time, but only at document level.",
    "Stratum 'assumed' basis: UNVERIFIED. sources.json carries no page-level layout, so tables, "
    "equations, two-column layout and a reference list are assumed from the entry's `field` "
    "(and a minimum page count for references). The host step checks the assumption per page and "
    "reports unsupported choices instead of picking an arbitrary page.",
    "Non-English entries are taken from `language` only; individual pages may still be mostly "
    "figures or references.",
    "Entries without `field` and `language` (the four original sources) can only qualify through "
    "`styles`; their language is treated as unknown, not English.",
]

# --- licence gate ----------------------------------------------------------------------------
_REJECT_CASE_SENSITIVE = re.compile(r"(?<![A-Za-z])(?:NC|ND|SA)(?![A-Za-z])")
_ACCEPT = re.compile(r"^\s*(?:CC[ -]BY\b|Creative Commons Attribution\b|CC0\b|Public domain\b)", re.IGNORECASE)


def license_gate(license_text: Any) -> tuple[bool, str]:
    """Corpus rule: CC BY (any version), CC0, public domain pass; NC/ND/SA/publisher/unverifiable fail.

    The whole licence string is inspected, not just its prefix: an entry whose note mentions an
    NC/ND/SA licence (for example a described dataset) is rejected as ambiguous until the
    maintainer resolves it, which is the conservative reading of "unverifiable".
    """
    if not isinstance(license_text, str) or not license_text.strip():
        return False, "no licence recorded (unverifiable)"
    text = license_text.strip()
    words = re.search(r"non-?commercial|no-?deriv|share-?alike|all rights reserved|publisher", text, re.IGNORECASE)
    token = _REJECT_CASE_SENSITIVE.search(text)
    if words or token:
        hit = (words or token).group(0)  # type: ignore[union-attr]
        return False, f"licence text mentions {hit!r}; NC/ND/SA/publisher terms are rejected (needs maintainer confirmation)"
    if not _ACCEPT.match(text):
        return False, "licence is not CC BY, CC0 or public domain (unverifiable)"
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
    if "two columns" in styles:
        found["two-column"] = "metadata"
    if "references" in styles:
        found["references"] = "metadata"
    if not styles & SCAN_STYLES:
        for stratum, fields in ASSUMED_FIELDS.items():
            if stratum not in found and field in fields and pages >= ASSUMED_MIN_PAGES.get(stratum, 1):
                found[stratum] = "assumed"
    return found


def _doc(entry: dict[str, Any], role: str, stratum: str | None, basis: str | None, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": entry["id"], "role": role, "stratum": stratum, "basis": basis,
        "field": entry.get("field"), "language": entry.get("language"),
        "license": entry["license"], "pdf_pages": entry["pages"], "pdf_sha256": entry["sha256"],
    }
    doc.update(extra)
    return doc


def _split(total: int, parts: int) -> list[int]:
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


# --- manifest --------------------------------------------------------------------------------


def build_manifest(sources: list[dict[str, Any]], seed: str = DEFAULT_SEED) -> dict[str, Any]:
    """Select pilot and held-out documents. Deterministic in (sources, seed); input order is irrelevant."""
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

    used: set[str] = set()
    counts: dict[str, dict[str, int]] = {"pilot": {}, "held_out": {}}
    picked: dict[str, dict[str, list[dict[str, Any]]]] = {"pilot": {}, "held_out": {}}
    unfilled: list[dict[str, Any]] = []

    def pick(subset: str, stratum: str, wanted: int) -> list[dict[str, Any]]:
        chosen: list[dict[str, Any]] = []
        for _ in range(wanted):
            pool = [i for i, c in candidates.items() if stratum in c and i not in used]
            if not pool:
                break

            def rank(i: str) -> tuple[int, int, str]:
                entry = by_id[i]
                spread = counts[subset].get(f"field:{entry.get('field')}", 0)
                if stratum == "non-english":
                    spread += counts[subset].get(f"lang:{entry.get('language')}", 0)
                return (0 if candidates[i][stratum] == "metadata" else 1, spread, _rank(seed, subset, stratum, i))

            best = min(pool, key=rank)
            used.add(best)
            for key in (f"field:{by_id[best].get('field')}", f"lang:{by_id[best].get('language')}"):
                counts[subset][key] = counts[subset].get(key, 0) + 1
            chosen.append(_doc(by_id[best], "stratum_pages", stratum, candidates[best][stratum]))
        if len(chosen) < wanted:
            unfilled.append({"set": subset, "stratum": stratum, "wanted": wanted, "found": len(chosen)})
        return chosen

    for stratum in order:
        docs = pick("pilot", stratum, PILOT_QUOTAS[stratum]["documents"])
        for doc, pages in zip(docs, _split(PILOT_QUOTAS[stratum]["pages"], max(len(docs), 1))):
            doc["page_quota"] = pages
        picked["pilot"][stratum] = docs
        picked["held_out"][stratum] = pick("held_out", stratum, HELD_OUT_QUOTAS[stratum])

    def whole(subset: str, wanted: int) -> list[dict[str, Any]]:
        lo, hi = WHOLE_PAPER_PAGES
        chosen: list[dict[str, Any]] = []
        for _ in range(wanted):
            pool = [
                e for e in eligible
                if e["id"] not in used and e.get("field") in JOURNAL_FIELDS and e.get("language") == "en"
                and lo <= e["pages"] <= hi and not set(e.get("styles") or []) & SCAN_STYLES
            ]
            if not pool:
                break
            best = min(pool, key=lambda e: (counts[subset].get(f"field:{e.get('field')}", 0), _rank(seed, subset, "whole", e["id"])))
            used.add(best["id"])
            counts[subset][f"field:{best.get('field')}"] = counts[subset].get(f"field:{best.get('field')}", 0) + 1
            chosen.append(_doc(best, "whole_paper", None, None))
        if len(chosen) < wanted:
            unfilled.append({"set": subset, "stratum": "whole_paper", "wanted": wanted, "found": len(chosen)})
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
        "note": "Document-level selection only; no labels. Pilot pages are chosen by the opt-in host "
                "step from locally cached, hash-verified PDFs. The held-out set is fixed now and must "
                "not be used for tuning, threshold calibration or injected-defect design.",
        "strata": list(STRATA),
        "pilot_page_target": sum(q["pages"] for q in PILOT_QUOTAS.values()),
        "assumptions": ASSUMPTIONS,
        "pilot": pilot_docs,
        "held_out": held_docs,
        "excluded": excluded,
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
    """Invariants a manifest must keep: disjoint sets, licence-clean, PDF pins match sources."""
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
    for name in ("pilot", "held_out"):
        for doc in manifest[name]:
            entry = by_id.get(doc["id"])
            if entry is None:
                problems.append(f"{name}: {doc['id']!r} is not in sources.json")
                continue
            ok, reason = license_gate(entry.get("license"))
            if not ok:
                problems.append(f"{name}: {doc['id']!r} fails the licence gate: {reason}")
            if entry["sha256"] != doc["pdf_sha256"]:
                problems.append(f"{name}: {doc['id']!r} pdf_sha256 differs from sources.json")
    excluded = {e["id"] for e in manifest["excluded"]}
    for shared in sorted(excluded & (set(sets["pilot"]) | set(sets["held_out"]))):
        problems.append(f"document {shared!r} is both excluded and selected")
    return problems


def check_manifest(
    manifest_path: Path = MANIFEST_PATH,
    pin_path: Path = PIN_PATH,
    sources_path: Path = SOURCES_PATH,
) -> list[str]:
    """Verify the committed manifest: pin matches, selection inputs unchanged, regeneration identical."""
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
    regenerated = render_manifest(build_manifest(sources, manifest.get("seed", DEFAULT_SEED)))
    if regenerated != committed.replace(b"\r\n", b"\n"):
        problems.append("manifest differs from a fresh regeneration with its seed")
    return problems


def summary_lines(manifest: dict[str, Any]) -> list[str]:
    lines = [f"seed {manifest['seed']}; pilot page target {manifest['pilot_page_target']}"]
    for name in ("pilot", "held_out"):
        lines.append(f"{name}:")
        for stratum in (*STRATA, None):
            ids = [
                f"{d['id']}[{d['basis'] or 'whole'}]" + (f" x{d['page_quota']}p" if d.get("page_quota") else "")
                for d in manifest[name] if d["stratum"] == stratum
            ]
            if ids:
                lines.append(f"  {stratum or 'whole papers'}: " + ", ".join(ids))
    for item in manifest["excluded"]:
        lines.append(f"excluded {item['id']}: {item['reason']}")
    for item in manifest["unfilled"]:
        lines.append(f"unfilled {item['set']}/{item['stratum']}: wanted {item['wanted']}, found {item['found']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate or verify the E2 pilot/held-out manifest.")
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--pin", type=Path, default=PIN_PATH)
    parser.add_argument("--seed", default=DEFAULT_SEED)
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

    manifest = build_manifest(load_sources(args.sources), args.seed)
    problems = validate_manifest(manifest, load_sources(args.sources))
    if problems:
        for problem in problems:
            print(f"PROBLEM {problem}")
        return 1
    data = render_manifest(manifest)
    if args.write:
        args.manifest.write_bytes(data)
        args.pin.write_text(manifest_pin(data) + "\n", encoding="utf-8")
        print(f"wrote {args.manifest} and {args.pin}")
    for line in summary_lines(manifest):
        print(line)
    return 0
