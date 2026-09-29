"""Run the zotero-pdf-text CLI while recording every raw debug-bridge exchange.

Used only by ``tests/test_live_zotero.py`` when ``ZOTERO_LIVE_RECORD=1``:

    python tests/live_zotero_recorder.py <records.json> <cli args...>

It is the real CLI (``zotero_pdf_text.cli.main``) with ``bibtex.execute_javascript`` wrapped, so the
recorded payloads are exactly what the CLI parsed. The records file is raw -- real keys and titles
-- and belongs in a temporary directory; the live test sanitizes it before anything reaches
``tests/fixtures/zotero_bridge/``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from zotero_pdf_text import bibtex
from zotero_pdf_text.cli import main


def run(records_path: Path, argv: list[str]) -> int:
    real = bibtex.execute_javascript
    records: list[dict[str, object]] = []

    def recording(code: str, **kwargs: object) -> bibtex.JavaScriptResult:
        result = real(code, **kwargs)  # type: ignore[arg-type]
        records.append({"ok": result.ok, "timed_out": result.timed_out, "result": result.result, "error": result.error})
        return result

    bibtex.execute_javascript = recording  # type: ignore[assignment]
    try:
        return main(argv)
    finally:
        records_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2:]))
