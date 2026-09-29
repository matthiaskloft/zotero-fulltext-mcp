"""Read-only checks that gate a Zotero write (duplicate DOI, existing PDF).

A wrong "nothing there" from one of these can cause a duplicate item or a second PDF copy, so the
answer must come from a source that sees recent commits, and an unanswered check must never look
like an empty one:

1. Live, through debug-bridge (`Zotero.DB.queryAsync`: sees the WAL, transactionally consistent).
2. Otherwise a hash-verified copy of `zotero.sqlite` (`zotero_db.snapshot_for_reading`), reported
   with ``live: False`` so the caller can say so.
3. Otherwise `PreWriteCheckUnavailable`: fail closed, never report "no duplicate"/"no PDF".

A write landing between the check and the import is still a race on every path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .bibtex import (
    DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    query_doi_rows_live,
    query_pdf_attachment_rows_live,
)
from .zotero_db import (
    SnapshotUnsafeError,
    SnapshotUnstableError,
    check_pdf_attachment,
    find_item_by_doi,
    match_doi_rows,
    pdf_attachment_result,
)

SOURCE_DEBUG_BRIDGE = "debug_bridge"
SOURCE_DB_COPY = "zotero_db_copy"


class PreWriteCheckUnavailable(RuntimeError):
    """Neither the live bridge nor a verified copy of the database could answer the check."""


def _copy_failure(bridge_error: str, exc: Exception) -> PreWriteCheckUnavailable:
    return PreWriteCheckUnavailable(
        f"debug-bridge unavailable ({bridge_error}) and no consistent copy of zotero.sqlite "
        f"could be read ({exc})"
    )


def check_doi_duplicate(
    doi: str,
    zotero_sqlite: Path,
    *,
    debug_bridge_endpoint: str = DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    debug_bridge_token: str = "",
) -> dict[str, object]:
    """Find an existing item with this DOI: ``{"key", "source", "live"[, "bridge_error"]}``.

    ``key`` is None only when the DOI was really checked and is absent.
    """
    live = query_doi_rows_live(debug_bridge_endpoint=debug_bridge_endpoint, debug_bridge_token=debug_bridge_token)
    if live.ok:
        return {"key": match_doi_rows(live.rows, doi), "source": SOURCE_DEBUG_BRIDGE, "live": True}
    try:
        key = find_item_by_doi(doi, zotero_sqlite)
    except (SnapshotUnstableError, SnapshotUnsafeError, sqlite3.Error, OSError) as exc:
        raise _copy_failure(live.error, exc) from exc
    return {"key": key, "source": SOURCE_DB_COPY, "live": False, "bridge_error": live.error}


def check_existing_pdf(
    parent_key: str,
    zotero_sqlite: Path,
    *,
    debug_bridge_endpoint: str = DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    debug_bridge_token: str = "",
) -> dict[str, object]:
    """PDF attachments of an item, in the `check_pdf_attachment` shape plus ``source``/``live``.

    ``bridge_error`` is added only when the live read failed and a verified copy answered.
    """
    live = query_pdf_attachment_rows_live(
        parent_key, debug_bridge_endpoint=debug_bridge_endpoint, debug_bridge_token=debug_bridge_token
    )
    if live.ok:
        return {**pdf_attachment_result(parent_key, live.rows), "source": SOURCE_DEBUG_BRIDGE, "live": True}
    try:
        copy_result = check_pdf_attachment(parent_key, zotero_sqlite)
    except (SnapshotUnstableError, SnapshotUnsafeError, sqlite3.Error, OSError) as exc:
        raise _copy_failure(live.error, exc) from exc
    return {**copy_result, "source": SOURCE_DB_COPY, "live": False, "bridge_error": live.error}
