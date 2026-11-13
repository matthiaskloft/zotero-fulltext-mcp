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


def test_doi_with_balanced_and_unbalanced_parentheses():
    doi = "10.1016/S0169-7439(95)00013-9"
    md = (
        "## References \n\n"
        f"[1] A. (2020). T. {doi}. \n\n"
        f"[2] B. (2019). U. [{doi}](https://doi.org/{doi}) \n\n"
        f"[3] C. (2018). V. [doi](https://doi.org/{doi}). \n\n"
        "[4] D. (2017). W. (doi:10.1234/x) \n\n"
        "[5] E. (2016). X. [doi](https://doi.org/10.1234/x) \n\n"
    )
    _, state = _blocks(md)
    assert [e["doi"]["value"] for e in state["entries"]] == [doi, doi, doi, "10.1234/x", "10.1234/x"]


# --- reference ordinals, running headers, bullets, run-together unnumbered lists ----------------


def _blocks_with(markdown: str, furniture=frozenset(), in_references=False, page: int = 1):
    state = {"order": 0, "in_references": in_references, "entries": [], "entry_count": 0, "furniture": furniture}
    return observe.build_page_blocks(page, markdown, 0, sha256_text(markdown), state, markdown), state


def test_repeated_page_furniture_is_found_across_pages():
    pages = [
        (1, "ARTICLE \n\nBody one. \n\nNATURE COMMUNICATIONS | (2019) 10:1523 | https://doi.org/10.1/x \n\n"),
        (2, "ARTICLE \n\nBody two. \n\nNATURE COMMUNICATIONS | (2019) 10:1524 | https://doi.org/10.1/y \n\n"),
        (3, "ARTICLE \n\nBody three. \n\n9 \n\n"),
        (4, "Only once \n\nBody four. \n\n9 \n\n"),
    ]
    assert observe.find_furniture(pages) == {"article", "naturecommunications##"}


def test_repeated_header_lines_around_reference_entries_do_not_shift_ordinals():
    furniture = observe.find_furniture([
        (1, "ARTICLE \n\nx \n\nFOOTER | 1 \n\n"), (2, "ARTICLE \n\ny \n\nFOOTER | 2 \n\n"),
        (3, "ARTICLE \n\nz \n\nFOOTER | 3 \n\n"),
    ])
    md = ("ARTICLE \n\n1. Doe, J. A study. 2020.\n2. Roe, R. Another. 2019. \n\n3. Poe, E. Third. 2018. \n\n"
          "9 \n\nFOOTER | 9 \n\n")
    blocks, state = _blocks_with(md, furniture, in_references=True)
    assert [b["type"] for b in blocks] == [
        "header", "reference_entry", "reference_entry", "reference_entry", "page_number", "footer"]
    assert [e["ordinal"] for e in state["entries"]] == [1, 2, 3]
    assert [e["raw"][:2] for e in state["entries"]] == ["1.", "2.", "3."]


def test_a_unique_short_line_is_never_furniture():
    blocks, state = _blocks_with("## References \n\nIbid \n\n1. Doe, J. A study. 2020. \n\n")
    assert [b["type"] for b in blocks] == ["heading", "reference_entry", "reference_entry"]


def test_bullet_markers_are_not_part_of_reference_entries():
    md = (
        "## References \n\n- Alice Doe and Bob Roe. A study. _Journal_ , 1(2):3-4, 2020.\n\n"
        "- Carol Poe. Another study. 2019.\n\n"
    )
    blocks, state = _blocks_with(md)
    assert [b["text"] for b in blocks[1:]] == [
        "Alice Doe and Bob Roe. A study. _Journal_ , 1(2):3-4, 2020.", "Carol Poe. Another study. 2019."]
    assert [e["raw"] for e in state["entries"]] == [b["text"] for b in blocks[1:]]
    for b in blocks:
        assert md[b["output_span"]["start"]:b["output_span"]["end"]] == b["text"]
    # Outside the reference list a bullet item keeps its marker, and a numbered marker is printed text.
    plain, _ = _blocks_with("- item \n\n")
    assert plain[0]["type"] == "list_item" and plain[0]["text"] == "- item"
    numbered, state = _blocks_with("1. Doe, J. A study. \n\n2. Roe, R. Another. \n\n", in_references=True)
    assert [e["raw"][:2] for e in state["entries"]] == ["1.", "2."]


