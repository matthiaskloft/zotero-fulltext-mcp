import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.artifacts import stage_and_publish, write_jsonl_from_existing
from zotero_pdf_text.fts import build_fts_index, lookup_citation_key
from zotero_pdf_text.mcp_contract import MAX_CONTEXT_RECORDS, PublicMcpError, create_server

from test_mcp_server import FakeFastMCP, assert_no_local_path


def _record(parent: str, attachment: str, citation_key: str, text: str) -> dict[str, object]:
    return {
        "zotero_parent_key": parent,
        "zotero_attachment_key": attachment,
        "title": f"Title {attachment}",
        "creators": "Author",
        "year": "2024",
        "doi": "",
        "citation_key": citation_key,
        "source_path": f"{attachment}.pdf",
        "markdown_path": f"{attachment}.md",
        "markdown_sha256": f"sha-{attachment}",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "char_count": len(text),
        "word_count": len(text.split()),
        "page_count": "1",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": False,
        "text": text,
    }


LONG_TEXT = " ".join(f"Sentence number {i} about consensus." for i in range(12))


def _build(root: Path) -> Path:
    records = [
        # Two attachments under one parent.
        _record("PARENTA", "ATTACHA2", "smith2024", LONG_TEXT),
        _record("PARENTA", "ATTACHA1", "smith2024", "Short text for the first attachment."),
        # The same citation key on a different parent: must be reported, not resolved.
        _record("PARENTB", "ATTACHB1", "dupKey2020", "Duplicate one."),
        _record("PARENTC", "ATTACHC1", "dupKey2020", "Duplicate two."),
        # Case variant of an existing key, and a record without a citation key.
        _record("PARENTD", "ATTACHD1", "Smith2024", "Case variant."),
        _record("PARENTE", "ATTACHE1", "", "No key."),
    ]
    jsonl = root / "index.jsonl"
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    sqlite_path = root / "index.sqlite"
    build_fts_index(jsonl, sqlite_path, chunk_chars=60, overlap_chars=0)
    # The MCP surface only reads a managed, published generation.
    stage_and_publish(
        root, write_jsonl_from_existing(jsonl), command="test", chunk_chars=60, overlap_chars=0
    )
    return sqlite_path


class LookupCitationKeyFtsTests(unittest.TestCase):
    def test_multiple_attachments_are_ordered_and_carry_chunk_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = lookup_citation_key(_build(Path(tmp)), "smith2024")
        records = result["records"]
        assert isinstance(records, list)
        self.assertEqual([r["zotero_attachment_key"] for r in records], ["ATTACHA1", "ATTACHA2"])
        self.assertFalse(result["truncated"])
        self.assertGreater(records[1]["chunk_count"], 1)

    def test_match_is_case_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = lookup_citation_key(_build(Path(tmp)), "Smith2024")
        records = result["records"]
        assert isinstance(records, list)
        self.assertEqual([r["zotero_attachment_key"] for r in records], ["ATTACHD1"])

    def test_limit_reports_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = lookup_citation_key(_build(Path(tmp)), "dupKey2020", limit=1)
        self.assertEqual(len(result["records"]), 1)  # type: ignore[arg-type]
        self.assertTrue(result["truncated"])

    def test_empty_key_is_rejected_rather_than_matching_keyless_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                lookup_citation_key(_build(Path(tmp)), "")


class LookupCitationKeyToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.server = create_server(_build(self.root), mcp_factory=FakeFastMCP)
        self.lookup = self.server.tools["lookup_citation_key"]
        self.read = self.server.tools["get_fulltext_chunk"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_key_is_an_empty_not_found_result(self):
        result = self.lookup(citation_key="nobody1999")
        self.assertEqual(
            result,
            {
                "citation_key": "nobody1999",
                "found": False,
                "parent_keys": [],
                "ambiguous": False,
                "truncated": False,
                "records": [],
            },
        )

    def test_duplicate_citation_key_across_parents_is_explicit(self):
        result = self.lookup(citation_key="dupKey2020")
        self.assertTrue(result["found"])
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["parent_keys"], ["PARENTB", "PARENTC"])
        self.assertEqual([r["attachment_key"] for r in result["records"]], ["ATTACHB1", "ATTACHC1"])

    def test_multiple_attachments_of_one_parent_are_not_ambiguous(self):
        result = self.lookup(citation_key="smith2024")
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["parent_keys"], ["PARENTA"])
        self.assertEqual([r["attachment_key"] for r in result["records"]], ["ATTACHA1", "ATTACHA2"])
        assert_no_local_path(self, result, self.root)
        for record in result["records"]:
            self.assertNotIn("source_path", record)
            self.assertNotIn("markdown_path", record)

    def test_parent_past_the_record_cap_still_marks_ambiguous(self):
        root = self.root / "capped"
        root.mkdir()
        records = [_record("PARENTA", f"ATTACHA{i:03d}", "big2024", "Text.") for i in range(50)]
        records.append(_record("PARENTB", "ATTACHB999", "big2024", "Text."))
        jsonl = root / "index.jsonl"
        jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        stage_and_publish(root, write_jsonl_from_existing(jsonl), command="test")
        server = create_server(root / "index.sqlite", mcp_factory=FakeFastMCP)
        result = server.tools["lookup_citation_key"](citation_key="big2024")
        self.assertEqual(len(result["records"]), MAX_CONTEXT_RECORDS)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["parent_keys"], ["PARENTA", "PARENTB"])
        self.assertTrue(result["ambiguous"])

    def test_zotero_keys_are_not_citation_keys(self):
        for key in ("PARENTA", "ATTACHA1"):
            self.assertFalse(self.lookup(citation_key=key)["found"])

    def test_invalid_citation_key_is_a_public_error(self):
        for bad in ("", "   ", None, 5, "x" * 300):
            with self.assertRaises(PublicMcpError) as raised:
                self.lookup(citation_key=bad)
            self.assertEqual(raised.exception.code, "invalid_citation_key")

    def test_lookup_then_read_first_chunk_and_follow_next(self):
        record = self.lookup(citation_key="smith2024")["records"][1]
        self.assertGreater(record["chunk_count"], 1)
        first = self.read(attachment_key=record["attachment_key"], chunk_index=0)
        verified = self.read(
            attachment_key=record["attachment_key"], chunk_index=0, chunk_sha256=first["source_locator"]["chunk_sha256"]
        )
        self.assertEqual(verified["text"], first["text"])
        self.assertTrue(first["text"].startswith("Sentence number 0"))
        self.assertEqual(first["next_chunk_index"], 1)
        second = self.read(attachment_key=record["attachment_key"], chunk_index=first["next_chunk_index"])
        self.assertEqual(second["previous_chunk_index"], 0)
        self.assertNotEqual(second["text"], first["text"])


if __name__ == "__main__":
    unittest.main()
