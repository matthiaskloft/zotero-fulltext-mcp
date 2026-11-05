"""Durable CLI reconversion jobs; source PDFs and Zotero remain read-only."""
from __future__ import annotations

import csv
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ._atomic import atomic_write_text
from .config import load_config, validate_config
from .checkpoint import read_checkpoint
from .converter import convert_verified
from .lock import pipeline_write_lock


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reserve(run: Path) -> str:
    token = os.urandom(16).hex()
    with (run / "attempt.lock").open("x", encoding="utf-8") as handle:
        handle.write(token)
    return token


def _save(run: Path, state: dict, **fields) -> None:
    state.update(fields, updated_at=_now())
    atomic_write_text(run / "job.json", json.dumps(state, indent=2))


def read_status(run: Path) -> dict:
    status = json.loads((run / "job.json").read_text(encoding="utf-8"))
    if status["phase"] in {"queued", "starting", "converting", "publishing"}:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(status["updated_at"])
        if age.total_seconds() > 90:
            status["phase"] = "unresponsive"
    counts: Counter = Counter()
    manifest = run / "manifest.csv"
    if manifest.exists() and status.get("manifest_complete", False):
        with manifest.open(encoding="utf-8-sig", newline="") as handle:
            counts.update(row["status"] for row in csv.DictReader(handle))
    else:
        entries = read_checkpoint(run / "conversion_checkpoint.jsonl")
        counts.update(entry.result.get("status", "unknown") for entry in entries.values())
    status["outcomes"] = dict(counts)
    status["recorded_results"] = sum(counts.values())
    log = run / "progress.log"
    if log.exists():
        with log.open("rb") as handle:
            handle.seek(status.get("log_offset", 0))
            for line in handle:
                match = re.match(rb"\[(\d+)/(\d+)\]", line)
                if match:
                    status["processed"] = int(match[1])
    status["log"] = str(run / "progress.log")
    return status


def _saved_config(run: Path):
    settings = json.loads((run / "request.json").read_text(encoding="utf-8"))
    snapshot = run / "config.snapshot.json"
    if hashlib.sha256(snapshot.read_bytes()).hexdigest() != settings["config_sha256"]:
        raise ValueError("Saved job configuration has changed")
    config = load_config(snapshot)
    validate_config(config)
    if not run.resolve().is_relative_to(config.output_root.resolve() / "runs"):
        raise ValueError("Run does not belong to the saved output root")
    return settings, config


def _owns(run: Path, token: str) -> bool:
    try:
        return (run / "attempt.lock").read_text(encoding="utf-8") == token
    except FileNotFoundError:
        return False


def start_job(args) -> Path:
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be positive")
    if args.timeout_seconds < 1:
        raise ValueError("--timeout-seconds must be positive")
    existing = args.resume or args.publish_only
    if existing:
        if args.run_dir is None:
            raise ValueError("--run-dir is required for resume/publication")
        run = args.run_dir.resolve()
        if (run / ".pipeline.lock").exists():
            raise ValueError("This run is locked; inspect its status before removing a stale lock")
    else:
        config_path = args.config.resolve()
        config = load_config(config_path)
        validate_config(config)
        run = args.run_dir.resolve() if args.run_dir else (
            config.output_root / "runs" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_reconversion_" + os.urandom(4).hex())
        ).resolve()
        if not run.is_relative_to(config.output_root.resolve() / "runs"):
            raise ValueError("--run-dir must be beneath the configured output_root/runs")
        if args.mapping_report is None:
            raise ValueError("--mapping-report is required for a new run")
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        for name in ("zotero_root", "zotero_data_directory", "linked_attachments", "output_root"):
            config_data[name] = str(getattr(config, name).resolve())
        snapshot = json.dumps(config_data, indent=2)
        mapping = args.mapping_report.resolve()
        with mapping.open(encoding="utf-8-sig", newline="") as handle:
            total = sum(row["classification"] == "mapped_verified" for row in csv.DictReader(handle))
        run.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(mapping, run / "mapping_report.csv")
        settings = dict(config=str(config_path), workers=args.workers,
                        timeout_seconds=args.timeout_seconds, total=total,
                        config_sha256=hashlib.sha256(snapshot.encode("utf-8")).hexdigest())
        atomic_write_text(run / "config.snapshot.json", snapshot)
        atomic_write_text(run / "request.json", json.dumps(settings, indent=2))
    token = _reserve(run)
    state = dict(run_id=run.name, attempt_id=token, phase="queued", manifest_complete=False,
                 log_offset=(run / "progress.log").stat().st_size if (run / "progress.log").exists() else 0)
    try:
        if args.publish_only:
            previous = json.loads((run / "job.json").read_text(encoding="utf-8"))
            if not previous.get("manifest_complete") or not (run / "manifest.csv").exists():
                raise ValueError("Publication requires a completed conversion manifest")
            state["manifest_complete"] = True
        settings, _ = _saved_config(run)
        state["total"] = settings["total"]
        _save(run, state)
        _launch(args, run, token)
    except BaseException as exc:
        # Foreground workers finalize their own attempt before propagating an exception.
        if _owns(run, token):
            try:
                _save(run, state, phase="failed", error=str(exc), finished_at=_now())
            finally:
                (run / "attempt.lock").unlink()
        raise
    return run


