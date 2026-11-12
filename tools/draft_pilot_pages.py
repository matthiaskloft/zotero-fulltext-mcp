"""Opt-in host tool: render selected pilot pages from cached corpus PDFs and write DRAFT proposals.

    python tools/draft_pilot_pages.py --out <dir outside the repo>

Reads the committed manifest (verifying its SHA-256 pin) and PDFs already in the local corpus
cache (never downloads, never touches a Zotero library). Writes page images, draft `expectation`
records (status `draft`, not ground truth) and `selection_report.json`. See
docs/extraction-pilot-review.md for the review procedure. Logic lives in benchmarks/extraction/drafts.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from extraction.drafts import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
