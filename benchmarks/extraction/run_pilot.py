"""Run harness (roadmap E1/E2): run one extractor over the reviewed pilot pages and score it.

Host-only and benchmark-side: no network, no Zotero access, nothing shipped. For a directory of
*reviewed* pilot expectations it

1. refuses drafts and any record the pilot loader rejects (``load_pilot_expectation``);
2. checks each PDF in the cache against the SHA-256 pinned in the manifest before extracting;
3. extracts every PDF once, for the union of the pages its expectations need;
4. scores each expectation against the observation (``score_records``, with the extractor's Markdown
   so ``output_span`` hashes round-trip);
5. writes a ``run`` record (validated against the run schema before it is written), the
   observation and Markdown files its hashes point at, a ``details.json`` with the hashes the run
   schema has no field for, and a human-readable ``summary.md``.

A failure, timeout or invalid observation becomes a ``failure_result`` for every expectation of that
PDF: it carries an error and no metrics, so it can neither pass nor read as a zero score.

The extractor is an injectable callable with the signature of
``extraction.observe.observe_pdf(pdf_path, *, pages, options) -> ObservationResult`` (attributes
``observation``, ``markdown``, ``extractor``, ``elapsed_s``; raises ``ExtractionFailed``). The default
imports ``extraction.observe`` lazily, so this module does not depend on it.

The held-out split is fixed in the manifest and must not be used for tuning, threshold calibration or
the design of injected defects; it needs ``split="heldout"`` explicitly and always carries a warning.

Per-document timeouts run the extractor in a daemon thread: a timed-out extraction is abandoned, not
killed, and may keep using CPU until it ends or the process exits.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .page_checks import verify_cached
from .pilot_records import load_pilot_expectation
from .records import RecordError, options_sha256, sha256_bytes, validate_record
from .scorer import METRIC_VERSIONS, SCORER_VERSION, ScoringError, failure_result, score_records, score_to_result

HARNESS_VERSION = "1"
SPLITS = ("pilot", "heldout")
HELDOUT_WARNING = (
    "WARNING: scoring the HELD-OUT split. It is fixed to measure final results once; it must not be used "
    "for tuning, threshold calibration or the design of injected defects.")
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_OUT = Path.home() / ".cache" / "zotero-fulltext-extraction-runs"
TOP_FAILING = 10

Extractor = Callable[..., Any]


class HarnessError(Exception):
    """A refusal before anything is extracted: drafts, hash mismatches, split violations, bad inputs."""


class ExtractionTimeout(Exception):
    """The extractor did not return within the per-document timeout."""


def default_extractor() -> Extractor:
    """The real extractor, imported lazily so the harness and its tests do not need it."""
    try:
        from .observe import observe_pdf
    except ImportError as exc:  # pragma: no cover - depends on the other package being present
        raise HarnessError(f"the default extractor (extraction.observe.observe_pdf) is unavailable: {exc}") from exc
    return observe_pdf  # type: ignore[no-any-return]


# --- inputs ------------------------------------------------------------------------------------


def load_expectations(directory: Path, manifest: dict[str, Any], cache: Path | None, split: str) -> list[dict[str, Any]]:
    """Every ``*.json`` in ``directory`` as a reviewed pilot expectation, in file-name order.

    Refuses (listing all findings) on a draft, an unreadable or rejected record, a duplicate record
    id, or a record outside ``split``. Nothing is skipped silently.
    """
    if split not in SPLITS:
        raise HarnessError(f"unknown split {split!r}; choose one of {SPLITS}")
    paths = sorted(p for p in directory.glob("*.json") if p.is_file())
    if not paths:
        raise HarnessError(f"no *.json expectation records in {directory}")
    split_ids = {d["id"] for d in manifest["pilot" if split == "pilot" else "held_out"]}
    items: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: dict[str, str] = {}
    for path in paths:
        try:
            record = load_pilot_expectation(path, manifest, cache)
        except RecordError as err:
            problems.append(f"{path.name}: {'; '.join(err.errors)}")
            continue
        corpus_id = record["document"]["corpus_id"]
        if corpus_id not in split_ids:
            problems.append(f"{path.name}: document {corpus_id!r} is not in the {split!r} split of the manifest")
        if record["record_id"] in seen:
            problems.append(f"{path.name}: record_id {record['record_id']!r} duplicates {seen[record['record_id']]}")
        seen[record["record_id"]] = path.name
        items.append({"record": record, "path": path, "sha256": sha256_bytes(path.read_bytes()), "corpus_id": corpus_id})
    if problems:
        raise HarnessError("refusing to run:\n  " + "\n  ".join(problems))
    return items


def _group_by_pdf(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(items, key=lambda i: (i["corpus_id"], i["record"]["record_id"])):
        groups.setdefault(item["corpus_id"], []).append(item)
    return groups


def _manifest_entry(manifest: dict[str, Any], corpus_id: str) -> dict[str, Any]:
    return next(d for name in ("pilot", "held_out") for d in manifest[name] if d["id"] == corpus_id)


# --- extraction --------------------------------------------------------------------------------


def _call_with_timeout(fn: Callable[[], Any], timeout: float | None) -> Any:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise ExtractionTimeout(f"no result within {timeout:g} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.") or "x"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the run -----------------------------------------------------------------------------------


def run_pilot(
    expectations_dir: Path,
    cache: Path,
    out_dir: Path,
    manifest: dict[str, Any],
    *,
    extractor: Extractor | None = None,
    options: Mapping[str, Any] | None = None,
    timeout: float | None = DEFAULT_TIMEOUT_S,
    split: str = "pilot",
    clock: Callable[[], datetime] = _now,
) -> dict[str, Any]:
    """Run ``extractor`` over the reviewed expectations and write the run files under ``out_dir/<run id>/``.

    Returns ``{"run": <record>, "run_dir": Path, "summary": str, "details": dict}``. Raises
    ``HarnessError`` before extracting anything on a refusal. ``out_dir`` is not checked here (the
    CLI refuses locations inside a git checkout).
    """
    options_dict = dict(options or {})
    items = load_expectations(expectations_dir, manifest, cache, split)
    groups = _group_by_pdf(items)

    pdf_paths: dict[str, Path] = {}
    problems = []
    for corpus_id in groups:
        status, path, detail = verify_cached(_manifest_entry(manifest, corpus_id), cache)
        if status != "ok":
            problems.append(f"{corpus_id}: {status}: {detail}")
        pdf_paths[corpus_id] = path
    if problems:
        raise HarnessError("refusing to extract (PDF cache does not match the manifest):\n  " + "\n  ".join(problems))

    extract = extractor if extractor is not None else default_extractor()
    started = clock()
    run_id = f"run-{started.astimezone(UTC):%Y%m%dT%H%M%SZ}-{split}"
    run_dir = out_dir / run_id
    (run_dir / "observations").mkdir(parents=True, exist_ok=True)
    (run_dir / "markdown").mkdir(exist_ok=True)

    identity: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    scores: dict[str, dict[str, Any]] = {}
    details_inputs: dict[str, dict[str, Any]] = {}
    documents: list[dict[str, Any]] = []
    # The configuration id depends on the extractor identity, which is only known after extraction,
    # so results are built with a placeholder and re-keyed once.
    placeholder = "@config"

    for corpus_id, group in groups.items():
        entry = _manifest_entry(manifest, corpus_id)
        pdf_sha = entry["pdf_sha256"]
        pages = sorted({p for item in group for p in item["record"]["scope"]["physical_pages"]})
        request_pages = None if pages == list(range(1, entry["pdf_pages"] + 1)) else pages
        document = {"corpus_id": corpus_id, "pdf_sha256": pdf_sha, "pages": pages, "status": "ok"}
        documents.append(document)
        error: tuple[str, str, str] | None = None
        result_obj = None
        wall_start = time.monotonic()
        try:
            result_obj = _call_with_timeout(
                lambda p=pdf_paths[corpus_id], pg=request_pages: extract(p, pages=pg, options=options_dict or None),
                timeout)
        except ExtractionTimeout as exc:
            error = ("timeout", "ExtractionTimeout", str(exc))
        except Exception as exc:  # noqa: BLE001 - any extractor failure is recorded, never fatal to the run
            error = ("failed", type(exc).__name__, str(exc) or type(exc).__name__)
        wall = time.monotonic() - wall_start

        output_sha = markdown_sha = None
        latency = wall
        if error is None:
            try:
                observation = dict(result_obj.observation)
                markdown = str(result_obj.markdown)
                latency = float(result_obj.elapsed_s)
                for key, value in dict(result_obj.extractor or {}).items():
                    identity.setdefault(key, value)
                obs_bytes = _json_bytes(observation)
                md_bytes = markdown.encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - a malformed ObservationResult is a failed extraction
                error = ("failed", type(exc).__name__, f"malformed extractor result: {exc}")
        if error is None:
            output_sha, markdown_sha = sha256_bytes(obs_bytes), sha256_bytes(md_bytes)
            (run_dir / "observations" / f"{_slug(corpus_id)}.observation.json").write_bytes(obs_bytes)
            (run_dir / "markdown" / f"{_slug(corpus_id)}.md").write_bytes(md_bytes)
        else:
            document.update(status=error[0], error={"kind": error[1], "message": error[2]})
        document.update(latency_seconds=round(latency, 3), observation_sha256=output_sha, markdown_sha256=markdown_sha)

        for item in group:
            record = item["record"]
            input_id = record["record_id"]
            details_inputs[input_id] = {"corpus_id": corpus_id, "pdf_sha256": pdf_sha, "expectation_sha256": item["sha256"],
                                        "observation_sha256": output_sha, "markdown_sha256": markdown_sha}
            extra = {"latency_seconds": round(latency, 3)}
            if error is not None:
                results.append(failure_result(placeholder, input_id, error[0], error[1], error[2], **extra))
                continue
            try:
                score = score_records(record, observation, markdown=markdown)
            except (ScoringError, RecordError) as exc:
                results.append(failure_result(placeholder, input_id, "failed", type(exc).__name__, str(exc), **extra))
                continue
            scores[input_id] = score
            results.append(score_to_result(placeholder, input_id, score, output_sha,
                                           observation_id=observation["record_id"], **extra))

    finished = clock()
    tool = str(identity.get("tool") or "unknown")
    version = str(identity.get("version") or "unknown")
    options_hash = options_sha256(options_dict)
    config_id = _slug(f"{tool}-{options_hash[:8]}")
    configuration: dict[str, Any] = {"id": config_id, "tool": tool, "version": version,
                                     "options": options_dict, "options_sha256": options_hash}
    if isinstance(identity.get("model"), str):
        configuration["model"] = identity["model"]
    for result in results:
        result["configuration_id"] = config_id

    inputs = [{"id": rec["record_id"], "pdf_sha256": rec["document"]["pdf_sha256"], "expectation_id": rec["record_id"],
               "expectation_sha256": details_inputs[rec["record_id"]]["expectation_sha256"],
               "physical_pages": sorted(rec["scope"]["physical_pages"])}
              for group in groups.values() for rec in (item["record"] for item in group)]
    verdicts = Counter(r.get("verdict", r["status"]) for r in results)
    run_record: dict[str, Any] = {
        "schema_version": 1, "record_kind": "run", "record_id": run_id,
        "started_at": _stamp(started), "finished_at": _stamp(finished),
        "environment": {
            "harness": f"extraction-run-pilot {HARNESS_VERSION}", "scorer_version": SCORER_VERSION, "split": split,
            "python": platform.python_version(), "platform": platform.platform(),
            "verdicts": " ".join(f"{k}={verdicts[k]}" for k in sorted(verdicts)),
        },
        "metric_versions": dict(METRIC_VERSIONS),
        "configurations": [configuration],
        "inputs": inputs,
        "results": results,
    }
    try:
        validate_record(run_record)
    except RecordError as err:
        raise HarnessError(f"internal error: the run record is invalid, nothing written: {err}") from err

    details = {"run_id": run_id, "harness_version": HARNESS_VERSION, "split": split, "extractor": identity,
               "documents": documents, "inputs": details_inputs}
    summary = render_summary(run_record, scores, documents, split)
    (run_dir / "run.json").write_bytes(_json_bytes(run_record))
    (run_dir / "details.json").write_bytes(_json_bytes(details))
    (run_dir / "summary.md").write_text(summary, encoding="utf-8", newline="\n")
    return {"run": run_record, "run_dir": run_dir, "summary": summary, "details": details}


# --- summary -----------------------------------------------------------------------------------


def aggregate_metrics(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per (metric, mode) macro mean over the ok results that report it. Strict and tolerant stay apart."""
    values: dict[tuple[str, str], list[float]] = {}
    for result in results:
        for metric in result.get("metrics", []):
            values.setdefault((metric["name"], metric["mode"]), []).append(metric["value"])
    return [{"name": name, "mode": mode, "mean": sum(v) / len(v), "pages": len(v)}
            for (name, mode), v in sorted(values.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def top_failing_checks(scores: Mapping[str, Mapping[str, Any]], limit: int = TOP_FAILING) -> list[dict[str, Any]]:
    """Deciding strict checks that failed, counted per metric name across all scored inputs."""
    counts: Counter[str] = Counter()
    inputs: dict[str, set[str]] = {}
    examples: dict[str, str] = {}
    for input_id in sorted(scores):
        for check in scores[input_id]["checks"]:
            if check["mode"] == "strict" and check["decides"] and not check["passed"]:
                counts[check["name"]] += 1
                inputs.setdefault(check["name"], set()).add(input_id)
                examples.setdefault(check["name"], f"{input_id}:{check['subject']}")
    ranked = sorted(counts, key=lambda n: (-counts[n], n))[:limit]
    return [{"name": n, "failures": counts[n], "inputs": len(inputs[n]), "example": examples[n]} for n in ranked]


def render_summary(run: Mapping[str, Any], scores: Mapping[str, Mapping[str, Any]],
                   documents: Sequence[Mapping[str, Any]], split: str) -> str:
    results = run["results"]
    counts = Counter(r.get("verdict", "error") if r["status"] == "ok" else "error" for r in results)
    config = run["configurations"][0]
    lines = [f"# Extraction run {run['record_id']}", ""]
    if split == "heldout":
        lines += [HELDOUT_WARNING, ""]
    lines += [
        f"- extractor: {config['tool']} {config['version']}" + (f" ({config['model']})" if "model" in config else ""),
        f"- options: `{json.dumps(config['options'], sort_keys=True)}`",
        f"- split: {split}; scorer version {SCORER_VERSION}; harness version {HARNESS_VERSION}",
        f"- started {run['started_at']}, finished {run['finished_at']}",
        f"- pages scored: {len(results)}; passed {counts['passed']}, failed {counts['failed']}, "
        f"incomplete {counts['incomplete']}, extraction/scoring errors {counts['error']}",
        "- verdicts depend on strict checks only; a passing strict run says nothing about tolerant matches",
        "", "## Per-page verdicts", "",
        "| input | verdict | strict failures / checks | unscored | latency s |", "|---|---|---|---|---|",
    ]
    for result in sorted(results, key=lambda r: r["input_id"]):
        score = scores.get(result["input_id"])
        if result["status"] == "ok" and score is not None:
            lines.append(f"| {result['input_id']} | {result['verdict']} | {score['strict_failures']} / {score['strict_checks']} "
                         f"| {result['unscored_items']} | {result.get('latency_seconds', 0):.2f} |")
        else:
            err = result.get("error", {})
            lines.append(f"| {result['input_id']} | {result['status']}: {err.get('kind', '')} | - | - | {result.get('latency_seconds', 0):.2f} |")
    lines += ["", "Latency is the whole-PDF extraction time, shared by every expectation of that PDF.", "",
              "## Metric aggregates (macro mean over pages reporting the metric)", ""]
    aggregates = aggregate_metrics(results)
    for mode in ("strict", "tolerant"):
        lines += [f"### {mode}", "", "| metric | mean | pages |", "|---|---|---|"]
        rows = [a for a in aggregates if a["mode"] == mode]
        lines += [f"| {a['name']} | {a['mean']:.3f} | {a['pages']} |" for a in rows] or ["| (none) | | |"]
        lines.append("")
    lines += ["## Top failing strict checks", ""]
    failing = top_failing_checks(scores)
    if failing:
        lines += ["| metric | failed checks | inputs | example |", "|---|---|---|---|"]
        lines += [f"| {f['name']} | {f['failures']} | {f['inputs']} | {f['example']} |" for f in failing]
    else:
        lines.append("None.")
    lines += ["", "## Documents", "", "| document | status | observation sha256 | markdown sha256 |", "|---|---|---|---|"]
    for doc in documents:
        status = doc["status"] if doc["status"] == "ok" else f"{doc['status']}: {doc['error']['kind']}"
        lines.append(f"| {doc['corpus_id']} | {status} | {(doc['observation_sha256'] or '-')[:16]} | {(doc['markdown_sha256'] or '-')[:16]} |")
    errors = [r for r in results if r["status"] != "ok"]
    if errors:
        lines += ["", "## Errors", ""]
        lines += [f"- {r['input_id']}: {r['status']} {r['error']['kind']}: {r['error']['message']}" for r in sorted(errors, key=lambda r: r["input_id"])]
    return "\n".join(lines) + "\n"


# --- command line ------------------------------------------------------------------------------


def _parse_option(text: str) -> tuple[str, Any]:
    key, sep, raw = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    try:
        return key, json.loads(raw)
    except ValueError:
        return key, raw


def main(argv: list[str] | None = None) -> int:
    from .drafts import DEFAULT_CACHE, _inside_git_checkout
    from .pilot import MANIFEST_PATH, PIN_PATH, check_manifest

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expectations", type=Path, required=True, help="directory of reviewed expectation *.json records (drafts are refused)")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="directory of <id>.pdf files; read-only, checked against the manifest pins")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory; must be outside every git checkout")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--pin", type=Path, default=PIN_PATH)
    parser.add_argument("--split", choices=SPLITS, default="pilot",
                        help="'heldout' must be given explicitly, prints a warning, and is never for tuning")
    parser.add_argument("--image-ocr", action="store_true", help="extractor option image_ocr=true")
    parser.add_argument("--option", action="append", default=[], type=_parse_option, metavar="KEY=VALUE",
                        help="extra extractor option (VALUE parsed as JSON, else a string); repeatable")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds per document (default %(default)s); 0 = none")
    args = parser.parse_args(argv)

    problems = check_manifest(args.manifest, args.pin)
    if problems:
        parser.error("manifest check failed, refusing to run against a changed selection: " + "; ".join(problems))
    if _inside_git_checkout(args.out):
        parser.error("--out must be outside every git checkout: extracted text is copyrighted and must not be committed")
    manifest = json.loads(args.manifest.read_bytes())
    options: dict[str, Any] = dict(args.option)
    if args.image_ocr:
        options["image_ocr"] = True
    if args.split == "heldout":
        print(HELDOUT_WARNING, file=sys.stderr)
    try:
        outcome = run_pilot(args.expectations, args.cache, args.out, manifest, options=options,
                            timeout=args.timeout or None, split=args.split)
    except HarnessError as err:
        print(err, file=sys.stderr)
        return 2
    print(outcome["summary"])
    print(f"run record: {outcome['run_dir'] / 'run.json'}")
    return 0


__all__ = ["HELDOUT_WARNING", "HarnessError", "HARNESS_VERSION", "SPLITS", "aggregate_metrics", "default_extractor",
           "load_expectations", "main", "render_summary", "run_pilot", "top_failing_checks"]
