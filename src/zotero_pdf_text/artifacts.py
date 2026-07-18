"""Transactional derived-index artifacts: immutable generations plus an atomic pointer.

Derived full-text output (the JSONL sidecar and its SQLite FTS database) is published as an
immutable *generation* directory under ``<index_root>/generations/<generation-id>/`` containing
``index.jsonl``, ``index.sqlite``, and an ``artifact_manifest.json`` with checksums and build
parameters. ``<index_root>/current.json`` is the sole publication pointer: it names exactly one
validated generation (plus the prior one retained for rollback) and is only ever replaced
atomically. Readers resolve the pointer and open that generation's SQLite database; a failed or
interrupted build therefore can never take the previously published index offline.

The pointer stores only a strictly validated relative generation ID, never a filesystem path, and
every reader containment-checks the resolved directory beneath ``generations/`` before opening
SQLite. Writers record a small publish journal immediately before swapping the pointer so an
interruption between "generation validated" and "pointer replaced" is recovered deterministically
by the next writer run (readers never attempt recovery).

This module stays path-agnostic about converted Markdown: generations snapshot the JSONL/SQLite
pair from wherever the Markdown currently lives. Canonical Markdown publication is a separate,
deliberately deferred piece of work (Package 3 in plan-mcp-server-hardening.md).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ._atomic import replace_with_retry
from .fts import DEFAULT_CHUNK_CHARS, DEFAULT_OVERLAP_CHARS, FtsBuildSummary, build_fts_index
from .indexer import TextIndexRecord, _converted_rows, _record_from_manifest_row

ARTIFACT_SCHEMA_VERSION = 1
GENERATIONS_DIRNAME = "generations"
CURRENT_POINTER_FILENAME = "current.json"
JOURNAL_FILENAME = "publish_journal.json"
GENERATION_JSONL_FILENAME = "index.jsonl"
GENERATION_DB_FILENAME = "index.sqlite"
GENERATION_MANIFEST_FILENAME = "artifact_manifest.json"

# Generation IDs are the only value current.json may name and are interpolated into filesystem
# paths, so the accepted alphabet is deliberately narrow: a UTC timestamp plus a short random
# suffix, nothing that could encode a path separator, drive letter, or parent-directory step.
_GENERATION_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


class ArtifactError(RuntimeError):
    """Base class for managed-artifact failures with user-actionable messages."""


class GenerationValidationError(ArtifactError):
    """A staged or recorded generation is incomplete, corrupt, or fails its checksums."""


class CurrentPointerError(ArtifactError):
    """current.json exists but is unreadable, malformed, or names an invalid generation."""


class ManagedIndexMissingError(ArtifactError):
    """No current.json exists next to the requested database path: no index is published here."""


@dataclass(frozen=True)
class IndexPaths:
    """Resolved locations of the managed layout beneath one index root."""

    index_root: Path

    @property
    def generations_dir(self) -> Path:
        return self.index_root / GENERATIONS_DIRNAME

    @property
    def pointer_path(self) -> Path:
        return self.index_root / CURRENT_POINTER_FILENAME

    @property
    def journal_path(self) -> Path:
        return self.index_root / JOURNAL_FILENAME

    def generation_dir(self, generation_id: str) -> Path:
        return resolve_generation_dir(self.index_root, generation_id)


@dataclass(frozen=True)
class GenerationInfo:
    generation_id: str
    directory: Path
    jsonl_path: Path
    db_path: Path
    manifest_path: Path
    summary: FtsBuildSummary


def new_generation_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def is_valid_generation_id(generation_id: object) -> bool:
    return isinstance(generation_id, str) and bool(_GENERATION_ID_PATTERN.fullmatch(generation_id))


def resolve_generation_dir(index_root: Path, generation_id: str) -> Path:
    """Validate a generation ID and containment-check its directory beneath generations/.

    The strict ID pattern already excludes separators and traversal, but the resolved-parent
    check below keeps holding even if the pattern is ever loosened, and it also rejects a
    ``generations`` entry that is itself a symlink escaping the index root.

    The containment check compares fully resolved paths, but the *returned* path keeps the
    caller's original (unresolved) spelling: ``.resolve()`` also rewrites harmless
    platform indirections — macOS's ``/var`` -> ``/private/var`` symlink, Windows 8.3 short
    names like ``RUNNER~1`` — which would make the result no longer comparable to sibling
    paths derived from the same ``index_root``.
    """
    if not is_valid_generation_id(generation_id):
        raise CurrentPointerError(
            f"'{generation_id}' is not a valid index generation ID; refusing to resolve it. "
            "If current.json was edited or corrupted, re-publish with 'zotero-pdf-text rebuild-index'."
        )
    generations_dir = (index_root / GENERATIONS_DIRNAME).resolve()
    candidate = index_root / GENERATIONS_DIRNAME / generation_id
    if candidate.resolve().parent != generations_dir:
        raise CurrentPointerError(
            f"Generation '{generation_id}' resolves outside the managed generations directory; "
            "refusing to open it."
        )
    return candidate


def read_current_pointer(index_root: Path) -> dict[str, object] | None:
    """Return the parsed current.json pointer, or None when the layout is legacy/unmanaged.

    A pointer that exists but cannot be parsed or validated raises CurrentPointerError rather
    than silently falling back to a legacy database: a corrupt pointer means the managed layout
    is in an unknown state, and quietly serving older data would hide that.
    """
    paths = IndexPaths(index_root)
    if not paths.pointer_path.exists():
        return None
    try:
        data = json.loads(paths.pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CurrentPointerError(
            f"{paths.pointer_path} is unreadable or not valid JSON ({exc}). "
            "Re-publish with 'zotero-pdf-text rebuild-index' to restore a valid pointer."
        ) from exc
    if not isinstance(data, dict) or not is_valid_generation_id(data.get("current_generation")):
        raise CurrentPointerError(
            f"{paths.pointer_path} does not name a valid current generation. "
            "Re-publish with 'zotero-pdf-text rebuild-index' to restore a valid pointer."
        )
    previous = data.get("previous_generation")
    if previous is not None and not is_valid_generation_id(previous):
        raise CurrentPointerError(
            f"{paths.pointer_path} names an invalid previous generation. "
            "Re-publish with 'zotero-pdf-text rebuild-index' to restore a valid pointer."
        )
    return data


def resolve_reader_db_path(db_path: Path) -> Path:
    """Resolve the SQLite file a reader should open for a configured/registered DB path.

    The managed ``current.json`` next to ``db_path`` is the only way an index is located: the
    ``--db`` argument is effectively an index-root anchor whose sibling pointer names the current
    generation. There is no legacy standalone-database fallback — a missing pointer means no
    index is published here, and the caller gets a clear error naming ``rebuild-index`` instead
    of silently opening whatever file happens to sit at ``db_path``.
    """
    index_root = db_path.parent
    pointer = read_current_pointer(index_root)
    if pointer is None:
        raise ManagedIndexMissingError(
            f"No managed index generation is published under {index_root} (no current.json). "
            "Publish one with 'zotero-pdf-text rebuild-index'."
        )
    generation_dir = resolve_generation_dir(index_root, str(pointer["current_generation"]))
    resolved = generation_dir / GENERATION_DB_FILENAME
    if not resolved.is_file():
        raise CurrentPointerError(
            f"current.json names generation '{pointer['current_generation']}' but its database "
            "file is missing. Re-publish with 'zotero-pdf-text rebuild-index'."
        )
    return resolved


def current_generation_jsonl(index_root: Path) -> Path | None:
    """Return the current generation's JSONL path, or None when the layout is legacy."""
    pointer = read_current_pointer(index_root)
    if pointer is None:
        return None
    generation_dir = resolve_generation_dir(index_root, str(pointer["current_generation"]))
    jsonl_path = generation_dir / GENERATION_JSONL_FILENAME
    if not jsonl_path.is_file():
        raise CurrentPointerError(
            f"current.json names generation '{pointer['current_generation']}' but its JSONL "
            "file is missing. Re-publish with 'zotero-pdf-text rebuild-index'."
        )
    return jsonl_path