def test_lower_case_unnumbered_list_item_continues_the_previous_entry():
    md = "## References \n\n- Zed Zheng, Xi Xie. An overview – \n\n- of blockchain technology (2017), pp. 557 564 \n\n"
    blocks, state = _blocks_with(md)
    assert [b["type"] for b in blocks] == ["heading", "reference_entry", "reference_entry"]
    assert len(state["entries"]) == 1


def test_unnumbered_entries_run_together_on_one_line_are_split():
    md = (
        "##### References \n\nR Beck, JS Czepluch, Blockchain – The Gateway. In Papers, (Istanbul, 2016) "
        "M Chung, J Kim, The internet. KSII Trans. 10(3), 1311–1320 (2016) K Fanning, DP Centers, Money. "
        "J. Corp. 27(5), 53–57 (2016) https://doi.org/10.1002/jcaf.22179 S Haber, WS Stornetta, Stamps. "
        "J. Cryptol. 3(2), 99–111 (1991) — J Yli-Huumo, D Ko, Where? PLoS One 11(10) (2016) \n\n"
    )
    blocks, state = _blocks(md)
    entries = state["entries"]
    assert [e["raw"].split(",")[0] for e in entries] == ["R Beck", "M Chung", "K Fanning", "S Haber", "J Yli-Huumo"]
    assert [e["ordinal"] for e in entries] == [1, 2, 3, 4, 5]
    assert entries[2]["doi"] == {"status": "present", "value": "10.1002/jcaf.22179"}
    assert entries[3]["raw"].endswith("(1991)")  # the stray dash separator belongs to neither entry
    for b in blocks[1:]:
        assert md[b["output_span"]["start"]:b["output_span"]["end"]] == b["text"]


def test_unnumbered_entries_on_separate_lines_are_split_by_line():
    md = (
        "## References \n\nBeck, R. A study of things. J. Stuff 1, 2-3 (2016).\n"
        "Chung, M. Another study of things. J. Other 4, 5-6 (2015).\n"
        "Fanning, K. Third one. J. More 7, 8-9 (2014) \n\n"
    )
    _, state = _blocks(md)
    assert [e["raw"].split(",")[0] for e in state["entries"]] == ["Beck", "Chung", "Fanning"]


def test_prose_in_an_unnumbered_paragraph_is_not_over_split():
    md = "## References \n\nBeck, R. A study of things in 2016 (see below) M Chung says. Not a new entry here. \n\n"
    _, state = _blocks(md)
    assert len(state["entries"]) == 1
    md = "## References \n\nSmith J. Effects of things. ACM Computing, 5(2), 3-4 (2016) IEEE Trans, vol 3 (2015). \n\n"
    _, state = _blocks(md)
    assert len(state["entries"]) == 1


def test_a_repeated_continued_caption_is_not_furniture():
    pages = [(1, "Table 2. (Continued.) \n\nBody. \n\n"), (2, "Table 2. (Continued.) \n\nBody two. \n\n")]
    assert observe.find_furniture(pages) == frozenset()


def _entries_of(markdown: str):
    _, state = _blocks(markdown)
    return [e["raw"] for e in state["entries"]]


@pytest.mark.parametrize("body", [
    "R Beck, Blockchain basics. In: Handbook of things (eds) JA Smith, B Jones, Wiley (2016)",
    "Doe J, Roe R (2019) Title of it. BMJ Open, 3(2) 1-4",
    "R Beck, A study of CO2 at 5 K Fanning, effects on things, J. Phys. 4(1), 2-3",
    "R Beck, Chapter. In: Things, ed. by JA Smith, B Jones, Wiley, 2016, pp. 1-9",
    "R Beck, A study (2016) BMJ Open, 3(2), 1-4",
])
def test_single_unnumbered_entries_are_never_cut_inline(body):
    assert _entries_of(f"## References \n\n{body} \n\n") == [body]


def test_inline_cut_needs_a_year_closer_or_a_url_tail():
    cut = _entries_of("## References \n\nR Beck, One. J. A 1(2), 3-4 (2016) M Chung, Two. J. B 2016) K Fanning, Three. "
                      "https://x.org/a S Haber, Four. \n\n")
    assert [c.split(",")[0] for c in cut] == ["R Beck", "M Chung", "K Fanning", "S Haber"]


def test_acronym_after_a_year_closer_is_not_an_author():
    body = "R Beck, One. J. A 1(2), 3-4 (2016) BMJ Open, 3(2), 1-4"
    assert _entries_of(f"## References \n\n{body} \n\n") == [body]

