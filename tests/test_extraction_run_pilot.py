"""Run harness: synthetic PDFs, synthetic reviewed expectations and a fake extractor (no observe.py)."""

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest

from extraction import drafts, load_record, sha256_text, validate_record
from extraction import run_pilot as harness

TEXT = "Reviewed paragraph."
DOCS = ("doc-pass", "doc-fail", "doc-incomplete", "doc-boom", "doc-slow")


class FakeFailure(Exception):
    pass


@dataclass(frozen=True)
class Result:
    observation: dict
    markdown: str
    extractor: dict
    elapsed_s: float


def _pdf(path: Path, label: str) -> str:
    doc = pymupdf.open()
    doc.new_page(width=612, height=792).insert_text((72, 100), f"Synthetic {label}", fontsize=11)
    doc.save(path)
    doc.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _block(block_id, order, text, **extra):
    return {"id": block_id, "type": "paragraph", "page": 1, "reading_order": order, "text": text,
            "text_sha256": sha256_text(text), "bbox": {"x0": 10, "y0": 10 + 40 * order, "x1": 200, "y1": 40 + 40 * order}, **extra}


@pytest.fixture
def world(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    pilot_docs = []
    for name in DOCS:
        digest = _pdf(cache / f"{name}.pdf", name)
        pilot_docs.append({"id": name, "role": "stratum_pages", "stratum": "two-column", "basis": "assumed",
                           "pdf_sha256": digest, "pdf_pages": 1, "candidate_pages": [1]})
    manifest = {"seed": "test-seed", "pilot": pilot_docs, "held_out": []}
    drafts.run(manifest, cache, tmp_path / "drafts-out", titles={n: n for n in DOCS})
    expectations = tmp_path / "expectations"
    expectations.mkdir()
    records = {}
    for name in DOCS:
        draft = tmp_path / "drafts-out" / "drafts" / f"pilot-{name}.expectation.draft.json"
        record = json.loads(draft.read_text(encoding="utf-8"))
        record["provenance"]["review"] = {"status": "reviewed", "reviewer": "rev1", "reviewed_at": "2026-11-06T10:00:00Z",
                                          "basis": "page_image_inspection"}
        record["provenance"].pop("uncertainty")
        record["blocks"] = [_block("b1", 1, TEXT)]
        if name == "doc-incomplete":
            record["blocks"].append(_block("b2", 2, "Doubtful text.", uncertainty={"level": "high", "note": "unreadable"}))
        records[name] = record
        (expectations / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
    return {"cache": cache, "manifest": manifest, "expectations": expectations, "records": records,
            "out": tmp_path / "out", "tmp": tmp_path}


def _observation_of(expectation, *, text=None):
    obs = copy.deepcopy(expectation)
    obs["record_kind"] = "observation"
    obs["record_id"] = "obs-" + expectation["record_id"]
    obs["provenance"] = {"produced_by": {"tool": "fake-extractor", "version": "9.9"}, "created_at": "2026-11-07T00:00:00Z"}
    if text is not None:
        obs["blocks"][0]["text"] = text
        obs["blocks"][0]["text_sha256"] = sha256_text(text)
    return obs


class FakeExtractor:
    def __init__(self, records, slow=0.0):
        self.records, self.slow, self.calls = records, slow, []

    def __call__(self, pdf_path, *, pages=None, options=None):
        name = Path(pdf_path).stem
        self.calls.append((name, pages, options))
        if name == "doc-boom":
            raise FakeFailure("backend exploded")
        if name == "doc-slow":
            time.sleep(self.slow)
        markdown = TEXT + "\n"
        obs = _observation_of(self.records[name], text="Reviewed paragraph X." if name == "doc-fail" else None)
        if name == "doc-pass":
            obs["blocks"][0]["output_span"] = {"markdown_sha256": sha256_text(markdown), "start": 0, "end": len(TEXT),
                                               "span_sha256": sha256_text(TEXT)}
        return Result(obs, markdown, {"tool": "fake-extractor", "version": "9.9"}, 0.25)


def _clock():
    return datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


def _run(world, **kwargs):
    extractor = kwargs.pop("extractor", None) or FakeExtractor(world["records"], slow=kwargs.pop("slow", 0.0))
    outcome = harness.run_pilot(world["expectations"], world["cache"], world["out"], world["manifest"],
                                extractor=extractor, clock=_clock, **kwargs)
    return outcome, extractor


def _by_input(outcome):
    return {r["input_id"]: r for r in outcome["run"]["results"]}


def _id(world, name):
    return world["records"][name]["record_id"]


def test_pass_fail_incomplete_and_failures_are_recorded(world):
    outcome, extractor = _run(world, timeout=0.3, slow=2.0, options={"image_ocr": True})
    results = _by_input(outcome)
    assert results[_id(world, "doc-pass")]["status"] == "ok" and results[_id(world, "doc-pass")]["verdict"] == "passed"
    assert results[_id(world, "doc-fail")]["verdict"] == "failed"
    incomplete = results[_id(world, "doc-incomplete")]
    assert incomplete["verdict"] == "incomplete" and incomplete["unscored_items"] >= 1
    boom = results[_id(world, "doc-boom")]
    assert boom["status"] == "failed" and boom["error"] == {"kind": "FakeFailure", "message": "backend exploded"} and "metrics" not in boom
    slow = results[_id(world, "doc-slow")]
    assert slow["status"] == "timeout" and slow["error"]["kind"] == "ExtractionTimeout" and "metrics" not in slow
    # strict and tolerant metrics stay separate entries; the output span round-trip ran for the passing page
    modes = {(m["name"], m["mode"]) for m in results[_id(world, "doc-pass")]["metrics"]}
    assert ("text_presence", "strict") in modes and ("text_presence", "tolerant") in modes
    assert ("output_span_roundtrip", "strict") in modes
    # each PDF is extracted once, for the pages its expectations need, with the options passed through
    assert sorted(c[0] for c in extractor.calls) == sorted(DOCS)
    assert all(c[1] == [1] or c[1] is None for c in extractor.calls) and all(c[2] == {"image_ocr": True} for c in extractor.calls)


def test_run_record_is_schema_valid_and_pins_identity_and_hashes(world):
    outcome, _ = _run(world, timeout=0.3, slow=2.0, options={"image_ocr": True})
    run_dir = outcome["run_dir"]
    record = load_record(run_dir / "run.json", kind="run")
    assert record == outcome["run"] and validate_record(record) == "run"
    config = record["configurations"][0]
    assert (config["tool"], config["version"], config["options"]) == ("fake-extractor", "9.9", {"image_ocr": True})
    assert record["metric_versions"] == harness.METRIC_VERSIONS
    assert record["environment"]["split"] == "pilot" and "passed=1" in record["environment"]["verdicts"]
    assert record["started_at"] == "2026-11-07T12:00:00Z"
    ok = _by_input(outcome)[_id(world, "doc-pass")]
    assert ok["latency_seconds"] == 0.25
    observation_file = run_dir / "observations" / "doc-pass.observation.json"
    assert hashlib.sha256(observation_file.read_bytes()).hexdigest() == ok["output_sha256"]
    details = json.loads((run_dir / "details.json").read_text(encoding="utf-8"))
    entry = details["inputs"][_id(world, "doc-pass")]
    assert hashlib.sha256((run_dir / "markdown" / "doc-pass.md").read_bytes()).hexdigest() == entry["markdown_sha256"]
    assert entry["pdf_sha256"] == world["manifest"]["pilot"][0]["pdf_sha256"]
    expectation_file = world["expectations"] / "doc-pass.json"
    assert entry["expectation_sha256"] == hashlib.sha256(expectation_file.read_bytes()).hexdigest()
    assert details["inputs"][_id(world, "doc-boom")]["observation_sha256"] is None


def test_summary_reports_verdicts_aggregates_and_failing_checks(world):
    outcome, _ = _run(world, timeout=0.3, slow=2.0)
    summary = (outcome["run_dir"] / "summary.md").read_text(encoding="utf-8")
    assert summary == outcome["summary"]
    assert "passed 1, failed 1, incomplete 1, extraction/scoring errors 2" in summary
    assert "### strict" in summary and "### tolerant" in summary
    assert "## Top failing strict checks" in summary and "text_presence" in summary
    assert "FakeFailure" in summary and "ExtractionTimeout" in summary
    assert "HELD-OUT" not in summary


def test_run_directories_never_collide(world):
    first, _ = _run(world, timeout=0.3)
    files = {p.relative_to(first["run_dir"]): p.read_bytes() for p in first["run_dir"].rglob("*") if p.is_file()}
    second, _ = _run(world, timeout=0.3)
    assert first["run_dir"] != second["run_dir"] and second["run_dir"].name == first["run_dir"].name + "-2"
    assert second["run"]["record_id"] == second["run_dir"].name
    assert files == {p.relative_to(first["run_dir"]): p.read_bytes() for p in first["run_dir"].rglob("*") if p.is_file()}
    third, _ = _run(world, timeout=0.3)
    assert third["run_dir"].name.endswith("-3")


def test_no_extraction_after_the_first_timeout(world):
    inner = FakeExtractor(world["records"])

    def hangs_on_fail(pdf_path, *, pages=None, options=None):
        if Path(pdf_path).stem == "doc-fail":
            time.sleep(2)
        return inner(pdf_path, pages=pages, options=options)

    outcome, _ = _run(world, extractor=hangs_on_fail, timeout=0.3)
    results = _by_input(outcome)
    assert results[_id(world, "doc-boom")]["status"] == "failed"  # sorted before the timeout: still extracted
    assert results[_id(world, "doc-fail")]["status"] == "timeout"
    for name in ("doc-incomplete", "doc-pass", "doc-slow"):
        assert results[_id(world, name)]["status"] == "skipped"
        assert results[_id(world, name)]["error"]["kind"] == "SkippedAfterTimeout" and "metrics" not in results[_id(world, name)]
    assert sorted(c[0] for c in inner.calls) == ["doc-boom"]  # nothing started after the timeout
    assert "SkippedAfterTimeout" in outcome["summary"] and "no further document was extracted" in outcome["summary"]
    assert "harness wall clock" in outcome["summary"]
    validate_record(outcome["run"])


def test_extractor_identity_is_recorded_per_document_and_must_agree(world):
    outcome, _ = _run(world, timeout=0.3)
    documents = {d["corpus_id"]: d for d in outcome["details"]["documents"]}
    assert documents["doc-pass"]["extractor"] == {"tool": "fake-extractor", "version": "9.9"}
    assert documents["doc-pass"]["latency_source"] == "extractor elapsed_s" and "extractor" not in documents["doc-boom"]
    inner = FakeExtractor(world["records"])

    def drifting(pdf_path, *, pages=None, options=None):
        result = inner(pdf_path, pages=pages, options=options)
        version = "1.0" if Path(pdf_path).stem == "doc-pass" else "2.0"
        return Result(result.observation, result.markdown, {"tool": "fake-extractor", "version": version}, 0.1)

    before = set(world["out"].iterdir())
    with pytest.raises(harness.HarnessError, match="different extractor identities"):
        _run(world, extractor=drifting, timeout=0.3)
    assert set(world["out"].iterdir()) == before


def test_all_failed_run_keeps_unknown_identity_and_says_so(world):
    def always_fails(pdf_path, *, pages=None, options=None):
        raise FakeFailure("nope")

    outcome, _ = _run(world, extractor=always_fails, timeout=0.3)
    assert outcome["run"]["configurations"][0]["tool"] == "unknown"
    assert "No extractor identity is known" in outcome["summary"]


def test_out_dir_inside_a_git_checkout_is_refused_by_the_library(world):
    inside = Path(harness.__file__).resolve().parent / "out"
    extractor = FakeExtractor(world["records"])
    with pytest.raises(harness.HarnessError, match="outside every git checkout"):
        harness.run_pilot(world["expectations"], world["cache"], inside, world["manifest"], extractor=extractor)
    assert extractor.calls == [] and not inside.exists()


def test_deterministic_ordering(world):
    first, _ = _run(world, timeout=0.3, slow=1.0)
    second, _ = _run(world, timeout=0.3, slow=1.0)
    assert [r["input_id"] for r in first["run"]["results"]] == [r["input_id"] for r in second["run"]["results"]]
    def stable(outcome):  # measured wall time of a failed extraction legitimately differs between runs
        return [{k: v for k, v in r.items() if k != "latency_seconds"} for r in outcome["run"]["results"]]

    assert stable(first) == stable(second)


def test_pdf_hash_mismatch_is_refused_before_extracting(world):
    (world["cache"] / "doc-pass.pdf").write_bytes(b"not the pinned pdf")
    extractor = FakeExtractor(world["records"])
    with pytest.raises(harness.HarnessError, match="doc-pass: cached PDF does not match the pinned hash"):
        _run(world, extractor=extractor)
    assert extractor.calls == [] and not world["out"].exists()


def test_missing_pdf_is_refused(world):
    (world["cache"] / "doc-fail.pdf").unlink()
    with pytest.raises(harness.HarnessError, match="doc-fail: PDF not in cache"):
        _run(world)


def test_draft_expectation_is_refused(world):
    draft = world["tmp"] / "drafts-out" / "drafts" / "pilot-doc-pass.expectation.draft.json"
    (world["expectations"] / "doc-pass.json").write_bytes(draft.read_bytes())
    extractor = FakeExtractor(world["records"])
    with pytest.raises(harness.HarnessError, match=r"doc-pass\.json.*not reviewed"):
        _run(world, extractor=extractor)
    assert extractor.calls == []


def test_empty_expectation_directory_is_refused(world, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(harness.HarnessError, match="no \\*.json"):
        harness.run_pilot(empty, world["cache"], world["out"], world["manifest"], extractor=FakeExtractor({}))


def test_heldout_needs_an_explicit_split_and_carries_a_warning(world):
    # the pilot split refuses a held-out document
    manifest = copy.deepcopy(world["manifest"])
    manifest["held_out"], manifest["pilot"] = manifest["pilot"][:1], manifest["pilot"][1:]
    with pytest.raises(harness.HarnessError, match="doc-pass.*not in the 'pilot' split"):
        harness.run_pilot(world["expectations"], world["cache"], world["out"], manifest, extractor=FakeExtractor(world["records"]))
    # the held-out split refuses pilot documents mixed in, and an unknown split name
    with pytest.raises(harness.HarnessError, match="not in the 'heldout' split"):
        harness.run_pilot(world["expectations"], world["cache"], world["out"], manifest, split="heldout",
                          extractor=FakeExtractor(world["records"]))
    with pytest.raises(harness.HarnessError, match="unknown split"):
        harness.run_pilot(world["expectations"], world["cache"], world["out"], manifest, split="all",
                          extractor=FakeExtractor(world["records"]))
    only = world["tmp"] / "heldout-only"
    only.mkdir()
    (only / "doc-pass.json").write_bytes((world["expectations"] / "doc-pass.json").read_bytes())
    outcome = harness.run_pilot(only, world["cache"], world["out"], manifest, split="heldout",
                                extractor=FakeExtractor(world["records"]), clock=_clock)
    assert outcome["run"]["environment"]["split"] == "heldout"
    assert harness.HELDOUT_WARNING in outcome["summary"] and outcome["run_dir"].name.endswith("-heldout")


def test_cli_defaults_to_pilot_prints_heldout_warning_and_stays_out_of_the_repo(world, capsys, monkeypatch):
    manifest_path = world["tmp"] / "manifest.json"
    manifest = copy.deepcopy(world["manifest"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr("extraction.pilot.check_manifest", lambda *a, **k: [])
    args = ["--expectations", str(world["expectations"]), "--cache", str(world["cache"]), "--manifest", str(manifest_path)]
    assert harness.main([*args, "--out", str(Path(harness.__file__).resolve().parent / "out")]) == 2
    assert "outside every git checkout" in capsys.readouterr().err
    monkeypatch.setattr(harness, "default_extractor", lambda: FakeExtractor(world["records"]))
    # held-out requires --split heldout: the pilot documents are not held out, so it refuses and warns
    code = harness.main([*args, "--out", str(world["out"]), "--split", "heldout"])
    captured = capsys.readouterr()
    assert code == 2 and harness.HELDOUT_WARNING in captured.err and "not in the 'heldout' split" in captured.err
    code = harness.main([*args, "--out", str(world["out"]), "--timeout", "0.3", "--image-ocr", "--option", "dpi=150"])
    captured = capsys.readouterr()
    assert code == 0 and "run record:" in captured.out and harness.HELDOUT_WARNING not in captured.err


def test_invalid_observation_is_a_failed_result_not_a_crash(world):
    def broken(pdf_path, *, pages=None, options=None):
        result = FakeExtractor(world["records"])(pdf_path, pages=pages, options=options)
        bad = copy.deepcopy(result.observation)
        bad["document"]["pdf_sha256"] = "0" * 64
        return Result(bad, result.markdown, result.extractor, 0.1)

    outcome, _ = _run(world, extractor=broken, timeout=1)
    ok_results = [r for r in outcome["run"]["results"] if r["status"] != "ok"]
    assert ok_results and all(r["error"]["kind"] in {"ScoringError", "FakeFailure", "ExtractionTimeout"} or r["status"] == "failed" for r in ok_results)
    assert _by_input(outcome)[_id(world, "doc-pass")]["error"]["kind"] == "ScoringError"
    validate_record(outcome["run"])
