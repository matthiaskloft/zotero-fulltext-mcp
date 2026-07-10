from __future__ import annotations

import contextlib
import json
import os
import platform
import time
from pathlib import Path
from typing import Iterator

LOCK_FILENAME = ".pipeline.lock"
STALE_AFTER_SECONDS = 6 * 60 * 60
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


class PipelineLockedError(RuntimeError):
    """Raised when another machine's lock file is still fresh."""


@contextlib.contextmanager
def pipeline_write_lock(root: Path, *, command: str = "") -> Iterator[Path]:
    """Serialize writes to a Nextcloud-shared output tree across machines.

    Two machines rebuilding the same synced SQLite/JSONL files at once is the same
    corruption class as syncing a live Zotero database — this raises before either
    machine's write can collide with the other's.
    """
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_FILENAME
    _acquire(lock_path, command)
    try:
        yield lock_path
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def _acquire(lock_path: Path, command: str) -> None:
    existing = _read_lock(lock_path)
    if existing is not None and not _is_stale(existing):
        raise PipelineLockedError(
            f"{lock_path} is held by host '{existing.get('hostname')}' "
            f"(pid {existing.get('pid')}, command '{existing.get('command')}', "
            f"started {existing.get('started_at')}). If that machine isn't actually running "
            "the pipeline right now, delete the lock file manually before retrying."
        )
    if existing is not None:
        print(
            f"Warning: ignoring stale lock at {lock_path} from host '{existing.get('hostname')}' "
            f"(started {existing.get('started_at')})."
        )
    payload = {
        "hostname": platform.node(),
        "pid": os.getpid(),
        "started_at": time.strftime(_TIMESTAMP_FORMAT),
        "command": command,
    }
    lock_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_lock(lock_path: Path) -> dict | None:
    if not lock_path.exists():
        return None
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _is_stale(payload: dict) -> bool:
    started_at = payload.get("started_at")
    if not started_at:
        return True
    try:
        started = time.mktime(time.strptime(started_at, _TIMESTAMP_FORMAT))
    except ValueError:
        return True
    return (time.time() - started) > STALE_AFTER_SECONDS
