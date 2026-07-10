import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class ExtractMarkdownMarkerTests(unittest.TestCase):
    """Stubs the marker package entirely at sys.modules level -- never imports the real
    (optional, heavy) marker-pdf dependency, so these tests run without it installed."""

    def test_writes_text_and_saves_images(self):
        fake_image = MagicMock()
        fake_marker_module = _make_fake_marker_module(text="# Marker body", images={"fig1.png": fake_image})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_path = root / "output.md"
            image_dir = root / "images" / "stem"

            with patch.dict(sys.modules, fake_marker_module):
                from zotero_pdf_text import _extract_markdown_marker

                exit_code = _extract_markdown_marker.main(
                    ["source.pdf", str(output_path), "--image-dir", str(image_dir)]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "# Marker body")
            fake_image.save.assert_called_once_with(image_dir / "fig1.png")

    def test_no_image_dir_skips_image_saving(self):
        fake_image = MagicMock()
        fake_marker_module = _make_fake_marker_module(text="# Marker body", images={"fig1.png": fake_image})

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "output.md"

            with patch.dict(sys.modules, fake_marker_module):
                from zotero_pdf_text import _extract_markdown_marker

                exit_code = _extract_markdown_marker.main(["source.pdf", str(output_path)])

        self.assertEqual(exit_code, 0)
        fake_image.save.assert_not_called()


def _make_fake_marker_module(*, text: str, images: dict) -> dict:
    fake_converter_instance = MagicMock(return_value="rendered")
    fake_pdf_converter_cls = MagicMock(return_value=fake_converter_instance)

    fake_converters_pdf = MagicMock()
    fake_converters_pdf.PdfConverter = fake_pdf_converter_cls
    fake_converters = MagicMock()
    fake_converters.pdf = fake_converters_pdf

    fake_models = MagicMock()
    fake_models.create_model_dict = MagicMock(return_value={})

    fake_output = MagicMock()
    fake_output.text_from_rendered = MagicMock(return_value=(text, {}, images))

    fake_marker = MagicMock()
    fake_marker.converters = fake_converters
    fake_marker.models = fake_models
    fake_marker.output = fake_output

    return {
        "marker": fake_marker,
        "marker.converters": fake_converters,
        "marker.converters.pdf": fake_converters_pdf,
        "marker.models": fake_models,
        "marker.output": fake_output,
    }


if __name__ == "__main__":
    unittest.main()
