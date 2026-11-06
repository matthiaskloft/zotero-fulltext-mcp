import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from zotero_pdf_text import reconversion as jobs


@pytest.fixture
def job(tmp_path, monkeypatch):
    config = SimpleNamespace(output_root=tmp_path, zotero_root=tmp_path,
                             zotero_data_directory=tmp_path, linked_attachments=tmp_path)
    monkeypatch.setattr(jobs, "load_config", lambda path: config)
    monkeypatch.setattr(jobs, "validate_config", lambda config: None)
    run = tmp_path / "runs" / "test"
    run.mkdir(parents=True)
    config_path = tmp_path / "config.json"
    config_path.write_text('{}')
    (run / "config.snapshot.json").write_text('{}')
    (run / "request.json").write_text(json.dumps(dict(config=str(config_path.resolve()), total=2, workers=1,
        timeout_seconds=10, config_sha256=hashlib.sha256(b'{}').hexdigest())))
    (run / "manifest.csv").write_text("status\nconverted\n")
    jobs._save(run, dict(phase="converted", manifest_complete=True))
    return run, config


def test_conversion_is_isolated_and_resumable(job, monkeypatch):
    run, config = job
    calls = []
    monkeypatch.setattr(jobs, "convert_verified", lambda *a, **kw: calls.append(kw))
    jobs.worker(run, publish=False, publish_only=False)
    assert calls[0]["canonical"] is False
    assert calls[0]["resume"] is True
    assert calls[0]["output_dir"] == run
    assert jobs.read_status(run)["phase"] == "converted"
    assert not (config.output_root / ".pipeline.lock").exists()


def test_failure_is_durable_and_releases_locks(job, monkeypatch):
    run, config = job

    def fail(*args, **kwargs):
        raise RuntimeError("extraction failed")

    monkeypatch.setattr(jobs, "convert_verified", fail)
    with pytest.raises(RuntimeError, match="extraction failed"):
        jobs.worker(run, publish=False, publish_only=False)
    status = jobs.read_status(run)
    assert status["phase"] == "failed"
    assert status["error"] == "extraction failed"
    assert status["finished_at"]
    assert not (run / ".pipeline.lock").exists()
    assert not (config.output_root / ".pipeline.lock").exists()


def test_publication_retry_does_not_convert(job, monkeypatch):
    from zotero_pdf_text import cli

    run, _ = job
    calls = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(jobs, "convert_verified", lambda *a, **kw: pytest.fail("must not convert"))
    jobs.worker(run, publish=True, publish_only=True)
    assert "--keep-current" in calls[0]
    assert jobs.read_status(run)["phase"] == "completed"


def test_stale_status_and_partial_checkpoint(job):
    run, _ = job
    (run / "job.json").write_text(json.dumps(dict(phase="converting", updated_at="2000-01-01T00:00:00+00:00")))
    entry = dict(checkpoint_version=1, output="one.md", zotero_attachment_key="TESTKEY1", source_path="paper.pdf",
                 source_size=1, source_mtime_ns=1, source_sha256="hash", output_sha256="hash",
                 recorded_at="2000-01-01T00:00:00+00:00", result=dict(status="converted"))
    (run / "conversion_checkpoint.jsonl").write_bytes((json.dumps(entry) + "\n" + json.dumps(entry)).encode() + b'\n{"output":"\xc3')
    status = jobs.read_status(run)
    assert status["phase"] == "unresponsive"
    assert status["outcomes"] == {"converted": 1}


def test_run_path_cannot_escape_output_root(job):
    run, _ = job
    args = SimpleNamespace(config=Path("config.json"), workers=1, timeout_seconds=10,
                           run_dir=run.parent.parent.parent / "outside", resume=False, publish_only=False)
    with pytest.raises(ValueError, match="beneath"):
        jobs.start_job(args)


def test_failed_publication_can_be_retried(job, monkeypatch):
    from zotero_pdf_text import cli

    run, _ = job
    monkeypatch.setattr(cli, "main", lambda argv: 2)
    with pytest.raises(RuntimeError, match="publish-only"):
        jobs.worker(run, publish=True, publish_only=True)
    assert jobs.read_status(run)["phase"] == "failed"
    monkeypatch.setattr(cli, "main", lambda argv: 0)
    jobs.worker(run, publish=True, publish_only=True)
    assert jobs.read_status(run)["phase"] == "completed"


def test_new_background_job_snapshots_mapping(job, monkeypatch):
    run, config = job
    mapping = config.output_root / "input.csv"
    mapping.write_text("classification\nmapped_verified\nmapped_unverified\n")
    launched = []
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda command, **kw: launched.append((command, kw)))
    target = run.parent / "new"
    args = SimpleNamespace(config=config.output_root / "config.json", workers=2, timeout_seconds=600,
                           run_dir=target, mapping_report=mapping, resume=False,
                           publish_only=False, publish=True, background=True)
    jobs.start_job(args)
    request = json.loads((target / "request.json").read_text())
    assert request["total"] == 1
    assert (target / "mapping_report.csv").read_text() == mapping.read_text()
    assert "--publish" in launched[0][0]
    assert jobs.read_status(target)["phase"] == "queued"


def test_resume_rejects_active_run(job):
    run, _ = job
    (run / ".pipeline.lock").write_text("holder")
    args = SimpleNamespace(config=Path("config.json"), workers=None, timeout_seconds=600,
                           run_dir=run, resume=True, publish_only=False)
    with pytest.raises(ValueError, match="locked"):
        jobs.start_job(args)


