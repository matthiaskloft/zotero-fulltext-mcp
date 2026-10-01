"""Deterministic integration tests in real Zotero, with no publisher/DOI requests."""

from __future__ import annotations

import json
import importlib.util
import os
import sys
import time
from pathlib import Path

import pymupdf
import pytest

from test_end_to_end import _call_server, _fetch, _keys
from test_live_zotero import _cli, live as live  # noqa: F401 -- imported pytest fixture
from zotero_pdf_text.bibtex import execute_javascript
from zotero_pdf_text.config import load_config
from zotero_pdf_text.fts import chunk_sha256

pytestmark = [
    pytest.mark.live_zotero_container,
    pytest.mark.skipif(os.environ.get("ZOTERO_LIVE_CONTAINER") != "1", reason="run in the isolated Zotero container"),
]

PAPERS = (
    {"title": "Synthetic Zebrafinch Estimator", "doi": "10.1000/container.fixture.1",
     "author": "Doe", "year": "2020", "citation_key": "fixture2020",
     "body": "Heteroscedasticity drives the zebrafinch estimator. Known source evidence."},
    {"title": "Synthetic Quokka Comparison", "doi": "10.1000/container.fixture.2",
     "author": "Roe", "year": "2024", "citation_key": "fixture2024",
     "body": "A quokka appears only in this comparison. References: Jane Doe, Zebrafinch Estimator, 2020."},
)


@pytest.fixture(scope="module")
def seeded(live: dict) -> list[dict]:
    """Seed through Zotero's own API, after the existing profile guards have passed."""
    config = load_config(live["config_path"])
    source_dir = config.zotero_root / "seed-pdfs"
    source_dir.mkdir(exist_ok=True)
    results = []
    for index, paper in enumerate(PAPERS):
        pdf = source_dir / f"fixture-{index}.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), f"{paper['title']}\ndoi:{paper['doi']}\n{paper['body']}")
            document.save(pdf)
        script = """
const p = PAPER;
const search = new Zotero.Search();
search.libraryID = Zotero.Libraries.userLibraryID;
search.addCondition('DOI', 'is', p.doi);
const ids = await search.search();
if (ids.length > 1) throw new Error('Duplicate synthetic fixture items');
let item;
if (ids.length) {
    item = await Zotero.Items.getAsync(ids[0]);
} else {
    item = new Zotero.Item('journalArticle');
    item.setField('title', p.title);
    item.setField('DOI', p.doi);
    item.setField('date', p.year);
    item.setField('abstractNote', 'Synthetic abstract for retrieval evaluation.');
    item.setField('extra', 'Citation Key: ' + p.citation_key);
    item.setCreators([{firstName: 'Jane', lastName: p.author, creatorType: 'author'}]);
    item.addTag('synthetic-test');
    await item.saveTx();
}
if (!item.getAttachments().length) {
    await Zotero.Attachments.importFromFile({file: PDF, parentItemID: item.id, contentType: 'application/pdf'});
}
return {key: item.key};
""".replace("PAPER", json.dumps(paper)).replace("PDF", json.dumps(str(pdf)))
        created = execute_javascript(script, timeout=30)
        assert created.ok, created.error
        key = created.result["key"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            rc, attached, output = _cli(live["config_path"], None, "check-pdf", "--key", key, "--json")
            assert rc == 0 and attached.get("source") == "debug_bridge", output
            attachments = attached.get("attachments", [])
            if len(attachments) == 1 and str(attachments[0].get("path", "")).startswith("attachments:"):
                break
            time.sleep(1)
        else:
            pytest.fail("ZotMoov did not settle the synthetic PDF as a linked attachment within 60 seconds")
        path = attachments[0]["path"]
        if path.startswith("attachments:"):
            path = str(config.linked_attachments / path.removeprefix("attachments:"))
        assert Path(path).resolve().is_relative_to(config.linked_attachments.resolve())
        assert Path(path).is_file()
        results.append({**paper, "parent_key": key, "attachment_key": attachments[0]["key"]})
    return results


def test_live_checks_see_committed_items_and_refuse_duplicate_imports(live: dict, seeded: list[dict]) -> None:
    for paper in seeded:
        rc, result, output = _cli(live["config_path"], None, "import-doi", "--doi", paper["doi"])
        assert rc == 0, output
        assert result["status"] == "already_in_library", result
        assert result["key"] == paper["parent_key"], result
        assert result["duplicate_check"] == {"source": "debug_bridge", "live": True}, result


def test_conversion_filters_citations_and_repeat_run(live: dict, seeded: list[dict]) -> None:
    config_path = live["config_path"]
    config = load_config(config_path)
    rc, _, output = _cli(config_path, None, "convert-new", "--workers", "1", timeout=900)
    assert rc == 0, output
    command = [sys.executable, "-m", "zotero_pdf_text.mcp_server", "--db",
               str(config.output_root / "index" / "zotero_text_index.sqlite"), "--config", str(config_path)]
    _, (body, author, year, citation) = _call_server(command, [
        ("search_fulltext", {"query": "heteroscedasticity"}),
        ("search_fulltext", {"author": "Doe"}),
        ("search_fulltext", {"title": "Synthetic", "year_from": 2024, "year_to": 2024}),
        ("lookup_citation_key", {"citation_key": "fixture2020"}),
    ])
    for result in (body, author, year, citation):
        assert not result.isError, result
    assert _keys(body) == [seeded[0]["attachment_key"]]
    assert _keys(author) == [seeded[0]["attachment_key"]]
    assert _keys(year) == [seeded[1]["attachment_key"]]
    assert seeded[0]["attachment_key"] in json.dumps(citation.structuredContent)
    hit = body.structuredContent["results"][0]
    _, (passage,) = _call_server(command, [_fetch(hit)])
    assert not passage.isError, passage
    assert seeded[0]["body"] in " ".join(passage.structuredContent["text"].split())
    assert chunk_sha256(passage.structuredContent["text"]) == hit["source_locator"]["chunk_sha256"]
    rc, _, output = _cli(config_path, None, "convert-new", "--workers", "1", timeout=900)
    assert rc == 0 and "Index is up to date" in output, output
    _, (unchanged,) = _call_server(command, [_fetch(hit)])
    assert not unchanged.isError, unchanged
    assert unchanged.structuredContent["text"] == passage.structuredContent["text"]


def test_manual_agent_harness_reads_live_state_and_refuses_nonempty_baseline(live: dict, seeded: list[dict], tmp_path, monkeypatch) -> None:
    path = Path(__file__).resolve().parents[1] / "containers/live-zotero/agent_workflow.py"
    spec = importlib.util.spec_from_file_location("agent_workflow", path)
    assert spec and spec.loader
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    monkeypatch.setattr(harness, "ROOT", tmp_path / "agent-task")
    monkeypatch.setattr(harness, "ARTICLE", seeded[0])
    state = harness.snapshot(seeded[0])
    assert len(state["items"]) == 1
    assert state["items"][0]["key"] == seeded[0]["parent_key"]
    assert state["items"][0]["attachments"][0]["linked"]
    assert state["items"][0]["attachments"][0]["pages"] == 1
    with pytest.raises(RuntimeError, match="already present"):
        harness.main(["prepare"])