def _launch(args, run: Path, token: str) -> None:
    if args.background:
        command = [sys.executable, "-u", "-m", "zotero_pdf_text.reconversion", str(run), token]
        if args.publish or args.publish_only:
            command.append("--publish")
        if args.publish_only:
            command.append("--publish-only")
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
                   if os.name == "nt" else {"start_new_session": True})
        with (run / "progress.log").open("a", encoding="utf-8") as log:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
    else:
        with (run / "progress.log").open("a", encoding="utf-8") as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                worker(run, publish=args.publish or args.publish_only, publish_only=args.publish_only, token=token)


def worker(run: Path, *, publish: bool, publish_only: bool, token: str | None = None) -> None:
    token = token or _reserve(run)
    if (run / "attempt.lock").read_text(encoding="utf-8") != token:
        raise ValueError("Worker does not own the attempt reservation")
    state = dict(run_id=run.name, attempt_id=token, hostname=platform.node(), pid=os.getpid(),
                 started_at=_now(), manifest_complete=False, phase="starting",
                 log_offset=(run / "progress.log").stat().st_size if (run / "progress.log").exists() else 0)
    stop = threading.Event()
    thread = None
    try:
        if publish_only:
            previous = json.loads((run / "job.json").read_text(encoding="utf-8"))
            if not previous.get("manifest_complete") or not (run / "manifest.csv").exists():
                raise ValueError("Publication requires a completed conversion manifest")
            state["manifest_complete"] = True
        _save(run, state)
        settings, config = _saved_config(run)
        snapshot = run / "config.snapshot.json"
        state.update(total=settings["total"],
                     phase="publishing" if publish_only else "converting")
        mutex = threading.Lock()

        def record(**fields):
            with mutex:
                _save(run, state, **fields)

        def heartbeat():
            while not stop.wait(15):
                record()

        record()
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            if not publish_only:
                with pipeline_write_lock(config.output_root, command="reconvert-library"):
                    convert_verified(config, run / "mapping_report.csv", output_dir=run,
                                     resume=True, canonical=False, workers=settings["workers"],
                                     timeout_seconds=settings["timeout_seconds"])
                record(manifest_complete=True)
            if publish:
                record(phase="publishing")
                from .cli import main

                result = main(["rebuild-index", "--config", str(snapshot), "--manifest",
                               str(run / "manifest.csv"), "--keep-current", "--refresh-zotero-metadata"])
                if result:
                    raise RuntimeError(f"Index publication failed (exit {result}); retry with --publish-only")
            record(phase="completed" if publish else "converted", finished_at=_now())
        finally:
            stop.set()
            thread.join()
    except BaseException as exc:
        _save(run, state, phase="failed", error=str(exc), finished_at=_now())
        traceback.print_exc()
        raise
    finally:
        stop.set()
        if thread is not None and thread.ident is not None:
            thread.join()
        (run / "attempt.lock").unlink()


if __name__ == "__main__":
    worker(Path(sys.argv[1]), token=sys.argv[2], publish="--publish" in sys.argv, publish_only="--publish-only" in sys.argv)
