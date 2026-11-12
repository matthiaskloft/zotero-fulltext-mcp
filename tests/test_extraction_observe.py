"""Observation exporter: runs the real production extractor on the committed synthetic corpus."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pymupdf4llm")
pytest.importorskip("jsonschema")

from extraction import validate_record, verify_output_spans  # noqa: E402
from extraction import observe  # noqa: E402
from extraction.observe import ExtractionFailed, ObservationResult, observe_pdf  # noqa: E402
from extraction.records import sha256_bytes, sha256_text  # noqa: E402
from extraction.scorer import score_records  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS_PDF = ROOT / "tests" / "fixtures" / "ocr_corpus" / "corpus.pdf"
EXAMPLE = ROOT / "benchmarks" / "extraction" / "examples" / "ocr_corpus_synthetic_example.draft_expectation.json"


@pytest.fixture(scope="module")
def result() -> ObservationResult:
    return observe_pdf(CORPUS_PDF, pages=[1, 3])


def _types(res: ObservationResult, page: int) -> list[str]:
    return [b["type"] for b in res.observation["blocks"] if b["page"] == page]


def test_record_is_schema_valid_and_pinned(result):
    obs = result.observation
    assert validate_record(obs) == "observation"
    assert obs["document"]["pdf_sha256"] == sha256_bytes(CORPUS_PDF.read_bytes())
    assert obs["document"]["page_count"] == 6
    assert obs["scope"] == {"kind": "pages", "physical_pages": [1, 3]}
    ident = obs["provenance"]["produced_by"]
    assert ident["tool"] == "pymupdf4llm.to_markdown" and "pymupdf4llm" in ident["version"]
    assert result.extractor["options"]["image_ocr"] is False
    assert result.extractor["options"]["pymupdf4llm_version"] and result.extractor["options"]["pymupdf_version"]
    assert result.elapsed_s > 0
    assert all("bbox" not in b for b in obs["blocks"]) and all("render" not in p for p in obs["pages"])


def test_output_spans_round_trip(result):
    assert verify_output_spans(result.observation, result.markdown) == []
    for block in result.observation["blocks"]:
        span = block["output_span"]
        sliced = result.markdown[span["start"]:span["end"]]
        assert sha256_text(sliced) == span["span_sha256"]
        if isinstance(block.get("text"), str):
            assert block["text"] == sliced
    assert [b["reading_order"] for b in result.observation["blocks"]] == list(range(len(result.observation["blocks"])))


def test_block_typing_on_corpus(result):
    page1, page3 = _types(result, 1), _types(result, 3)
    assert page1[0] == "heading" and page1[-1] == "page_number"
    assert "paragraph" in page1
    assert "table" in page3 and "caption" in page3 and page3[-1] == "page_number"
    table = next(b for b in result.observation["blocks"] if b["type"] == "table")
    cells = {(c["row"], c["col"]): c["text"] for c in table["table"]["cells"]}
    assert cells[(0, 0)] == "Condition" and cells[(2, 2)] == "1.640" and table["table"]["n_rows"] == 4
    assert table["table"]["cells"][0]["is_header"] is True
    caption = next(b for b in result.observation["blocks"] if b["type"] == "caption")
    assert caption["text"].startswith("**Fig. 1**")
    # a sentence that merely begins with a label is not a caption
    assert not any(b["type"] == "caption" and b["text"].startswith("Table 3 reports") for b in result.observation["blocks"])


def test_whole_document_is_complete_scope():
    res = observe_pdf(CORPUS_PDF)
    assert res.observation["scope"]["kind"] == "complete_document"
    assert len(res.observation["pages"]) == 6
    assert verify_output_spans(res.observation, res.markdown) == []
    # pages subset slices the same text a whole-document run produced
    sub = observe_pdf(CORPUS_PDF, pages=[3])
    assert sub.markdown in res.markdown


def test_scores_against_promoted_synthetic_expectation(result):
    # SYNTHETIC: the draft example is hand-written from corpus.tex, not human-reviewed. It is promoted to
    # "reviewed" in this test only, so the scorer has something to run against. Never baseline data.
    expectation = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    expectation = copy.deepcopy(expectation)
    expectation["record_id"] = "synthetic-test-promotion-not-ground-truth"
    for page in expectation["pages"]:
        page["render"]["image_sha256"] = "0" * 64
    for block in expectation["blocks"]:
        if isinstance(block.get("text"), str):
            block["text_sha256"] = sha256_text(block["text"])
    expectation["provenance"] = {
        "review": {
            "status": "reviewed", "reviewer": "synthetic-test-only", "reviewed_at": "2026-01-01T00:00:00Z",
            "basis": "pdf_inspection",
        },
    }
    assert validate_record(expectation) == "expectation"
    score = score_records(expectation, result.observation, markdown=result.markdown)
    assert score["verdict"] in {"passed", "failed", "incomplete"}
    strict = {(m["name"], m["mode"]): m for m in score["metrics"]}
    assert strict[("output_span_roundtrip", "strict")]["value"] == 1.0
    # Every expected prose block, heading and folio is found on its page, in reading order, and
    # the ruled table matches cell for cell. Equation LaTeX needs crop OCR, the draft covers only part
    # of each page (so text_precision and caption_precision fail), and the corpus prints the next
    # element's lead-in on the caption's line: those mismatches are expected and not asserted.
    assert strict[("text_presence", "strict")]["value"] == 1.0
    assert strict[("reading_order", "strict")]["value"] == 1.0
    assert strict[("table_exact", "strict")]["value"] == 1.0


def test_failure_raises_extraction_failed(tmp_path, monkeypatch):
    not_a_pdf = tmp_path / "broken.pdf"
    not_a_pdf.write_text("this is not a PDF", encoding="utf-8")
    with pytest.raises(ExtractionFailed):
        observe_pdf(not_a_pdf)
    with pytest.raises(ExtractionFailed):
        observe_pdf(tmp_path / "missing.pdf")

    from zotero_pdf_text import converter

    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "extractor", stderr="synthetic extractor crash")

    monkeypatch.setattr(converter, "_run_extractor", boom)
    with pytest.raises(ExtractionFailed, match="synthetic extractor crash"):
        observe_pdf(CORPUS_PDF, pages=[1])


def test_bad_page_number_is_a_value_error():
    with pytest.raises(ValueError):
        observe_pdf(CORPUS_PDF, pages=[99])
    with pytest.raises(ValueError):
        observe_pdf(CORPUS_PDF, pages=[])


def test_default_run_is_the_production_configuration(result):
    # Production always writes crop images, which leaves ![](...) placeholders; none may leak a temp path.
    assert result.extractor["options"]["pymupdf4llm"]["write_images"] is True
    assert "![" in result.markdown and "observe-" not in result.markdown
    assert any(b["type"] == "figure" and b["graphic"]["kind"] == "image" for b in result.observation["blocks"])


# --- block splitter on literal Markdown (no PDF) ----------------------------------------------


def _blocks(markdown: str, page: int = 1):
    state = {"order": 0, "in_references": False, "entries": [], "entry_count": 0}
    blocks = observe.build_page_blocks(page, markdown, 0, sha256_text(markdown), state, markdown)
    return blocks, state


def test_equations_lists_figures_and_captions():
    md = (
        "## 2. Method \n\nIntro text. \n\n$$\nx = \\frac{a}{b}\n$$\n\n- first item\n- second item\n  wrapped\n\n"
        "![](images/p1.png)\n\n**Fig. 2** A caption. \n\n<!-- Start of picture text -->\nax<br>\n<!-- End of picture text -->\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n**Table 1** Values. \n\n7 \n\n"
    )
    blocks, _ = _blocks(md)
    assert [b["type"] for b in blocks] == [
        "heading", "paragraph", "equation", "list_item", "list_item", "figure", "caption", "figure", "table", "caption", "page_number",
    ]
    assert blocks[2]["equation"] == {"latex": "x = \\frac{a}{b}", "display": True}
    assert blocks[4]["text"] == "- second item\n  wrapped"
    assert blocks[6]["caption_for"] == blocks[5]["id"]
    assert blocks[9]["caption_for"] == blocks[8]["id"]
    for b in blocks:
        assert md[b["output_span"]["start"]:b["output_span"]["end"]] == md[b["output_span"]["start"]:b["output_span"]["end"]].strip()


def test_references_under_heading_and_doi():
    md = (
        "## **References** \n\n[1] Doe, J. (2020). A study. Journal 1, 1-2. https://doi.org/10.1234/abc.def. \n\n"
        "[2] Roe, R. (2019). Another one. \n\n## Appendix \n\nBack to prose. \n\n"
    )
    blocks, state = _blocks(md)
    assert [b["type"] for b in blocks] == ["heading", "reference_entry", "reference_entry", "heading", "paragraph"]
    entries = state["entries"]
    assert [e["ordinal"] for e in entries] == [1, 2]
    assert entries[0]["doi"] == {"status": "present", "value": "10.1234/abc.def"}
    assert entries[1]["doi"] == {"status": "not_found"}
    assert entries[0]["raw"] == blocks[1]["text"] and entries[0]["block_id"] == blocks[1]["id"]


def _fake_ocr(monkeypatch, *, ok=True):
    from types import SimpleNamespace

    from zotero_pdf_text import image_ocr

    monkeypatch.setattr(image_ocr, "probe", lambda base_url, model, **kw: SimpleNamespace(ok=ok, detail="server down"))
    monkeypatch.setattr(image_ocr, "generate", lambda base_url, model, prompt, png, timeout=0: "x = \\alpha + 1")


def test_image_ocr_option_uses_package_ocr_path(monkeypatch):
    _fake_ocr(monkeypatch)
    res = observe_pdf(CORPUS_PDF, pages=[1], options={"image_ocr": True})
    assert res.extractor["options"]["image_ocr"] is True and res.observation["provenance"]["produced_by"]["model"]
    assert verify_output_spans(res.observation, res.markdown) == []
    equations = [b for b in res.observation["blocks"] if b["type"] == "equation"]
    assert equations and all(b["equation"]["latex"] == "x = \\alpha + 1" for b in equations)
    # no temporary path leaks into the Markdown the offsets refer to
    assert "observe-" not in res.markdown


def test_image_ocr_unavailable_is_extraction_failed(monkeypatch):
    _fake_ocr(monkeypatch, ok=False)
    with pytest.raises(ExtractionFailed, match="server down"):
        observe_pdf(CORPUS_PDF, pages=[1], options={"image_ocr": True})


def test_reference_tail_from_previous_page_is_kept():
    md = "## References \n\npp. 12-34.\n[7] Doe, J. (2020). A study.\n[8] Roe, R. (2019). Another. \n\n"
    blocks, state = _blocks(md)
    assert [b["type"] for b in blocks] == ["heading", "reference_entry", "reference_entry", "reference_entry"]
    assert blocks[1]["text"] == "pp. 12-34."
    assert len(state["entries"]) == 2 and state["entries"][0]["raw"].startswith("[7]")
    covered = "".join(md[b["output_span"]["start"]:b["output_span"]["end"]] for b in blocks)
    assert "".join(covered.split()) == "".join(md.split())


def test_fully_bold_caption_and_strict_folio():
    blocks, _ = _blocks("**Table 1. Descriptive statistics** \n\ncivil \n\n")
    assert [b["type"] for b in blocks] == ["caption", "paragraph"]
    for word in ("civil", "mix", "mild"):
        assert not observe._ROMAN_FOLIO.match(word)
    for folio in ("12", "iv", "XIV", "xxxix"):
        assert observe._ROMAN_FOLIO.match(folio)
    blocks, _ = _blocks("Text. \n\nxii \n\n")
    assert blocks[-1]["type"] == "page_number"


def test_identity_pins_package_version(result):
    options = result.extractor["options"]
    assert "zotero_pdf_text_version" in options and "zotero_pdf_text_git" in options


def test_ocr_plans_on_joined_markdown(monkeypatch):
    from zotero_pdf_text import image_ocr

    seen = []
    real = image_ocr.plan_crops
    monkeypatch.setattr(image_ocr, "plan_crops", lambda body, *a, **k: seen.append(body) or real(body, *a, **k))
    _fake_ocr(monkeypatch)
    res = observe_pdf(CORPUS_PDF, pages=[1], options={"image_ocr": True})
    assert len(seen) == 1 and seen[0].count("CORPUSMARK-FIG-006") == 1  # whole document, one call
    assert verify_output_spans(res.observation, res.markdown) == []


def test_failed_crop_ocr_is_extraction_failed(monkeypatch):
    from zotero_pdf_text import image_ocr
    from zotero_pdf_text._ollama_client import OllamaError

    _fake_ocr(monkeypatch)
    calls = []

    def flaky(base_url, model, prompt, png, timeout=0):
        calls.append(png)
        if len(calls) == 1:
            raise OllamaError("synthetic crop failure")
        return "x = 1"

    monkeypatch.setattr(image_ocr, "generate", flaky)
    with pytest.raises(ExtractionFailed, match="synthetic crop failure"):
        observe_pdf(CORPUS_PDF, pages=[1], options={"image_ocr": True})


def _shape(blocks):
    return [(b["id"], b["type"], b["page"], b.get("text")) for b in blocks]


def test_section_state_carries_across_unselected_pages(monkeypatch):
    # Page 1 opens the reference list; page 2 continues it. Page-scoped and whole-document runs must agree.
    pages = [(1, "## References \n\n[1] Doe, J. (2020). A study. \n\n"), (2, "[2] Roe, R. (2019). Another. \n\n")]
    from zotero_pdf_text import converter

    monkeypatch.setattr(observe, "_extract_pages", lambda *a, **k: (pages, False, Path(".")))
    monkeypatch.setattr(converter, "_run_extractor", lambda *a, **k: None)
    whole = observe_pdf(CORPUS_PDF, pages=[1, 2])
    only2 = observe_pdf(CORPUS_PDF, pages=[2])
    assert [b["type"] for b in only2.observation["blocks"]] == ["reference_entry"]
    assert _shape(only2.observation["blocks"]) == _shape([b for b in whole.observation["blocks"] if b["page"] == 2])
    assert verify_output_spans(only2.observation, only2.markdown) == []


def test_heading_formatted_caption_keeps_reference_context():
    md = "## References \n\n[1] Doe, J. (2020). A study. \n\n### **Fig. 1** A caption. \n\n[2] Roe, R. (2019). Another. \n\n"
    blocks, state = _blocks(md)
    assert [b["type"] for b in blocks] == ["heading", "reference_entry", "caption", "reference_entry"]
    assert len(state["entries"]) == 2


def test_extractor_identity_has_tool_and_model(monkeypatch, result):
    assert result.extractor["tool"] == "pymupdf4llm.to_markdown" and "model" not in result.extractor
    _fake_ocr(monkeypatch)
    res = observe_pdf(CORPUS_PDF, pages=[1], options={"image_ocr": True})
    assert res.extractor["tool"] == "pymupdf4llm.to_markdown" and res.extractor["model"]


def test_real_harness_records_the_real_tool(tmp_path):
    from extraction import drafts, run_pilot

    cache = tmp_path / "cache"
    cache.mkdir()
    pdf = cache / "corpus-synth.pdf"
    pdf.write_bytes(CORPUS_PDF.read_bytes())
    digest = sha256_bytes(pdf.read_bytes())
    manifest = {"seed": "test-seed", "held_out": [], "pilot": [{
        "id": "corpus-synth", "role": "stratum_pages", "stratum": "non-english", "basis": "assumed",
        "pdf_sha256": digest, "pdf_pages": 6, "candidate_pages": [1],
    }]}
    drafts.run(manifest, cache, tmp_path / "drafts-out", titles={"corpus-synth": "Synthetic OCR Validation Corpus"})
    draft = tmp_path / "drafts-out" / "drafts" / "pilot-corpus-synth.expectation.draft.json"
    record = json.loads(draft.read_text(encoding="utf-8"))
    # SYNTHETIC: a hand-promoted copy of a draft of the committed synthetic corpus; not ground truth.
    record["provenance"]["review"] = {
        "status": "reviewed", "reviewer": "synthetic-test-only", "reviewed_at": "2026-11-06T10:00:00Z",
        "basis": "page_image_inspection",
    }
    record["provenance"].pop("uncertainty")
    text = "This document exists to be converted, not read."
    record["blocks"] = [{
        "id": "b1", "type": "paragraph", "page": 1, "reading_order": 1, "text": text, "text_sha256": sha256_text(text),
        "bbox": {"x0": 10, "y0": 10, "x1": 200, "y1": 40},
    }]
    expectations = tmp_path / "expectations"
    expectations.mkdir()
    (expectations / "corpus-synth.json").write_text(json.dumps(record), encoding="utf-8")
    outcome = run_pilot.run_pilot(expectations, cache, tmp_path / "out", manifest, extractor=observe_pdf, timeout=300)
    config = outcome["run"]["configurations"][0]
    assert config["tool"] == "pymupdf4llm.to_markdown" and config["tool"] != "unknown"
    assert config["version"].startswith("pymupdf4llm ")
    assert all(r["status"] == "ok" for r in outcome["run"]["results"])


def test_doi_in_markdown_links_and_bare():
    md = (
        "## References \n\n[1] A. (2020). T. [10.1234/abc.def](https://doi.org/10.1234/abc.def) \n\n"
        "[2] B. (2019). U. [doi](https://doi.org/10.5555/xyz-1). \n\n[3] C. (2018). V. doi:10.7777/q.r. \n\n"
    )
    _, state = _blocks(md)
    assert [e["doi"]["value"] for e in state["entries"]] == ["10.1234/abc.def", "10.5555/xyz-1", "10.7777/q.r"]


def test_ordinals_count_through_unselected_pages(monkeypatch):
    pages = [(1, "## References \n\n[1] Doe, J. (2020). A. \n\n"), (2, "[2] Roe, R. (2019). B. \n\n"),
             (3, "[3] Poe, E. (2018). C. \n\n")]
    monkeypatch.setattr(observe, "_extract_pages", lambda *a, **k: (pages, False, Path(".")))
    whole = observe_pdf(CORPUS_PDF, pages=[1, 2, 3])
    skipped = observe_pdf(CORPUS_PDF, pages=[1, 3])
    ordinals = {e["raw"]: e["ordinal"] for e in whole.observation["references"]["entries"]}
    got = {e["raw"]: e["ordinal"] for e in skipped.observation["references"]["entries"]}
    assert got and all(ordinals[raw] == n for raw, n in got.items()) and sorted(got.values()) == [1, 3]
