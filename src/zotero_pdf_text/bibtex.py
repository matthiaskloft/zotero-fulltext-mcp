from __future__ import annotations

import json
import re
import uuid
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
) -> JavaScriptResult:
    """Execute JavaScript inside Zotero via the debug-bridge plugin.

    Requires the debug-bridge XPI to be installed in Zotero and a Bearer token configured
    at extensions.zotero.debug-bridge.token in Zotero's Config Editor.
    Token can also be provided via the ZOTERO_DEBUG_BRIDGE_TOKEN environment variable.

    Returns JavaScriptResult with ok=False when the plugin is not installed or token is wrong.
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
        with urllib.request.urlopen(request, timeout=30) as response:
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
        f"?q={urllib.request.quote(query[:100])}&limit=20"
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

    Returns ConnectorImportResult; caller must poll to find the new item key.
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
    return {{ key: item.key, title: item.getField('title'), itemType: item.itemTypeID }};
}}
return {{ error: 'no items created' }};
"""
    bridge_result = execute_javascript(
        js, endpoint=debug_bridge_endpoint, token=debug_bridge_token
    )
    if bridge_result.ok:
        payload = bridge_result.result
        if isinstance(payload, dict) and "key" in payload:
            return ConnectorImportResult(
                ok=True, doi=doi,
                item_type=str(payload.get("itemType", "")),
                title=str(payload.get("title", "")),
                error="", connector_endpoint=debug_bridge_endpoint,
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


@dataclass
class FindPdfResult:
    ok: bool
    key: str
    found: bool
    attachment_key: str
    error: str
    endpoint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def find_available_pdf_for_item(
    key: str,
    *,
    debug_bridge_endpoint: str = DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    debug_bridge_token: str = "",
) -> FindPdfResult:
    """Trigger Zotero's own "Find Available PDF" search for an existing library item.

    Runs Zotero.Attachments.addAvailablePDF via debug-bridge -- the same lookup Zotero's
    own "Find Available PDF" context-menu action uses (OA repositories, publisher pages,
    etc.). Requires the debug-bridge plugin; use this as a fallback when check-pdf finds
    no local PDF attachment, before resorting to a manual attach.
    """
    js = f"""
var item = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, {json.dumps(key)});
if (!item) {{
    return {{ error: 'item not found' }};
}}
if (!Zotero.Attachments || typeof Zotero.Attachments.addAvailablePDF !== 'function') {{
    return {{ error: 'Zotero.Attachments.addAvailablePDF is not available' }};
}}
var attachment = await Zotero.Attachments.addAvailablePDF(item);
if (attachment) {{
    return {{ found: true, attachmentKey: attachment.key }};
}}
return {{ found: false }};
"""
    result = execute_javascript(js, endpoint=debug_bridge_endpoint, token=debug_bridge_token)
    if not result.ok:
        return FindPdfResult(
            ok=False, key=key, found=False, attachment_key="", error=result.error,
            endpoint=debug_bridge_endpoint,
        )
    payload = result.result
    if isinstance(payload, dict) and payload.get("error"):
        return FindPdfResult(
            ok=False, key=key, found=False, attachment_key="", error=str(payload["error"]),
            endpoint=debug_bridge_endpoint,
        )
    if isinstance(payload, dict) and payload.get("found"):
        return FindPdfResult(
            ok=True, key=key, found=True, attachment_key=str(payload.get("attachmentKey", "")),
            error="", endpoint=debug_bridge_endpoint,
        )
    return FindPdfResult(ok=True, key=key, found=False, attachment_key="", error="", endpoint=debug_bridge_endpoint)


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


def _doi_meta_to_zotero(meta: dict[str, object], doi: str) -> dict[str, object]:
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
        creators = [
            {
                "firstName": str(a.get("given", "")),
                "lastName": str(a.get("family", "")),
                "creatorType": "author",
            }
            for a in (d.get("author") or [])
        ]
        titles: list = d.get("title") or [""]  # type: ignore[assignment]
        title = str(titles[0]) if titles else ""
        date_parts = ((d.get("published") or {}).get("date-parts") or [[""]])[0]  # type: ignore[union-attr]
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
            for a in (d.get("creators") or [])
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
