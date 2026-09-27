# Performance Baselines

Release guardrails from hardening Package 5, step 6 — not hard limits. Re-measure before a release
that touches indexing, search, or the audit, and treat a large regression against the table below
as something to explain before shipping.

## Reference library and machine

One real library of a few thousand indexed attachments and tens of thousands of chunks (an
`index.sqlite` on the order of 1 GB), on a Windows 11 laptop with the index on a local SSD.
Version 0.10.0, default chunking. Compare a re-run against these figures on a library of similar
scale.

## Results (2026-09-27)

| Measurement | Command | Result |
| --- | --- | --- |
| Mapping snapshot | `dry-run` | 2 min 31 s |
| Index build (re-chunk from JSONL) | `rebuild-index --output-root <scratch> --from-jsonl <current index.jsonl>` | 1 min 24 s |
| Index size | — | about 1 GB SQLite, JSONL about a third of that |
| Audit, fast | `audit-library --mapping-report <run> --json` | 6.7 s |
| Audit, full (re-hash PDFs) | `audit-library --mapping-report <run> --full --json` | 18.5 s |
| Search, first call | `benchmarks/latency.py` | 5.0 s |
| Search, p50 / p95 / max | `benchmarks/latency.py` (60 calls, limit 10) | 418 ms / 16.1 s / 27.0 s |
| Passage (one chunk), p50 / p95 / max | `benchmarks/latency.py` (120 calls) | 436 ms / 481 ms / 1.7 s |

The index build ran into a scratch output root, so the published generation was not touched. Command
times include interpreter start-up; the latency figures are measured in-process and exclude it.

## What the numbers show

**Search latency depends on how common the terms are, not on the library size.** Per query, a
rare term (`heteroscedasticity`) answers in 60 ms and a specific phrase in 70–700 ms. A common term
takes 1.9 s (`regression`), an `any_terms` query 1.7 s, and a stop word 17 s. BM25 ranking scores
every matching chunk before the result limit applies, so a term that matches most chunks costs about
as much as scanning the index. The p95 is dominated by that tail.

**Passage fetches scanned the whole `chunks` table.** The table had no index on
`(record_id, chunk_index)` (`EXPLAIN QUERY PLAN` reported `SCAN chunks`), so each `get_fulltext`
call, and the `chunk_count` subquery in it, read through every chunk in the index. That was the
flat ~435 ms per passage.

## After adding `chunks_record_chunk_idx` (same day, same library)

| Measurement | Before | After |
| --- | --- | --- |
| Passage p50 / p95 / max | 436 ms / 481 ms / 1.7 s | 1.6 ms / 2.0 ms / 2.4 ms |
| Index build | 1 min 24 s | 1 min 16 s |
| Index size | about 1 GB | unchanged within 1% |
| Search p50 / p95 | 418 ms / 16.1 s | 335 ms / 15.0 s |

Search was not the target and the change is within run-to-run noise; the common-term tail remains.
An index built before this change keeps working, but it only gets the faster lookups after
`rebuild-index`.

## Re-running

```powershell
$python benchmarks\latency.py --db $data\index\generations\<generation_id>\index.sqlite
```

Time the build and the audits with the commands in the table. Point `rebuild-index` at a scratch
`--output-root`, never at `$data`.
