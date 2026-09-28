from __future__ import annotations

import json
import re
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast


DEFAULT_BBT_ENDPOINT = "http://127.0.0.1:23119/better-bibtex/json-rpc"
DEFAULT_BBT_TRANSLATOR = "Better BibLaTeX"
DEFAULT_CONNECTOR_ENDPOINT = "http://127.0.0.1:23119"
DEFAULT_DEBUG_BRIDGE_ENDPOINT = "http://127.0.0.1:23119/debug-bridge/execute"
_DEBUG_BRIDGE_TOKEN_ENV = "ZOTERO_DEBUG_BRIDGE_TOKEN"


@dataclass
class JavaScriptResult:
    ok: bool
    result: object
    error: str
    endpoint: str
    # The request reached debug-bridge but no answer arrived in time: the script may have run
    # (or may still be running) in Zotero, so its outcome is unknown rather than failed.
    timed_out: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class BibtexExport:
    citation_keys: list[str]
    translator: str
    entry: str
    endpoint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class BibtexAppendResult:
    references_bib: str
    citation_keys: list[str]
    added_keys: list[str]
    skipped_existing_keys: list[str]
    translator: str
    endpoint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def export_bibtex_entries(
    citation_keys: list[str],
    *,
    translator: str = DEFAULT_BBT_TRANSLATOR,
    endpoint: str = DEFAULT_BBT_ENDPOINT,
    library_id: str | int | None = None,
    max_response_bytes: int | None = None,
) -> BibtexExport:
    keys = _clean_citation_keys(citation_keys)
    if not keys:
        raise ValueError("At least one citation key is required")
    params: list[Any] = [keys, translator]
    if library_id is not None:
        params.append(library_id)
    entry = _json_rpc(endpoint, "item.export", params, max_response_bytes=max_response_bytes)
    if not isinstance(entry, str):
        raise RuntimeError(f"Better BibTeX returned a non-string export: {type(entry).__name__}")
    return BibtexExport(citation_keys=keys, translator=translator, entry=entry.strip() + "\n", endpoint=endpoint)


def append_bibtex_entries(
    citation_keys: list[str],
    references_bib: Path,
    *,
    translator: str = DEFAULT_BBT_TRANSLATOR,
    endpoint: str = DEFAULT_BBT_ENDPOINT,
    library_id: str | int | None = None,
) -> BibtexAppendResult:
    keys = _clean_citation_keys(citation_keys)
    if not keys:
        raise ValueError("At least one citation key is required")
    existing_text = references_bib.read_text(encoding="utf-8") if references_bib.exists() else ""
    existing_keys = _bibtex_keys(existing_text)
    missing_keys = [key for key in keys if key not in existing_keys]
    skipped = [key for key in keys if key in existing_keys]

    if missing_keys:
        export = export_bibtex_entries(
            missing_keys,
            translator=translator,
            endpoint=endpoint,
            library_id=library_id,
        )
        references_bib.parent.mkdir(parents=True, exist_ok=True)
        with references_bib.open("a", encoding="utf-8", newline="\n") as handle:
            if existing_text and not existing_text.endswith("\n"):
                handle.write("\n")
            if existing_text.strip():
                handle.write("\n")
            handle.write(export.entry.rstrip() + "\n")

    return BibtexAppendResult(
        references_bib=str(references_bib),
        citation_keys=keys,
        added_keys=missing_keys,
        skipped_existing_keys=skipped,
        translator=translator,
        endpoint=endpoint,
    )


def check_better_bibtex(endpoint: str = DEFAULT_BBT_ENDPOINT) -> dict[str, object]:
    result = _json_rpc(endpoint, "api.ready", [])
    if not isinstance(result, dict):
        raise RuntimeError(f"Better BibTeX returned an unexpected readiness payload: {result!r}")
    return result


