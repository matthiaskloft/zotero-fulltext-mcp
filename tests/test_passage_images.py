"""Equations and figures that convert as images: listing them in passages and serving them.

A passage carries `images/...` links relative to its converted Markdown. The MCP surface is
path-free, so instead of publishing the directory those links resolve against, the server lists
the links (`image_links`) and serves the images themselves through `get_passage_image`.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.artifacts import stage_and_publish, write_jsonl_from_existing
from zotero_pdf_text.fts import build_fts_index
from zotero_pdf_text.library import extracted_image_name, image_link_targets, read_extracted_image
from zotero_pdf_text.mcp_contract import create_server

from test_mcp_server import FakeFastMCP, _assert_tool_error, assert_no_local_path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 32
EQUATION = "Smith (2020) - Title.pdf-0005-14.png"
EQUATION_LINK = "images/Smith%20%282020%29%20-%20Title.pdf-0005-14.png"
TEXT = (
    "Reliability is defined as\n\n"
    f"![]({EQUATION_LINK})\n\n"
    "where the error variance is estimated per item.\n\n"
    "![](images/figure-2.png) and ![](https://example.com/remote.png)"
)


def _build_image_index(root: Path) -> tuple[Path, Path]:
    """Index one attachment in the canonical layout, with its images beside the Markdown."""
    paper_dir = root / "output" / "library" / "ATTACH1"
    images = paper_dir / "images"
    images.mkdir(parents=True)
    markdown = paper_dir / "ATTACH1.md"
    markdown.write_text(TEXT, encoding="utf-8")
    (images / EQUATION).write_bytes(PNG)
    (images / "figure-2.png").write_bytes(PNG)
    (root / "paper.pdf").write_bytes(b"%PDF")
    index_root = root / "output" / "index"
    index_root.mkdir(parents=True)
    jsonl_path = index_root / "zotero_text_index.jsonl"
    jsonl_path.write_text(
        json.dumps(
            {
                "zotero_parent_key": "PARENT1",
                "zotero_attachment_key": "ATTACH1",
                "title": "Reliability",
                "creators": "Smith",
                "year": "2020",
                "doi": "",
                "citation_key": "smith2020",
                "source_path": str(root / "paper.pdf"),
                "markdown_path": str(markdown),
                "markdown_sha256": "abc123",
                "extraction_tool": "pymupdf4llm.to_markdown",
                "char_count": len(TEXT),
                "word_count": 20,
                "page_count": "1",
                "classification": "mapped_verified",
                "identity_status": "verified",
                "identity_rule": "doi_exact",
                "has_math": True,
                "text": TEXT,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sqlite_path = index_root / "zotero_text_index.sqlite"
    build_fts_index(jsonl_path, sqlite_path)
    stage_and_publish(index_root, write_jsonl_from_existing(jsonl_path), command="test")
    return images, sqlite_path


class ImageLinkParsingTests(unittest.TestCase):
    def test_lists_local_links_once_in_order_and_skips_remote_ones(self):
        self.assertEqual(image_link_targets(TEXT), [EQUATION_LINK, "images/figure-2.png"])
        self.assertEqual(image_link_targets(TEXT + f"\n![]({EQUATION_LINK})"), [EQUATION_LINK, "images/figure-2.png"])

    def test_reduces_a_link_to_its_decoded_file_name(self):
        self.assertEqual(extracted_image_name(EQUATION_LINK), EQUATION)
        self.assertEqual(extracted_image_name("<images/a.PNG>"), "a.PNG")
        # The directory part never selects a directory: only the name is resolved.
        self.assertEqual(extracted_image_name("../../elsewhere/a.png"), "a.png")
        self.assertEqual(extracted_image_name(r"C:\elsewhere\a.png"), "a.png")

    def test_refuses_names_that_are_not_plain_image_files(self):
        for link in (
            "",
            "images/",
            "images/..",
            "images/..%2Fsecret.png",
            "images/a%5Csecret.png",
            "images/a%00.png",
            "images/notes.md",
            "images/a.svg",
            "https://example.com/a.png",
        ):
            with self.subTest(link=link), self.assertRaises(ValueError):
                extracted_image_name(link)


class ReadExtractedImageTests(unittest.TestCase):
    def test_refuses_a_symlink_that_leaves_the_images_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "library" / "ATTACH1" / "images"
            images.mkdir(parents=True)
            outside = root / "outside.png"
            outside.write_bytes(PNG)
            try:
                os.symlink(outside, images / "escape.png")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(LookupError):
                read_extracted_image(root / "library" / "ATTACH1" / "ATTACH1.md", "images/escape.png", max_bytes=10_000)

    def test_refuses_bytes_that_do_not_match_the_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = Path(tmp) / "library" / "ATTACH1" / "images"
            images.mkdir(parents=True)
            (images / "fake.png").write_bytes(b"not an image")
            with self.assertRaises(LookupError):
                read_extracted_image(images.parent / "ATTACH1.md", "images/fake.png", max_bytes=10_000)

    def test_resolves_the_run_directory_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            (run / "images" / "0001_paper").mkdir(parents=True)
            (run / "images" / "0001_paper" / "eq.png").write_bytes(PNG)
            data, image_format = read_extracted_image(run / "markdown" / "0001_paper.md", "../images/0001_paper/eq.png", max_bytes=10_000)
            self.assertEqual((data, image_format), (PNG, "png"))


@unittest.skipUnless(importlib.util.find_spec("mcp"), "requires the optional MCP extra")
class PassageImageToolTests(unittest.TestCase):
    def test_passage_lists_its_image_links_without_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sqlite_path = _build_image_index(Path(tmp))
            server = create_server(sqlite_path, mcp_factory=FakeFastMCP)

            passage = server.tools["get_fulltext_chunk"]("ATTACH1", chunk_index=0)

            self.assertEqual(passage["image_links"], [EQUATION_LINK, "images/figure-2.png"])
            assert_no_local_path(self, passage, Path(tmp))

    def test_returns_the_linked_image_as_image_content_without_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sqlite_path = _build_image_index(Path(tmp))
            server = create_server(sqlite_path, mcp_factory=FakeFastMCP)

            result = server.tools["get_passage_image"]("ATTACH1", EQUATION_LINK)

            image = result.content[0]
            self.assertEqual(image.type, "image")
            self.assertEqual(image.mimeType, "image/png")
            self.assertEqual(base64.b64decode(image.data), PNG)
            self.assertEqual(
                result.structuredContent,
                {
                    "attachment_key": "ATTACH1",
                    "image_link": EQUATION_LINK,
                    "mime_type": "image/png",
                    "byte_count": len(PNG),
                    "content_trust": "untrusted_source",
                },
            )
            assert_no_local_path(self, result.model_dump(), Path(tmp))

    def test_refusals_carry_specific_codes_and_no_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            images, sqlite_path = _build_image_index(Path(tmp))
            (images.parent / "beside-markdown.png").write_bytes(PNG)
            server = create_server(sqlite_path, mcp_factory=FakeFastMCP)
            tool = server.tools["get_passage_image"]

            for link, code in (
                ("images/missing.png", "image_not_found"),
                ("../beside-markdown.png", "image_not_found"),
                ("images/..%2Fbeside-markdown.png", "invalid_image_link"),
                ("images/ATTACH1.md", "invalid_image_link"),
                ("", "invalid_image_link"),
                (None, "invalid_image_link"),
            ):
                with self.subTest(link=link):
                    error = _assert_tool_error(self, lambda: tool("ATTACH1", link), code)
                    assert_no_local_path(self, error.message, Path(tmp))
            _assert_tool_error(self, lambda: tool("MISSING1", EQUATION_LINK), "attachment_not_found")
            with patch("zotero_pdf_text.mcp_contract.MAX_IMAGE_BYTES", 8):
                _assert_tool_error(self, lambda: tool("ATTACH1", EQUATION_LINK), "image_too_large")

    def test_real_server_sends_image_content_and_valid_structured_content(self):
        import jsonschema
        from mcp.types import CallToolRequest, CallToolRequestParams

        with tempfile.TemporaryDirectory() as tmp:
            _, sqlite_path = _build_image_index(Path(tmp))
            server = create_server(sqlite_path)
            tool = {t.name: t for t in asyncio.run(server.list_tools())}["get_passage_image"]
            self.assertTrue(tool.annotations.readOnlyHint)

            handler = server._mcp_server.request_handlers[CallToolRequest]
            request = CallToolRequest(
                params=CallToolRequestParams(
                    name="get_passage_image",
                    arguments={"attachment_key": "ATTACH1", "image_link": EQUATION_LINK},
                )
            )
            result = asyncio.run(handler(request)).root

            self.assertFalse(result.isError)
            self.assertEqual([block.type for block in result.content], ["image", "text"])
            self.assertEqual(base64.b64decode(result.content[0].data), PNG)
            jsonschema.validate(instance=result.structuredContent, schema=tool.outputSchema)


if __name__ == "__main__":
    unittest.main()
