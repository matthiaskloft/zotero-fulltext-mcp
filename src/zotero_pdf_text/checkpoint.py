"""Append-only, crash-tolerant ledger of completed conversions inside one run directory.

A conversion run writes its manifest only after every selected row has finished. The checkpoint
records each completed row as it finishes, so an interrupted run can be resumed without
re-extracting validated completions and without losing the source hash recorded at extraction
time. Only a row whose Markdown has already been published (atomically) is recorded, so an entry
never certifies a partially written file.

The file is JSON Lines: one self-contained entry per line, appended with flush + fsync. A crash
can leave at most a torn final line, which :func:`read_checkpoint` skips. When the same output is
recorded more than once, the last valid entry wins.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

CHECKPOINT_FILENAME = "conversion_checkpoint.jsonl"
CHECKPOINT_VERSION = 1

Verdict = Literal["reuse", "foreign", "output_changed"]


@dataclass(frozen=True)
class CheckpointEntry:
    """One completed conversion, as it stood when its Markdown was published.

    ``output`` is the Markdown path relative to the run directory (POSIX separators), so a run
    directory synced to another machine still matches. ``source_size``/``source_mtime_ns`` and
    ``source_sha256`` describe the source PDF at extraction time and are never refreshed later.
    ``result`` is the conversion manifest row recorded for this output.
    """

    output: str
    zotero_attachment_key: str
    source_path: str
    source_size: int | None
    source_mtime_ns: int | None
    source_sha256: str
    output_sha256: str
    recorded_at: str
    result: dict[str, str]

    def to_json(self) -> str:
        return json.dumps({"checkpoint_version": CHECKPOINT_VERSION, **asdict(self)}, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Any) -> CheckpointEntry | None:
        if not isinstance(data, dict) or data.get("checkpoint_version") != CHECKPOINT_VERSION:
            return None
        text_fields = ("output", "zotero_attachment_key", "source_path", "source_sha256", "output_sha256", "recorded_at")
        if not all(isinstance(data.get(name), str) for name in text_fields):
            return None
        if not data["output"] or not data["source_sha256"] or not data["output_sha256"]:
            return None
        result = data.get("result")
        if not isinstance(result, dict) or not all(isinstance(value, str) for value in result.values()):
            return None
        size, mtime = data.get("source_size"), data.get("source_mtime_ns")
        if not all(value is None or (isinstance(value, int) and not isinstance(value, bool)) for value in (size, mtime)):
            return None
        return cls(
            output=data["output"],
            zotero_attachment_key=data["zotero_attachment_key"],
            source_path=data["source_path"],
            source_size=size,
            source_mtime_ns=mtime,
            source_sha256=data["source_sha256"],
            output_sha256=data["output_sha256"],
            recorded_at=data["recorded_at"],
            result=dict(result),
        )


def read_checkpoint(path: Path) -> dict[str, CheckpointEntry]:
    """Return the last valid entry per output. Torn, malformed, or foreign lines are skipped."""
    entries: dict[str, CheckpointEntry] = {}
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return entries
    for line in raw.splitlines():
        try:
            entry = CheckpointEntry.from_dict(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            continue
        if entry is not None:
            entries[entry.output] = entry
    return entries


def classify_entry(
    entry: CheckpointEntry,
    *,
    attachment_key: str,
    source_path: str,
    output_sha256: str,
    current_source_sha256: str | None = None,
) -> Verdict:
    """Decide whether ``entry`` may stand in for re-extracting this row's existing Markdown.

    - ``foreign``: the entry was recorded for a different attachment or a different source at
      this output path, so the existing Markdown does not belong to this row.
    - ``output_changed``: the Markdown was modified after it was recorded; the entry no longer
      describes it, so its provenance is unknown.
    - ``reuse``: same attachment, same source, byte-identical Markdown.

    The source counts as the same when its path matches, or -- when the path differs, e.g. a run
    directory resumed from another machine -- when ``current_source_sha256`` equals the recorded
    extraction-time hash. The current hash is only ever compared, never recorded.
    """
    if entry.zotero_attachment_key != attachment_key:
        return "foreign"
    if entry.source_path != source_path and current_source_sha256 != entry.source_sha256:
        return "foreign"
    if entry.output_sha256 != output_sha256:
        return "output_changed"
    return "reuse"


class ConversionCheckpoint:
    """Thread-safe appender for a run directory's checkpoint file.

    Appends are serialized within this process by a lock; separate processes are kept apart by
    the pipeline write lock the CLI holds for every conversion command.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.path = run_dir / CHECKPOINT_FILENAME
        self.entries = read_checkpoint(self.path)
        self._lock = threading.Lock()
        self._needs_newline = _ends_without_newline(self.path)
        # Outputs reused in this invocation instead of re-extracted, mapped to whether the
        # source file's size/mtime differ from those recorded at extraction time.
        self.reused: dict[str, bool] = {}

    def mark_reused(self, output_path: Path, *, source_modified: bool) -> None:
        with self._lock:
            self.reused[self.relative(output_path)] = source_modified

    def relative(self, output_path: Path) -> str:
        return output_path.relative_to(self.run_dir).as_posix()

    def lookup(self, output_path: Path) -> CheckpointEntry | None:
        with self._lock:
            return self.entries.get(self.relative(output_path))

    def record(
        self,
        output_path: Path,
        *,
        result: dict[str, str],
        source_size: int | None,
        source_mtime_ns: int | None,
        source_sha256: str,
        output_sha256: str,
    ) -> CheckpointEntry:
        entry = CheckpointEntry(
            output=self.relative(output_path),
            zotero_attachment_key=result.get("zotero_attachment_key", ""),
            source_path=result.get("source_path", ""),
            source_size=source_size,
            source_mtime_ns=source_mtime_ns,
            source_sha256=source_sha256,
            output_sha256=output_sha256,
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            result=dict(result),
        )
        self._append(entry)
        return entry

    def amend_result(self, output_path: Path, result: dict[str, str]) -> None:
        """Re-record the latest entry for ``output_path`` with an updated manifest row.

        Used when a row's final result differs from the one recorded at completion (e.g. a note
        added by the native-crash retry pass) while the published Markdown is unchanged.
        """
        with self._lock:
            previous = self.entries.get(self.relative(output_path))
        if previous is None:
            return
        self._append(
            CheckpointEntry(
                **{**asdict(previous), "result": dict(result), "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            )
        )

    def _append(self, entry: CheckpointEntry) -> None:
        line = entry.to_json() + "\n"
        with self._lock:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                if self._needs_newline:
                    # Terminate a torn line left by an earlier crash so it cannot swallow this one.
                    handle.write("\n")
                    self._needs_newline = False
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self.entries[entry.output] = entry


def _ends_without_newline(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False
