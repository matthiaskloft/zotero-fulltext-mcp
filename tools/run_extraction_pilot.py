"""Opt-in host tool: run an extractor over the reviewed pilot pages and score it.

    python tools/run_extraction_pilot.py --expectations <dir of reviewed records> --out <dir outside the repo>

Verifies cached PDFs against the manifest pins, extracts each PDF once, scores every reviewed
expectation and writes run.json, summary.md, details.json and the observation/Markdown files. The
held-out split needs an explicit --split heldout and is never for tuning. Logic lives in
benchmarks/extraction/run_pilot.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from extraction.run_pilot import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
