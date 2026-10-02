"""Passage-level evaluation for the S1a chunking experiments (issue #107).

Extends the paper-level harness in ``retrieval.py``: private evidence-span judgments, graded
passage nDCG, evidence-span recall and precision, duplicate rate, locator validity, equal top-k and
equal returned-token budgets, and paired uncertainty between configurations. Retrievers share one
ranked-result interface (:class:`PassageHit`), so lexical, semantic and hybrid channels are scored
by the same code. Semantic retrieval stays *unavailable* until an embedding backend exists; the
hybrid channel fuses ranks (RRF) of whatever channels are available and reports the rest as such.

Read-only with respect to the user's data: generations are opened through the server's read-only
connection, experimental indexes are separate scratch files (see ``chunking.py``), and nothing is
printed except question ids, ranks, aggregates and configuration facts.
"""

from __future__ import annotations

import math
import random
import sqlite3
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from chunking import (
    CorpusDoc,
    ExperimentSummary,
    Tokenizer,
    open_experiment,
    percentile,
)

from zotero_pdf_text.fts import (
    MAX_SEARCH_RESULTS,
    _match_query,
    _validate_search_request,
    chunk_sha256,
    connect_readonly,
    search_fts,
)

PASSAGE_CANDIDATES = MAX_SEARCH_RESULTS
DEFAULT_BUDGETS = (500, 1000, 2000)
COVERAGE_THRESHOLD = 0.5  # an evidence span counts as retrieved when this share of it is returned
RRF_K = 60
HYBRID_CANDIDATES = 50
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261002
REGRESSION_EPSILON = 1e-9
LEXICAL, SEMANTIC, HYBRID = "lexical", "semantic", "hybrid"
CHANNELS = (LEXICAL, SEMANTIC, HYBRID)
METRIC_NAMES = (
    "ndcg", "span_recall", "char_recall", "precision", "dup_rate",
    "cited_span_recall", "trap_rate", "qualifier_recall", "tokens", "n_hits",
)
PRIMARY_METRICS = ("ndcg", "span_recall")


# --- ranked-result interface --------------------------------------------------------------------


@dataclass(frozen=True)
class HitSpan:
    """One original-source locator: a character range of the record text and its hash."""

    start: int
    end: int
    sha256: str = ""


@dataclass(frozen=True)
class PassageHit:
    """One ranked result, whatever produced it.

    ``spans`` are the original source ranges the result returns. A plain chunk has one; an expanded
    or assembled result has several, each with its own locator, never one invented continuous
    range. ``tokens`` is the returned text; ``extra_tokens`` is ancestor/expansion text charged to
    the same budget.
    """

    attachment_key: str
    citation_key: str
    spans: tuple[HitSpan, ...]
    tokens: int
    extra_tokens: int = 0
    channels: tuple[str, ...] = ()

    @property
    def cost(self) -> int:
        return self.tokens + self.extra_tokens

    @property
    def identity(self) -> tuple[str, int, int]:
        first = self.spans[0]
        return (self.attachment_key, first.start, first.end)


class Retriever(Protocol):
    name: str
    channel: str

    def unavailable_reason(self) -> str | None: ...

    def retrieve(self, query: str, search_mode: str, limit: int) -> list[PassageHit]: ...


