"""Retrieval quality (recall@k, MRR) on a private question set (roadmap step S1).

Runs each question of a local, never-committed ``*.questions.json`` file through ``search_fts`` and
reports mean recall@k and MRR overall and per question type, plus the questions whose outcome
changed between configurations. A configuration is one index (``--db``) crossed with one search
mode (``--mode``); the first configuration is the baseline. Read-only: indexes are opened through
the same read-only connection the server uses, and nothing is written anywhere.

    python benchmarks/retrieval.py --questions my.questions.json \\
        --db <output_root>/index/generations/<id>/index.sqlite \\
        [--db <other>/index.sqlite] [--label A --label B] \\
        [--mode per-question|all_terms|any_terms|phrase ...] [--k 5,10,20] [--json]

Question file (see docs/search-quality.md)::

    {"format": "zotero-fulltext-retrieval-questions", "version": 1, "synthetic": false,
     "questions": [{"id": "q01", "query": "...", "type": "conceptual", "search_mode": "phrase",
                    "expected": [{"citation_key": "..."}, {"attachment_key": "..."}]}]}

Passage mode (roadmap step S1a, issue #107) adds ``--chunking``/``--sweep``/``--passages``: judged
evidence spans, held-out splits, nDCG and evidence-span metrics at equal top-k and equal returned-token
budgets, and chunk-size/boundary/overlap sweeps over separate experimental indexes built from
``--corpus``. See docs/search-quality.md.

    python benchmarks/retrieval.py --questions my.questions.json --corpus <index.jsonl> \
        --chunking chars:6000:500 --chunking sentence:384:48 --sweep structural \
        --budget 500,1000,2000 --split heldout

Output contains question ids, ranks and aggregates only: never query text, keys, titles or paths.
"""

from __future__ import annotations

import argparse
import itertools
import json
import string
import tempfile
from dataclasses import dataclass
from pathlib import Path

from zotero_pdf_text.fts import (
    MAX_SEARCH_RESULTS,
    SEARCH_MODES,
    _validate_search_request,
    connect_readonly,
    lookup_citation_key,
    search_fts,
)

FORMAT = "zotero-fulltext-retrieval-questions"
VERSION = 1
PER_QUESTION = "per-question"
DEFAULT_MODE = "all_terms"
DEFAULT_KS = (5, 10, 20)
UNTYPED = "untyped"
ENTRY_KEYS = ("attachment_key", "citation_key")
SPLITS = ("dev", "heldout")
DEFAULT_SPLIT = "dev"
GRADES = (1, 2, 3)
DEFAULT_GRADE = 3


@dataclass(frozen=True)
class SpanSpec:
    """A judged span as written in the question file, before quotes are resolved to offsets."""

    field: str
    key: str
    start: int | None = None
    end: int | None = None
    quote: str | None = None
    occurrence: int | None = None
    grade: int = DEFAULT_GRADE


@dataclass(frozen=True)
class Question:
    id: str
    query: str
    type: str
    search_mode: str | None
    expected: tuple[dict[str, str], ...]
    split: str = DEFAULT_SPLIT
    evidence: tuple[SpanSpec, ...] = ()
    traps: tuple[SpanSpec, ...] = ()
    qualifiers: tuple[SpanSpec, ...] = ()


@dataclass(frozen=True)
class QuestionScore:
    recall: dict[int, float]
    rr: float
    first_rank: int | None
    found: int


# --- pure metrics -------------------------------------------------------------------------------


def entry_rank(ranked_keys: list[tuple[str, str]], entry: dict[str, str]) -> int | None:
    """1-based rank of the first hit satisfying ``entry``, or None."""
    field = 0 if "attachment_key" in entry else 1
    wanted = entry["attachment_key"] if field == 0 else entry["citation_key"]
    for rank, keys in enumerate(ranked_keys, start=1):
        if wanted and keys[field] == wanted:
            return rank
    return None


def score_question(
    ranked_keys: list[tuple[str, str]], expected: list[dict[str, str]], ks: tuple[int, ...] | list[int]
) -> QuestionScore:
    """Score one ranked list of (attachment_key, citation_key) against the expected entries.

    A citation_key entry is satisfied by any attachment of that paper and counts once.
    """
    ranks = [entry_rank(ranked_keys, entry) for entry in expected]
    hits = [r for r in ranks if r is not None]
    first = min(hits) if hits else None
    total = len(expected)
    return QuestionScore(
        recall={k: (sum(1 for r in hits if r <= k) / total if total else 0.0) for k in ks},
        rr=1 / first if first else 0.0,
        first_rank=first,
        found=len(hits),
    )


