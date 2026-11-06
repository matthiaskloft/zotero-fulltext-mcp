"""Generate or verify the E2 pilot / held-out manifest (benchmarks/extraction/pilot_manifest.json).

    python tools/build_pilot_manifest.py            # print the selection, write nothing
    python tools/build_pilot_manifest.py --write    # rewrite the manifest and its SHA-256 pin
    python tools/build_pilot_manifest.py --check    # verify the committed files; exit 1 on drift

Metadata only: no PDFs are read and nothing is downloaded. Logic lives in benchmarks/extraction/pilot.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from extraction.pilot import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
