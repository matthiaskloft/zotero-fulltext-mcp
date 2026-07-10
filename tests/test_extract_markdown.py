import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text import _extract_markdown
from zotero_pdf_text.math_detection import MathDetectionResult


class ExtractMarkdownTests(unittest.TestCase):
    def test_image_dir_forwards_write_images_to_pymupdf4llm(self):
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value="# Body") as mock_to_markdown:
            with patch("zotero_pdf_text._extract_markdown._safe_detect_math", return_value=None):
                with patch.object(Path, "mkdir") as mock_mkdir, patch.object(Path, "write_text") as mock_write_text:
                    exit_code = _extract_markdown.main(
                        ["source.pdf", "output.md", "--tool", _extract_markdown.PRIMARY_TOOL, "--image-dir", "images/stem"]
                    )

        self.assertEqual(exit_code, 0)
        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)
        mock_write_text.assert_called_once()
        mock_to_markdown.assert_called_once_with(
            Path("source.pdf"),
            write_images=True,
            image_path=str(Path("images/stem")),
            image_format="png",
            image_size_limit=0.05,
            dpi=150,
        )

    def test_no_image_dir_calls_to_markdown_without_image_options(self):
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value="# Body") as mock_to_markdown:
            with patch("zotero_pdf_text._extract_markdown._safe_detect_math", return_value=None):
                with patch.object(Path, "write_text") as mock_write_text:
                    exit_code = _extract_markdown.main(["source.pdf", "output.md", "--tool", _extract_markdown.PRIMARY_TOOL])

        self.assertEqual(exit_code, 0)
        mock_write_text.assert_called_once()
        mock_to_markdown.assert_called_once_with(Path("source.pdf"))

    def test_fallback_tool_ignores_image_dir(self):
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown") as mock_to_markdown:
            with patch("zotero_pdf_text._extract_markdown._extract_plain_text", return_value="Fallback text") as mock_fallback:
                with patch("zotero_pdf_text._extract_markdown._safe_detect_math", return_value=None):
                    with patch.object(Path, "mkdir") as mock_mkdir, patch.object(Path, "write_text") as mock_write_text:
                        exit_code = _extract_markdown.main(
                            ["source.pdf", "output.md", "--tool", _extract_markdown.FALLBACK_TOOL, "--image-dir", "images/stem"]
                        )

        self.assertEqual(exit_code, 0)
        mock_fallback.assert_called_once()
        mock_to_markdown.assert_not_called()
        mock_mkdir.assert_not_called()
        mock_write_text.assert_called_once()

    def test_math_sidecar_written_when_detection_succeeds(self):
        fake_result = MathDetectionResult(
            has_math=True,
            font_signals=["cmmi"],
            unicode_math_char_count=5,
            unicode_math_density=0.01,
            pages_sampled=3,
        )
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value="# Body"):
            with patch("zotero_pdf_text._extract_markdown.detect_math", return_value=fake_result):
                with patch.object(Path, "write_text") as mock_write_text:
                    exit_code = _extract_markdown.main(["source.pdf", "output.md", "--tool", _extract_markdown.PRIMARY_TOOL])

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_write_text.call_count, 2)
        sidecar_call = mock_write_text.call_args_list[1]
        self.assertIn('"has_math": true', sidecar_call.args[0])

    def test_math_sidecar_skipped_when_detection_fails(self):
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value="# Body"):
            with patch("zotero_pdf_text._extract_markdown.detect_math", side_effect=RuntimeError("corrupt pdf")):
                with patch.object(Path, "write_text") as mock_write_text:
                    exit_code = _extract_markdown.main(["source.pdf", "output.md", "--tool", _extract_markdown.PRIMARY_TOOL])

        self.assertEqual(exit_code, 0)
        mock_write_text.assert_called_once()


if __name__ == "__main__":
    unittest.main()
