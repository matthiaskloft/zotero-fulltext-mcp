import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text import _extract_markdown
from zotero_pdf_text.math_detection import MathDetectionResult

CORPUS_PDF = Path(__file__).parent / "fixtures" / "ocr_corpus" / "corpus.pdf"


def _page_chunks(*texts):
    return [{"metadata": {"page_number": index}, "text": text} for index, text in enumerate(texts, start=1)]


class ExtractMarkdownTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.output = self.root / "output.raw.tmp"

    def _main(self, *extra):
        return _extract_markdown.main(["source.pdf", str(self.output), *extra])

    def _page_lengths(self):
        return json.loads(self.output.with_suffix(_extract_markdown.PAGES_SIDECAR_SUFFIX).read_text(encoding="utf-8"))

    def test_image_dir_forwards_write_images_to_pymupdf4llm(self):
        image_dir = self.root / "images" / "stem"
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value=_page_chunks("# Body")) as mock_to_markdown:
            with patch("zotero_pdf_text._extract_markdown._safe_detect_math", return_value=None):
                exit_code = self._main("--tool", _extract_markdown.PRIMARY_TOOL, "--image-dir", str(image_dir))

        self.assertEqual(exit_code, 0)
        self.assertTrue(image_dir.is_dir())
        mock_to_markdown.assert_called_once_with(
            Path("source.pdf"),
            page_chunks=True,
            write_images=True,
            image_path=str(image_dir),
            image_format="png",
            image_size_limit=0.05,
            dpi=150,
        )

    def test_no_image_dir_calls_to_markdown_without_image_options(self):
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value=_page_chunks("# Body")) as mock_to_markdown:
            with patch("zotero_pdf_text._extract_markdown._safe_detect_math", return_value=None):
                exit_code = self._main("--tool", _extract_markdown.PRIMARY_TOOL)

        self.assertEqual(exit_code, 0)
        mock_to_markdown.assert_called_once_with(Path("source.pdf"), page_chunks=True)

    def test_pages_are_concatenated_unchanged_and_their_lengths_recorded(self):
        pages = _page_chunks("# Title\n\nFirst page.\n\n", "", "Third page.\n")
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value=pages):
            with patch("zotero_pdf_text._extract_markdown._safe_detect_math", return_value=None):
                self._main()

        self.assertEqual(self.output.read_text(encoding="utf-8"), "# Title\n\nFirst page.\n\nThird page.\n")
        self.assertEqual(self._page_lengths(), [[1, 22], [2, 0], [3, 12]])

    def test_fallback_tool_ignores_image_dir_and_records_its_pages(self):
        pages = [(1, "## Page 1\n\nOne\n\n"), (3, "## Page 3\n\nThree")]
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown") as mock_to_markdown:
            with patch("zotero_pdf_text._extract_markdown._extract_plain_text_pages", return_value=pages) as mock_fallback:
                with patch("zotero_pdf_text._extract_markdown._safe_detect_math", return_value=None):
                    exit_code = self._main("--tool", _extract_markdown.FALLBACK_TOOL, "--image-dir", str(self.root / "images"))

        self.assertEqual(exit_code, 0)
        mock_fallback.assert_called_once()
        mock_to_markdown.assert_not_called()
        self.assertFalse((self.root / "images").exists())
        self.assertEqual(self.output.read_text(encoding="utf-8"), "## Page 1\n\nOne\n\n## Page 3\n\nThree")
        self.assertEqual(self._page_lengths(), [[1, 16], [3, 16]])

    def test_math_sidecar_written_when_detection_succeeds(self):
        fake_result = MathDetectionResult(
            has_math=True,
            font_signals=["cmmi"],
            unicode_math_char_count=5,
            unicode_math_density=0.01,
            pages_sampled=3,
        )
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value=_page_chunks("# Body")):
            with patch("zotero_pdf_text._extract_markdown.detect_math", return_value=fake_result):
                exit_code = self._main()

        self.assertEqual(exit_code, 0)
        self.assertIn('"has_math": true', self.output.with_suffix(".math.json").read_text(encoding="utf-8"))

    def test_math_sidecar_skipped_when_detection_fails(self):
        with patch("zotero_pdf_text._extract_markdown.pymupdf4llm.to_markdown", return_value=_page_chunks("# Body")):
            with patch("zotero_pdf_text._extract_markdown.detect_math", side_effect=RuntimeError("corrupt pdf")):
                exit_code = self._main()

        self.assertEqual(exit_code, 0)
        self.assertFalse(self.output.with_suffix(".math.json").exists())

    def test_real_pdf_page_output_matches_the_unsplit_conversion(self):
        # The page map rests on this: page_chunks must not change the Markdown, only split it.
        import pymupdf4llm

        _extract_markdown.main([str(CORPUS_PDF), str(self.output)])
        markdown = self.output.read_text(encoding="utf-8")
        self.assertEqual(markdown, pymupdf4llm.to_markdown(CORPUS_PDF))
        lengths = self._page_lengths()
        self.assertEqual([page for page, _length in lengths], list(range(1, len(lengths) + 1)))
        self.assertEqual(sum(length for _page, length in lengths), len(markdown))


if __name__ == "__main__":
    unittest.main()
