import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.cli import build_parser, main
from zotero_pdf_text.math_ocr import ReconvertResult


class ReconvertMathCliTests(unittest.TestCase):
    def test_parser_defaults(self):
        args = build_parser().parse_args(["reconvert-math", "--key", "ABCD1234"])
        self.assertEqual(args.command, "reconvert-math")
        self.assertEqual(args.key, "ABCD1234")
        self.assertEqual(args.config, Path("config.json"))
        self.assertIsNone(args.jsonl)
        self.assertIsNone(args.fts_db)
        self.assertEqual(args.timeout_seconds, 5400)

    def test_dispatch_calls_reconvert_with_marker_and_returns_zero_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                '{"zotero_root": "%s", "zotero_data_directory": "%s", '
                '"linked_attachments": "%s", "output_root": "%s"}'
                % (root, root, root, root / "output"),
                encoding="utf-8",
            )
            success = ReconvertResult(
                ok=True,
                attachment_key="ABCD1234",
                previous_extraction_tool="pymupdf4llm.to_markdown",
                new_extraction_tool="marker",
                previous_char_count=10,
                new_char_count=20,
                markdown_path=str(root / "paper.md"),
                source_path=str(root / "paper.pdf"),
                reconverted_at="2026-07-05T00:00:00",
                error="",
            )
            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch("zotero_pdf_text.math_ocr.reconvert_with_marker", return_value=success) as mock_reconvert:
                from zotero_pdf_text.config import ProjectConfig

                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(["reconvert-math", "--key", "ABCD1234", "--config", str(config_path)])

            self.assertEqual(exit_code, 0)
            mock_reconvert.assert_called_once()
            _, kwargs = mock_reconvert.call_args
            self.assertEqual(kwargs["timeout_seconds"], 5400)

    def test_dispatch_returns_nonzero_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failure = ReconvertResult(
                ok=False,
                attachment_key="ABCD1234",
                previous_extraction_tool="",
                new_extraction_tool="",
                previous_char_count=0,
                new_char_count=0,
                markdown_path="",
                source_path="",
                reconverted_at="",
                error="No indexed record found",
            )
            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch("zotero_pdf_text.math_ocr.reconvert_with_marker", return_value=failure):
                from zotero_pdf_text.config import ProjectConfig

                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(["reconvert-math", "--key", "ABCD1234"])

            self.assertEqual(exit_code, 1)


class InstallMcpCliTests(unittest.TestCase):
    def test_parser_defaults(self):
        args = build_parser().parse_args(["install-mcp"])
        self.assertEqual(args.command, "install-mcp")
        self.assertEqual(args.server_name, "zotero-fulltext")
        self.assertIsNone(args.config)
        self.assertIsNone(args.db)
        self.assertFalse(args.apply)

    def test_missing_config_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            exit_code = main(["install-mcp", "--config", str(missing)])
            self.assertEqual(exit_code, 2)

    def test_prints_resolved_paths_and_claude_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(root),
                        "zotero_data_directory": str(root),
                        "linked_attachments": str(root),
                        "output_root": str(root / "converted_text"),
                    }
                ),
                encoding="utf-8",
            )
            with patch("sys.executable", str(root / "Scripts" / "python.exe")), patch("sys.stdout") as mock_stdout:
                exit_code = main(["install-mcp", "--config", str(config_path)])
            printed = "".join(call.args[0] for call in mock_stdout.write.call_args_list)
            self.assertEqual(exit_code, 0)
            self.assertIn("claude mcp add-json", printed)
            self.assertIn(str(root / "converted_text" / "index" / "zotero_text_index.sqlite"), printed)
            self.assertIn("[mcp_servers.zotero_fulltext]", printed)
            self.assertIn(str(config_path), printed)
            self.assertIn("'--config'", printed)

    def test_apply_invokes_subprocess_with_expected_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(root),
                        "zotero_data_directory": str(root),
                        "linked_attachments": str(root),
                        "output_root": str(root / "converted_text"),
                    }
                ),
                encoding="utf-8",
            )
            with patch("sys.executable", str(root / "Scripts" / "python.exe")), patch(
                "zotero_pdf_text.cli.subprocess.run"
            ) as mock_run:
                mock_run.return_value.returncode = 0
                exit_code = main(["install-mcp", "--config", str(config_path), "--apply"])
            self.assertEqual(exit_code, 0)
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            self.assertEqual(call_args[:5], ["claude", "mcp", "add-json", "--scope", "user"])
            self.assertEqual(call_args[5], "zotero-fulltext")
            payload = json.loads(call_args[6])
            self.assertIn("--config", payload["args"])
            self.assertEqual(payload["args"][payload["args"].index("--config") + 1], str(config_path))


class AppendIndexCliTests(unittest.TestCase):
    def test_parser_defaults(self):
        args = build_parser().parse_args(["append-index", "--manifest", "new.csv"])
        self.assertEqual(args.command, "append-index")
        self.assertEqual(args.manifest, Path("new.csv"))
        self.assertEqual(args.index, Path("converted_text/index/zotero_text_index.jsonl"))
        self.assertIsNone(args.fts_db)

    def test_appends_new_rows_and_skips_existing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing_md = root / "existing.md"
            existing_md.write_text("---\nzotero_attachment_key: OLD1\n---\nOld body", encoding="utf-8")
            new_md = root / "new.md"
            new_md.write_text("---\nzotero_attachment_key: NEW1\n---\nNew body", encoding="utf-8")

            existing_manifest = root / "existing_manifest.csv"
            existing_manifest.write_text(
                "status,output_path,zotero_attachment_key,zotero_parent_key,title,creators,year,doi,"
                "citation_key,source_path,extraction_tool,page_count,classification,identity_status,"
                "identity_rule,has_math\n"
                f"converted,{existing_md},OLD1,P1,Old Title,,,,,,pymupdf,1,mapped_verified,verified,doi_exact,false\n",
                encoding="utf-8",
            )
            index_path = root / "index" / "zotero_text_index.jsonl"
            from zotero_pdf_text.indexer import build_text_index

            build_text_index(existing_manifest, index_path)

            new_manifest = root / "new_manifest.csv"
            new_manifest.write_text(
                "status,output_path,zotero_attachment_key,zotero_parent_key,title,creators,year,doi,"
                "citation_key,source_path,extraction_tool,page_count,classification,identity_status,"
                "identity_rule,has_math\n"
                f"converted,{new_md},NEW1,P2,New Title,,,,,,pymupdf,1,mapped_verified,fulltext_verified,"
                "fulltext_review:agent,false\n"
                f"converted,{existing_md},OLD1,P1,Old Title,,,,,,pymupdf,1,mapped_verified,verified,doi_exact,false\n",
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "append-index",
                    "--manifest",
                    str(new_manifest),
                    "--index",
                    str(index_path),
                ]
            )
            self.assertEqual(exit_code, 0)

            keys = {
                json.loads(line)["zotero_attachment_key"]
                for line in index_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            self.assertEqual(keys, {"OLD1", "NEW1"})
            self.assertTrue((root / "index" / "zotero_text_index.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