def test_reservation_loser_cannot_overwrite_status(job):
    run, _ = job
    token = jobs._reserve(run)
    jobs._save(run, dict(phase="completed", attempt_id=token))
    before = (run / "job.json").read_bytes()
    with pytest.raises(FileExistsError):
        jobs.worker(run, publish=False, publish_only=False)
    assert (run / "job.json").read_bytes() == before


@pytest.mark.parametrize("broken", ["request", "config", "validation"])
def test_startup_failures_are_durable(job, monkeypatch, broken):
    run, _ = job
    if broken == "request":
        (run / "request.json").write_text('broken')
    elif broken == "config":
        (run / "config.snapshot.json").write_text('{"changed": true}')
    else:
        def fail(config):
            raise ValueError("invalid configuration")
        monkeypatch.setattr(jobs, "validate_config", fail)
    with pytest.raises(ValueError):
        jobs.worker(run, publish=False, publish_only=False)
    assert jobs.read_status(run)["phase"] == "failed"
    assert not (run / "attempt.lock").exists()


def test_failed_resume_does_not_read_previous_manifest(job, monkeypatch):
    run, _ = job
    (run / "manifest.csv").write_text("status\nconverted\nconverted\n")
    def fail(*args, **kwargs):
        raise RuntimeError("resume failed")
    monkeypatch.setattr(jobs, "convert_verified", fail)
    with pytest.raises(RuntimeError):
        jobs.worker(run, publish=False, publish_only=False)
    assert jobs.read_status(run)["outcomes"] == {}
    assert jobs.read_status(run)["manifest_complete"] is False


def test_publication_uses_snapshot_not_original_config(job, monkeypatch):
    from zotero_pdf_text import cli
    run, config = job
    (config.output_root / "config.json").write_text('{"output_root":"different-library"}')
    calls = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 0)
    jobs.worker(run, publish=True, publish_only=True)
    assert calls[0][calls[0].index("--config") + 1] == str(run / "config.snapshot.json")


def test_publish_only_rejects_old_manifest_after_failed_resume(job):
    run, config = job
    (run / "manifest.csv").write_text("status\nconverted\n")
    jobs._save(run, dict(phase="failed", manifest_complete=False))
    args = SimpleNamespace(config=config.output_root / "config.json", workers=None, timeout_seconds=600,
                           run_dir=run, resume=False, publish_only=True)
    with pytest.raises(ValueError, match="completed conversion manifest"):
        jobs.start_job(args)


def test_foreground_failure_does_not_remove_successor_reservation(job, monkeypatch):
    run, config = job
    successor = []
    def launch(args, run, token):
        (run / "attempt.lock").unlink()
        new_token = jobs._reserve(run)
        successor.append(new_token)
        jobs._save(run, dict(phase="converting", attempt_id=new_token))
        raise RuntimeError("old worker failure")
    monkeypatch.setattr(jobs, "_launch", launch)
    args = SimpleNamespace(config=Path("missing.json"), workers=None, timeout_seconds=600,
                           run_dir=run, resume=True, publish_only=False)
    with pytest.raises(RuntimeError, match="old worker"):
        jobs.start_job(args)
    assert (run / "attempt.lock").read_text() == successor[0]
    assert jobs.read_status(run)["attempt_id"] == successor[0]


def test_publication_eligibility_checked_after_reservation(job, monkeypatch):
    run, _ = job
    reserve = jobs._reserve
    def intervening_resume(run):
        jobs._save(run, dict(phase="failed", manifest_complete=False))
        return reserve(run)
    monkeypatch.setattr(jobs, "_reserve", intervening_resume)
    args = SimpleNamespace(config=Path("missing.json"), workers=None, timeout_seconds=600,
                           run_dir=run, resume=False, publish_only=True)
    with pytest.raises(ValueError, match="completed conversion manifest"):
        jobs.start_job(args)
    assert not (run / "attempt.lock").exists()


def test_resume_uses_snapshot_when_original_config_is_deleted(job, monkeypatch):
    run, config = job
    (config.output_root / "config.json").unlink()
    called = []
    monkeypatch.setattr(jobs, "convert_verified", lambda *args, **kwargs: called.append(kwargs))
    args = SimpleNamespace(config=Path("missing.json"), workers=None, timeout_seconds=600,
                           run_dir=run, resume=True, publish_only=False, publish=False, background=False)
    jobs.start_job(args)
    assert called
    assert jobs.read_status(run)["phase"] == "converted"


def test_failed_startup_does_not_report_previous_progress(job, monkeypatch):
    run, _ = job
    (run / "progress.log").write_text("[99/100] OLDKEY: converted\n")
    (run / "request.json").write_text("broken")
    with pytest.raises(ValueError):
        jobs.worker(run, publish=False, publish_only=False)
    assert "processed" not in jobs.read_status(run)


def test_publication_retry_survives_transient_config_failure(job, monkeypatch):
    from zotero_pdf_text import cli
    run, _ = job
    args = SimpleNamespace(config=Path("missing.json"), workers=None, timeout_seconds=600,
                           run_dir=run, resume=False, publish_only=True, publish=False, background=False)
    def unavailable(config):
        raise FileNotFoundError("Zotero path temporarily unavailable")
    monkeypatch.setattr(jobs, "validate_config", unavailable)
    with pytest.raises(FileNotFoundError):
        jobs.start_job(args)
    assert jobs.read_status(run)["manifest_complete"] is True
    assert not (run / "attempt.lock").exists()
    monkeypatch.setattr(jobs, "validate_config", lambda config: None)
    monkeypatch.setattr(cli, "main", lambda argv: 0)
    jobs.start_job(args)
    assert jobs.read_status(run)["phase"] == "completed"
