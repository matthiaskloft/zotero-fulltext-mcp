"""Load and validate extraction-evaluation records.

Validation is two-stage: JSON Schema for structure, then ``_check_*`` functions for rules a schema
cannot express (unique IDs, links that must resolve, page numbers within the page count, hash
round-trips, completeness of runs). Semantic checks run only on structurally valid records and all
findings are reported together in one ``RecordError``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_VERSION = 1
RECORD_KINDS = ("expectation", "observation", "run")

# Extractors emit boxes that overshoot the page box slightly (PyMuPDF reports y0 of about -2 for
# near-margin text). Observations in pt/px units may overshoot by this many units; expectations and
# normalized coordinates are always strict.
BBOX_TOLERANCE = 3.0

_TEXT_FREE_TYPES = ("table", "figure", "equation")
_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


class RecordError(ValueError):
    """A record failed validation; ``errors`` lists every finding."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def schema_path(kind: str) -> Path:
    """Path of the JSON Schema for ``kind`` (one of ``RECORD_KINDS``)."""
    if kind not in RECORD_KINDS:
        raise ValueError(f"Unknown record kind: {kind!r}")
    return _SCHEMA_DIR / f"{kind}.schema.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """SHA-256 of the UTF-8 bytes of ``text``, with no normalization."""
    return sha256_bytes(text.encode("utf-8"))


def options_sha256(options: dict[str, Any]) -> str:
    """Canonical hash of a configuration's options (sorted keys, compact JSON, UTF-8)."""
    return sha256_text(json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


@lru_cache(maxsize=1)
def _registry() -> Registry:
    resources = []
    for path in _SCHEMA_DIR.glob("*.schema.json"):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def _validator(kind: str) -> Draft202012Validator:
    schema = json.loads(schema_path(kind).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=_registry())


def validate_record(record: Any) -> str:
    """Validate a record; return its kind or raise ``RecordError``."""
    if not isinstance(record, dict):
        raise RecordError(["record must be a JSON object"])
    kind = record.get("record_kind")
    if kind not in RECORD_KINDS:
        raise RecordError([f"record_kind must be one of {list(RECORD_KINDS)}, got {kind!r}"])
    errors = [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(_validator(kind).iter_errors(record), key=lambda e: list(map(str, e.absolute_path)))
    ]
    if not errors:
        errors = _CHECKS[kind](record)
    if errors:
        raise RecordError(errors)
    return kind


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not valid JSON")


def load_record(path: str | Path, *, kind: str | None = None) -> dict[str, Any]:
    """Read a JSON record, validate it and return it. ``kind`` pins the expected record kind."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except ValueError as exc:
        raise RecordError([f"invalid JSON: {exc}"]) from exc
    actual = validate_record(record)
    if kind is not None and actual != kind:
        raise RecordError([f"expected a {kind} record, got {actual}"])
    return record  # type: ignore[no-any-return]


def load_reviewed_expectation(path: str | Path) -> dict[str, Any]:
    """Load an expectation and refuse drafts: the only entry point scorers should use for truth."""
    record = load_record(path, kind="expectation")
    if record["provenance"]["review"]["status"] != "reviewed":
        raise RecordError(["expectation is not reviewed; drafts are not ground truth"])
    return record


def verify_output_spans(observation: dict[str, Any], markdown: str) -> list[str]:
    """Check an observation's block ``output_span`` fields against the Markdown text itself.

    Returns findings (empty = every span round-trips): the Markdown hash must match, spans must lie
    inside the text, and ``span_sha256`` must equal the hash of the sliced characters.
    """
    findings = []
    digest = sha256_text(markdown)
    for block in observation.get("blocks", []):
        span = block.get("output_span")
        if span is None:
            continue
        if span["markdown_sha256"] != digest:
            findings.append(f"block {block['id']!r} output_span hash does not match the supplied Markdown")
        elif span["end"] > len(markdown):
            findings.append(f"block {block['id']!r} output_span ends beyond the Markdown")
        elif "span_sha256" in span and sha256_text(markdown[span["start"]:span["end"]]) != span["span_sha256"]:
            findings.append(f"block {block['id']!r} span_sha256 does not match the sliced Markdown")
    return findings


# --- semantic checks -------------------------------------------------------------------------


def _duplicates(values: list[Any]) -> list[Any]:
    return sorted({v for v in values if values.count(v) > 1}, key=str)


def _timestamp(value: str, label: str, errors: list[str]) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"{label} is not a valid timestamp: {value!r}")
        return None


def _check_layout(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    strict = record["record_kind"] == "expectation"
    page_count = record["document"]["page_count"]
    scope_pages = record["scope"]["physical_pages"]
    scope = set(scope_pages)
    for number in scope_pages:
        if number > page_count:
            errors.append(f"scope page {number} exceeds page_count {page_count}")
    if record["scope"]["kind"] == "complete_document" and sorted(scope_pages) != list(range(1, page_count + 1)):
        errors.append("complete_document scope must list every physical page 1..page_count")

    pages = {}
    for page in record["pages"]:
        number = page["physical_page"]
        if number in pages:
            errors.append(f"duplicate page entry for physical page {number}")
        pages[number] = page
    if set(pages) != scope:
        errors.append("pages must cover exactly the physical pages listed in scope")

    created = record["provenance"].get("created_at")
    if created is not None:
        _timestamp(created, "created_at", errors)

    for dup in _duplicates([b["id"] for b in record["blocks"]]):
        errors.append(f"duplicate block id {dup!r}")
    for dup in _duplicates([b["reading_order"] for b in record["blocks"]]):
        errors.append(f"duplicate reading_order {dup}")
    blocks = {b["id"]: b for b in record["blocks"]}
    spans = set()
    for block in record["blocks"]:
        bid = block["id"]
        page = pages.get(block["page"])
        if page is None:
            errors.append(f"block {bid!r} is on page {block['page']}, outside the scope")
        elif "bbox" in block:
            box, geo = block["bbox"], page["geometry"]
            tol = 0 if strict or geo["unit"] == "normalized" else BBOX_TOLERANCE
            if box["x0"] > box["x1"] or box["y0"] > box["y1"]:
                errors.append(f"block {bid!r} bbox has x0>x1 or y0>y1")
            elif min(box["x0"], box["y0"]) < -tol or box["x1"] > geo["width"] + tol or box["y1"] > geo["height"] + tol:
                errors.append(f"block {bid!r} bbox lies outside the page geometry")
        text = block.get("text")
        if "text_sha256" in block:
            if not isinstance(text, str):
                errors.append(f"block {bid!r} has text_sha256 but no text")
            elif sha256_text(text) != block["text_sha256"]:
                errors.append(f"block {bid!r} text_sha256 does not match its text")
        for field, allowed in (("table", "table"), ("graphic", "figure"), ("equation", "equation")):
            if field in block and block[field] is not None and block["type"] != allowed:
                errors.append(f"block {bid!r} has a {field} payload but type {block['type']!r}")
        if "caption_for" in block and block["type"] != "caption":
            errors.append(f"block {bid!r} has caption_for but type {block['type']!r}")
        errors.extend(_check_table(block))
        if "output_span" in block:
            span = block["output_span"]
            spans.add(span["markdown_sha256"])
            if span["end"] < span["start"]:
                errors.append(f"block {bid!r} output_span has end before start")
    if len(spans) > 1:
        errors.append("output_span fields must all refer to one Markdown output")

    for block in record["blocks"]:
        if "caption_for" in block:
            target = blocks.get(block["caption_for"])
            if target is None:
                errors.append(f"caption {block['id']!r} points at unknown block {block['caption_for']!r}")
            elif target["type"] not in ("table", "figure"):
                errors.append(f"caption {block['id']!r} points at a {target['type']} block")

    continued_by = _check_continuations(record["blocks"], blocks, errors)

    for number, page in pages.items():
        anchors = {}
        for name in ("body_start", "body_end"):
            if name in page:
                target = blocks.get(page[name]["block_id"])
                if target is None or target["page"] != number:
                    errors.append(f"page {number} {name} must name a block on that page")
                else:
                    anchors[name] = target["reading_order"]
        if len(anchors) == 2 and anchors["body_start"] > anchors["body_end"]:
            errors.append(f"page {number} body_start follows body_end in reading order")

    if "references" in record:
        errors.extend(_check_references(record["references"], blocks, continued_by, scope, record["scope"]["kind"]))
    if strict and record["provenance"]["review"]["status"] == "reviewed":
        errors.extend(_check_reviewed(record, pages))
    return errors


def _check_continuations(block_list: list[dict[str, Any]], blocks: dict[str, dict[str, Any]], errors: list[str]) -> dict[str, str]:
    """Validate continues_from links; return the map target id -> id of the block continuing it."""
    continued_by: dict[str, str] = {}
    for block in block_list:
        if "continues_from" not in block:
            continue
        bid, target = block["id"], blocks.get(block["continues_from"])
        if target is None:
            errors.append(f"block {bid!r} continues unknown block {block['continues_from']!r}")
            continue
        if target["type"] != block["type"]:
            errors.append(f"block {bid!r} continues a block of a different type")
        if target["page"] >= block["page"]:
            errors.append(f"block {bid!r} must continue a block on an earlier page")
        if target["id"] in continued_by:
            errors.append(f"block {target['id']!r} is continued by more than one block")
        continued_by[target["id"]] = bid
        if block["type"] == "table" and "table" in block and "table" in target:
            if block["table"]["n_cols"] != target["table"]["n_cols"]:
                errors.append(f"table {bid!r} and the table it continues differ in n_cols")
    return continued_by


def _check_table(block: dict[str, Any]) -> list[str]:
    table = block.get("table")
    if table is None:
        return []
    errors = []
    occupied: set[tuple[int, int]] = set()
    for cell in table["cells"]:
        rows = range(cell["row"], cell["row"] + cell.get("rowspan", 1))
        cols = range(cell["col"], cell["col"] + cell.get("colspan", 1))
        if rows[-1] >= table["n_rows"] or cols[-1] >= table["n_cols"]:
            errors.append(f"block {block['id']!r} table cell ({cell['row']},{cell['col']}) exceeds the grid")
        for r in rows:
            for c in cols:
                if (r, c) in occupied:
                    errors.append(f"block {block['id']!r} table cells overlap at ({r},{c})")
                occupied.add((r, c))
    return errors


def _check_references(refs: dict[str, Any], blocks: dict[str, dict[str, Any]], continued_by: dict[str, str], scope: set[int], scope_kind: str) -> list[str]:
    errors: list[str] = []
    entries = refs["entries"]
    for dup in _duplicates([e["id"] for e in entries]):
        errors.append(f"duplicate reference entry id {dup!r}")
    for dup in _duplicates([e["ordinal"] for e in entries]):
        errors.append(f"duplicate reference ordinal {dup}")
    for entry in entries:
        eid = entry["id"]
        page = entry.get("page")
        if page is not None and page not in scope:
            errors.append(f"reference entry {eid!r} is on page {page}, outside the scope")
        end_page = entry.get("end_page")
        if end_page is not None:
            if end_page not in scope:
                errors.append(f"reference entry {eid!r} ends on page {end_page}, outside the scope")
            if page is None or end_page <= page:
                errors.append(f"reference entry {eid!r} end_page must come after its start page")
        block = blocks.get(entry["block_id"]) if "block_id" in entry else None
        if "block_id" in entry:
            if block is None:
                errors.append(f"reference entry {eid!r} names unknown block {entry['block_id']!r}")
            else:
                if page is not None and block["page"] != page:
                    errors.append(f"reference entry {eid!r} page differs from its block's page")
                last = block
                while last["id"] in continued_by:
                    last = blocks[continued_by[last["id"]]]
                if last is not block and end_page != last["page"]:
                    errors.append(f"reference entry {eid!r} continues across pages; end_page must be {last['page']}")
                if last is block and end_page is not None:
                    errors.append(f"reference entry {eid!r} has end_page but its block has no continuation")
        if "raw_sha256" in entry and sha256_text(entry["raw"]) != entry["raw_sha256"]:
            errors.append(f"reference entry {eid!r} raw_sha256 does not match raw")
    entry_ids = {e["id"] for e in entries}
    for dup in _duplicates([m["id"] for m in refs["markers"]]):
        errors.append(f"duplicate marker id {dup!r}")
    for marker in refs["markers"]:
        mid = marker["id"]
        if marker["page"] not in scope:
            errors.append(f"marker {mid!r} is on page {marker['page']}, outside the scope")
        block = blocks.get(marker["block_id"]) if "block_id" in marker else None
        if "block_id" in marker and (block is None or block["page"] != marker["page"]):
            errors.append(f"marker {mid!r} block_id must name a block on the marker's page")
        for target in marker["entry_ids"]:
            if target not in entry_ids:
                errors.append(f"marker {mid!r} points at unknown reference entry {target!r}")
        if (marker["target_status"] == "resolved") != bool(marker["entry_ids"]):
            errors.append(f"marker {mid!r} entry_ids must be non-empty exactly when target_status is resolved")
        if marker["target_status"] == "out_of_scope" and scope_kind != "pages":
            errors.append(f"marker {mid!r} cannot be out_of_scope in a complete_document record")
    return errors


def _check_reviewed(record: dict[str, Any], pages: dict[int, dict[str, Any]]) -> list[str]:
    """Extra requirements that only reviewed (ground-truth) expectations must meet."""
    errors: list[str] = []
    review = record["provenance"]["review"]
    _timestamp(review["reviewed_at"], "reviewed_at", errors)
    proposer = record["provenance"].get("proposed_by", {})
    if review["reviewer"].casefold() in {str(proposer.get("tool", "")).casefold(), str(proposer.get("model", "")).casefold()} - {""}:
        errors.append("reviewer must not be the proposing tool or model")
    if review["basis"] == "page_image_inspection":
        for number, page in pages.items():
            if "image_sha256" not in page["render"]:
                errors.append(f"page {number} needs render.image_sha256 for a page_image_inspection review")
    for block in record["blocks"]:
        bid, kind = block["id"], block["type"]
        if kind not in _TEXT_FREE_TYPES and "text" not in block:
            errors.append(f"block {bid!r} must state text (a value or explicit null)")
        if isinstance(block.get("text"), str) and "text_sha256" not in block:
            errors.append(f"block {bid!r} needs text_sha256 in a reviewed record")
        if kind == "table" and not block.get("table", {}).get("cells"):
            errors.append(f"table block {bid!r} needs a table payload with at least one cell")
        if kind == "figure" and "graphic" not in block:
            errors.append(f"figure block {bid!r} needs a graphic payload")
        if kind == "equation" and "equation" not in block:
            errors.append(f"equation block {bid!r} needs an equation payload")
    for entry in record.get("references", {}).get("entries", []):
        if "raw_sha256" not in entry:
            errors.append(f"reference entry {entry['id']!r} needs raw_sha256 in a reviewed record")
    return errors


def _check_run(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    started = _timestamp(record["started_at"], "started_at", errors)
    if "finished_at" in record:
        finished = _timestamp(record["finished_at"], "finished_at", errors)
        # The schema pattern guarantees both carry a UTC offset, so they are comparable.
        if started and finished and finished < started:
            errors.append("finished_at precedes started_at")
    for label, items in (("configuration", record["configurations"]), ("input", record["inputs"])):
        for dup in _duplicates([item["id"] for item in items]):
            errors.append(f"duplicate {label} id {dup!r}")
    for config in record["configurations"]:
        if "options" in config and options_sha256(config["options"]) != config["options_sha256"]:
            errors.append(f"configuration {config['id']!r} options_sha256 does not match its options")
    config_ids = {c["id"] for c in record["configurations"]}
    input_ids = {i["id"] for i in record["inputs"]}
    pairs: set[tuple[str, str]] = set()
    for result in record["results"]:
        pair = (result["configuration_id"], result["input_id"])
        label = f"result {pair[0]!r}/{pair[1]!r}"
        if pair[0] not in config_ids:
            errors.append(f"{label} names an unknown configuration")
        if pair[1] not in input_ids:
            errors.append(f"{label} names an unknown input")
        if pair in pairs:
            errors.append(f"duplicate {label}")
        pairs.add(pair)
        metrics = result.get("metrics", [])
        for dup in _duplicates([(m["name"], m["mode"]) for m in metrics]):
            errors.append(f"{label} reports metric {dup[0]!r} ({dup[1]}) more than once")
        for metric in metrics:
            pinned = record["metric_versions"].get(metric["name"])
            if pinned is None:
                errors.append(f"{label} reports metric {metric['name']!r} missing from metric_versions")
            elif pinned != metric["version"]:
                errors.append(f"{label} metric {metric['name']!r} version {metric['version']!r} != pinned {pinned!r}")
    for config_id in sorted(config_ids):
        for input_id in sorted(input_ids):
            if (config_id, input_id) not in pairs:
                errors.append(f"missing result for configuration {config_id!r} and input {input_id!r}; record skipped pairs with status skipped")
    return errors


_CHECKS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "expectation": _check_layout,
    "observation": _check_layout,
    "run": _check_run,
}