class ExperimentRetriever:
    """BM25 over an experimental index; every matching chunk is a candidate, no per-paper dedup."""

    channel = LEXICAL

    def __init__(self, summary: ExperimentSummary, expand: int = 0, name: str | None = None) -> None:
        self.summary = summary
        self.expand = expand
        self.name = name or summary.spec.label + (f"+expand{expand}" if expand else "")
        self._con = open_experiment(summary.path)

    def close(self) -> None:
        self._con.close()

    def unavailable_reason(self) -> str | None:
        return None

    def retrieve(self, query: str, search_mode: str, limit: int) -> list[PassageHit]:
        terms, *_ = _validate_search_request(query, 1, search_mode)
        rows = self._con.execute(
            """
            SELECT c.doc_id, c.chunk_index, c.start_char, c.end_char, c.tokens, c.chunk_sha256,
                   d.attachment_key, d.citation_key
            FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.rowid JOIN docs d ON d.doc_id = c.doc_id
            WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts), c.chunk_id LIMIT ?
            """,
            (_match_query(terms, search_mode), limit),
        ).fetchall()
        return [self._hit(row) for row in rows]

    def _hit(self, row: sqlite3.Row) -> PassageHit:
        spans = [HitSpan(row["start_char"], row["end_char"], row["chunk_sha256"])]
        extra = 0
        if self.expand:
            neighbours = self._con.execute(
                "SELECT start_char, end_char, tokens, chunk_sha256 FROM chunks WHERE doc_id = ? AND chunk_index"
                " BETWEEN ? AND ? AND chunk_index != ? ORDER BY chunk_index",
                (row["doc_id"], row["chunk_index"] - self.expand, row["chunk_index"] + self.expand, row["chunk_index"]),
            ).fetchall()
            spans += [HitSpan(n["start_char"], n["end_char"], n["chunk_sha256"]) for n in neighbours]
            extra = sum(n["tokens"] for n in neighbours)
            spans.sort(key=lambda s: s.start)
        return PassageHit(
            row["attachment_key"], row["citation_key"], tuple(spans), row["tokens"], extra, (LEXICAL,)
        )


class GenerationRetriever:
    """The current production search (one best chunk per paper), read-only. Not rebuilt or modified."""

    channel = LEXICAL

    def __init__(self, db: Path, tokenizer: Tokenizer, name: str) -> None:
        self.db, self.tokenizer, self.name = db, tokenizer, name

    def unavailable_reason(self) -> str | None:
        return None

    def retrieve(self, query: str, search_mode: str, limit: int) -> list[PassageHit]:
        hits = search_fts(self.db, query, limit=min(limit, MAX_SEARCH_RESULTS), search_mode=search_mode)  # type: ignore[arg-type]
        con = connect_readonly(self.db)
        try:
            out = []
            for hit in hits:
                row = con.execute(
                    "SELECT c.text FROM chunks c JOIN metadata m ON m.record_id = c.record_id"
                    " WHERE m.zotero_attachment_key = ? AND c.start_char = ? AND c.end_char = ?",
                    (hit.zotero_attachment_key, hit.start_char, hit.end_char),
                ).fetchone()
                tokens = self.tokenizer.count(row[0]) if row else 0
                out.append(
                    PassageHit(
                        hit.zotero_attachment_key, hit.citation_key,
                        (HitSpan(hit.start_char, hit.end_char, hit.chunk_sha256),), tokens, 0, (LEXICAL,),
                    )
                )
            return out
        finally:
            con.close()


class SemanticRetriever:
    """Placeholder for the semantic channel: no embedding backend exists yet (roadmap S6/S6a)."""

    channel = SEMANTIC
    name = "semantic"

    def unavailable_reason(self) -> str | None:
        return "no embedding backend is implemented; semantic runs stay unavailable until S6"

    def retrieve(self, query: str, search_mode: str, limit: int) -> list[PassageHit]:
        raise RuntimeError(self.unavailable_reason())


class HybridRetriever:
    """Reciprocal-rank fusion of a lexical and a semantic retriever at the passage level."""

    channel = HYBRID

    def __init__(
        self, lexical: Retriever, semantic: Retriever, *, candidates: int = HYBRID_CANDIDATES, rrf_k: int = RRF_K
    ) -> None:
        self.lexical, self.semantic, self.candidates, self.rrf_k = lexical, semantic, candidates, rrf_k
        self.name = f"hybrid({lexical.name}+{semantic.name})"

    def unavailable_reason(self) -> str | None:
        return self.lexical.unavailable_reason() or self.semantic.unavailable_reason()

    def retrieve(self, query: str, search_mode: str, limit: int) -> list[PassageHit]:
        return rrf_fuse(
            [
                self.lexical.retrieve(query, search_mode, self.candidates),
                self.semantic.retrieve(query, search_mode, self.candidates),
            ],
            self.rrf_k,
        )[:limit]


