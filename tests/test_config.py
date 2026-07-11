import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.config import load_config


class LoadConfigSharedOverrideTests(unittest.TestCase):
    def test_machine_file_alone_still_works(self):
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

    def test_shared_file_supplies_defaults_machine_file_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.shared.json").write_text(
                json.dumps(
                    {
                        "early_pages": 3,
                        "max_page_chars": 12000,
                        "manually_accepted_mappings": [
                            {"attachment_key": "ABCD1234", "source_name": "shared.pdf"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.nu006612.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(root),
                        "zotero_data_directory": str(root),
                        "linked_attachments": str(root),
                        "output_root": str(root / "output"),
                        "max_page_chars": 20000,
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(config.early_pages, 3)
            self.assertEqual(config.max_page_chars, 20000)
            self.assertIn(("ABCD1234", "shared.pdf"), config.manually_accepted_mappings)


if __name__ == "__main__":
    unittest.main()