WORDS = {1: "Apples", 2: "Bananas", 3: "Cherries", 4: "Dates", 5: "Elderberries"}



def test_repeated_body_lines_are_not_furniture():
    heading = "## Results \n\n"
    pages = [(n, f"{heading}{WORDS[n]} alpha. \n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n") for n in range(1, 6)]
    assert observe.find_furniture(pages) == frozenset()
    # A short line that repeats at the top of some pages but is body text elsewhere stays text.
    pages = [(1, "Summary line \n\nBody one. \n\n"), (2, "Summary line \n\nBody two. \n\n"),
             (3, "".join(f"Filler paragraph {chr(97 + i)}. \n\n" for i in range(9)) + "Summary line \n\n"
                 + "".join(f"Other paragraph {chr(97 + i)}. \n\n" for i in range(9)))]
    assert observe.find_furniture(pages) == frozenset()


def test_header_on_every_page_is_found_and_needs_one_side():
    pages = [(n, f"Journal of Things {n} \n\n{WORDS[n]} one. \n\n{WORDS[n]} two. \n\nFooter text \n\n") for n in range(1, 6)]
    assert observe.find_furniture(pages) == {"journalofthings#", "footertext"}
    # Same line on top of some pages and at the bottom of others is ambiguous: body text.
    mixed = [(1, "Same line \n\nBody. \n\n"), (2, "Body. \n\nSame line \n\n"), (3, "Same line \n\nBody. \n\n")]
    assert observe.find_furniture(mixed) == frozenset()


def test_folio_rules():
    furniture = frozenset()
    blocks, _ = _blocks_with("Body. \n\n12.5 \n\n", furniture)
    assert [b["type"] for b in blocks] == ["paragraph", "paragraph"]
    blocks, _ = _blocks_with("Body. \n\n016004-8 \n\n", furniture)
    assert [b["type"] for b in blocks] == ["paragraph", "page_number"]
    blocks, _ = _blocks_with("9 \n\nBody. \n\nMore. \n\nStill. \n\n", furniture)
    assert blocks[0]["type"] == "paragraph"
    blocks, _ = _blocks_with("Body. \n\n7 \n\nMore. \n\nStill. \n\n", furniture)
    assert [b["type"] for b in blocks] == ["paragraph", "paragraph", "paragraph", "paragraph"]


def test_lower_case_continuation_is_appended_to_the_previous_entry():
    md = "## References \n\n- Zed Zheng, Xi Xie. An overview – \n\n- of blockchain https://doi.org/10.1234/abc.5 \n\n"
    blocks, state = _blocks(md)
    assert len(state["entries"]) == 1
    assert state["entries"][0]["raw"] == "Zed Zheng, Xi Xie. An overview – of blockchain https://doi.org/10.1234/abc.5"
    assert state["entries"][0]["raw_sha256"] == sha256_text(state["entries"][0]["raw"])
    assert state["entries"][0]["doi"] == {"status": "present", "value": "10.1234/abc.5"}


def test_particle_entries_are_entries_not_continuations():
    first = "## References \n\n- van der Waals, J. D. A study. 2020.\n\n- Roe, R. Another. 2019.\n\n"
    _, state = _blocks(first)
    assert [e["raw"][:10] for e in state["entries"]] == ["van der Wa", "Roe, R. An"]
    mid = "## References \n\n- Doe, J. A study of things, J. X 1, 2-3 \n\n- de la Cruz, M. Another study. 2019.\n\n"
    _, state = _blocks(mid)
    assert [e["raw"][:6] for e in state["entries"]] == ["Doe, J", "de la "]


def test_lower_case_item_after_a_closed_entry_is_a_new_entry():
    md = "## References \n\n- Doe, J. A study. 2020.\n\n- some wrapped-looking line\n\n"
    _, state = _blocks(md)
    assert len(state["entries"]) == 2


def test_lower_case_item_with_no_previous_entry_is_not_dropped():
    _, state = _blocks("## References \n\n- of blockchain technology (2017)\n\n")
    assert len(state["entries"]) == 1