def rrf_fuse(rankings: Sequence[Sequence[PassageHit]], k: int = RRF_K) -> list[PassageHit]:
    """Fuse ranked lists by ``sum(1 / (k + rank))`` per source span; ties keep first-seen order.

    Hits are identified by document and leading span, so a passage returned by two channels is one
    result that records both channels.
    """
    score: dict[tuple[str, int, int], float] = defaultdict(float)
    best: dict[tuple[str, int, int], PassageHit] = {}
    channels: dict[tuple[str, int, int], list[str]] = defaultdict(list)
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            score[hit.identity] += 1 / (k + rank)
            best.setdefault(hit.identity, hit)
            channels[hit.identity].extend(c for c in hit.channels if c not in channels[hit.identity])
    order = sorted(score, key=lambda ident: -score[ident])  # sorted is stable: first-seen breaks ties
    return [
        PassageHit(
            h.attachment_key, h.citation_key, h.spans, h.tokens, h.extra_tokens, tuple(channels[ident])
        )
        for ident in order
        for h in (best[ident],)
    ]


# --- judgments ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A resolved judged span in the original record text."""

    field: str  # "attachment_key" or "citation_key"
    key: str
    start: int
    end: int
    grade: int = 1

    def matches(self, hit: PassageHit) -> bool:
        return (hit.attachment_key if self.field == "attachment_key" else hit.citation_key) == self.key

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Judgments:
    evidence: tuple[Span, ...]
    traps: tuple[Span, ...] = ()
    qualifiers: tuple[Span, ...] = ()


def resolve_judgments(question, docs: dict[str, CorpusDoc] | None) -> Judgments:
    """Turn a question's span specs into character ranges, resolving quotes against the corpus."""

    def resolve(spec) -> Span:
        if spec.quote is None:
            start, end = spec.start, spec.end
            text = _spec_text(question.id, spec, docs, required=False)
        else:
            text = _spec_text(question.id, spec, docs, required=True)
            found = [i for i in _occurrences(text, spec.quote)]
            if not found:
                raise SystemExit(f"Question {question.id}: a judged quote was not found in its document.")
            if spec.occurrence is None and len(found) > 1:
                raise SystemExit(
                    f"Question {question.id}: a judged quote occurs {len(found)} times; add \"occurrence\"."
                )
            index = (spec.occurrence or 1) - 1
            if index >= len(found):
                raise SystemExit(f"Question {question.id}: \"occurrence\" exceeds the quote's occurrences.")
            start, end = found[index], found[index] + len(spec.quote)
        if text is not None and not 0 <= start < end <= len(text):
            raise SystemExit(f"Question {question.id}: a judged span lies outside its document.")
        return Span(spec.field, spec.key, start, end, spec.grade)

    return Judgments(
        tuple(resolve(s) for s in question.evidence),
        tuple(resolve(s) for s in question.traps),
        tuple(resolve(s) for s in question.qualifiers),
    )


def _occurrences(text: str, quote: str):
    pos = text.find(quote)
    while pos != -1:
        yield pos
        pos = text.find(quote, pos + 1)


def _spec_text(qid: str, spec, docs: dict[str, CorpusDoc] | None, *, required: bool) -> str | None:
    if docs is None:
        if required:
            raise SystemExit(f"Question {qid}: judged quotes need --corpus (a JSONL index export or JSON list).")
        return None
    if spec.field == "attachment_key":
        doc = docs.get(spec.key)
    else:
        matches = [d for d in docs.values() if d.citation_key == spec.key]
        if len(matches) > 1:
            raise SystemExit(f"Question {qid}: a judged citation_key matches several documents; use attachment_key.")
        doc = matches[0] if matches else None
    if doc is None:
        if required:
            raise SystemExit(f"Question {qid}: a judged document is not in the corpus.")
        return None
    return doc.text


# --- pure passage metrics -----------------------------------------------------------------------