def aggregate(scores: list[QuestionScore]) -> dict[str, object]:
    """Mean recall@k and MRR over ``scores``, rounded to 3 decimals."""
    n = len(scores)
    if n == 0:
        return {"n": 0, "recall": {}, "mrr": 0.0}
    ks = sorted(scores[0].recall)
    return {
        "n": n,
        "recall": {str(k): round(sum(s.recall[k] for s in scores) / n, 3) for k in ks},
        "mrr": round(sum(s.rr for s in scores) / n, 3),
    }


def _outcome_key(score: QuestionScore) -> tuple[float, int]:
    """Sortable outcome: lower is better; a missing first hit is worst."""
    return (float("inf") if score.first_rank is None else score.first_rank, -score.found)


# --- question file ------------------------------------------------------------------------------


def load_questions(path: Path) -> tuple[list[Question], bool]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read the question file: {type(exc).__name__}.") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SystemExit(f'Question file must have "format": "{FORMAT}".')
    if data.get("version") != VERSION:
        raise SystemExit(f"Unsupported question file version (expected {VERSION}).")
    synthetic = data.get("synthetic", False)
    if not isinstance(synthetic, bool):
        raise SystemExit('"synthetic" must be true or false.')
    raw = data.get("questions")
    if not isinstance(raw, list) or not raw:
        raise SystemExit('"questions" must be a non-empty list.')
    questions: list[Question] = []
    seen: set[str] = set()
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"Question #{position} must be an object.")
        qid = item.get("id")
        if not isinstance(qid, str) or not qid.strip():
            raise SystemExit(f'Question #{position} needs a non-empty string "id".')
        if qid in seen:
            raise SystemExit(f"Duplicate question id: {qid}")
        seen.add(qid)
        query = item.get("query")
        if not isinstance(query, str):
            raise SystemExit(f'Question {qid}: "query" must be a string.')
        try:
            _validate_search_request(query, 1, DEFAULT_MODE)
        except ValueError as exc:
            raise SystemExit(f"Question {qid}: query rejected by search validation ({exc}).") from None
        qtype = item.get("type", UNTYPED)
        if not isinstance(qtype, str) or not qtype.strip():
            raise SystemExit(f'Question {qid}: "type" must be a non-empty string when present.')
        mode = item.get("search_mode")
        if mode is not None and mode not in SEARCH_MODES:
            raise SystemExit(f"Question {qid}: search_mode must be one of {sorted(SEARCH_MODES)}.")
        split = item.get("split", DEFAULT_SPLIT)
        if split not in SPLITS:
            raise SystemExit(f"Question {qid}: split must be one of {list(SPLITS)}.")
        evidence = _parse_spans(qid, item, "evidence", graded=True)
        traps = _parse_spans(qid, item, "traps", graded=False)
        qualifiers = _parse_spans(qid, item, "qualifiers", graded=False)
        expected = item.get("expected")
        if expected is None and evidence:
            # Papers with judged evidence are the expected papers, in first-seen order.
            expected = [{field: key} for field, key in dict.fromkeys((s.field, s.key) for s in evidence)]
        if not isinstance(expected, list) or not expected:
            raise SystemExit(f'Question {qid}: "expected" must be a non-empty list.')
        entries: list[dict[str, str]] = []
        for entry in expected:
            present = [k for k in ENTRY_KEYS if isinstance(entry, dict) and k in entry]
            if not isinstance(entry, dict) or len(present) != 1 or set(entry) != set(present):
                raise SystemExit(
                    f"Question {qid}: each expected entry needs exactly one of "
                    f"{' or '.join(ENTRY_KEYS)} and nothing else."
                )
            value = entry[present[0]]
            if not isinstance(value, str) or not value.strip():
                raise SystemExit(f"Question {qid}: expected keys must be non-empty strings.")
            entries.append({present[0]: value})
        questions.append(Question(qid, query, qtype, mode, tuple(entries), split, evidence, traps, qualifiers))
    return questions, synthetic


