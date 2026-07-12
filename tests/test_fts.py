import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.fts import (
    DEFAULT_CONTEXT_RECORD_LIMIT,
    _chunk_text,
    build_fts_index,
    coverage_report,
    get_fulltext,
    get_item_context,
    search_fts,
)


class FtsTests(unittest.TestCase):
    def test_build_search_and_fetch_fulltext(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)

            summary = build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)

            self.assertEqual(summary.records, 2)
            self.assertGreater(summary.chunks, 1)
            results = search_fts(sqlite_db, "cultural consensus", limit=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].zotero_attachment_key, "ATTACH1")
            self.assertEqual(results[0].citation_key, "smithConsensus2024")
            self.assertIn("cultural", results[0].snippet.casefold())
            self.assertIs(results[0].has_math, True)

            fulltext = get_fulltext(sqlite_db, attachment_key="ATTACH1", max_chars=50)
            self.assertEqual(fulltext.zotero_parent_key, "PARENT1")
            self.assertEqual(fulltext.citation_key, "smithConsensus2024")
            self.assertLessEqual(len(fulltext.text), 50)
            self.assertIs(fulltext.has_math, True)

            fulltext_no_math = get_fulltext(sqlite_db, attachment_key="ATTACH2", max_chars=50)
            self.assertIs(fulltext_no_math.has_math, False)

            context = get_item_context(sqlite_db, parent_key="PARENT1")
            self.assertEqual(context["records"][0]["zotero_attachment_key"], "ATTACH1")
            self.assertEqual(context["records"][0]["citation_key"], "smithConsensus2024")
            self.assertIs(context["records"][0]["has_math"], True)

            citation_results = search_fts(sqlite_db, "smithConsensus2024", limit=1)
            self.assertEqual(citation_results[0].zotero_attachment_key, "ATTACH1")

            old_record_context = get_item_context(sqlite_db, parent_key="PARENT2")
            self.assertEqual(old_record_context["records"][0]["citation_key"], "")
            self.assertIs(old_record_context["records"][0]["has_math"], False)

            coverage = coverage_report(sqlite_db)
            self.assertEqual(coverage["records"], 2)
            self.assertEqual(coverage["by_extraction_tool"]["pymupdf4llm.to_markdown"], 2)
            self.assertEqual(coverage["by_has_math"], {True: 1, False: 1})

    def test_interrupted_rebuild_preserves_previous_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)
            previous_bytes = sqlite_db.read_bytes()

            with patch("zotero_pdf_text.fts._insert_chunk", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)

            # The previous database is untouched, byte-for-byte, and still queryable.
            self.assertEqual(sqlite_db.read_bytes(), previous_bytes)
            results = search_fts(sqlite_db, "cultural consensus", limit=5)
            self.assertEqual(len(results), 1)

            # No leftover temp file from the failed build.
            self.assertEqual(list(root.glob(".index.sqlite.tmp-*")), [])

    def test_failed_integrity_check_preserves_previous_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)
            previous_bytes = sqlite_db.read_bytes()

            with patch("zotero_pdf_text.fts._check_integrity", side_effect=RuntimeError("corrupt")):
                with self.assertRaises(RuntimeError):
                    build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)

            self.assertEqual(sqlite_db.read_bytes(), previous_bytes)
            self.assertEqual(list(root.glob(".index.sqlite.tmp-*")), [])

    def test_replace_retries_past_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)

            real_replace = os.replace
            calls = {"count": 0}

            def flaky_replace(src, dst):
                calls["count"] += 1
                if calls["count"] < 3:
                    raise PermissionError("simulated concurrent reader")
                real_replace(src, dst)

            with patch("zotero_pdf_text.fts.os.replace", side_effect=flaky_replace), patch(
                "zotero_pdf_text.fts.time.sleep"
            ):
                summary = build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)

            self.assertEqual(calls["count"], 3)
            self.assertEqual(summary.records, 2)
            self.assertEqual(list(root.glob(".index.sqlite.tmp-*")), [])

    def test_replace_gives_up_after_exhausting_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)

            with patch(
                "zotero_pdf_text.fts.os.replace", side_effect=PermissionError("always locked")
            ), patch("zotero_pdf_text.fts.time.sleep"):
                with self.assertRaises(PermissionError):
                    build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)

            self.assertFalse(sqlite_db.exists())
            self.assertEqual(list(root.glob(".index.sqlite.tmp-*")), [])

    def test_successful_rebuild_fully_replaces_previous_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)

            records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
            records = records[:1]
            jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            summary = build_fts_index(jsonl, sqlite_db, chunk_chars=40, overlap_chars=5)

            self.assertEqual(summary.records, 1)
            self.assertEqual(list(root.glob(".index.sqlite.tmp-*")), [])
            results = search_fts(sqlite_db, "cultural consensus", limit=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(search_fts(sqlite_db, "response time models", limit=5), [])

    def test_search_rejects_empty_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, sqlite_db)

            with self.assertRaises(ValueError):
                search_fts(sqlite_db, " / ")

    def test_search_modes_and_bounds_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, sqlite_db)

            self.assertEqual(search_fts(sqlite_db, "cultural psychometric", search_mode="all_terms"), [])
            self.assertEqual(
                {result.zotero_attachment_key for result in search_fts(sqlite_db, "cultural psychometric", search_mode="any_terms")},
                {"ATTACH1", "ATTACH2"},
            )
            phrase_results = search_fts(sqlite_db, "cultural consensus", search_mode="phrase")
            self.assertEqual([result.zotero_attachment_key for result in phrase_results], ["ATTACH1"])
            self.assertEqual(
                [result.zotero_attachment_key for result in search_fts(sqlite_db, "consensus cultural", search_mode="all_terms")],
                ["ATTACH1"],
            )
            self.assertEqual(search_fts(sqlite_db, "consensus cultural", search_mode="phrase"), [])

            with self.assertRaises(ValueError):
                search_fts(sqlite_db, "topic", search_mode="unsupported")
            with self.assertRaises(ValueError):
                search_fts(sqlite_db, "x" * 65)
            with self.assertRaises(ValueError):
                search_fts(sqlite_db, "topic", limit=101)

    def test_title_matches_rank_before_body_only_matches_and_ties_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
            records[0].update({"title": "Unrelated", "text": "target " * 20})
            records[1].update({"title": "Target methods", "text": "unrelated"})
            jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            build_fts_index(jsonl, sqlite_db)

            ranked = search_fts(sqlite_db, "target")
            self.assertEqual(ranked[0].zotero_attachment_key, "ATTACH2")

            records[0].update({"title": "Unrelated", "citation_key": "", "text": "target"})
            records[1].update({"title": "Unrelated", "citation_key": "target", "text": "unrelated"})
            jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            build_fts_index(jsonl, sqlite_db)
            self.assertEqual(search_fts(sqlite_db, "target")[0].zotero_attachment_key, "ATTACH2")

            records[0].update({"title": "Tie", "creators": "", "citation_key": "", "text": "target"})
            records[1].update({"title": "Tie", "creators": "", "citation_key": "", "text": "target"})
            jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            build_fts_index(jsonl, sqlite_db)
            self.assertEqual(
                [result.zotero_attachment_key for result in search_fts(sqlite_db, "target")],
                ["ATTACH1", "ATTACH2"],
            )

    def test_chunk_offsets_describe_the_stored_trimmed_text(self):
        text = "  abcdefghij  "
        chunks = list(_chunk_text(text, chunk_chars=5, overlap_chars=2))

        self.assertEqual(chunks[0], (0, 2, 5, "abc"))
        self.assertEqual(chunks[-1], (3, 9, 12, "hij"))
        for _, start, end, chunk in chunks:
            self.assertEqual(text[start:end], chunk)

    def test_rebuild_replaces_old_chunks_and_preserves_trimmed_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, sqlite_db)

            records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
            records[0]["text"] = "  replacement evidence  "
            records[0]["char_count"] = len(records[0]["text"])
            jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            build_fts_index(jsonl, sqlite_db)

            self.assertEqual(search_fts(sqlite_db, "bayesian"), [])
            replacement = get_fulltext(sqlite_db, attachment_key="ATTACH1", chunk_index=0)
            self.assertEqual((replacement.start_char, replacement.end_char, replacement.text), (2, 22, "replacement evidence"))

    def test_search_candidate_query_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            build_fts_index(jsonl, sqlite_db)

            class _RecordingConnection(sqlite3.Connection):
                captured_limit = None

                def execute(self, sql, parameters=()):
                    if "FROM chunks_fts f" in sql and "LIMIT ?" in sql:
                        type(self).captured_limit = parameters[-1]
                    return super().execute(sql, parameters)

            real_connect = sqlite3.connect

            def connect_with_recording(*args, **kwargs):
                kwargs.setdefault("factory", _RecordingConnection)
                return real_connect(*args, **kwargs)

            with patch("zotero_pdf_text.fts.sqlite3.connect", side_effect=connect_with_recording):
                search_fts(sqlite_db, "cultural", limit=1)
                self.assertEqual(_RecordingConnection.captured_limit, 50)
                search_fts(sqlite_db, "cultural", limit=100)

            self.assertEqual(_RecordingConnection.captured_limit, 500)

    def test_search_deduplicates_long_title_matches_before_applying_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
            records[0].update({"title": "Shared title", "text": "x " * 2_000})
            records[1].update({"title": "Shared title", "text": "short"})
            jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            build_fts_index(jsonl, sqlite_db, chunk_chars=20, overlap_chars=0)

            self.assertEqual(
                {result.zotero_attachment_key for result in search_fts(sqlite_db, "shared title", limit=2)},
                {"ATTACH1", "ATTACH2"},
            )

    def test_literal_brackets_in_body_text_do_not_win_the_representative_chunk(self):
        # Regression test: the per-record chunk-selection heuristic used to check for a literal
        # '[' in the snippet, which collides with citation/math brackets that commonly occur in
        # body text unrelated to the actual match. Neither chunk here genuinely matches the query
        # in its body (the match is title-only), so the bracket-free, lower-chunk_index chunk
        # should win on the ordinary score/chunk_index tie-break rather than the chunk that merely
        # happens to contain a literal bracket.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            _write_jsonl(jsonl)
            records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
            records[0].update(
                {
                    "title": "Consensus study",
                    "text": (
                        "Plain background details unrelated to the search term whatsoever. "
                        "References [3][4][5] cited here for additional background context."
                    ),
                }
            )
            jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            # Both sentences are 66 characters, so chunk_chars=66 puts the bracket-free sentence
            # in chunk_index 0 and the bracket-containing sentence in chunk_index 1.
            build_fts_index(jsonl, sqlite_db, chunk_chars=66, overlap_chars=0)

            results = search_fts(sqlite_db, "consensus", limit=1)

            self.assertEqual(results[0].zotero_attachment_key, "ATTACH1")
            self.assertEqual(results[0].chunk_index, 0)
            self.assertNotIn("[", results[0].snippet)

    def test_get_fulltext_bounds_the_chunk_query_for_large_documents(self):
        # Regression test: get_fulltext used to fetch every chunk row for a record before
        # truncating the assembled text to max_chars. A small window request against a very
        # large document should only ever pull a bounded number of chunk rows from SQLite.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            long_text = " ".join(f"word{i}" for i in range(5000))
            record = {
                "zotero_parent_key": "PARENT3",
                "zotero_attachment_key": "ATTACH3",
                "title": "Long document",
                "creators": "A",
                "year": "2024",
                "doi": "",
                "citation_key": "",
                "source_path": "long.pdf",
                "markdown_path": "long.md",
                "markdown_sha256": "x",
                "extraction_tool": "pymupdf4llm.to_markdown",
                "char_count": len(long_text),
                "word_count": 5000,
                "page_count": "50",
                "classification": "mapped_verified",
                "identity_status": "verified",
                "identity_rule": "doi_exact",
                "has_math": False,
                "text": long_text,
            }
            with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record) + "\n")
            summary = build_fts_index(jsonl, sqlite_db, chunk_chars=200, overlap_chars=20)
            self.assertGreater(summary.chunks, 50)

            class _RecordingConnection(sqlite3.Connection):
                captured_limit = None

                def execute(self, sql, parameters=()):
                    if "FROM chunks" in sql and "LIMIT ?" in sql:
                        type(self).captured_limit = parameters[-1]
                    return super().execute(sql, parameters)

            real_connect = sqlite3.connect

            def connect_with_recording(*args, **kwargs):
                kwargs.setdefault("factory", _RecordingConnection)
                return real_connect(*args, **kwargs)

            with patch("zotero_pdf_text.fts.sqlite3.connect", side_effect=connect_with_recording):
                result = get_fulltext(sqlite_db, attachment_key="ATTACH3", max_chars=500)

            self.assertIsNotNone(_RecordingConnection.captured_limit)
            self.assertLess(_RecordingConnection.captured_limit, summary.chunks)
            self.assertLessEqual(len(result.text), 500)

    def test_get_fulltext_covers_max_chars_for_non_default_chunk_sizing(self):
        # Regression test: the chunk-covering query used to compute a single LIMIT from the
        # DEFAULT_CHUNK_CHARS/DEFAULT_OVERLAP_CHARS estimate. An index built with a smaller
        # chunk_chars (as here) advances faster than that estimate assumes, so the old fixed
        # LIMIT under-fetched and silently returned less than max_chars of text even though
        # more was available.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            long_text = " ".join(f"word{i}" for i in range(5000))
            record = {
                "zotero_parent_key": "PARENT4",
                "zotero_attachment_key": "ATTACH4",
                "title": "Long document",
                "creators": "A",
                "year": "2024",
                "doi": "",
                "citation_key": "",
                "source_path": "long.pdf",
                "markdown_path": "long.md",
                "markdown_sha256": "x",
                "extraction_tool": "pymupdf4llm.to_markdown",
                "char_count": len(long_text),
                "word_count": 5000,
                "page_count": "50",
                "classification": "mapped_verified",
                "identity_status": "verified",
                "identity_rule": "doi_exact",
                "has_math": False,
                "text": long_text,
            }
            with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record) + "\n")
            build_fts_index(jsonl, sqlite_db, chunk_chars=2000, overlap_chars=200)

            result = get_fulltext(sqlite_db, attachment_key="ATTACH4", max_chars=12000)

            self.assertEqual(len(result.text), 12000)

    def test_get_item_context_bounds_record_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            sqlite_db = root / "index.sqlite"
            record_count = DEFAULT_CONTEXT_RECORD_LIMIT + 10
            with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
                for i in range(record_count):
                    handle.write(
                        json.dumps(
                            {
                                "zotero_parent_key": "SHARED_PARENT",
                                "zotero_attachment_key": f"ATTACH{i}",
                                "title": f"Paper {i}",
                                "creators": "A",
                                "year": "2024",
                                "doi": "",
                                "citation_key": "",
                                "source_path": f"{i}.pdf",
                                "markdown_path": f"{i}.md",
                                "markdown_sha256": "x",
                                "extraction_tool": "pymupdf4llm.to_markdown",
                                "char_count": 10,
                                "word_count": 2,
                                "page_count": "1",
                                "classification": "mapped_verified",
                                "identity_status": "verified",
                                "identity_rule": "doi_exact",
                                "has_math": False,
                                "text": "short",
                            }
                        )
                        + "\n"
                    )
            build_fts_index(jsonl, sqlite_db)

            context = get_item_context(sqlite_db, parent_key="SHARED_PARENT")
            self.assertEqual(len(context["records"]), DEFAULT_CONTEXT_RECORD_LIMIT)

            with self.assertRaises(ValueError):
                get_item_context(sqlite_db, parent_key="SHARED_PARENT", limit=0)