def execute_javascript(
    code: str,
    *,
    endpoint: str = DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    token: str = "",
    timeout: float = 30,
) -> JavaScriptResult:
    """Execute JavaScript inside Zotero via the debug-bridge plugin.

    Requires the debug-bridge XPI to be installed in Zotero and a Bearer token configured
    at extensions.zotero.debug-bridge.token in Zotero's Config Editor.
    Token can also be provided via the ZOTERO_DEBUG_BRIDGE_TOKEN environment variable.

    Returns JavaScriptResult with ok=False when the plugin is not installed or token is wrong,
    and additionally timed_out=True when no answer arrived within ``timeout`` seconds.
    """
    import os
    resolved_token = token or os.environ.get(_DEBUG_BRIDGE_TOKEN_ENV, "")
    headers: dict[str, str] = {"Content-Type": "text/plain"}
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    request = urllib.request.Request(
        endpoint,
        data=code.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            result = body
        return JavaScriptResult(ok=True, result=result, error="", endpoint=endpoint)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return JavaScriptResult(ok=False, result=None, error=f"HTTP {exc.code}: {error_body}", endpoint=endpoint)
    except urllib.error.URLError as exc:
        return JavaScriptResult(ok=False, result=None, error=f"debug-bridge unreachable: {exc}", endpoint=endpoint)
    except TimeoutError:
        # urllib wraps connect-phase failures (including timeouts) in URLError above, so a bare
        # TimeoutError means the request was sent and the answer never came.
        return JavaScriptResult(
            ok=False, result=None, endpoint=endpoint, timed_out=True,
            error=(
                f"debug-bridge did not answer within {timeout:g} s; the script may still be running "
                "in Zotero, so its outcome is unknown -- check Zotero before rerunning."
            ),
        )


# ---------------------------------------------------------------------------
# Zotero connector-based import
# ---------------------------------------------------------------------------


@dataclass
class ConnectorImportResult:
    ok: bool
    doi: str
    item_type: str
    title: str
    error: str
    connector_endpoint: str
    item_key: str = ""  # key of the created item, when the import path reports it (debug-bridge only)
    item_keys: list[str] = field(default_factory=list)  # every item the debug-bridge translator created
    outcome: str = ""  # "unknown" when a debug-bridge timeout left the import's result undetermined

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def find_item_key_via_connector(
    doi: str,
    title_hint: str = "",
    *,
    connector_endpoint: str = DEFAULT_CONNECTOR_ENDPOINT,
) -> str | None:
    """Search the Zotero local REST API for a newly imported item and return its key.

    Uses title_hint for the search query and filters results by DOI. This reads from
    Zotero's live in-process data (no WAL lag), making it suitable for post-import lookups.
    """
    from .identity import normalize_doi
    needle = normalize_doi(doi)
    query = (title_hint or doi).replace("/", " ")
    url = (
        f"{connector_endpoint}/api/users/0/items"
        f"?q={urllib.parse.quote(query[:100])}&limit=20"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            items = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    for item in items:
        item_doi = item.get("data", {}).get("DOI", "") or item.get("DOI", "")
        if normalize_doi(str(item_doi)) == needle:
            return str(item.get("key", ""))
    return None


def import_doi_via_connector(
    doi: str,
    *,
    connector_endpoint: str = DEFAULT_CONNECTOR_ENDPOINT,
    debug_bridge_endpoint: str = DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    debug_bridge_token: str = "",
) -> ConnectorImportResult:
    """Import a paper by DOI into Zotero.

    Strategy (tried in order):
    1. debug-bridge plugin: runs createItemsFromIdentifier(doi) inside Zotero using its own
       translators — the same lookup as "Add by Identifier". Requires the debug-bridge XPI
       and ZOTERO_DEBUG_BRIDGE_TOKEN env var (or debug_bridge_token kwarg).
    2. CrossRef/DataCite + /connector/saveItems: fetches external metadata and posts directly.
       Works without any plugins; metadata quality depends on CrossRef/DataCite.

    Returns ConnectorImportResult. Strategy 1 reports the created items' keys in item_keys and,
    when exactly one item was created, in item_key; strategy 2 leaves both empty, so the caller
    must poll to find the new item key. Strategy 2 runs only when debug-bridge is unavailable: a
    debug-bridge timeout returns outcome="unknown" instead, because the translator may still have
    created the item and a second import would duplicate it.
    """
    # Strategy 1: debug-bridge (Zotero's own translator lookup)
    # NOTE: return plain JS values, not JSON.stringify(...) -- debug-bridge already
    # serializes the return value once. Returning an already-stringified JSON string
    # here would get serialized a second time, producing a string that execute_javascript
    # cannot distinguish from a plain string result.
    js = f"""
var doi = {json.dumps(doi)};
var translate = new Zotero.Translate.Search();
translate.setIdentifier({{ DOI: doi }});
var translators = await translate.getTranslators();
if (!translators || translators.length === 0) {{
    return {{ error: 'no translators found for DOI' }};
}}
translate.setTranslator(translators);
var items = await translate.translate({{ libraryID: Zotero.Libraries.userLibraryID }});
if (items && items.length > 0) {{
    var item = items[0];
    return {{
        key: item.key,
        keys: items.map(function (i) {{ return i.key; }}),
        title: item.getField('title'),
        itemType: Zotero.ItemTypes.getName(item.itemTypeID)
    }};
}}
return {{ error: 'no items created' }};
"""
    bridge_result = execute_javascript(
        js, endpoint=debug_bridge_endpoint, token=debug_bridge_token
    )
    if bridge_result.timed_out:
        return ConnectorImportResult(
            ok=False, doi=doi, item_type="", title="",
            error=(
                f"{bridge_result.error} The import may still have created the item: check Zotero for "
                "this DOI before importing again; do not rerun import-doi blindly."
            ),
            connector_endpoint=debug_bridge_endpoint, outcome="unknown",
        )
    if bridge_result.ok:
        payload = bridge_result.result
        if isinstance(payload, dict) and "key" in payload:
            keys = [str(k) for k in payload.get("keys") or [payload.get("key")] if k]
            return ConnectorImportResult(
                ok=True, doi=doi,
                item_type=str(payload.get("itemType", "")),
                title=str(payload.get("title", "")),
                error="", connector_endpoint=debug_bridge_endpoint,
                item_key=keys[0] if len(keys) == 1 else "",
                item_keys=keys,
            )
        if isinstance(payload, dict) and "error" in payload:
            return ConnectorImportResult(
                ok=False, doi=doi, item_type="", title="",
                error=f"debug-bridge JS error: {payload['error']}",
                connector_endpoint=debug_bridge_endpoint,
            )

    # Strategy 2: CrossRef/DataCite metadata → /connector/saveItems
    try:
        meta = _fetch_doi_metadata(doi)
    except Exception as exc:
        return ConnectorImportResult(
            ok=False, doi=doi, item_type="", title="",
            error=f"Metadata fetch failed: {exc}", connector_endpoint=connector_endpoint,
        )

    zotero_item = _doi_meta_to_zotero(meta, doi)
    session_id = f"import-doi-{uuid.uuid4().hex[:12]}"
    payload_bytes = json.dumps({
        "sessionID": session_id,
        "items": [zotero_item],
        "uri": f"https://doi.org/{doi}",
        "detectedDataType": "item",
    }).encode("utf-8")

    request = urllib.request.Request(
        f"{connector_endpoint}/connector/saveItems",
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Zotero-Connector-API-Version": "3",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            status = resp.status
    except urllib.error.URLError as exc:
        return ConnectorImportResult(
            ok=False, doi=doi, item_type=zotero_item["itemType"], title=zotero_item["title"],
            error=f"Connector unreachable: {exc}. Ensure Zotero is running.",
            connector_endpoint=connector_endpoint,
        )

    if status in (200, 201):
        return ConnectorImportResult(
            ok=True, doi=doi, item_type=zotero_item["itemType"], title=zotero_item["title"],
            error="", connector_endpoint=connector_endpoint,
        )
    return ConnectorImportResult(
        ok=False, doi=doi, item_type=zotero_item["itemType"], title=zotero_item["title"],
        error=f"Connector returned HTTP {status}", connector_endpoint=connector_endpoint,
    )


# Zotero.Attachments.LINK_MODE_* values (chrome/content/zotero/xpcom/attachments.js).
_LINK_MODE_NAMES = {0: "imported_file", 1: "imported_url", 2: "linked_file", 3: "linked_url", 4: "embedded_image"}
# find-pdf's debug-bridge HTTP timeout: addAvailableFile's resolver lookups and download plus the
# wait for a ZotMoov auto-move must fit inside it.
FIND_PDF_TIMEOUT_SECONDS = 90
# The auto-move wait ends extensions.zotmoov.auto_process_delay plus this margin after the file was
# attached, and never later than FIND_PDF_SETTLE_CAP_SECONDS after the script started.
FIND_PDF_SETTLE_MARGIN_SECONDS = 5
FIND_PDF_SETTLE_CAP_SECONDS = 75


@dataclass
class FindPdfResult:
    ok: bool
    key: str
    found: bool
    attachment_key: str  # final attachment key; empty unless outcome == "attached"
    error: str
    endpoint: str
    outcome: str = ""  # "attached", "not_found", "unsettled", "unknown" (bridge timeout), or "error"
    moved: bool = False  # ZotMoov replaced the downloaded attachment with a linked file
    link_mode: str = ""
    attachments: list[dict[str, str]] = field(default_factory=list)  # new file attachments last observed
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def find_available_pdf_for_item(
    key: str,
    *,
    debug_bridge_endpoint: str = DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    debug_bridge_token: str = "",
) -> FindPdfResult:
    """Trigger Zotero's own "Find Available PDF" search for an existing library item.

    Runs Zotero.Attachments.addAvailableFile (addAvailablePDF on older Zotero) via
    debug-bridge -- the same lookup Zotero's own "Find Available PDF" context-menu action
    uses (OA repositories, publisher pages, etc.). Requires the debug-bridge plugin; use this
    as a fallback when check-pdf finds no local PDF attachment, before resorting to link-pdf.

    With ZotMoov's auto-move active, the downloaded attachment is replaced a few seconds later
    (extensions.zotmoov.auto_process_delay) by a linked-file attachment under a new key. When
    ZotMoov's own conditions predict that move, this waits for the replacement (without
    triggering a move itself) and reports only a key it observed as current; otherwise the
    outcome is "unsettled". A debug-bridge timeout gives outcome "unknown".
    """
    js = f"""
var scriptStart = Date.now();
var item = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, {json.dumps(key)});
if (!item) {{
    return {{ error: 'item not found' }};
}}
var A = Zotero.Attachments;
var addFile = A && (A.addAvailableFile || A.addAvailablePDF);
if (typeof addFile !== 'function') {{
    return {{ error: 'Zotero.Attachments.addAvailableFile is not available' }};
}}
var fileChildren = async function () {{
    var children = await Zotero.Items.getAsync(item.getAttachments());
    return children.filter(function (a) {{ return a.isFileAttachment(); }});
}};
var before = (await fileChildren()).map(function (a) {{ return a.id; }});
var attachment = await addFile.call(A, item);
if (!attachment) {{
    return {{ found: false }};
}}
var attachedAt = Date.now();
// Mirrors ZotMoov's auto-move (src/01-zotmoov-notify-callback.js, move() in src/02-zotmoov.js):
// it skips linked files, an empty dst_dir, and extensions outside a non-empty allowed_fileext.
var P = function (name) {{ return Zotero.Prefs.get('extensions.zotmoov.' + name, true); }};
var filePath = attachment.getFilePath();
var allowedExt = [];
try {{
    allowedExt = JSON.parse(P('allowed_fileext') || '[]');
}} catch (e) {{
    allowedExt = [];
}}
var extAllowed = !Array.isArray(allowedExt) || !allowedExt.length
    || (!!filePath && allowedExt.map(function (x) {{ return String(x).toLowerCase(); }})
        .indexOf(Zotero.File.getExtension(filePath).toLowerCase()) !== -1);
var autoMove = !!Zotero.ZotMoov
    && !!P('enable_automove')
    && P('file_behavior') === 'move'
    && !!P('dst_dir')
    && !!filePath
    && extAllowed
    && (attachment.attachmentLinkMode === A.LINK_MODE_IMPORTED_FILE
        || attachment.attachmentLinkMode === A.LINK_MODE_IMPORTED_URL);
var moveDelay = Number(P('auto_process_delay'));
if (!(moveDelay >= 0)) {{
    moveDelay = 5000;
}}
var deadline = Math.min(
    attachedAt + moveDelay + {FIND_PDF_SETTLE_MARGIN_SECONDS * 1000},
    scriptStart + {FIND_PDF_SETTLE_CAP_SECONDS * 1000}
);
var added = [];
var final = null;
while (true) {{
    added = (await fileChildren()).filter(function (a) {{ return before.indexOf(a.id) === -1; }});
    var originalExists = Zotero.Items.exists(attachment.id);
    if (!autoMove && originalExists) {{
        final = attachment;
    }} else if (autoMove && !originalExists && added.length === 1) {{
        final = added[0];
    }}
    if (final || Date.now() >= deadline) {{
        break;
    }}
    await Zotero.Promise.delay(500);
}}
return {{
    found: true,
    settled: !!final,
    originalKey: attachment.key,
    attachmentKey: final ? final.key : '',
    linkMode: final ? final.attachmentLinkMode : null,
    attachments: added.map(function (a) {{
        return {{ key: a.key, linkMode: a.attachmentLinkMode, contentType: a.attachmentContentType }};
    }})
}};
"""
    result = execute_javascript(
        js, endpoint=debug_bridge_endpoint, token=debug_bridge_token, timeout=FIND_PDF_TIMEOUT_SECONDS
    )
    if result.timed_out:
        return FindPdfResult(
            ok=False, key=key, found=False, attachment_key="", error=result.error,
            endpoint=debug_bridge_endpoint, outcome="unknown",
            message=(
                "Zotero may have attached a file after all. Do not rerun find-pdf, which could attach "
                f"a second copy; check the item in Zotero (or run check-pdf --key {key}) first."
            ),
        )
    if not result.ok:
        return FindPdfResult(
            ok=False, key=key, found=False, attachment_key="", error=result.error,
            endpoint=debug_bridge_endpoint, outcome="error",
        )
    payload = result.result
    if isinstance(payload, dict) and payload.get("error"):
        return FindPdfResult(
            ok=False, key=key, found=False, attachment_key="", error=str(payload["error"]),
            endpoint=debug_bridge_endpoint, outcome="error",
        )
    if not (isinstance(payload, dict) and payload.get("found")):
        return FindPdfResult(
            ok=True, key=key, found=False, attachment_key="", error="", endpoint=debug_bridge_endpoint,
            outcome="not_found",
            message=(
                "Zotero's PDF resolvers (the same lookup as 'Find Available PDF') found no PDF for "
                "this item. If an open-access copy exists (e.g. the publisher's open-access page or "
                f"arXiv), download it and attach it with: link-pdf --key {key} --file <path-to-pdf>"
            ),
        )
    attachments = [
        {
            "key": str(entry.get("key", "")),
            "link_mode": _link_mode_name(entry.get("linkMode")),
            "content_type": str(entry.get("contentType") or ""),
        }
        for entry in payload.get("attachments") or []
        if isinstance(entry, dict)
    ]
    original_key = str(payload.get("originalKey", ""))
    final_key = str(payload.get("attachmentKey") or "")
    if payload.get("settled") and final_key:
        moved = final_key != original_key
        return FindPdfResult(
            ok=True, key=key, found=True, attachment_key=final_key, error="", endpoint=debug_bridge_endpoint,
            outcome="attached", moved=moved, link_mode=_link_mode_name(payload.get("linkMode")),
            attachments=attachments,
            message=(
                f"ZotMoov replaced the downloaded attachment {original_key} with a linked file; "
                f"{final_key} is the current attachment key."
                if moved else ""
            ),
        )
    return FindPdfResult(
        ok=True, key=key, found=True, attachment_key="", error="", endpoint=debug_bridge_endpoint,
        outcome="unsettled", attachments=attachments,
        message=(
            f"Zotero attached a file (initial attachment {original_key}), but the item's attachments "
            "had not settled by the end of the wait -- ZotMoov's auto-move may still be "
            "running, so 'attachments' is only the last observed state and its keys may be stale. "
            "Do not rerun find-pdf, which could attach a second copy; check the item in Zotero "
            f"(or run check-pdf --key {key}) after a moment."
        ),
    )


def _link_mode_name(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return _LINK_MODE_NAMES.get(value, str(value))
    return ""


@dataclass
class LinkPdfResult:
    ok: bool
    key: str
    attachment_key: str
    path: str
    moved: bool
    warning: str
    error: str
    endpoint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def link_local_pdf(
    parent_key: str,
    file_path: str,
    *,
    debug_bridge_endpoint: str = DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    debug_bridge_token: str = "",
) -> LinkPdfResult:
    """Link a local PDF to a Zotero item and relocate it via the ZotMoov plugin.

    Runs Zotero.Attachments.linkFromFile, then immediately triggers ZotMoov's own move()
    (the same logic behind its "Move to Attachment Folder" menu action) so the file ends
    up copied and renamed into the configured extensions.zotmoov.dst_dir, instead of
    staying linked at its original location. Calling linkFromFile alone (without this
    follow-up) leaves the file wherever it started -- if that location is later deleted
    (e.g. a sibling project's cleanup), the Zotero attachment becomes a dangling link.
    Requires the debug-bridge plugin and the ZotMoov plugin, both active in Zotero.
    """
    js = f"""
var parentItem = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, {json.dumps(parent_key)});
if (!parentItem) {{
    return {{ error: 'parent item not found' }};
}}
var filePath = {json.dumps(file_path)};
if (!(await IOUtils.exists(filePath))) {{
    return {{ error: 'source file does not exist: ' + filePath }};
}}
var attachment = await Zotero.Attachments.linkFromFile({{ file: filePath, parentItemID: parentItem.id }});
if (!attachment) {{
    return {{ error: 'linkFromFile failed' }};
}}
if (!Zotero.ZotMoov) {{
    return {{ linked: true, moved: false, key: attachment.key, path: attachment.getFilePath(), warning: 'ZotMoov not installed/active -- file left at its original location' }};
}}
var core = Zotero.ZotMoov.Menus._zotmoov;
var dstPath = Zotero.Prefs.get('extensions.zotmoov.dst_dir', true);
if (!dstPath) {{
    return {{ linked: true, moved: false, key: attachment.key, path: attachment.getFilePath(), warning: 'extensions.zotmoov.dst_dir pref is empty' }};
}}
var allowedExt = JSON.parse(Zotero.Prefs.get('extensions.zotmoov.allowed_fileext', true));
var pref = {{
    ignore_linked: false,
    into_subfolder: Zotero.Prefs.get('extensions.zotmoov.enable_subdir_move', true),
    subdir_str: Zotero.Prefs.get('extensions.zotmoov.subdirectory_string', true),
    rename_title: Zotero.Prefs.get('extensions.zotmoov.rename_title', true),
    allowed_file_ext: allowedExt.length ? allowedExt : null,
    preferred_collection: null,
    undefined_str: Zotero.Prefs.get('extensions.zotmoov.undefined_str', true),
    allow_group_libraries: Zotero.Prefs.get('extensions.zotmoov.copy_group_libraries', true),
    custom_wc: JSON.parse(Zotero.Prefs.get('extensions.zotmoov.cwc_commands', true)),
    add_zotmoov_tag: Zotero.Prefs.get('extensions.zotmoov.add_zotmoov_tag', true),
    tag_str: Zotero.Prefs.get('extensions.zotmoov.tag_str', true),
    rename_file: Zotero.Attachments.shouldAutoRenameFile() && !Zotero.Prefs.get('extensions.zotmoov.no_rename_file', true),
    max_io: Zotero.Prefs.get('extensions.zotmoov.max_io_concurrency', true),
    strip_diacritics: Zotero.Prefs.get('extensions.zotmoov.strip_diacritics', true),
    copy_overwrite: Zotero.Prefs.get('extensions.zotmoov.copy_overwrite', true)
}};
var results = await core.move([attachment], dstPath, pref);
if (!results || !results.length) {{
    return {{ linked: true, moved: false, key: attachment.key, path: attachment.getFilePath() }};
}}
var moved = results[0];
return {{ linked: true, moved: true, key: moved.key, path: moved.getFilePath() }};
"""
    result = execute_javascript(js, endpoint=debug_bridge_endpoint, token=debug_bridge_token)
    if not result.ok:
        return LinkPdfResult(
            ok=False, key="", attachment_key="", path="", moved=False, warning="",
            error=result.error, endpoint=debug_bridge_endpoint,
        )
    payload = result.result
    if isinstance(payload, dict) and payload.get("error"):
        return LinkPdfResult(
            ok=False, key="", attachment_key="", path="", moved=False, warning="",
            error=str(payload["error"]), endpoint=debug_bridge_endpoint,
        )
    if isinstance(payload, dict) and payload.get("linked"):
        return LinkPdfResult(
            ok=True,
            key=str(payload.get("key", "")),
            attachment_key=str(payload.get("key", "")),
            path=str(payload.get("path", "")),
            moved=bool(payload.get("moved", False)),
            warning=str(payload.get("warning", "")),
            error="",
            endpoint=debug_bridge_endpoint,
        )
    return LinkPdfResult(
        ok=False, key="", attachment_key="", path="", moved=False, warning="",
        error="unexpected response from debug-bridge", endpoint=debug_bridge_endpoint,
    )


def _fetch_doi_metadata(doi: str) -> dict[str, object]:
    """Fetch DOI metadata from CrossRef (journals) or DataCite (arXiv, Zenodo, etc.)."""
    crossref_url = f"https://api.crossref.org/works/{doi}"
    try:
        req = urllib.request.Request(
            crossref_url,
            headers={"User-Agent": "zotero-pdf-text/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {"source": "crossref", "data": data["message"]}
    except Exception:
        pass

    datacite_url = f"https://api.datacite.org/dois/{doi}"
    req = urllib.request.Request(
        datacite_url,
        headers={"User-Agent": "zotero-pdf-text/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    return {"source": "datacite", "data": data["data"]["attributes"]}


_HTML_TAG_RE = re.compile(r"<[^>]+>")


class _ZoteroCreator(TypedDict):
    firstName: str
    lastName: str
    creatorType: str


class _ZoteroConnectorItem(TypedDict):
    itemType: str
    title: str
    DOI: str
    url: str
    date: str
    abstractNote: str
    publicationTitle: str
    volume: str
    issue: str
    pages: str
    publisher: str
    creators: list[_ZoteroCreator]
    tags: list[object]
    relations: dict[str, object]


def _doi_meta_to_zotero(meta: dict[str, object], doi: str) -> _ZoteroConnectorItem:
    """Convert CrossRef or DataCite metadata dict to a Zotero connector item JSON."""
    source: str = meta["source"]  # type: ignore[assignment]
    d: dict[str, object] = meta["data"]  # type: ignore[assignment]

    if source == "crossref":
        _type_map = {
            "journal-article": "journalArticle",
            "proceedings-article": "conferencePaper",
            "book-chapter": "bookSection",
            "book": "book",
            "dataset": "dataset",
            "posted-content": "preprint",
        }
        item_type = _type_map.get(str(d.get("type", "")), "journalArticle")
        creators: list[_ZoteroCreator] = [
            {
                "firstName": str(a.get("given", "")),
                "lastName": str(a.get("family", "")),
                "creatorType": "author",
            }
            for a in cast(list[dict[str, Any]], d.get("author") or [])
        ]
        titles: list = d.get("title") or [""]  # type: ignore[assignment]
        title = str(titles[0]) if titles else ""
        date_parts = (cast(dict[str, Any], d.get("published") or {}).get("date-parts") or [[""]])[0]
        year = str(date_parts[0]) if date_parts else ""
        abstract = _HTML_TAG_RE.sub("", str(d.get("abstract") or ""))
        containers: list = d.get("container-title") or [""]  # type: ignore[assignment]
        pub_title = str(containers[0]) if containers else ""
        volume = str(d.get("volume") or "")
        issue = str(d.get("issue") or "")
        pages = str(d.get("page") or "")
        publisher_raw = d.get("publisher")
        publisher = str(publisher_raw) if publisher_raw else ""
    else:  # datacite
        types: dict = d.get("types") or {}  # type: ignore[assignment]
        resource_type = str(types.get("resourceTypeGeneral", ""))
        _dc_type_map = {"Preprint": "preprint", "JournalArticle": "journalArticle", "Dataset": "dataset"}
        item_type = _dc_type_map.get(resource_type, "journalArticle")
        creators = [
            {
                "firstName": str(a.get("givenName", "")),
                "lastName": str(a.get("familyName", "")),
                "creatorType": "author",
            }
            for a in cast(list[dict[str, Any]], d.get("creators") or [])
        ]
        dc_titles: list = d.get("titles") or [{}]  # type: ignore[assignment]
        title = str((dc_titles[0] if dc_titles else {}).get("title", ""))
        year = str(d.get("publicationYear") or "")
        descs: list = d.get("descriptions") or []  # type: ignore[assignment]
        abstract_raw = next(
            (str(desc.get("description", "")) for desc in descs if desc.get("descriptionType") == "Abstract"),
            "",
        )
        abstract = _HTML_TAG_RE.sub("", abstract_raw)
        pub_title = ""
        volume = ""
        issue = ""
        pages = ""
        pub_raw = d.get("publisher")
        publisher = str(pub_raw.get("name", "")) if isinstance(pub_raw, dict) else str(pub_raw or "")

    return {
        "itemType": item_type,
        "title": title,
        "DOI": doi,
        "url": f"https://doi.org/{doi}",
        "date": year,
        "abstractNote": abstract,
        "publicationTitle": pub_title,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "publisher": publisher,
        "creators": creators,
        "tags": [],
        "relations": {},
    }


def _json_rpc(
    endpoint: str,
    method: str,
    params: list[Any] | dict[str, Any],
    *,
    max_response_bytes: int | None = None,
) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = _read_bounded(response, max_response_bytes)
            data = json.loads(raw.decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not reach Better BibTeX JSON-RPC. Ensure Zotero is running and Better BibTeX is installed."
        ) from exc
    if "error" in data:
        message = data["error"].get("message", data["error"]) if isinstance(data["error"], dict) else data["error"]
        raise RuntimeError(f"Better BibTeX JSON-RPC error: {message}")
    return data.get("result")


def _read_bounded(response: Any, max_response_bytes: int | None) -> bytes:
    """Read a response body, refusing to buffer more than max_response_bytes into memory."""
    if max_response_bytes is None:
        return response.read()
    body = response.read(max_response_bytes + 1)
    if len(body) > max_response_bytes:
        raise RuntimeError("Better BibTeX response exceeds the allowed size limit.")
    return body


def _clean_citation_keys(citation_keys: list[str]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for raw in citation_keys:
        for key in re.split(r"[\s,;]+", raw or ""):
            key = key.strip()
            if not key or key in seen:
                continue
            keys.append(key)
            seen.add(key)
    return keys


def _bibtex_keys(text: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,", text or "", flags=re.MULTILINE)
    }