@dataclass(frozen=True)
class PassageMetrics:
    ndcg: float
    span_recall: float
    char_recall: float
    precision: float
    dup_rate: float
    tokens: int
    n_hits: int
    locator_valid: int
    locator_checked: int
    cited_span_recall: float | None = None
    trap_rate: float | None = None
    qualifier_recall: float | None = None


def _union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(i for i in intervals if i[1] > i[0]):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _length(intervals: list[tuple[int, int]]) -> int:
    return sum(hi - lo for lo, hi in intervals)


def _intersect(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for lo1, hi1 in a:
        for lo2, hi2 in b:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if hi > lo:
                out.append((lo, hi))
    return out


def _covered(span: Span, hits: Sequence[PassageHit]) -> int:
    clipped = [
        (max(span.start, s.start), min(span.end, s.end))
        for h in hits if span.matches(h) for s in h.spans
    ]
    return _length(_union(clipped))


def _overlaps(span: Span, hit: PassageHit) -> bool:
    return span.matches(hit) and any(min(span.end, s.end) > max(span.start, s.start) for s in hit.spans)


def hit_locator_counts(hit: PassageHit, docs: dict[str, CorpusDoc] | None) -> tuple[int, int]:
    """``(valid, checked)`` locators of a hit against the original record text.

    A locator is valid when its range lies inside the record and the hash of that slice equals the
    hash the result carries. Without the record text a locator cannot be checked and is not counted.
    """
    doc = docs.get(hit.attachment_key) if docs else None
    if doc is None:
        return 0, 0
    valid = 0
    for s in hit.spans:
        in_range = 0 <= s.start < s.end <= len(doc.text)
        valid += in_range and (not s.sha256 or chunk_sha256(doc.text[s.start : s.end]) == s.sha256)
    return valid, len(hit.spans)


def _all_valid(hit: PassageHit, docs: dict[str, CorpusDoc]) -> bool:
    valid, checked = hit_locator_counts(hit, docs)
    return checked > 0 and valid == checked


def passage_metrics(
    hits: Sequence[PassageHit], judgments: Judgments, docs: dict[str, CorpusDoc] | None, ideal_n: int | None = None
) -> PassageMetrics:
    """Score a returned list (already cut at k or at a token budget) against the judgments.

    nDCG credits each evidence span once, to the first hit overlapping it, with gain ``2^grade - 1``
    and a ``1/log2(rank+1)`` discount, so repeated hits on the same evidence earn nothing. The ideal
    list has one hit per evidence span, cut at ``ideal_n`` (default: the number of hits returned).
    """
    evidence = judgments.evidence
    ideal_n = len(hits) if ideal_n is None else ideal_n
    credited: set[int] = set()
    dcg = 0.0
    for rank, hit in enumerate(hits, start=1):
        options = [(-s.grade, i) for i, s in enumerate(evidence) if i not in credited and _overlaps(s, hit)]
        if options:
            _, i = min(options)
            credited.add(i)
            dcg += (2 ** evidence[i].grade - 1) / math.log2(rank + 1)
    ideal = sorted((s.grade for s in evidence), reverse=True)[:ideal_n]
    idcg = sum((2**g - 1) / math.log2(r + 1) for r, g in enumerate(ideal, start=1))

    def recall(subset: Sequence[PassageHit], threshold: float) -> float:
        done = sum(1 for s in evidence if _covered(s, subset) >= threshold * s.length)
        return done / len(evidence) if evidence else 0.0

    total_evidence = sum(s.length for s in evidence)
    char_recall = sum(_covered(s, hits) for s in evidence) / total_evidence if total_evidence else 0.0

    returned = sum(sp.end - sp.start for h in hits for sp in h.spans)
    union_len = relevant = 0
    by_doc: dict[str, list[PassageHit]] = defaultdict(list)
    for h in hits:
        by_doc[h.attachment_key].append(h)
    for group in by_doc.values():
        returned_union = _union([(sp.start, sp.end) for h in group for sp in h.spans])
        evidence_union = _union([(s.start, s.end) for s in evidence if any(s.matches(h) for h in group)])
        union_len += _length(returned_union)
        relevant += _length(_intersect(returned_union, evidence_union))

    valid = checked = 0
    for h in hits:
        v, c = hit_locator_counts(h, docs)
        valid, checked = valid + v, checked + c
    cited = None
    if docs is not None:
        verified = [h for h in hits if _all_valid(h, docs)]
        cited = recall(verified, COVERAGE_THRESHOLD)
    traps = judgments.traps
    trap_rate = None
    if traps:
        trap_rate = sum(1 for h in hits if any(_overlaps(t, h) for t in traps)) / len(hits) if hits else 0.0
    quals = judgments.qualifiers
    qualifier_recall = (
        sum(1 for q in quals if _covered(q, hits) >= q.length) / len(quals) if quals else None
    )
    return PassageMetrics(
        ndcg=dcg / idcg if idcg > 0 else 0.0,
        span_recall=recall(hits, COVERAGE_THRESHOLD),
        char_recall=char_recall,
        precision=relevant / returned if returned else 0.0,
        dup_rate=1 - union_len / returned if returned else 0.0,
        tokens=sum(h.cost for h in hits),
        n_hits=len(hits),
        locator_valid=valid,
        locator_checked=checked,
        cited_span_recall=cited,
        trap_rate=trap_rate,
        qualifier_recall=qualifier_recall,
    )


def budget_prefix(hits: Sequence[PassageHit], budget: int) -> list[PassageHit]:
    """Leading hits whose total cost (returned plus ancestor/expansion tokens) fits ``budget``.

    Stops at the first hit that does not fit: a result is never cut to size, so evidence is not
    silently truncated, and a budget too small for the top hit returns nothing.
    """
    out: list[PassageHit] = []
    spent = 0
    for hit in hits:
        if spent + hit.cost > budget:
            break
        spent += hit.cost
        out.append(hit)
    return out


# --- per-configuration run ----------------------------------------------------------------------


@dataclass
class QuestionResult:
    topk: dict[int, PassageMetrics] = field(default_factory=dict)
    budget: dict[int, PassageMetrics] = field(default_factory=dict)
    paper: object = None  # retrieval.QuestionScore


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.fmean(present), 3) if present else None


