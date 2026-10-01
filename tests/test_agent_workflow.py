"""Grading tests run on the host, without Zotero or an LLM."""

import copy
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from zotero_pdf_text.fts import chunk_sha256
from zotero_pdf_text.config import ProjectConfig

spec = importlib.util.spec_from_file_location("agent_workflow", Path(__file__).resolve().parents[1] /
                                            "containers/live-zotero/agent_workflow.py")
assert spec and spec.loader
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


@pytest.fixture
def evidence():
    article = dict(harness.ARTICLE)
    text = "Reviewed evidence: a 69% increase in citations."
    digest = chunk_sha256(text)
    state = {"items": [{"key": "PARENT01", "doi": article["doi"], "title": article["title"],
                        "attachments": [{"key": "ATTACH01", "linked": True, "exists": True,
                                         "pages": 5, "sha256": article["pdf_sha256"]}]}],
             "indexed": [{"zotero_attachment_key": "ATTACH01", "zotero_parent_key": "PARENT01", "doi": article["doi"]}]}
    operations = [
        ("lookup-doi", {"doi": article["doi"]}, {"key": None, "live": True}),
        ("import-doi", ["--doi", article["doi"]], {"key": "PARENT01"}),
        ("link-pdf", ["--key", "PARENT01", "--url", article["pdf_url"]], {}),
        ("convert-new", [], {}),
        ("search_fulltext", {"query": "citation"}, {"results": [{"attachment_key": "ATTACH01", "source_locator": {"chunk_sha256": digest, "chunk_index": 0}}]}),
        ("get_fulltext_chunk", {"attachment_key": "ATTACH01", "chunk_index": 0, "chunk_sha256": digest}, {"text": text}),
        ("import-doi", ["--doi", article["doi"]], {"status": "already_in_library", "key": "PARENT01"}),
    ]
    trace = [{"sequence": i, "phase": "initial" if i < 6 else "repeat", "name": name,
              "arguments": arguments, "result": result, "ok": True, "verified_current": True}
             for i, (name, arguments, result) in enumerate(operations)]
    return article, {"items": [], "indexed": []}, state, copy.deepcopy(state), trace


def test_complete_trace_and_independent_state_pass(evidence):
    verdict = harness.assess(*evidence)
    assert verdict["passed"]
    assert len(verdict["checks"]) == 7


def test_final_state_alone_is_insufficient(evidence):
    article, before, after, repeat, _ = evidence
    verdict = harness.assess(article, before, after, repeat, [])
    assert not verdict["passed"]
    assert {c["status"] for c in verdict["checks"]} == {"insufficient_evidence"}


def test_importer_can_acquire_pdf_automatically(evidence):
    article, before, after, repeat, trace = evidence
    trace[1]["state_after"] = copy.deepcopy(after)
    trace = [event for event in trace if event["name"] != "link-pdf"]
    assert harness.assess(article, before, after, repeat, trace)["passed"]


def test_snapshot_uses_published_generation_when_nominal_database_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "guarded_config", lambda: SimpleNamespace(output_root=tmp_path))
    monkeypatch.setattr(harness, "execute_javascript", lambda script: SimpleNamespace(ok=True, result=[]))
    published = tmp_path / "index/generations/example/index.sqlite"
    monkeypatch.setattr(harness, "resolve_reader_db_path", lambda nominal: published)
    def search(db, **kwargs):
        assert db == published
        return [SimpleNamespace(to_dict=lambda: {"doi": harness.ARTICLE["doi"]})]
    monkeypatch.setattr(harness, "search_fts", search)
    assert harness.snapshot(harness.ARTICLE)["indexed"] == [{"doi": harness.ARTICLE["doi"]}]


def test_normal_mcp_schemas_available_before_conversion(tmp_path):
    pytest.importorskip("mcp")
    config = ProjectConfig(zotero_root=tmp_path, zotero_data_directory=tmp_path / "data",
                           linked_attachments=tmp_path / "linked", output_root=tmp_path / "output")
    result, ok = asyncio.run(harness.mcp_call("list", {}, config))
    assert ok
    assert {"search_fulltext", "search_within_fulltext", "get_fulltext_chunk"} <= {t["name"] for t in result["tools"]}


def test_verified_source_passage_does_not_require_exact_prose(evidence):
    article, before, after, repeat, trace = evidence
    trace[5]["result"]["text"] = "Citation appears in a reference list."
    trace[5]["arguments"]["chunk_sha256"] = chunk_sha256(trace[5]["result"]["text"])
    trace[4]["result"]["results"][0]["source_locator"]["chunk_sha256"] = trace[5]["arguments"]["chunk_sha256"]
    verdict = harness.assess(article, before, after, repeat, trace)
    assert verdict["passed"]


