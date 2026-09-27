"""Search and passage latency against a real published index (hardening Package 5, step 6).

Times ``search_fts`` and ``get_fulltext`` in-process, so interpreter and import start-up are not
counted -- those are paid once per MCP server launch, not per tool call. Read-only: it opens the
index through the same read-only connection the server uses.

The queries are generic research vocabulary chosen to span hit counts from rare to ubiquitous;
they say nothing about any particular library. Passage requests reuse the search hits' locators,
the same path an agent takes from discovery to evidence.

    python benchmarks/latency.py --db <output_root>/index/generations/<id>/index.sqlite
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from zotero_pdf_text.fts import get_fulltext, search_fts

QUERIES = [
    ("regression", "all_terms"),
    ("measurement invariance", "all_terms"),
    ("randomized controlled trial", "phrase"),
    ("bayesian hierarchical model", "all_terms"),
    ("reliability validity", "any_terms"),
    ("longitudinal panel survey", "all_terms"),
    ("missing data imputation", "all_terms"),
    ("effect size", "phrase"),
    ("structural equation modeling", "all_terms"),
    ("item response theory", "phrase"),
    ("the", "all_terms"),
    ("heteroscedasticity", "all_terms"),
]


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    q = statistics.quantiles(ordered, n=100, method="inclusive")
    return {
        "n": len(ordered),
        "p50_ms": round(q[49], 2),
        "p95_ms": round(q[94], 2),
        "max_ms": round(ordered[-1], 2),
    }


def _timed(fn):
    start = time.perf_counter()
    result = fn()
    return (time.perf_counter() - start) * 1000, result


def run(db: Path, repeats: int, limit: int) -> dict[str, object]:
    cold_ms, _ = _timed(lambda: search_fts(db, QUERIES[0][0], limit=limit, search_mode=QUERIES[0][1]))

    search_ms: list[float] = []
    locators: list[tuple[str, int, str]] = []
    for _ in range(repeats):
        for query, mode in QUERIES:
            elapsed, hits = _timed(lambda: search_fts(db, query, limit=limit, search_mode=mode))
            search_ms.append(elapsed)
            locators.extend((h.zotero_attachment_key, h.chunk_index, h.chunk_sha256) for h in hits)
    locators = list(dict.fromkeys(locators))

    passage_ms: list[float] = []
    for key, chunk_index, sha in locators:
        elapsed, _ = _timed(
            lambda: get_fulltext(db, attachment_key=key, chunk_index=chunk_index, expected_chunk_sha256=sha)
        )
        passage_ms.append(elapsed)

    return {
        "db_bytes": db.stat().st_size,
        "search_limit": limit,
        "first_search_ms": round(cold_ms, 2),
        "search": _percentiles(search_ms),
        "passage": _percentiles(passage_ms),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True, help="Published index.sqlite to measure.")
    parser.add_argument("--repeats", type=int, default=5, help="Passes over the query set.")
    parser.add_argument("--limit", type=int, default=10, help="Search result limit (the MCP default).")
    args = parser.parse_args()
    print(json.dumps(run(args.db, args.repeats, args.limit), indent=2))


if __name__ == "__main__":
    main()