def aggregate_metrics(items: list[PassageMetrics]) -> dict[str, object]:
    """Mean of each metric over questions; locator validity is pooled over every checked locator."""
    out: dict[str, object] = {"n": len(items)}
    for name in METRIC_NAMES:
        out[name] = _mean([getattr(m, name) for m in items])
    checked = sum(m.locator_checked for m in items)
    out["locator_validity"] = round(sum(m.locator_valid for m in items) / checked, 3) if checked else None
    out["locators_checked"] = checked
    return out


def _series_aggregates(results: dict[str, QuestionResult]) -> dict[str, dict[str, dict[str, object]]]:
    ks = sorted({k for r in results.values() for k in r.topk})
    budgets = sorted({b for r in results.values() for b in r.budget})
    return {
        "topk": {str(k): aggregate_metrics([r.topk[k] for r in results.values()]) for k in ks},
        "budget": {str(b): aggregate_metrics([r.budget[b] for r in results.values()]) for b in budgets},
    }


def run_passage_configuration(
    retriever: Retriever,
    questions: list,
    mode: str,
    judgments: dict[str, Judgments],
    docs: dict[str, CorpusDoc] | None,
    ks: list[int],
    budgets: list[int],
    score_paper: Callable[[list[tuple[str, str]], object], object],
) -> dict[str, object]:
    """Run every question once, timing retrieval, and score paper and passage metrics."""
    results: dict[str, QuestionResult] = {}
    latencies: list[float] = []
    invalid: list[str] = []
    for q in questions:
        search_mode = (q.search_mode or "all_terms") if mode == "per-question" else mode
        started = time.perf_counter()
        try:
            hits = retriever.retrieve(q.query, search_mode, PASSAGE_CANDIDATES)
        except ValueError:
            hits = []
            invalid.append(q.id)
        latencies.append((time.perf_counter() - started) * 1000)
        result = QuestionResult()
        papers: list[tuple[str, str]] = []
        for h in hits:
            if (h.attachment_key, h.citation_key) not in papers:
                papers.append((h.attachment_key, h.citation_key))
        result.paper = score_paper(papers, q)
        judged = judgments.get(q.id)
        if judged and judged.evidence:
            for k in ks:
                result.topk[k] = passage_metrics(hits[:k], judged, docs, ideal_n=k)
            for b in budgets:
                result.budget[b] = passage_metrics(budget_prefix(hits, b), judged, docs)
        results[q.id] = result
    judged_results = {qid: r for qid, r in results.items() if r.topk}
    by_type: dict[str, dict[str, QuestionResult]] = defaultdict(dict)
    by_split: dict[str, dict[str, QuestionResult]] = defaultdict(dict)
    for q in questions:
        if q.id in judged_results:
            by_type[q.type][q.id] = results[q.id]
            by_split[q.split][q.id] = results[q.id]
    ordered = sorted(latencies)
    return {
        "results": results,
        "judged": len(judged_results),
        "passage": {
            **_series_aggregates(judged_results),
            "by_type": {t: _series_aggregates(r) for t, r in sorted(by_type.items())},
            "by_split": {s: _series_aggregates(r) for s, r in sorted(by_split.items())},
        },
        "latency_ms": {"p50": round(percentile(ordered, 50), 3), "p95": round(percentile(ordered, 95), 3)},
        "invalid_queries": invalid,
    }


