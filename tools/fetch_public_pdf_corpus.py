"""Fetch checksum-pinned public PDFs; originals remain in an ignored local cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "public_pdfs"


def fetch(source: dict, cache: Path) -> Path:
    target = cache / f"{source['id']}.pdf"
    if target.exists():
        data = target.read_bytes()
    else:
        request = urllib.request.Request(source["url"], headers={"User-Agent": "zotero-pdf-text-corpus/1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        if not data.startswith(b"%PDF-"):
            raise ValueError(f"{source['id']}: server returned no PDF; save it manually as {target} (see the source's note)")
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ValueError(f"Checksum mismatch for {source['id']}; review upstream changes before updating the manifest")
    if not target.exists():
        cache.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", action="append", help="Select source ID (repeatable); default: all")
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache")
    args = parser.parse_args()
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
    known = {source["id"] for source in sources}
    if args.id and set(args.id) - known:
        parser.error(f"Unknown source IDs: {sorted(set(args.id) - known)}")
    failed = []
    for source in sources:
        if not args.id or source["id"] in args.id:
            try:
                print(fetch(source, args.cache))
            except (OSError, ValueError) as error:
                print(f"FAILED {error}")
                failed.append(source["id"])
    if failed:
        raise SystemExit(f"{len(failed)} source(s) not fetched: {', '.join(failed)}")


if __name__ == "__main__":
    main()