@pytest.mark.parametrize("tail", [
    "J Foo, 2016.", "J Foo 3, 1-10.", "J Foo 2016;3:1-10.", "J Foo (2016).", "J Foo (in press).",
    "J Foo (forthcoming).", "J Foo (n.d.).", "J Foo, forthcoming.",
])
def test_inline_cut_after_dotted_year_range_and_press_closers(tail):
    md = f"## References \n\nR Beck, Title. {tail} JS Czepluch, Other (2017). \n\n"
    entries = _entries_of(md)
    assert len(entries) == 2 and entries[1].startswith("JS Czepluch")


def test_inline_cut_still_refuses_editor_and_acronym_contexts():
    for body in [
        "R Beck, Chapter. In: Things (eds) JA Smith, B Jones, Wiley (2016)",
        "R Beck, Title. Proc. JA Smith, B Jones, Wiley (2016)",
        "R Beck, Title (2016). BMJ Open, 3(2), 1-4",
    ]:
        assert _entries_of(f"## References \n\n{body} \n\n") == [body]


def test_line_boundary_closes_on_bare_year_range_or_forthcoming_but_not_abbreviation():
    cut = _entries_of("## References \n\nBeck, R. A study. J. Foo 2016\nChung, M. Other. J. Bar 3, 1-10\n"
                      "Fanning, K. Third. forthcoming\nHaber, S. Fourth. Berlin. \n\n")
    assert [c.split(",")[0] for c in cut] == ["Beck", "Chung", "Fanning", "Haber"]
    held = _entries_of("## References \n\nBeck, R. A study. Proc.\nChung, M. Other. J. Phys.\n\n")
    assert len(held) == 1


def test_doi_url_or_pages_fragment_always_continues_the_previous_entry():
    md = "## References \n\n- Doe, J. A study. 2020.\n\n- https://doi.org/10.1234/abc.5\n\n- pp. 12-14\n\n"
    _, state = _blocks(md)
    assert len(state["entries"]) == 1
    assert state["entries"][0]["doi"] == {"status": "present", "value": "10.1234/abc.5"}
    assert state["entries"][0]["raw"].endswith("pp. 12-14")
    # With no previous entry such a fragment is still kept as an entry.
    _, state = _blocks("## References \n\n- https://doi.org/10.1234/abc.5\n\n")
    assert len(state["entries"]) == 1


def test_abbreviation_final_entry_stays_open_for_a_continuation():
    md = "## References \n\n- Doe, J. A study. Proc.\n\n- of the things (2019)\n\n"
    _, state = _blocks(md)
    assert len(state["entries"]) == 1


def test_two_page_documents_need_a_page_number_token_for_furniture():
    plain = [(1, "Running title \n\nA. \n\n"), (2, "Running title \n\nB. \n\n")]
    assert observe.find_furniture(plain) == frozenset()
    numbered = [(1, "Journal of Things 12 \n\nA. \n\n"), (2, "Journal of Things 13 \n\nB. \n\n")]
    assert observe.find_furniture(numbered) == {"journalofthings#"}
    three = plain + [(3, "Running title \n\nC. \n\n")]
    assert observe.find_furniture(three) == {"runningtitle"}


def test_year_or_range_inside_a_reference_list_is_not_a_folio():
    blocks, _ = _blocks_with("1. Doe, J. A study. \n\n2020 \n\n", in_references=True)
    assert blocks[-1]["type"] == "reference_entry"
    blocks, _ = _blocks_with("1. Doe, J. A study. \n\n12-14 \n\n", in_references=True)
    assert blocks[-1]["type"] == "reference_entry"
    blocks, _ = _blocks_with("1. Doe, J. A study. \n\n57 \n\n", in_references=True)
    assert blocks[-1]["type"] == "page_number"
    blocks, _ = _blocks_with("Body. \n\n2020 \n\n")
    assert blocks[-1]["type"] == "page_number"


def test_line_start_acronym_is_not_an_entry_start():
    body = "Doe J, Roe R (2019) Title of it.\nBMJ Open, 3(2) 1-4"
    assert len(_entries_of(f"## References \n\n{body} \n\n")) == 1


