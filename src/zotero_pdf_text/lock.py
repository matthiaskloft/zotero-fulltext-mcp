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

    This only serializes writers against each other; it does not protect a concurrent reader
    (e.g. a running MCP server executing a search) from observing a half-written file while a
    writer holds this lock. That guarantee comes from each writer using a temp-file-then-atomic-
    replace pattern (see `build_fts_index` in fts.py and `_atomic_write_text` in indexer.py), not
    from this lock.
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
    """Acquire the lock with an exclusive create; never silently replace an existing lock.

    ``O_CREAT | O_EXCL`` makes creation atomic, closing the check-then-write race two local
    processes could previously win simultaneously. A stale or corrupt lock file now fails
    loudly, naming the recorded holder, instead of being silently overwritten: on a cloud-synced
    output tree a lock that merely *looks* stale can belong to a machine whose sync is lagging,
    and clobbering it would let two writers collide. The user deletes the file manually once
    they have confirmed no other machine is mid-run.
    """
    payload = json.dumps(
        {
            "hostname": platform.node(),
            "pid": os.getpid(),
            "started_at": time.strftime(_TIMESTAMP_FORMAT),
            "command": command,
        },
        indent=2,
    )
    for _ in range(3):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_lock(lock_path)
            if existing is None:
                if not lock_path.exists():
                    # Holder released between our failed create and the read; retry the create.
                    continue
                raise PipelineLockedError(
                    f"{lock_path} exists but is unreadable or corrupt. Confirm no other machine "
                    "is running the pipeline, then delete the lock file manually before retrying."
                )
            holder = (
                f"host '{existing.get('hostname')}' (pid {existing.get('pid')}, "
                f"command '{existing.get('command')}', started {existing.get('started_at')})"
            )
            if _is_stale(existing):
                raise PipelineLockedError(
                    f"{lock_path} is held by {holder} and looks stale. Confirm that machine is "
                    "not actually running the pipeline, then delete the lock file manually "
                    "before retrying."
                )
            raise PipelineLockedError(
                f"{lock_path} is held by {holder}. If that machine isn't actually running the "
                "pipeline right now, delete the lock file manually before retrying."
            )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return
    raise PipelineLockedError(
        f"Could not acquire {lock_path}: another process kept creating and releasing it."
    )


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
