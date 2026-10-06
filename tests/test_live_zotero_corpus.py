"""Opt-in acquisition of the public corpus through real Zotero (docs/public-corpus-uses.md, uses 3-5).

For every `benchmarks/public_pdfs/sources.json` entry with a DOI, run the user's path
`import-doi --with-pdf --pdf-url <pinned url>`: import, keep a PDF the import already attached,
otherwise Find Available PDF, otherwise attach from the pinned URL. Then check that the item has
exactly one PDF and that it is a readable file. Runs only in the isolated container
(`run.py corpus`) with network access, behind the same guards as test_live_zotero.py.

Which step supplied the PDF, whether it is the pinned copy, and PDFs not found or refused by a
publisher are recorded rather than failed: Zotero's resolvers may pick a PMC copy or manuscript,
and publishers block or move files. An import whose bridge call timed out is skipped, since its
outcome is unknown. The run writes `corpus_acquisition.json` next to the test config. Pinned corpus
bytes for quality work still come from tools/fetch_public_pdf_corpus.py on the host.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from test_live_zotero import _check_pdf_until_found, _cli, live as live  # noqa: F401 -- imported pytest fixture
from zotero_pdf_text.config import load_config
from zotero_pdf_text.identity import resolve_attachment_paths

SOURCES = Path(__file__).resolve().parent.parent / "benchmarks" / "public_pdfs" / "sources.json"
CORPUS = [s for s in json.loads(SOURCES.read_text(encoding="utf-8"))["sources"] if s.get("doi")]
RESULTS: dict[str, dict[str, Any]] = {}


@pytest.fixture(scope="module", autouse=True)
def report(live: dict[str, Any]):
    yield
    path = Path(live["config_path"]).parent / "corpus_acquisition.json"
    path.write_text(json.dumps(RESULTS, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for result in RESULTS.values():
        label = f"{result['pdf_outcome']}/{result['pdf']}"
        counts[label] = counts.get(label, 0) + 1
    print(f"\ncorpus acquisition: {counts}; details in {path}")


@pytest.mark.live_zotero_corpus
@pytest.mark.parametrize("source", CORPUS, ids=[s["id"] for s in CORPUS])
def test_corpus_doi_imports_with_one_usable_pdf(live: dict[str, Any], source: dict[str, Any]) -> None:
    config_path: Path = live["config_path"]
    result: dict[str, Any] = {
        "doi": source["doi"],
        "publisher_refuses_scripts": "refuses scripted downloads" in str(source.get("note", "")),
        "pdf_outcome": "not_run", "pdf": "none",
    }
    RESULTS[source["id"]] = result

    _, imported, text = _cli(
        config_path, None, "import-doi", "--doi", source["doi"], "--with-pdf", "--pdf-url", source["url"],
    )
    if imported.get("outcome") == "unknown":
        result["pdf_outcome"] = "import_unknown"
        pytest.skip(f"import-doi timed out for {source['doi']}; outcome unknown")
    assert imported.get("status") == "imported", f"import-doi failed for {source['doi']}:\n{text}"
    item_key = str(imported["key"])
    steps = imported.get("steps") or []
    result.update(
        item_type=imported.get("item_type"),
        pdf_outcome=imported.get("pdf_outcome"),
        steps=[{k: s.get(k) for k in ("step", "outcome", "error") if s.get(k)} for s in steps],
    )

    outcome = result["pdf_outcome"]
    if outcome in ("unknown", "unsettled"):
        pytest.skip(f"Zotero did not settle the PDF step for {source['doi']} ({outcome})")
    if outcome == "error":
        # Only the final download from the pinned URL may fail: that is the publisher refusing it.
        assert steps and steps[-1].get("step") == "link-pdf --url", f"PDF chain failed before the URL step:\n{text}"
    else:
        assert outcome in ("attached", "already_has_pdf", "not_found"), f"unexpected PDF outcome:\n{text}"

    after = _check_pdf_until_found(config_path, item_key, attempts=5)
    assert after.get("source") == "debug_bridge" and isinstance(after.get("found"), bool), (
        f"check-pdf did not read Zotero live, so the PDF state is unknown: {after}"
    )
    if not after["found"]:
        assert outcome not in ("attached", "already_has_pdf"), after
        return
    assert len(after["attachments"]) == 1, f"item {item_key} has more than one PDF: {after}"
    linked_root = load_config(config_path).linked_attachments
    files = [p for p in resolve_attachment_paths(after["attachments"][0]["path"], linked_root) if p.is_file()]
    assert files, f"attachment of {item_key} has no file on disk: {after}"
    data = files[0].read_bytes()
    with pymupdf.open(stream=data, filetype="pdf") as document:
        assert document.page_count > 0, f"attachment of {item_key} has no pages"
    result["pdf"] = "pinned" if hashlib.sha256(data).hexdigest() == source["sha256"] else "other_copy"
