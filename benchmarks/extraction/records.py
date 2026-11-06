"""Load and validate extraction-evaluation records.

Validation is two-stage: JSON Schema for structure, then ``_check_*`` functions for rules a schema
cannot express (unique IDs, links that must resolve, page numbers within the page count, hash
round-trips). Semantic checks run only on structurally valid records and all findings are reported
together in one ``RecordError``.
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


def load_record(path: str | Path, *, kind: str | None = None) -> dict[str, Any]:
    """Read a JSON record, validate it and return it. ``kind`` pins the expected record kind."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))
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


# --- semantic checks -------------------------------------------------------------------------


def _duplicates(values: list[Any]) -> list[Any]:
    return sorted({v for v in values if values.count(v) > 1}, key=str)


def _check_layout(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    page_count = record["document"]["page_count"]
    scope_pages = record["scope"]["physical_pages"]
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
    if set(pages) != set(scope_pages):
        errors.append("pages must cover exactly the physical pages listed in scope")

    blocks: dict[str, dict[str, Any]] = {}
    for dup in _duplicates([b["id"] for b in record["blocks"]]):
        errors.append(f"duplicate block id {dup!r}")
    for dup in _duplicates([b["reading_order"] for b in record["blocks"]]):
        errors.append(f"duplicate reading_order {dup}")
    for block in record["blocks"]:
        blocks[block["id"]] = block
        bid = block["id"]
        page = pages.get(block["page"])
        if page is None:
            errors.append(f"block {bid!r} is on page {block['page']}, outside the scope")
        else:
            box, geo = block["bbox"], page["geometry"]
            if box["x0"] > box["x1"] or box["y0"] > box["y1"]:
                errors.append(f"block {bid!r} bbox has x0>x1 or y0>y1")
            elif min(box["x0"], box["y0"]) < 0 or box["x1"] > geo["width"] or box["y1"] > geo["height"]:
                errors.append(f"block {bid!r} bbox lies outside the page geometry")
        text = block.get("text")
        if "text_sha256" in block:
            if not isinstance(text, str):
                errors.append(f"block {bid!r} has text_sha256 but no text")
            elif sha256_text(text) != block["text_sha256"]:
                errors.append(f"block {bid!r} text_sha256 does not match its text")
        for field, allowed in (("table", "table"), ("graphic", "figure"), ("equation", "equation")):
            if field in block and block["type"] != allowed:
                errors.append(f"block {bid!r} has a {field} payload but type {block['type']!r}")
        if "caption_for" in block and block["type"] != "caption":
            errors.append(f"block {bid!r} has caption_for but type {block['type']!r}")
        errors.extend(_check_table(block))
    for block in record["blocks"]:
        if "caption_for" in block:
            target = blocks.get(block["caption_for"])
            if target is None:
                errors.append(f"caption {block['id']!r} points at unknown block {block['caption_for']!r}")
            elif target["type"] not in ("table", "figure"):
                errors.append(f"caption {block['id']!r} points at a {target['type']} block")

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
        errors.extend(_check_references(record["references"], blocks, set(scope_pages)))
    return errors


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


def _check_references(refs: dict[str, Any], blocks: dict[str, dict[str, Any]], scope: set[int]) -> list[str]:
    errors = []
    entries = refs["entries"]
    for dup in _duplicates([e["id"] for e in entries]):
        errors.append(f"duplicate reference entry id {dup!r}")
    for dup in _duplicates([e["ordinal"] for e in entries]):
        errors.append(f"duplicate reference ordinal {dup}")
    for entry in entries:
        if entry["page"] not in scope:
            errors.append(f"reference entry {entry['id']!r} is on page {entry['page']}, outside the scope")
        if "block_id" in entry and entry["block_id"] not in blocks:
            errors.append(f"reference entry {entry['id']!r} names unknown block {entry['block_id']!r}")
        if "raw_sha256" in entry and sha256_text(entry["raw"]) != entry["raw_sha256"]:
            errors.append(f"reference entry {entry['id']!r} raw_sha256 does not match raw")
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
    return errors


def _check_run(record: dict[str, Any]) -> list[str]:
    errors = []
    for label, items in (("configuration", record["configurations"]), ("input", record["inputs"])):
        for dup in _duplicates([item["id"] for item in items]):
            errors.append(f"duplicate {label} id {dup!r}")
    for config in record["configurations"]:
        if "options" in config and options_sha256(config["options"]) != config["options_sha256"]:
            errors.append(f"configuration {config['id']!r} options_sha256 does not match its options")
    if "finished_at" in record:
        # The schema pattern guarantees both carry a UTC offset, so they are comparable.
        if datetime.fromisoformat(record["finished_at"]) < datetime.fromisoformat(record["started_at"]):
            errors.append("finished_at precedes started_at")
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
        for metric in result.get("metrics", []):
            pinned = record["metric_versions"].get(metric["name"])
            if pinned is None:
                errors.append(f"{label} reports metric {metric['name']!r} missing from metric_versions")
            elif pinned != metric["version"]:
                errors.append(f"{label} metric {metric['name']!r} version {metric['version']!r} != pinned {pinned!r}")
    return errors


_CHECKS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "expectation": _check_layout,
    "observation": _check_layout,
    "run": _check_run,
}
