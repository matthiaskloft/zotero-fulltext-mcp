"""Where things live under ``output_root``: the single definition of its top-level folders.

    library/   the converted papers, one folder per Zotero attachment (see ``library.py``)
    index/     the search index: ``current.json`` names the live folder in ``versions/``
    runs/      one folder per command run, named ``<date>_<time>_<command>``
    review/    lists waiting for a person's decision (timeouts, orphan PDFs, the skip list)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

RUNS_DIRNAME = "runs"
REVIEW_DIRNAME = "review"
INDEX_DIRNAME = "index"


def runs_root(output_root: Path) -> Path:
    return output_root / RUNS_DIRNAME


def review_root(output_root: Path) -> Path:
    return output_root / REVIEW_DIRNAME


def run_name(command: str, *, detail: str = "", now: datetime | None = None) -> str:
    """``2026-10-10_141500_convert-verified``, plus ``_<detail>`` when given.

    Date first so a folder listing is chronological; the command so the name says what ran.
    """
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    return f"{stamp}_{command}" + (f"_{detail}" if detail else "")


def new_run_dir(output_root: Path, command: str, *, detail: str = "") -> Path:
    """A run folder path no earlier run has used. Not created here.

    A second run of the same command within the same second gets ``-2``, ``-3``, ... rather than
    sharing a folder with the first.
    """
    base = runs_root(output_root) / run_name(command, detail=detail)
    candidate, number = base, 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{number}")
        number += 1
    return candidate
