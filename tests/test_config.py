import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.config import load_config


class LoadConfigTests(unittest.TestCase):
    def test_loads_paths_and_optional_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(root),
                        "zotero_data_directory": str(root),
                        "linked_attachments": str(root),
                        "output_root": str(root / "output"),
                        "early_pages": 5,
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(config.early_pages, 5)
            self.assertEqual(config.max_page_chars, 12000)


if __name__ == "__main__":
    unittest.main()