def test_continuation_across_an_omitted_page_does_not_extend_an_emitted_entry(monkeypatch):
    pages = [
        (1, "## References \n\n- Doe, J. A study of the\n\n"),
        (2, "- Roe, R. Another open entry about\n\n"),
        (3, "- things and stuff (2019)\n\n"),
    ]
    from zotero_pdf_text import converter

    monkeypatch.setattr(observe, "_extract_pages", lambda *a, **k: (pages, False, Path(".")))
    monkeypatch.setattr(converter, "_run_extractor", lambda *a, **k: None)
    whole = observe_pdf(CORPUS_PDF, pages=[1, 2, 3])
    assert [e["raw"] for e in whole.observation["references"]["entries"]] == [
        "Doe, J. A study of the", "Roe, R. Another open entry about things and stuff (2019)"]
    part = observe_pdf(CORPUS_PDF, pages=[1, 3])
    entries = part.observation["references"]["entries"]
    assert [e["raw"] for e in entries] == ["Doe, J. A study of the"]
    assert [b["type"] for b in part.observation["blocks"] if b["page"] == 3] == ["reference_entry"]
    assert verify_output_spans(part.observation, part.markdown) == []


def test_paragraph_path_particle_first_piece_is_an_entry():
    md = "## References \n\nvan der Waals, J. D. A study. 2020.\nRoe, R. Another. 2019. \n\n"
    entries = _entries_of(md)
    assert [e.split(",")[0] for e in entries] == ["van der Waals", "Roe"]


def test_paragraph_path_lower_case_first_piece_continues_an_open_entry():
    md = "## References \n\nDoe, J. A study of the \n\nthings and stuff (2019).\nRoe, R. Another. 2019. \n\n"
    _, state = _blocks(md)
    assert [e["raw"] for e in state["entries"]] == ["Doe, J. A study of the things and stuff (2019).", "Roe, R. Another. 2019."]


def test_omitted_page_fragment_closes_the_document_predecessor(monkeypatch):
    pages = [
        (1, "## References \n\n- Doe, J. A study of the\n\n"),
        (2, "- things (2019).\n\n"),
        (3, "- eLife Editorial Team. New report (2020).\n\n"),
    ]
    from zotero_pdf_text import converter

    monkeypatch.setattr(observe, "_extract_pages", lambda *a, **k: (pages, False, Path(".")))
    monkeypatch.setattr(converter, "_run_extractor", lambda *a, **k: None)
    whole = observe_pdf(CORPUS_PDF, pages=[1, 2, 3])
    assert [e["raw"] for e in whole.observation["references"]["entries"]] == [
        "Doe, J. A study of the things (2019).", "eLife Editorial Team. New report (2020)."]
    part = observe_pdf(CORPUS_PDF, pages=[1, 3])
    entries = part.observation["references"]["entries"]
    # Same boundaries as the whole run for the selected pages; the omitted page's text is not copied.
    assert [e["raw"] for e in entries] == ["Doe, J. A study of the", "eLife Editorial Team. New report (2020)."]
    assert [e["ordinal"] for e in entries] == [1, 2]


def test_folio_decision_follows_the_references_heading_on_the_same_page():
    body = "Doe, J. A study. \n\n12-14 \n\n"
    same_page, _ = _blocks_with("## References \n\n" + body)
    previous_page, _ = _blocks_with(body, in_references=True)
    assert [b["type"] for b in same_page][1:] == [b["type"] for b in previous_page] == ["reference_entry", "reference_entry"]
    # A heading that ends the list on the same page makes the number a folio again.
    after, _ = _blocks_with("## Appendix \n\nText. \n\n12-14 \n\n", in_references=True)
    assert after[-1]["type"] == "page_number"


def test_article_page_folio_stays_a_folio_inside_a_reference_list():
    blocks, _ = _blocks_with("## References \n\n1. Doe, J. A study. \n\n016004-8 \n\n")
    assert blocks[-1]["type"] == "page_number"


def test_continuation_block_is_chained_and_validates_across_pages(monkeypatch):
    pages = [
        (1, "## References \n\n- Doe, J. A study of the\n\n"),
        (2, "- things https://doi.org/10.1234/abc.5\n\n"),
    ]
    from zotero_pdf_text import converter

    monkeypatch.setattr(observe, "_extract_pages", lambda *a, **k: (pages, False, Path(".")))
    monkeypatch.setattr(converter, "_run_extractor", lambda *a, **k: None)
    res = observe_pdf(CORPUS_PDF, pages=[1, 2])  # validate_record runs inside observe_pdf
    blocks = [b for b in res.observation["blocks"] if b["type"] == "reference_entry"]
    assert blocks[1]["continues_from"] == blocks[0]["id"]
    (entry,) = res.observation["references"]["entries"]
    assert entry["block_id"] == blocks[0]["id"] and entry["end_page"] == 2
    assert entry["doi"] == {"status": "present", "value": "10.1234/abc.5"}
