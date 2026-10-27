"""Page maps (#79) and structure-based chunks with page ranges and get_page (#80)."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text import _extract_markdown
from zotero_pdf_text.converter import _publish_page_map, _read_page_texts, _with_front_matter
from zotero_pdf_text.fts import (
    PageMapUnavailableError,
    PageNotFoundError,
    _chunk_text,
    build_fts_index,
    get_fulltext,
    get_page,
    search_fts,
)
from zotero_pdf_text.identity import strip_front_matter
from zotero_pdf_text.indexer import _record_from_manifest_row
from zotero_pdf_text.pages import (
    page_map_for_body,
    page_map_from_fallback_markers,
    page_map_sidecar_path,
    page_range,
    read_page_map_sidecar,
    validate_page_map,
)

try:
    from test_mcp_server import FakeFastMCP, _assert_tool_error, assert_no_local_path
    from zotero_pdf_text.mcp_contract import create_server
except ImportError:  # pragma: no cover - the MCP extra is optional
    FakeFastMCP = None

CORPUS_PDF = Path(__file__).parent / "fixtures" / "ocr_corpus" / "corpus.pdf"


def _reconstructs(page_map, text):
    return "".join(text[start:end] for _page, start, end in page_map) == text


def _record(key, text, page_map=None):
    return {
        "zotero_parent_key": f"P{key}",
        "zotero_attachment_key": key,
        "title": "T",
        "creators": "C",
        "year": "2026",
        "doi": "",
        "citation_key": "",
        "source_path": "s.pdf",
        "markdown_path": "m.md",
        "markdown_sha256": "h",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "char_count": len(text),
        "word_count": len(text.split()),
        "page_count": "",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": False,
        "text": text,
        "page_map": page_map,
    }


class PageMapTests(unittest.TestCase):
    def test_body_map_absorbs_trimmed_whitespace_and_drops_empty_pages(self):
        pages = [(1, "\n\n# Title\n\nOne.\n\n"), (2, ""), (3, "Three.\n\n")]
        body = "".join(text for _page, text in pages).strip()
        page_map = page_map_for_body(pages, body)
        self.assertEqual([page for page, _s, _e in page_map], [1, 3])
        self.assertTrue(_reconstructs(page_map, body))
        self.assertEqual(body[page_map[1][1] : page_map[1][2]], "Three.")

    def test_rewritten_body_has_no_map(self):
        self.assertIsNone(page_map_for_body([(1, "One."), (2, "Two.")], "One. Two (edited)."))

    def test_fallback_markers_yield_a_map_and_unordered_markers_do_not(self):
        text = "## Page 1\n\nOne\n\n## Page 4\n\nFour"
        page_map = page_map_from_fallback_markers(text)
        self.assertEqual(page_map, [(1, 0, 16), (4, 16, len(text))])
        self.assertTrue(_reconstructs(page_map, text))
        self.assertIsNone(page_map_from_fallback_markers("Preamble\n## Page 1\n\nOne"))
        self.assertIsNone(page_map_from_fallback_markers("## Page 2\n\nTwo\n\n## Page 1\n\nOne"))

    def test_validation_rejects_gaps_and_short_maps(self):
        self.assertEqual(validate_page_map([[1, 0, 3], [2, 3, 6]], 6), [(1, 0, 3), (2, 3, 6)])
        self.assertIsNone(validate_page_map([[1, 0, 3], [2, 4, 6]], 6))
        self.assertIsNone(validate_page_map([[1, 0, 3]], 6))
        self.assertIsNone(validate_page_map([[2, 0, 3], [1, 3, 6]], 6))
        self.assertIsNone(validate_page_map(None, 6))

    def test_page_range_uses_the_last_character_of_a_span(self):
        page_map = [(1, 0, 10), (2, 10, 20), (5, 20, 30)]
        self.assertEqual(page_range(page_map, 2, 10), (1, 1))
        self.assertEqual(page_range(page_map, 9, 11), (1, 2))
        self.assertEqual(page_range(page_map, 25, 30), (5, 5))


class ConversionPageMapTests(unittest.TestCase):
    def test_new_conversion_records_a_map_that_reconstructs_the_stored_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "paper.raw.tmp"
            _extract_markdown.main([str(CORPUS_PDF), str(raw)])
            raw_markdown = raw.read_text(encoding="utf-8")
            page_texts = _read_page_texts(raw.with_suffix(".pages.json"), raw_markdown)
            output = root / "paper.md"
            row = {"zotero_attachment_key": "A1", "extraction_tool": "pymupdf4llm.to_markdown", "output_path": str(output)}
            output.write_text(_with_front_matter(row, raw_markdown), encoding="utf-8", newline="\n")
            _publish_page_map(output, page_texts)

            record = _record_from_manifest_row(row)
            self.assertIsNotNone(record.page_map)
            self.assertGreater(len(record.page_map), 1)
            self.assertTrue(_reconstructs(record.page_map, record.text))

            # A later rewrite of the body (equation OCR, enrichment) leaves the map unusable.
            output.write_text(_with_front_matter(row, raw_markdown + "\n\nAppended."), encoding="utf-8", newline="\n")
            self.assertIsNone(_record_from_manifest_row(row).page_map)

    def test_mismatched_page_lengths_publish_no_map_and_remove_a_stale_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sidecar = root / "paper.raw.pages.json"
            sidecar.write_text(json.dumps([[1, 3], [2, 99]]), encoding="utf-8")
            self.assertEqual(_read_page_texts(sidecar, "One two"), [])
            output = root / "paper.md"
            output.write_text("Body", encoding="utf-8")
            page_map_sidecar_path(output).write_text("{}", encoding="utf-8")
            _publish_page_map(output, [])
            self.assertFalse(page_map_sidecar_path(output).exists())

    def test_fallback_record_without_sidecar_parses_its_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "paper.md"
            body = "## Page 1\n\nOne\n\n## Page 2\n\nTwo"
            row = {"zotero_attachment_key": "A1", "extraction_tool": "pymupdf.get_text", "output_path": str(output)}
            output.write_text(_with_front_matter(row, body, "pymupdf.get_text"), encoding="utf-8")
            record = _record_from_manifest_row(row)
            self.assertEqual([page for page, _s, _e in record.page_map], [1, 2])
            self.assertIsNone(read_page_map_sidecar(output, strip_front_matter(output.read_text(encoding="utf-8"))))

    def test_old_primary_record_without_sidecar_stays_page_less(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "paper.md"
            row = {"zotero_attachment_key": "A1", "extraction_tool": "pymupdf4llm.to_markdown", "output_path": str(output)}
            output.write_text(_with_front_matter(row, "## Page 1\n\nLooks like a marker"), encoding="utf-8")
            self.assertIsNone(_record_from_manifest_row(row).page_map)


class StructureChunkTests(unittest.TestCase):
    def test_no_chunk_crosses_a_heading_unless_a_section_is_below_the_minimum(self):
        body = "word " * 60
        text = f"# Paper\n\n## Methods\n\n### Participants\n\n{body}\n\n### Measures\n\n{body}\n\n## Results\n\n{body}"
        chunks = list(_chunk_text(text, chunk_chars=400, overlap_chars=40))
        # "# Paper" and "## Methods" are heading-only sections below the minimum: they merge forward.
        self.assertTrue(chunks[0][3].startswith("# Paper"))
        self.assertEqual(
            [chunk[4] for chunk in chunks],
            ["Paper > Methods > Participants", "Paper > Methods > Measures", "Paper > Results"],
        )
        for chunk in chunks[1:]:
            self.assertEqual(chunk[3].count("\n#"), 0)
            self.assertTrue(chunk[3].startswith("#"))

    def test_code_fences_and_fallback_markers_are_not_headings(self):
        prose = "Prose sentence here. " * 30
        text = f"# Real\n\n```\n# comment\nx = 1\n```\n\n{prose}\n\n## Page 2\n\n{prose}"
        paths = {chunk[4] for chunk in _chunk_text(text, chunk_chars=2000, overlap_chars=50)}
        self.assertEqual(paths, {"Real"})

    def test_merged_short_section_keeps_the_heading_its_text_is_under(self):
        chunks = list(_chunk_text("# A\n\nshort\n\n# B\n\ntiny", chunk_chars=400, overlap_chars=40))
        self.assertEqual([chunk[4] for chunk in chunks], ["A"])

    def test_later_chunks_of_a_merged_span_take_their_own_heading(self):
        methods = "\n\n".join("Methods paragraph text here. " * 4 for _ in range(6))
        chunks = list(_chunk_text(f"# Abstract\n\nShort.\n\n# Methods\n\n{methods}", chunk_chars=300, overlap_chars=30))
        self.assertEqual(chunks[0][4], "Abstract")
        self.assertEqual({chunk[4] for chunk in chunks[1:]}, {"Methods"})

    def test_a_long_sentence_is_cut_between_words(self):
        text = "a " * 749 + "needle" + " b" * 100
        chunks = list(_chunk_text(text, chunk_chars=1500, overlap_chars=150))
        self.assertTrue(any("needle" in chunk[3].split() for chunk in chunks))
        for chunk in chunks:
            self.assertTrue(set(chunk[3].split()) <= {"a", "b", "needle"})

    def test_paragraphs_are_packed_whole_within_the_budget(self):
        paragraphs = [f"Paragraph {i} " + "x" * 80 for i in range(6)]
        text = "\n\n".join(paragraphs)
        chunks = list(_chunk_text(text, chunk_chars=200, overlap_chars=20))
        for _index, start, end, chunk, _path in chunks:
            self.assertLessEqual(len(chunk), 200)
            self.assertEqual(text[start:end], chunk)
            self.assertTrue(chunk.startswith("Paragraph"))
            self.assertTrue(chunk.endswith("x"))
        self.assertEqual(sum(chunk.count("Paragraph") for *_rest, chunk, _path in chunks), 6)

    def test_long_paragraph_splits_at_sentences_with_overlap(self):
        sentences = [f"Sentence number {i} says something." for i in range(30)]
        text = " ".join(sentences)
        chunks = list(_chunk_text(text, chunk_chars=200, overlap_chars=60))
        self.assertGreater(len(chunks), 1)
        for (_i, start, end, chunk, _p), following in zip(chunks, chunks[1:]):
            self.assertLessEqual(len(chunk), 200)
            self.assertTrue(chunk.endswith("."))
            self.assertTrue(following[3].startswith("Sentence"))
            self.assertLess(following[1], end)  # overlap
            self.assertGreater(following[2], end)  # progress
        self.assertEqual(chunks[-1][2], len(text))

    def test_unbroken_text_is_cut_at_the_budget(self):
        chunks = list(_chunk_text("a" * 1000, chunk_chars=300, overlap_chars=30))
        self.assertTrue(all(len(chunk[3]) <= 300 for chunk in chunks))
        self.assertEqual(chunks[-1][2], 1000)


class IndexPageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.page1 = "## Introduction\n\n" + "Alpha intro text. " * 10 + "\n\n"
        self.page2 = "Beta continues here. " * 10 + "\n\n## Methods\n\n"
        self.page3 = "Gamma method zebra. " * 10
        text = self.page1 + self.page2 + self.page3
        b1 = len(self.page1)
        b2 = b1 + len(self.page2)
        jsonl = root / "index.jsonl"
        jsonl.write_text(
            json.dumps(_record("PAGED", text, [[1, 0, b1], [2, b1, b2], [4, b2, len(text)]])) + "\n"
            + json.dumps(_record("PAGELESS", "zebra without pages")) + "\n",
            encoding="utf-8",
        )
        self.db = root / "index.sqlite"
        build_fts_index(jsonl, self.db, chunk_chars=250, overlap_chars=20)

    def test_chunks_carry_page_ranges_and_heading_paths(self):
        con = sqlite3.connect(self.db)
        rows = con.execute(
            "SELECT c.start_char, c.end_char, c.page_start, c.page_end, c.heading_path FROM chunks c"
            " JOIN metadata m USING (record_id) WHERE m.zotero_attachment_key = 'PAGED' ORDER BY chunk_index"
        ).fetchall()
        con.close()
        b1 = len(self.page1)
        for start, end, page_start, page_end, _path in rows:
            self.assertEqual(page_start, 1 if start < b1 else (2 if start < b1 + len(self.page2) else 4))
            self.assertLessEqual(page_start, page_end)
        self.assertEqual(rows[0][4], "Introduction")
        self.assertEqual(rows[-1][4], "Methods")
        self.assertEqual(rows[-1][3], 4)

    def test_search_and_passages_report_pages_or_null(self):
        hits = {hit.zotero_attachment_key: hit for hit in search_fts(self.db, "zebra")}
        self.assertEqual(hits["PAGED"].page_end, 4)
        self.assertEqual(hits["PAGED"].heading_path, "Methods")
        self.assertIsNone(hits["PAGELESS"].page_start)
        passage = get_fulltext(self.db, attachment_key="PAGED", max_chars=50, chunk_index=hits["PAGED"].chunk_index)
        self.assertEqual((passage.page_start, passage.page_end), (hits["PAGED"].page_start, 4))
        self.assertTrue(passage.has_page_map)
        self.assertFalse(get_fulltext(self.db, attachment_key="PAGELESS", max_chars=50, chunk_index=0).has_page_map)

    def test_get_page_returns_the_stored_page_and_its_neighbours(self):
        page = get_page(self.db, attachment_key="PAGED", page=2, max_chars=10_000)
        self.assertEqual(page.text, self.page2)
        self.assertEqual((page.previous_page, page.next_page), (1, 4))
        self.assertFalse(page.truncated)
        short = get_page(self.db, attachment_key="PAGED", page=2, max_chars=10)
        self.assertTrue(short.truncated)
        self.assertEqual(short.page_sha256, page.page_sha256)
        with self.assertRaises(PageNotFoundError):
            get_page(self.db, attachment_key="PAGED", page=3, max_chars=100)
        with self.assertRaises(PageMapUnavailableError):
            get_page(self.db, attachment_key="PAGELESS", page=1, max_chars=100)
        with self.assertRaises(KeyError):
            get_page(self.db, attachment_key="MISSING", page=1, max_chars=100)

    def test_index_built_before_pages_reads_as_page_less(self):
        con = sqlite3.connect(self.db)
        con.executescript(
            "DROP TABLE pages; ALTER TABLE chunks DROP COLUMN page_start;"
            " ALTER TABLE chunks DROP COLUMN page_end; ALTER TABLE chunks DROP COLUMN heading_path;"
        )
        con.close()
        hit = next(hit for hit in search_fts(self.db, "zebra") if hit.zotero_attachment_key == "PAGED")
        self.assertIsNone(hit.page_start)
        passage = get_fulltext(self.db, attachment_key="PAGED", max_chars=50, chunk_index=0)
        self.assertIsNone(passage.page_start)
        self.assertFalse(passage.has_page_map)
        with self.assertRaises(PageMapUnavailableError):
            get_page(self.db, attachment_key="PAGED", page=1, max_chars=100)


    @unittest.skipIf(FakeFastMCP is None, "requires the optional MCP extra")
    def test_mcp_tools_label_physical_pages_and_refuse_unknown_ones(self):
        from zotero_pdf_text.artifacts import stage_and_publish, write_jsonl_from_existing

        index_root = self.db.parent
        stage_and_publish(
            index_root,
            write_jsonl_from_existing(index_root / "index.jsonl"),
            command="test",
            chunk_chars=250,
            overlap_chars=20,
        )
        server = create_server(index_root / "zotero_text_index.sqlite", mcp_factory=FakeFastMCP)
        hit = next(r for r in server.tools["search_fulltext"]("zebra")["results"] if r["attachment_key"] == "PAGED")
        self.assertEqual(hit["pdf_pages"], {"start": hit["pdf_pages"]["start"], "end": 4, "numbering": "physical_pdf_page_1_based"})
        page = server.tools["get_page"]("PAGED", 4)
        self.assertEqual(page["page_locator"]["page"], 4)
        self.assertEqual(page["provenance"]["content_trust"], "untrusted_source")
        assert_no_local_path(self, page, index_root)
        _assert_tool_error(self, lambda: server.tools["get_page"]("PAGED", 3), "page_not_found")
        _assert_tool_error(self, lambda: server.tools["get_page"]("PAGELESS", 1), "page_map_unavailable")
        _assert_tool_error(self, lambda: server.tools["get_page"]("PAGED", 0), "invalid_page")
        pageless = next(r for r in server.tools["search_fulltext"]("zebra")["results"] if r["attachment_key"] == "PAGELESS")
        self.assertIsNone(pageless["pdf_pages"])


if __name__ == "__main__":
    unittest.main()