def test_repeat_lookup_and_reuse_passes_without_another_import(evidence):
    evidence[-1][-1].update(name="lookup-doi", arguments={"doi": evidence[0]["doi"]},
                           result={"key": "PARENT01", "live": True})
    assert harness.assess(*evidence)["passed"]


def test_repeat_lookup_for_wrong_item_does_not_pass(evidence):
    evidence[-1][-1].update(name="lookup-doi", arguments={"doi": evidence[0]["doi"]},
                           result={"key": "OTHER001", "live": True})
    assert not harness.assess(*evidence)["passed"]


def test_search_locator_must_match_retrieved_chunk_index(evidence):
    evidence[-1][4]["result"]["results"][0]["source_locator"]["chunk_index"] = 1
    assert not harness.assess(*evidence)["passed"]


def test_within_paper_search_is_a_valid_route(evidence):
    evidence[-1][4]["name"] = "search_within_fulltext"
    assert harness.assess(*evidence)["passed"]


@pytest.mark.parametrize("damage, step", [
    ("wrong_pdf", "attach_correct_pdf"), ("wrong_parent", "convert_and_index"),
    ("duplicate", "avoid_duplicates"), ("wrong_hash", "retrieve_verified_passage"),
    ("stale", "retrieve_verified_passage"), ("lookup_after_import", "identify_absence"),
    ("unsuccessful_import", "add_article"), ("no_repeat", "avoid_duplicates"),
])
def test_invalid_or_missing_proof_is_rejected(evidence, damage, step):
    article, before, after, repeat, trace = evidence
    if damage == "wrong_pdf":
        after["items"][0]["attachments"][0]["sha256"] = "wrong"
    elif damage == "wrong_parent":
        after["indexed"][0]["zotero_parent_key"] = "OTHER001"
    elif damage == "duplicate":
        repeat["items"].append(copy.deepcopy(repeat["items"][0]))
    elif damage == "wrong_hash":
        trace[5]["arguments"]["chunk_sha256"] = "wrong"
    elif damage == "stale":
        trace[5]["verified_current"] = False
    elif damage == "lookup_after_import":
        trace[0]["sequence"] = 9
    elif damage == "unsuccessful_import":
        trace[1]["ok"] = False
    elif damage == "no_repeat":
        repeat = None
    verdict = harness.assess(article, before, after, repeat, trace)
    assert not verdict["passed"]
    assert next(c for c in verdict["checks"] if c["step"] == step)["status"] != "pass"


@pytest.mark.parametrize("option", ["--config", "--conf", "--db", "--debug-bridge-endp", "--debug-bridge-tok", "--connector-endp"])
@pytest.mark.parametrize("equals", [False, True])
def test_cli_rejects_isolation_overrides_before_execution(option, equals):
    arguments = ["import-doi", "--doi", harness.ARTICLE["doi"]]
    arguments += [option + "=other"] if equals else [option, "other"]
    with pytest.raises(ValueError, match="override"):
        harness.validate_cli_arguments(arguments)


def test_equals_cli_arguments_grade_like_separate_values(evidence):
    for event in evidence[-1]:
        if event["name"] == "import-doi":
            event["arguments"] = ["--doi=" + evidence[0]["doi"]]
        elif event["name"] == "link-pdf":
            event["arguments"] = ["--key=PARENT01", "--url=" + evidence[0]["pdf_url"]]
    assert harness.assess(*evidence)["passed"]
    harness.validate_cli_arguments(["link-pdf", "--key=PARENT01", "--url=" + evidence[0]["pdf_url"]])


def test_repeat_import_wrong_key_is_insufficient(evidence):
    evidence[-1][-1]["result"]["key"] = "OTHER001"
    assert not harness.assess(*evidence)["passed"]


@pytest.mark.parametrize("key_option", ["--ke", "--k"])
def test_allowed_cli_abbreviations_grade_correctly(evidence, key_option):
    from zotero_pdf_text.cli import build_parser
    arguments = ["link-pdf", key_option + "=PARENT01", "--url=" + evidence[0]["pdf_url"]]
    harness.validate_cli_arguments(arguments)
    assert build_parser().parse_args(arguments).key == "PARENT01"
    evidence[-1][2]["arguments"] = arguments[1:]
    assert harness.assess(*evidence)["passed"]



def test_retrieval_without_search_locator_hash_does_not_pass(evidence):
    del evidence[-1][5]["arguments"]["chunk_sha256"]
    assert not harness.assess(*evidence)["passed"]