def _parse_spans(qid: str, item: dict, name: str, *, graded: bool) -> tuple[SpanSpec, ...]:
    raw = item.get(name, [])
    if not isinstance(raw, list):
        raise SystemExit(f"Question {qid}: {name!r} must be a list.")
    allowed = {*ENTRY_KEYS, "quote", "start_char", "end_char", "occurrence"} | ({"grade"} if graded else set())
    specs: list[SpanSpec] = []
    for entry in raw:
        if not isinstance(entry, dict) or not set(entry) <= allowed:
            raise SystemExit(f"Question {qid}: each {name} entry may only use {sorted(allowed)}.")
        present = [k for k in ENTRY_KEYS if k in entry]
        if len(present) != 1 or not isinstance(entry[present[0]], str) or not entry[present[0]].strip():
            raise SystemExit(f"Question {qid}: each {name} entry needs exactly one non-empty key.")
        offsets = "start_char" in entry or "end_char" in entry
        quote = entry.get("quote")
        if offsets == (quote is not None):
            raise SystemExit(f"Question {qid}: each {name} entry needs a quote or start_char and end_char.")
        start, end = entry.get("start_char"), entry.get("end_char")
        if offsets and not (
            all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end)) and 0 <= start < end
        ):
            raise SystemExit(f"Question {qid}: {name} offsets must be integers with 0 <= start_char < end_char.")
        if quote is not None and (not isinstance(quote, str) or not quote.strip()):
            raise SystemExit(f"Question {qid}: {name} quotes must be non-empty strings.")
        occurrence = entry.get("occurrence")
        if occurrence is not None and (not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1):
            raise SystemExit(f"Question {qid}: occurrence must be a positive integer.")
        grade = entry.get("grade", DEFAULT_GRADE)
        if grade not in GRADES or isinstance(grade, bool):
            raise SystemExit(f"Question {qid}: grade must be one of {list(GRADES)}.")
        specs.append(SpanSpec(present[0], entry[present[0]], start, end, quote, occurrence, grade))
    return tuple(specs)


# --- running ------------------------------------------------------------------------------------


def _absent_entries(db: Path, questions: list[Question]) -> set[str]:
    """Ids of questions with at least one expected entry missing from the index (read-only)."""
    absent: set[str] = set()
    con = connect_readonly(db)
    try:
        for q in questions:
            for entry in q.expected:
                if "attachment_key" in entry:
                    row = con.execute(
                        "SELECT 1 FROM metadata WHERE zotero_attachment_key = ?", (entry["attachment_key"],)
                    ).fetchone()
                    missing = row is None
                else:
                    missing = not lookup_citation_key(db, entry["citation_key"], limit=1)["records"]
                if missing:
                    absent.add(q.id)
    finally:
        con.close()
    return absent


def run_configuration(
    db: Path, questions: list[Question], mode: str, ks: list[int], absent: set[str]
) -> dict[str, object]:
    limit = min(max(ks), MAX_SEARCH_RESULTS)
    scores: dict[str, QuestionScore] = {}
    invalid: list[str] = []
    for q in questions:
        search_mode = (q.search_mode or DEFAULT_MODE) if mode == PER_QUESTION else mode
        try:
            hits = search_fts(db, q.query, limit=limit, search_mode=search_mode)  # type: ignore[arg-type]
        except ValueError:
            hits = []
            invalid.append(q.id)
        ranked = [(h.zotero_attachment_key, h.citation_key) for h in hits]
        scores[q.id] = score_question(ranked, list(q.expected), ks)
    by_type: dict[str, list[QuestionScore]] = {}
    for q in questions:
        by_type.setdefault(q.type, []).append(scores[q.id])
    return {
        "scores": scores,
        "overall": aggregate(list(scores.values())),
        "by_type": {t: aggregate(s) for t, s in sorted(by_type.items())},
        "not_in_index": [q.id for q in questions if q.id in absent],
        "invalid_queries": invalid,
    }


def _direction(delta: float) -> int:
    """-1 when the candidate is better, 1 when worse, 0 when equal (recall is higher-is-better)."""
    return -1 if delta > 1e-9 else 1 if delta < -1e-9 else 0


