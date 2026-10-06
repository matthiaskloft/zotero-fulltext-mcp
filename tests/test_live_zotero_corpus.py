"""Opt-in acquisition of the public corpus through real Zotero (docs/public-corpus-uses.md, uses 3-5).

For every `benchmarks/public_pdfs/sources.json` entry with a DOI: import it, ask Zotero to find a free
PDF, and check that the attachment is a readable PDF that check-pdf sees. Runs only in the isolated
container (`run.py corpus`) with network access, behind the same guards as test_live_zotero.py.

A different copy than the pinned one, or no PDF found, is recorded rather than failed: Zotero's
resolvers may pick a PMC copy or manuscript, and publishers block or move files. The run writes
`corpus_acquisition.json` next to the test config. Pinned corpus bytes for quality work still come
from tools/fetch_public_pdf_corpus.py on the host.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from test_live_zotero import _check_pdf_until_found, _cli, live as live  # noqa: F401 -- imported pytest fixture
from zotero_pdf_text.config import load_config
from zotero_pdf_text.identity import resolve_attachment_paths

SOURCES = Path(__file__).resolve().parent.parent / "benchmarks" / "public_pdfs" / "sources.json"
CORPUS = [s for s in json.loads(SOURCES.read_text(encoding="utf-8"))["sources"] if s.get("doi")]
RESULTS: dict[str, dict[str, Any]] = {}


def _manual(source: dict[str, Any]) -> bool:
    return "manual" in str(source.get("note", "")).lower()


@pytest.fixture(scope="module", autouse=True)
def report(live: dict[str, Any]):
    yield
    path = Path(live["config_path"]).parent / "corpus_acquisition.json"
    path.write_text(json.dumps(RESULTS, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for result in RESULTS.values():
        counts[result["pdf"]] = counts.get(result["pdf"], 0) + 1
    print(f"\ncorpus acquisition: {counts}; details in {path}")


@pytest.mark.live_zotero_corpus
@pytest.mark.parametrize("source", CORPUS, ids=[s["id"] for s in CORPUS])
def test_corpus_doi_imports_and_finds_a_usable_pdf(live: dict[str, Any], source: dict[str, Any]) -> None:
    config_path: Path = live["config_path"]
    result: dict[str, Any] = {"doi": source["doi"], "manual_download": _manual(source), "pdf": "not_run"}
    RESULTS[source["id"]] = result

    rc, imported, text = _cli(config_path, None, "import-doi", "--doi", source["doi"])
    assert rc == 0, f"import-doi failed for {source['doi']}:\n{text}"
    assert imported.get("status") == "imported", imported
    item_key = str(imported["key"])
    result.update(item_type=imported.get("item_type"), pdf="not_found")

    rc, found, text = _cli(config_path, None, "find-pdf", "--key", item_key)
    assert rc == 0, text
    result["find_outcome"] = found.get("outcome")
    if found.get("outcome") != "attached":
        return

    after = _check_pdf_until_found(config_path, item_key)
    assert after.get("found"), f"check-pdf never saw the PDF attached to {item_key}: {after}"
    assert len(after["attachments"]) == 1, after
    linked_root = load_config(config_path).linked_attachments
    files = [p for p in resolve_attachment_paths(after["attachments"][0]["path"], linked_root) if p.is_file()]
    assert files, f"attachment of {item_key} has no file on disk: {after}"
    data = files[0].read_bytes()
    assert data.startswith(b"%PDF"), f"attachment of {item_key} is not a PDF"
    result["pdf"] = "pinned" if hashlib.sha256(data).hexdigest() == source["sha256"] else "other_copy"