# --- paired uncertainty -------------------------------------------------------------------------


def paired_bootstrap(
    diffs: Sequence[float], resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED
) -> dict[str, object]:
    """Mean paired difference with a 95% percentile-bootstrap interval over questions.

    Deterministic for a given input and seed. With fewer than two pairs the interval is None.
    """
    n = len(diffs)
    mean = statistics.fmean(diffs) if n else 0.0
    out: dict[str, object] = {"n": n, "mean_diff": round(mean, 4), "ci95": None}
    if n >= 2:
        rng = random.Random(seed)
        means = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(resamples))
        out["ci95"] = [round(means[int(0.025 * resamples)], 4), round(means[int(0.975 * resamples) - 1], 4)]
    return out


def compare_passage(
    base: dict[str, object], other: dict[str, object], ks: list[int], budgets: list[int]
) -> list[dict[str, object]]:
    """Paired comparison on the questions judged in both runs, per series and primary metric."""
    rows = []
    base_results: dict[str, QuestionResult] = base["results"]  # type: ignore[assignment]
    other_results: dict[str, QuestionResult] = other["results"]  # type: ignore[assignment]
    shared = [qid for qid, r in base_results.items() if r.topk and other_results.get(qid) and other_results[qid].topk]
    series = [(f"topk:{k}", lambda r, k=k: r.topk[k]) for k in ks]
    series += [(f"budget:{b}", lambda r, b=b: r.budget[b]) for b in budgets]
    for name, pick in series:
        for metric in PRIMARY_METRICS:
            diffs = {
                qid: getattr(pick(other_results[qid]), metric) - getattr(pick(base_results[qid]), metric)
                for qid in shared
            }
            row = paired_bootstrap(list(diffs.values()))
            row.update(
                series=name,
                metric=metric,
                wins=sum(d > REGRESSION_EPSILON for d in diffs.values()),
                losses=sum(d < -REGRESSION_EPSILON for d in diffs.values()),
                regressed=[qid for qid, d in diffs.items() if d < -REGRESSION_EPSILON],
            )
            rows.append(row)
    return rows


# --- output -------------------------------------------------------------------------------------