def compare(base: dict[str, object], other: dict[str, object], questions: list[Question]) -> dict[str, list]:
    """Per-question changes against the baseline: first-hit rank, entries found, and recall@k.

    A question is *improved* when something got better and nothing got worse, *regressed* in the
    opposite case, and *mixed* when the signals disagree, so an aggregate change is never hidden.
    """
    improved: list[dict[str, object]] = []
    regressed: list[dict[str, object]] = []
    mixed: list[dict[str, object]] = []
    for q in questions:
        a: QuestionScore = base["scores"][q.id]  # type: ignore[index]
        b: QuestionScore = other["scores"][q.id]  # type: ignore[index]
        ka, kb = _outcome_key(a), _outcome_key(b)
        signals = [-1 if kb < ka else 1 if kb > ka else 0]
        recall_changes = {}
        for k in sorted(a.recall):
            signals.append(_direction(b.recall[k] - a.recall[k]))
            if signals[-1]:
                recall_changes[str(k)] = [round(a.recall[k], 3), round(b.recall[k], 3)]
        better, worse = min(signals) < 0, max(signals) > 0
        if not (better or worse):
            continue
        row = {
            "id": q.id,
            "type": q.type,
            "baseline_rank": a.first_rank,
            "rank": b.first_rank,
            "found_baseline": a.found,
            "found": b.found,
            "recall_changes": recall_changes,
        }
        (mixed if better and worse else improved if better else regressed).append(row)
    return {"improved": improved, "regressed": regressed, "mixed": mixed}


# --- output -------------------------------------------------------------------------------------


def _labels(labels: list[str] | None, count: int) -> list[str]:
    if labels is None:
        if count > len(string.ascii_uppercase):
            raise SystemExit("Too many --db values for default labels; pass --label for each.")
        return list(string.ascii_uppercase[:count])
    if len(labels) != count:
        raise SystemExit("Pass one --label per --db, or none.")
    if len(set(labels)) != len(labels):
        raise SystemExit("--label values must be unique.")
    return labels


