from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path


def replace_with_retry(src: Path, dst: Path, *, attempts: int = 5, initial_delay: float = 0.05) -> None:
    """os.replace with short retries against a transient Windows PermissionError.

    Windows can raise PermissionError if another process (e.g. a concurrently running search
    query) has `dst` open at the exact instant of rename; POSIX allows renaming over an open file
    unconditionally, so this only matters on Windows. Callers only ever hold `dst` open briefly
    per query (open/execute/close, never held across requests), so a short retry resolves the
    collision without weakening the atomicity guarantee -- the destination is still replaced in
    one step whenever a retry succeeds.
    """
    delay = initial_delay
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to a hidden sibling temp file, then replace ``path`` in one step.

    The temp name starts with a dot and ends in ``.tmp-<pid>``, so an interrupted write is never
    mistaken for a completed file; the destination keeps its previous content until the replace
    succeeds.
    """
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8", newline="\n")
        replace_with_retry(tmp_path, path)
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
