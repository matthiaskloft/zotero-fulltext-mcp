"""Explain which converted files the published index actually uses."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .artifacts import read_current_pointer, resolve_generation_dir


def output_status(output_root: Path, *, list_files: bool = False) -> dict[str, object]:
    index_root = output_root / "index"
    pointer = read_current_pointer(index_root)
    if pointer is None:
        raise ValueError(f"No published index exists under {index_root}; run rebuild-index first.")

    def describe(generation_id: str) -> dict[str, object]:
        jsonl_path = resolve_generation_dir(index_root, generation_id) / "index.jsonl"
        folders: Counter[tuple[str, str]] = Counter()
        paths: set[str] = set()
        missing = 0
        with jsonl_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                path = Path(json.loads(line)["markdown_path"])
                folders[(str(path.parent), str(path.parent.resolve()))] += 1
                paths.add(str(path))
                if not path.is_file():
                    missing += 1
        result: dict[str, object] = {
            "generation_id": generation_id,
            "records": sum(folders.values()),
            "missing_markdown": missing,
            "folders": [
                {"path": stored, "physical_path": physical, "records": count}
                for (stored, physical), count in sorted(folders.items())
            ],
        }
        if list_files:
            result["markdown_files"] = sorted(paths)
        return result

    current = describe(str(pointer["current_generation"]))
    previous_id = pointer.get("previous_generation")
    previous = describe(str(previous_id)) if previous_id else None
    return {
        "output_root": str(output_root),
        "mapping_snapshots": str(output_root / "mapping-runs"),
        "conversion_runs": [str(output_root / "conversion-runs" / name) for name in ("verified", "samples", "unverified-review")],
        "legacy_roots": [str(output_root / name) for name in ("runs", "verified", "samples", "unverified_review") if (output_root / name).exists()],
        "index_pointer": str(index_root / "current.json"),
        "current": current,
        "previous": previous,
    }
