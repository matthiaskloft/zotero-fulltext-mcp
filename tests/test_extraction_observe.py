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
    state = {"order": 0, "in_references": False, "entries": []}
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