def _cell(value: object, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def _passage_table(rows: list[tuple[str, str, dict[str, object]]]) -> list[str]:
    head = ["label", "channel", "n", "nDCG", "spanR", "charR", "prec", "dup", "cited", "locOK", "trap", "qual", "tokens", "hits"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
    for label, channel, agg in rows:
        cells = [
            label, channel, str(agg["n"]), _cell(agg["ndcg"]), _cell(agg["span_recall"]),
            _cell(agg["char_recall"]), _cell(agg["precision"]), _cell(agg["dup_rate"]),
            _cell(agg["cited_span_recall"]), _cell(agg["locator_validity"]), _cell(agg["trap_rate"]),
            _cell(agg["qualifier_recall"]), _cell(agg["tokens"], 0 if agg["tokens"] is None else 1), _cell(agg["n_hits"], 1),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_passage_markdown(report: dict) -> str:
    configs = report["configurations"]
    live = [c for c in configs if c["status"] == "ok"]
    out = [
        "## Setup",
        "",
        f"tokenizer: {report['tokenizer']['name']} {report['tokenizer']['version']}; "
        f"splits: {', '.join(report['splits'])}; judged questions: {report['judged_questions']}",
        "",
        "| label | channel | mode | status | chunks | tok p50 | tok p95 | tok max | over target | build s | index bytes | lat p50 ms | lat p95 ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in configs:
        b = c.get("build") or {}
        sizes = b.get("size_tokens", {})
        lat = c.get("latency_ms", {})
        out.append(
            "| " + " | ".join(
                [c["label"], c["channel"], c["mode"], c["status"] if c["status"] == "ok" else f"unavailable: {c['reason']}",
                 _cell(b.get("chunks")), _cell(sizes.get("p50")), _cell(sizes.get("p95")), _cell(sizes.get("max")),
                 _cell(b.get("chunks_over_target")), _cell(b.get("build_seconds")), _cell(b.get("index_bytes")),
                 _cell(lat.get("p50")), _cell(lat.get("p95"))]
            ) + " |"
        )
    out += ["", "## Paper level", "", "| label | channel | n | " + " | ".join(f"R@{k}" for k in report["k"]) + " | MRR |"]
    out.append("|" + "|".join(["---"] * (4 + len(report["k"]))) + "|")
    for c in live:
        p = c["paper"]
        out.append(
            f"| {c['label']} | {c['channel']} | {p['n']} | "
            + " | ".join(f"{p['recall'][str(k)]:.3f}" for k in report["k"]) + f" | {p['mrr']:.3f} |"
        )
    for series_kind, values in (("topk", report["k"]), ("budget", report["budgets"])):
        for v in values:
            title = f"top-{v} passages" if series_kind == "topk" else f"returned-token budget {v}"
            out += ["", f"## Passage metrics: {title}", ""]
            out += _passage_table([(c["label"], c["channel"], c["passage"][series_kind][str(v)]) for c in live])
    for c in live:
        splits = c["passage"]["by_split"]
        if len(splits) > 1:
            for s, series in splits.items():
                top = str(report["k"][-1])
                out += ["", f"split {s} ({c['label']}, {c['channel']}): nDCG@{top} {_cell(series['topk'][top]['ndcg'])}, "
                        f"spanR@{top} {_cell(series['topk'][top]['span_recall'])} (n={series['topk'][top]['n']})"]
    for c in live:
        if c["invalid_queries"]:
            out += ["", f"invalid_queries ({c['label']}, {c['channel']}): " + ", ".join(c["invalid_queries"])]
    for ch in report["changes"]:
        out += ["", f"## Paired change vs baseline: {ch['baseline']} -> {ch['against']}", "",
                "| series | metric | n | mean diff | 95% CI | wins | losses | regressed |", "|---|---|---|---|---|---|---|---|"]
        for r in ch["rows"]:
            ci = "-" if r["ci95"] is None else f"[{r['ci95'][0]:+.3f}, {r['ci95'][1]:+.3f}]"
            out.append(
                f"| {r['series']} | {r['metric']} | {r['n']} | {r['mean_diff']:+.3f} | {ci} | {r['wins']} | "
                f"{r['losses']} | {', '.join(r['regressed']) or '-'} |"
            )
    return "\n".join(out) + "\n"