def _table(rows: list, ks: list[int]) -> list[str]:
    head = ["label", "mode", "n", *[f"R@{k}" for k in ks], "MRR", "not_in_index"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
    for label, mode, agg, nii in rows:
        recall = agg["recall"]
        cells = [label, mode, str(agg["n"]), *[f"{recall[str(k)]:.3f}" for k in ks], f"{agg['mrr']:.3f}"]
        cells.append("" if nii is None else str(nii))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _rank(value: object) -> str:
    return "-" if value is None else str(value)


def render_markdown(report: dict, ks: list[int]) -> str:
    configs = report["configurations"]
    out = ["## Overall", ""]
    out += _table([(c["label"], c["mode"], c["overall"], len(c["not_in_index"])) for c in configs], ks)
    for t in sorted({t for c in configs for t in c["by_type"]}):
        out += ["", f"## Type: {t}", ""]
        out += _table([(c["label"], c["mode"], c["by_type"][t], None) for c in configs], ks)
    for c in configs:
        if c["not_in_index"]:
            out += ["", f"not_in_index ({c['label']}, {c['mode']}): " + ", ".join(c["not_in_index"])]
        if c["invalid_queries"]:
            out += ["", f"invalid_queries ({c['label']}, {c['mode']}): " + ", ".join(c["invalid_queries"])]
    for ch in report["changes"]:
        out += ["", f"## Changed vs baseline: {ch['baseline']} -> {ch['against']}"]
        for name in ("improved", "regressed", "mixed"):
            out += ["", f"{name} ({len(ch[name])})"]
            for r in ch[name]:
                cutoffs = "".join(
                    f", R@{k} {before:.3f} -> {after:.3f}" for k, (before, after) in r["recall_changes"].items()
                )
                out.append(
                    f"- {r['id']} ({r['type']}): rank {_rank(r['baseline_rank'])} -> {_rank(r['rank'])}, "
                    f"found {r['found_baseline']} -> {r['found']}{cutoffs}"
                )
    return "\n".join(out) + "\n"


def build_report(
    questions: list[Question], dbs: list[Path], labels: list[str], modes: list[str], ks: list[int]
) -> dict:
    absent_by_db: dict[int, set[str]] = {}
    for i, db in enumerate(dbs):
        try:
            absent_by_db[i] = _absent_entries(db, questions)
        except Exception as exc:  # never echo the path
            raise SystemExit(f"Cannot open index {labels[i]!r}: {type(exc).__name__}.") from None
    runs = []
    for (i, db), mode in itertools.product(enumerate(dbs), modes):
        runs.append((labels[i], mode, run_configuration(db, questions, mode, ks, absent_by_db[i])))
    changes = []
    for label, mode, run in runs[1:]:
        result = compare(runs[0][2], run, questions)
        changes.append({"baseline": f"{runs[0][0]}/{runs[0][1]}", "against": f"{label}/{mode}", **result})
    return {
        "k": ks,
        "configurations": [
            {
                "label": label,
                "mode": mode,
                "overall": run["overall"],
                "by_type": run["by_type"],
                "not_in_index": run["not_in_index"],
                "invalid_queries": run["invalid_queries"],
            }
            for label, mode, run in runs
        ],
        "changes": changes,
    }


def _parse_ks(text: str) -> list[int]:
    try:
        ks = sorted({int(part) for part in text.split(",")})
    except ValueError:
        raise SystemExit("--k must be comma-separated integers, e.g. 5,10,20.") from None
    if not ks or ks[0] < 1 or ks[-1] > MAX_SEARCH_RESULTS:
        raise SystemExit(f"--k values must be between 1 and {MAX_SEARCH_RESULTS}.")
    return ks


# --- passage mode (S1a) -------------------------------------------------------------------------


def _parse_budgets(text: str) -> list[int]:
    try:
        budgets = sorted({int(part) for part in text.split(",")})
    except ValueError:
        raise SystemExit("--budget must be comma-separated integers, e.g. 500,1000,2000.") from None
    if not budgets or budgets[0] < 1:
        raise SystemExit("--budget values must be positive.")
    return budgets


def build_passage_report(
    questions: list[Question],
    *,
    retrievers: list,
    builds: dict[str, dict],
    channels: list[str],
    modes: list[str],
    ks: list[int],
    budgets: list[int],
    docs,
    tokenizer,
) -> dict:
    """Run every (retriever, channel, mode) configuration and compare each with the first live one."""
    import passages

    judgments = {q.id: passages.resolve_judgments(q, docs) for q in questions if q.evidence}

    def score_paper(ranked, q):
        return score_question(ranked, list(q.expected), ks)

    configs: list[dict] = []
    runs: list[tuple[dict, dict]] = []
    for lexical in retrievers:
        for channel in channels:
            channel_retriever = {
                passages.LEXICAL: lexical,
                passages.SEMANTIC: passages.SemanticRetriever(),
                passages.HYBRID: passages.HybridRetriever(lexical, passages.SemanticRetriever()),
            }[channel]
            for mode in modes:
                label = lexical.name if channel == passages.LEXICAL else channel_retriever.name
                config = {"label": label, "channel": channel, "mode": mode, "build": builds.get(lexical.name)}
                reason = channel_retriever.unavailable_reason()
                if reason:
                    configs.append({**config, "status": "unavailable", "reason": reason})
                    continue
                run = passages.run_passage_configuration(
                    channel_retriever, questions, mode, judgments, docs, ks, budgets, score_paper
                )
                entry = {
                    **config,
                    "status": "ok",
                    "paper": aggregate([r.paper for r in run["results"].values()]),
                    "paper_by_type": {
                        t: aggregate([run["results"][q.id].paper for q in questions if q.type == t])
                        for t in sorted({q.type for q in questions})
                    },
                    "passage": run["passage"],
                    "judged_questions": run["judged"],
                    "latency_ms": run["latency_ms"],
                    "invalid_queries": run["invalid_queries"],
                }
                configs.append(entry)
                runs.append((entry, run))
    changes = []
    if runs:
        base_entry, base_run = runs[0]
        for entry, run in runs[1:]:
            changes.append(
                {
                    "baseline": f"{base_entry['label']}/{base_entry['channel']}/{base_entry['mode']}",
                    "against": f"{entry['label']}/{entry['channel']}/{entry['mode']}",
                    "rows": passages.compare_passage(base_run, run, ks, budgets),
                }
            )
    return {
        "k": ks,
        "budgets": budgets,
        "tokenizer": {"name": tokenizer.name, "version": tokenizer.version},
        "splits": sorted({q.split for q in questions}),
        "judged_questions": len(judgments),
        "configurations": configs,
        "changes": changes,
    }


def run_passage_mode(args, questions: list[Question], ks: list[int], modes: list[str]) -> dict:
    import chunking
    import passages

    tokenizer = chunking.get_tokenizer(args.tokenizer)
    budgets = _parse_budgets(args.budget)
    channels = list(dict.fromkeys(args.channel or [passages.LEXICAL]))
    specs = [chunking.parse_spec(text) for text in args.chunking or []]
    for strategy in args.sweep or []:
        specs.extend(chunking.sweep_specs(strategy))
    specs = list(dict.fromkeys(specs))
    docs = chunking.load_corpus(args.corpus) if args.corpus else None
    if specs and docs is None:
        raise SystemExit("--chunking and --sweep build experimental indexes and need --corpus.")
    try:
        expansions = sorted({int(v) for v in args.expand.split(",")})
    except ValueError:
        raise SystemExit("--expand must be comma-separated integers.") from None
    if expansions[0] < 0:
        raise SystemExit("--expand values must be non-negative.")
    labels = _labels(args.label, len(args.db or []))
    retrievers: list = []
    builds: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="s1a-experiments-") as scratch:
        root = args.experiment_dir or Path(scratch)
        try:
            for db, label in zip(args.db or [], labels, strict=True):
                retrievers.append(passages.GenerationRetriever(db, tokenizer, label))
            for spec in specs:
                name = f"{spec.strategy}-{spec.target}-{spec.overlap}{chunking.EXPERIMENT_SUFFIX}"
                summary = chunking.build_experiment_index(docs, spec, tokenizer, root / name)
                for expand in expansions:
                    retriever = passages.ExperimentRetriever(summary, expand)
                    builds[retriever.name] = summary.to_dict()
                    retrievers.append(retriever)
            return build_passage_report(
                questions, retrievers=retrievers, builds=builds, channels=channels, modes=modes,
                ks=ks, budgets=budgets, docs=docs, tokenizer=tokenizer,
            )
        finally:
            for retriever in retrievers:
                getattr(retriever, "close", lambda: None)()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=Path, required=True, help="Local *.questions.json file.")
    parser.add_argument("--db", type=Path, action="append", help="Generation index.sqlite; repeatable.")
    parser.add_argument("--label", action="append", help="Name for each --db (default A, B, ...).")
    parser.add_argument(
        "--mode", action="append", choices=[PER_QUESTION, *sorted(SEARCH_MODES)],
        help="Search mode; repeatable (default per-question).",
    )
    parser.add_argument("--k", default=",".join(map(str, DEFAULT_KS)), help="Comma-separated cutoffs.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    parser.add_argument("--split", choices=["all", *SPLITS], default="all", help="Score only this split.")
    passage = parser.add_argument_group("passage mode (S1a)")
    passage.add_argument("--passages", action="store_true", help="Score passages for the --db generations.")
    passage.add_argument(
        "--corpus", type=Path,
        help="Index JSONL export or JSON list: record texts for quotes, locators and experiments.",
    )
    passage.add_argument(
        "--chunking", action="append", help="Experimental index spec strategy:target[:overlap]; repeatable."
    )
    passage.add_argument(
        "--sweep", action="append", choices=["sentence", "structural"],
        help="128/256/384/512/768 tokens, each with zero and bounded overlap.",
    )
    passage.add_argument("--expand", default="0", help="Neighbour-chunk expansion widths to evaluate (default 0).")
    passage.add_argument(
        "--channel", action="append", choices=["lexical", "semantic", "hybrid"],
        help="Retrieval channel; repeatable (default lexical). Semantic stays unavailable.",
    )
    passage.add_argument("--budget", default="500,1000,2000", help="Returned-token budgets.")
    passage.add_argument("--tokenizer", default="regex", help="regex (default) or tiktoken:<encoding>.")
    passage.add_argument(
        "--experiment-dir", type=Path,
        help="Where to write experimental indexes (default: a scratch folder removed afterwards).",
    )
    args = parser.parse_args(argv)

    passage_mode = bool(args.passages or args.chunking or args.sweep)
    if not args.db and not (args.chunking or args.sweep):
        parser.error("pass --db, --chunking or --sweep.")
    ks = _parse_ks(args.k)
    questions, _ = load_questions(args.questions)
    if args.split != "all":
        questions = [q for q in questions if q.split == args.split]
        if not questions:
            raise SystemExit(f"No questions in split {args.split!r}.")
    modes = list(dict.fromkeys(args.mode or [PER_QUESTION]))
    if passage_mode:
        from passages import render_passage_markdown

        report = run_passage_mode(args, questions, ks, modes)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(render_passage_markdown(report), end="")
        return
    labels = _labels(args.label, len(args.db))
    report = build_report(questions, args.db, labels, modes, ks)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report, ks), end="")


if __name__ == "__main__":
    main()