def _write_jsonl(path: Path) -> None:
    records = [
        {
            "zotero_parent_key": "PARENT1",
            "zotero_attachment_key": "ATTACH1",
            "title": "Cultural consensus theory",
            "creators": "Jane Smith",
            "year": "2024",
            "doi": "10.1000/one",
            "citation_key": "smithConsensus2024",
            "source_path": "one.pdf",
            "markdown_path": "one.md",
            "markdown_sha256": "abc",
            "extraction_tool": "pymupdf4llm.to_markdown",
            "char_count": 80,
            "word_count": 10,
            "page_count": "2",
            "classification": "mapped_verified",
            "identity_status": "verified",
            "identity_rule": "doi_exact",
            "has_math": True,
            "text": "Cultural consensus theory models shared knowledge. Bayesian models can extend it.",
        },
        {
            "zotero_parent_key": "PARENT2",
            "zotero_attachment_key": "ATTACH2",
            "title": "Response time models",
            "creators": "John Doe",
            "year": "2020",
            "doi": "10.1000/two",
            "source_path": "two.pdf",
            "markdown_path": "two.md",
            "markdown_sha256": "def",
            "extraction_tool": "pymupdf4llm.to_markdown",
            "char_count": 50,
            "word_count": 8,
            "page_count": "1",
            "classification": "mapped_verified",
            "identity_status": "verified",
            "identity_rule": "title_author_or_year",
            "text": "Response time models are useful for psychometric data.",
        },
    ]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    unittest.main()