def read_generation_manifest(index_root: Path, generation_id: str) -> dict[str, object]:
    manifest_path = resolve_generation_dir(index_root, generation_id) / GENERATION_MANIFEST_FILENAME
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationValidationError(
            f"Generation '{generation_id}' has no readable artifact manifest ({exc})."
        ) from exc
    if not isinstance(data, dict):
        raise GenerationValidationError(f"Generation '{generation_id}' has a malformed artifact manifest.")
    return data


def stage_generation(
    index_root: Path,
    write_jsonl: Callable[[Path], None],
    *,
    command: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> GenerationInfo:
    """Stage one complete generation: JSONL, SQLite FTS build, and artifact manifest.

    ``write_jsonl`` receives the staged JSONL path and must write the complete sidecar content
    there. Any failure removes the partially staged directory; nothing outside the new
    generation directory is touched, so the currently published generation is never at risk.
    Callers must already hold the pipeline write lock for the tree containing ``index_root``.
    """
    generation_id = new_generation_id()
    generation_dir = index_root / GENERATIONS_DIRNAME / generation_id
    generation_dir.mkdir(parents=True, exist_ok=False)
    try:
        jsonl_path = generation_dir / GENERATION_JSONL_FILENAME
        write_jsonl(jsonl_path)
        db_path = generation_dir / GENERATION_DB_FILENAME
        summary = build_fts_index(jsonl_path, db_path, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
        manifest = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "generation_id": generation_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "command": command,
            "chunk_chars": chunk_chars,
            "overlap_chars": overlap_chars,
            "records": summary.records,
            "chunks": summary.chunks,
            "total_chars": summary.total_chars,
            "total_words": summary.total_words,
            "files": {
                GENERATION_JSONL_FILENAME: _file_stamp(jsonl_path),
                GENERATION_DB_FILENAME: _file_stamp(db_path),
            },
        }
        manifest_path = generation_dir / GENERATION_MANIFEST_FILENAME
        _atomic_write_json(manifest_path, manifest)
    except BaseException:
        shutil.rmtree(generation_dir, ignore_errors=True)
        raise
    return GenerationInfo(
        generation_id=generation_id,
        directory=generation_dir,
        jsonl_path=jsonl_path,
        db_path=db_path,
        manifest_path=manifest_path,
        summary=summary,
    )


def validate_generation(index_root: Path, generation_id: str) -> dict[str, object]:
    """Verify a generation is complete: files present, checksums match, DB counts match.

    Returns the parsed artifact manifest on success. This runs before every pointer publication
    and during writer recovery, so only a generation that fully round-trips its recorded state
    can ever become current.
    """
    generation_dir = resolve_generation_dir(index_root, generation_id)
    manifest = read_generation_manifest(index_root, generation_id)
    if manifest.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise GenerationValidationError(
            f"Generation '{generation_id}' has artifact schema "
            f"{manifest.get('schema_version')!r}; this build supports {ARTIFACT_SCHEMA_VERSION}. "
            "Re-publish with 'zotero-pdf-text rebuild-index'."
        )
    if manifest.get("generation_id") != generation_id:
        raise GenerationValidationError(
            f"Generation directory '{generation_id}' contains a manifest for "
            f"{manifest.get('generation_id')!r}."
        )
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise GenerationValidationError(f"Generation '{generation_id}' manifest lists no files.")
    for filename in (GENERATION_JSONL_FILENAME, GENERATION_DB_FILENAME):
        expected = files.get(filename)
        actual_path = generation_dir / filename
        if not isinstance(expected, dict) or not actual_path.is_file():
            raise GenerationValidationError(
                f"Generation '{generation_id}' is missing {filename} or its recorded checksum."
            )
        actual = _file_stamp(actual_path)
        if actual != expected:
            raise GenerationValidationError(
                f"Generation '{generation_id}': {filename} does not match its recorded checksum; "
                "the generation is corrupt or was modified after staging."
            )
    db_path = generation_dir / GENERATION_DB_FILENAME
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        records = con.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
        chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise GenerationValidationError(
            f"Generation '{generation_id}': database is unreadable ({exc})."
        ) from exc
    finally:
        con.close()
    if records != manifest.get("records") or chunks != manifest.get("chunks"):
        raise GenerationValidationError(
            f"Generation '{generation_id}': database contains {records} records/{chunks} chunks "
            f"but the manifest records {manifest.get('records')}/{manifest.get('chunks')}."
        )
    return manifest


def publish_generation(index_root: Path, generation_id: str) -> dict[str, object]:
    """Atomically make a validated generation current; returns the new pointer content.

    The journal written immediately before the pointer swap is what makes an interruption here
    deterministic: recovery either completes this exact publication or leaves the prior pointer
    untouched. Callers must hold the pipeline write lock. After a successful swap, generations
    no longer referenced by the pointer are swept, keeping current + previous for rollback.

    The pointer swap is the non-rollbackable commit point: once it succeeds, this function
    returns success no matter what post-commit cleanup does. A journal-unlink or retention-sweep
    failure must not escape as an exception, because callers (e.g. math reconversion) treat an
    exception as "publication failed" and roll back their source artifacts while readers already
    resolve the new generation. A leftover journal that names the now-current generation is
    harmless — the next writer's recovery pass simply deletes it.
    """
    paths = IndexPaths(index_root)
    validate_generation(index_root, generation_id)
    try:
        previous_pointer = read_current_pointer(index_root)
    except CurrentPointerError:
        # Publishing a freshly validated generation is the documented recovery path for a
        # corrupt pointer, so an unreadable prior pointer must not block it. The prior current
        # generation cannot be preserved as rollback state because the corrupt pointer no longer
        # says which generation that was.
        previous_pointer = None
    journal = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "state": "publishing",
        "generation_id": generation_id,
        "previous_pointer": previous_pointer,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_write_json(paths.journal_path, journal)
    pointer = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "current_generation": generation_id,
        "previous_generation": (previous_pointer or {}).get("current_generation"),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_write_json(paths.pointer_path, pointer)
    _post_commit_cleanup(paths, keep={generation_id, pointer.get("previous_generation")})
    return pointer


def _post_commit_cleanup(paths: IndexPaths, keep: set[object]) -> None:
    """Best-effort journal removal and retention sweep after a successful pointer swap.

    Nothing here may raise: the publication is already committed, and an escaping exception
    would make callers believe it failed. Failures leave at most a stale journal (cleaned by the
    next recovery pass) or an extra retained generation directory (swept on the next publish).
    """
    with contextlib.suppress(OSError):
        paths.journal_path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        _sweep_unreferenced_generations(paths, keep=keep)


def recover_pending_publication(index_root: Path) -> str | None:
    """Deterministically finish or roll back an interrupted publication; writers only.

    Returns a short description of the action taken, or None when there was nothing to recover.
    Callers must hold the pipeline write lock. Readers never call this: until the pointer is
    replaced they keep resolving the prior complete generation, which is always safe.
    """
    paths = IndexPaths(index_root)
    if not paths.journal_path.exists():
        _sweep_incomplete_stagings(paths)
        return None
    try:
        journal = json.loads(paths.journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(
            f"{paths.journal_path} is unreadable or not valid JSON ({exc}); refusing to guess at "
            "recovery. Inspect the file, then delete it and re-publish with "
            "'zotero-pdf-text rebuild-index'."
        ) from exc
    generation_id = journal.get("generation_id") if isinstance(journal, dict) else None
    if not is_valid_generation_id(generation_id):
        raise ArtifactError(
            f"{paths.journal_path} does not name a valid generation; refusing to guess at "
            "recovery. Inspect the file, then delete it and re-publish with "
            "'zotero-pdf-text rebuild-index'."
        )
    generation_id = str(generation_id)
    try:
        pointer = read_current_pointer(index_root)
    except CurrentPointerError:
        pointer = None
    if pointer is not None and pointer.get("current_generation") == generation_id:
        # The swap completed; only the journal cleanup was interrupted.
        _post_commit_cleanup(paths, keep={generation_id, pointer.get("previous_generation")})
        return f"completed interrupted publication of generation '{generation_id}'"
    try:
        validate_generation(index_root, generation_id)
    except (GenerationValidationError, CurrentPointerError):
        # The staged generation never became valid; the pointer was never replaced (the swap is
        # atomic), so dropping the journal and the partial staging restores the prior state.
        generation_dir = paths.generations_dir / generation_id
        if is_valid_generation_id(generation_id):
            shutil.rmtree(generation_dir, ignore_errors=True)
        paths.journal_path.unlink(missing_ok=True)
        _sweep_incomplete_stagings(paths)
        return f"rolled back incomplete publication of generation '{generation_id}'"
    pointer = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "current_generation": generation_id,
        "previous_generation": ((journal.get("previous_pointer") or {}) or {}).get("current_generation"),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_write_json(paths.pointer_path, pointer)
    _post_commit_cleanup(paths, keep={generation_id, pointer.get("previous_generation")})
    return f"completed interrupted publication of generation '{generation_id}'"


def stage_and_publish(
    index_root: Path,
    write_jsonl: Callable[[Path], None],
    *,
    command: str,
    chunk_chars: int | None = None,
    overlap_chars: int | None = None,
) -> GenerationInfo:
    """Recover any pending publication, then stage, validate, and publish one generation.

    This is the single write path every publishing command goes through. Chunking parameters
    default to the current generation's recorded values so successor generations preserve the
    chunking the library was deliberately built with. Callers must hold the pipeline write lock
    for the tree containing ``index_root``.
    """
    recover_pending_publication(index_root)
    if chunk_chars is None or overlap_chars is None:
        default_chunk, default_overlap = chunking_params_from_current(index_root)
        chunk_chars = default_chunk if chunk_chars is None else chunk_chars
        overlap_chars = default_overlap if overlap_chars is None else overlap_chars
    info = stage_generation(
        index_root, write_jsonl, command=command, chunk_chars=chunk_chars, overlap_chars=overlap_chars
    )
    publish_generation(index_root, info.generation_id)
    return info


# ---------------------------------------------------------------------------
# High-level staging flows used by the managed CLI command family.
# ---------------------------------------------------------------------------


def write_jsonl_from_conversion_manifest(manifest_csv: Path) -> Callable[[Path], None]:
    """Return a stage_generation writer that builds the JSONL from a conversion manifest."""
    if not manifest_csv.exists():
        raise FileNotFoundError(manifest_csv)

    def _write(jsonl_path: Path) -> None:
        with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in _converted_rows(manifest_csv):
                record = _record_from_manifest_row(row)
                handle.write(json.dumps(_record_dict(record), ensure_ascii=False) + "\n")

    return _write


def write_jsonl_from_existing(jsonl_source: Path) -> Callable[[Path], None]:
    """Return a stage_generation writer that copies an existing JSONL sidecar.

    This is the one-time migration path from the legacy standalone layout: the cumulative
    ``zotero_text_index.jsonl`` is not reproducible from any single conversion manifest, so the
    first managed generation snapshots it as-is.
    """
    if not jsonl_source.exists():
        raise FileNotFoundError(jsonl_source)

    def _write(jsonl_path: Path) -> None:
        shutil.copyfile(jsonl_source, jsonl_path)

    return _write


def write_jsonl_appending_manifest(
    current_jsonl: Path, manifest_csv: Path
) -> tuple[Callable[[Path], None], int]:
    """Writer that appends a conversion manifest's new records to the current JSONL.

    Returns the writer plus the number of records that will actually be appended (rows whose
    attachment key is already indexed are skipped, matching the long-standing append semantics).
    """
    if not manifest_csv.exists():
        raise FileNotFoundError(manifest_csv)
    existing_keys = _jsonl_attachment_keys(current_jsonl)
    new_rows = [
        row
        for row in _converted_rows(manifest_csv)
        if row.get("zotero_attachment_key", "") not in existing_keys
    ]

    def _write(jsonl_path: Path) -> None:
        with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
            content = current_jsonl.read_text(encoding="utf-8")
            if content:
                handle.write(content)
                if not content.endswith("\n"):
                    handle.write("\n")
            for row in new_rows:
                record = _record_from_manifest_row(row)
                handle.write(json.dumps(_record_dict(record), ensure_ascii=False) + "\n")

    return _write, len(new_rows)


def write_jsonl_upserting_record(
    current_jsonl: Path, attachment_key: str, new_record: TextIndexRecord
) -> Callable[[Path], None]:
    """Writer that replaces (or appends) one attachment's record in the current JSONL."""

    def _write(jsonl_path: Path) -> None:
        # Split on the literal "\n" the writer uses, not str.splitlines() -- splitlines() also
        # breaks on Unicode line/paragraph separators (U+2028/U+2029), which can legitimately
        # appear inside a record's extracted text and would otherwise fragment that JSON line.
        content = current_jsonl.read_text(encoding="utf-8")
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        replaced = False
        with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                if line.strip():
                    record = json.loads(line)
                    if isinstance(record, dict) and record.get("zotero_attachment_key") == attachment_key:
                        handle.write(json.dumps(_record_dict(new_record), ensure_ascii=False) + "\n")
                        replaced = True
                        continue
                handle.write(line + "\n")
            if not replaced:
                handle.write(json.dumps(_record_dict(new_record), ensure_ascii=False) + "\n")

    return _write


def chunking_params_from_current(index_root: Path) -> tuple[int, int]:
    """Read chunking parameters from the current generation's manifest, else the defaults.

    Successor generations must preserve the chunking a library was deliberately built with;
    falling back to the defaults only happens when no managed generation exists yet.
    """
    pointer = read_current_pointer(index_root)
    if pointer is None:
        return DEFAULT_CHUNK_CHARS, DEFAULT_OVERLAP_CHARS
    manifest = read_generation_manifest(index_root, str(pointer["current_generation"]))
    chunk_chars = manifest.get("chunk_chars")
    overlap_chars = manifest.get("overlap_chars")
    if isinstance(chunk_chars, int) and isinstance(overlap_chars, int):
        return chunk_chars, overlap_chars
    return DEFAULT_CHUNK_CHARS, DEFAULT_OVERLAP_CHARS


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _record_dict(record: TextIndexRecord) -> dict[str, object]:
    return asdict(record)


def _jsonl_attachment_keys(jsonl_path: Path) -> set[str]:
    keys: set[str] = set()
    if not jsonl_path.exists():
        return keys
    with jsonl_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if isinstance(record, dict):
                key = record.get("zotero_attachment_key", "")
                if key:
                    keys.add(str(key))
    return keys


def _file_stamp(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"sha256": digest.hexdigest(), "bytes": size}


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        replace_with_retry(tmp_path, path)
    finally:
        # Suppress cleanup failures so they never shadow a real exception from the write/replace.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def _sweep_unreferenced_generations(paths: IndexPaths, keep: set[object]) -> None:
    """Delete generation directories not referenced by the pointer being published.

    Retention is deliberately bounded at current + previous: each generation holds a complete
    JSONL/SQLite copy of the library text, so unbounded history would silently multiply disk
    use on every publish. Sweeping only runs after a successful atomic pointer swap.
    """
    if not paths.generations_dir.is_dir():
        return
    for entry in paths.generations_dir.iterdir():
        if entry.name in keep:
            continue
        if not is_valid_generation_id(entry.name):
            # Not something this module created; leave unknown files/directories alone.
            continue
        shutil.rmtree(entry, ignore_errors=True)


def _sweep_incomplete_stagings(paths: IndexPaths) -> None:
    """Delete crash-orphaned staging directories that never completed their manifest.

    Only directories with a valid generation-ID name, no artifact manifest, and no reference
    from the current pointer are removed. Runs only under the writer lock during recovery.
    """
    if not paths.generations_dir.is_dir():
        return
    try:
        pointer = read_current_pointer(paths.index_root)
    except CurrentPointerError:
        return
    referenced = set()
    if pointer is not None:
        referenced = {pointer.get("current_generation"), pointer.get("previous_generation")}
    for entry in paths.generations_dir.iterdir():
        if entry.name in referenced or not is_valid_generation_id(entry.name):
            continue
        if not (entry / GENERATION_MANIFEST_FILENAME).is_file():
            shutil.rmtree(entry, ignore_errors=True)
