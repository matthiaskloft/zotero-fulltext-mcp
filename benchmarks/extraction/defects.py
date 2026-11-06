"""Defect injectors for evaluator-sensitivity tests (roadmap E1, package B).

Each function returns a deep-copied observation with one targeted defect and leaves the input
untouched; ``text_sha256`` is recomputed where present so the result stays a valid record. They
raise ``ValueError`` when the target is missing rather than silently injecting nothing, so a test
cannot pass because the defect was a no-op.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .records import sha256_text

_SIGN = re.compile(r"[-−](?=[\w.])")


def _block(obs: dict[str, Any], block_id: str) -> dict[str, Any]:
    for block in obs["blocks"]:
        if block["id"] == block_id:
            return block
    raise ValueError(f"no block {block_id!r}")


def _set_text(block: dict[str, Any], text: str) -> None:
    block["text"] = text
    if "text_sha256" in block:
        block["text_sha256"] = sha256_text(text)


def _cell(block: dict[str, Any], row: int, col: int) -> dict[str, Any]:
    for cell in block.get("table", {}).get("cells", []):
        if (cell["row"], cell["col"]) == (row, col):
            return cell  # type: ignore[no-any-return]
    raise ValueError(f"block {block['id']!r} has no table cell ({row},{col})")


def drop_sign(obs: dict[str, Any], block_id: str, *, cell: tuple[int, int] | None = None) -> dict[str, Any]:
    """Remove the first minus sign (``-`` or U+2212) that precedes a digit, letter or point, from the
    block text, a table cell (``cell=(row, col)``) or, for equation blocks, the LaTeX."""
    out = copy.deepcopy(obs)
    block = _block(out, block_id)
    if cell is not None:
        target, key = _cell(block, *cell), "text"
    elif "equation" in block:
        target, key = block["equation"], "latex"
    else:
        target, key = block, "text"
    value = target.get(key)
    if not isinstance(value, str) or not _SIGN.search(value):
        raise ValueError(f"no sign to drop in block {block_id!r}")
    changed = _SIGN.sub("", value, count=1)
    if target is block:
        _set_text(block, changed)
    else:
        target[key] = changed
    return out


def swap_table_values(obs: dict[str, Any], block_id: str, a: tuple[int, int], b: tuple[int, int]) -> dict[str, Any]:
    """Swap the text of two table cells (``(row, col)`` each); they must differ."""
    out = copy.deepcopy(obs)
    block = _block(out, block_id)
    first, second = _cell(block, *a), _cell(block, *b)
    if first["text"] == second["text"]:
        raise ValueError("cells hold identical text; swapping would change nothing")
    first["text"], second["text"] = second["text"], first["text"]
    return out


def omit_text(obs: dict[str, Any], block_id: str, fragment: str | None = None) -> dict[str, Any]:
    """Delete ``fragment`` from a block's text, or the whole block when ``fragment`` is None."""
    out = copy.deepcopy(obs)
    block = _block(out, block_id)
    if fragment is None:
        out["blocks"] = [b for b in out["blocks"] if b["id"] != block_id]
        return out
    text = block.get("text")
    if not isinstance(text, str) or fragment not in text:
        raise ValueError(f"fragment not in block {block_id!r}")
    _set_text(block, text.replace(fragment, "", 1))
    return out


def reorder_columns(obs: dict[str, Any], block_id: str, order: list[int]) -> dict[str, Any]:
    """Permute table columns: new column ``i`` holds old column ``order[i]``. No spanning cells."""
    out = copy.deepcopy(obs)
    block = _block(out, block_id)
    table = block.get("table")
    if table is None or sorted(order) != list(range(table["n_cols"])) or order == sorted(order):
        raise ValueError("order must be a non-identity permutation of the table's columns")
    if any(c.get("colspan", 1) > 1 for c in table["cells"]):
        raise ValueError("cannot reorder columns of a table with column-spanning cells")
    new_index = {old: new for new, old in enumerate(order)}
    for cell in table["cells"]:
        cell["col"] = new_index[cell["col"]]
    return out


def swap_reading_order(obs: dict[str, Any], a: str, b: str) -> dict[str, Any]:
    """Exchange the ``reading_order`` of two blocks."""
    out = copy.deepcopy(obs)
    first, second = _block(out, a), _block(out, b)
    first["reading_order"], second["reading_order"] = second["reading_order"], first["reading_order"]
    return out
