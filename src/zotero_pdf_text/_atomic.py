from __future__ import annotations

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
