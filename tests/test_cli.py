import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.cli import _shell_quote, build_parser, main
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


class ShellQuoteTests(unittest.TestCase):
    def test_plain_windows_path_is_not_quoted(self):
        self.assertEqual(_shell_quote(r"C:\Users\you\Scripts\zotero-fulltext-mcp.exe"), r"C:\Users\you\Scripts\zotero-fulltext-mcp.exe")

    def test_path_with_space_is_quoted(self):
        self.assertEqual(_shell_quote(r"C:\Program Files\zotero-fulltext-mcp.exe"), '"C:\\Program Files\\zotero-fulltext-mcp.exe"')

    def test_path_with_shell_metacharacter_is_quoted(self):
        # '&' is a command separator in both cmd and PowerShell when left unquoted.
        self.assertEqual(_shell_quote(r"C:\Zotero&Research\config.json"), '"C:\\Zotero&Research\\config.json"')


class InstallMcpCliTests(unittest.TestCase):
    def test_parser_defaults(self):
        args = build_parser().parse_args(["install-mcp"])
        self.assertEqual(args.command, "install-mcp")
        self.assertEqual(args.server_name, "zotero-fulltext")
        self.assertIsNone(args.config)
        self.assertIsNone(args.db)
        self.assertFalse(args.enable_bibtex)
        self.assertIsNone(args.bibtex_endpoint)
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
            self.assertIn("claude mcp add", printed)
            self.assertNotIn("add-json", printed)
            self.assertIn(" -- --db ", printed)  # separator, or Claude parses --db as its own flag
            self.assertIn(str(root / "converted_text" / "index" / "zotero_text_index.sqlite"), printed)
            self.assertIn("[mcp_servers.zotero_fulltext]", printed)
            self.assertIn(str(config_path), printed)
            self.assertIn("--config", printed)

    def test_codex_toml_block_round_trips_windows_style_paths(self):
        # Regression test: the codex_block used to build its `args` list with Python's repr(),
        # which escapes each backslash as two characters. TOML single-quoted (literal) strings
        # don't interpret escapes, so parsing that output doubled every backslash in a Windows
        # path. Build a config/db path that contains backslashes even on non-Windows CI, and
        # verify the printed TOML block parses back to the exact original path.
        import tomllib

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            windows_style_root = root / "Users" / "Matze" / "zotero-data"
            windows_style_root.mkdir(parents=True)
            config_path = windows_style_root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(windows_style_root),
                        "zotero_data_directory": str(windows_style_root),
                        "linked_attachments": str(windows_style_root),
                        "output_root": str(windows_style_root / "converted_text"),
                    }
                ),
                encoding="utf-8",
            )
            with patch("sys.executable", str(root / "Scripts" / "python.exe")), patch("sys.stdout") as mock_stdout:
                exit_code = main(["install-mcp", "--config", str(config_path)])
            self.assertEqual(exit_code, 0)
            printed = "".join(call.args[0] for call in mock_stdout.write.call_args_list)

            toml_start = printed.index("[mcp_servers.zotero_fulltext]")
            toml_block = printed[toml_start:]
            parsed = tomllib.loads(toml_block)
            server = parsed["mcp_servers"]["zotero_fulltext"]
            self.assertIn("--config", server["args"])
            config_arg = server["args"][server["args"].index("--config") + 1]
            self.assertEqual(config_arg, str(config_path))

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
                "zotero_pdf_text.cli.shutil.which", return_value="C:/fake/claude.cmd"
            ), patch("zotero_pdf_text.cli.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                exit_code = main(["install-mcp", "--config", str(config_path), "--apply"])
            self.assertEqual(exit_code, 0)
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            expected_db = str(root / "converted_text" / "index" / "zotero_text_index.sqlite")
            # _install_mcp derives the exe suffix from the actual running OS (os.name), not from
            # the shape of the mocked sys.executable path, so the expectation must match whatever
            # platform this test is actually running on.
            exe_name = "zotero-fulltext-mcp.exe" if os.name == "nt" else "zotero-fulltext-mcp"
            # _install_mcp resolves sys.executable's parent (Path.resolve()), which on macOS
            # follows the /tmp -> /private/tmp (and /var -> /private/var) symlink -- resolve here
            # too so the expectation matches on macOS runners, not just Windows/Linux.
            expected_exe = str((root / "Scripts" / exe_name).resolve())
            # Exact ordered argv, not just membership -- catches regressions like a dropped '--'
            # separator, which would make Claude parse '--db'/'--config' as its own options
            # instead of forwarding them to the server.
            self.assertEqual(
                call_args,
                [
                    "C:/fake/claude.cmd",
                    "mcp", "add", "--scope", "user", "zotero-fulltext",
                    expected_exe,
                    "--", "--db", expected_db, "--config", str(config_path),
                ],
            )

    def test_apply_reports_error_when_claude_not_on_path(self):
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
                "zotero_pdf_text.cli.shutil.which", return_value=None
            ):
                exit_code = main(["install-mcp", "--config", str(config_path), "--apply"])
            self.assertEqual(exit_code, 2)

    def test_optional_bibtex_registration_forwards_only_startup_options(self):
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
                exit_code = main(["install-mcp", "--config", str(config_path), "--enable-bibtex"])
            printed = "".join(call.args[0] for call in mock_stdout.write.call_args_list)
            self.assertEqual(exit_code, 0)
            self.assertIn("--enable-bibtex", printed)
            self.assertIn("export_bibtex_entries_by_key", printed)

    def test_bibtex_endpoint_requires_integration_flag(self):
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
            self.assertEqual(
                main(["install-mcp", "--config", str(config_path), "--bibtex-endpoint", "http://127.0.0.1:23119/x"]),
                2,
            )


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


class SearchCliTests(unittest.TestCase):
    def test_parser_exposes_the_explicit_search_modes(self):
        args = build_parser().parse_args(["search-fts", "--query", "topic"])
        self.assertEqual(args.search_mode, "all_terms")

        any_terms = build_parser().parse_args(["search-fts", "--query", "topic", "--search-mode", "any_terms"])
        self.assertEqual(any_terms.search_mode, "any_terms")

    def test_json_search_result_reports_mode_and_no_results(self):
        with patch("zotero_pdf_text.cli.search_fts", return_value=[]) as search:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["search-fts", "--db", "unused.sqlite", "--query", "topic", "--search-mode", "any_terms", "--json"]
                )

        self.assertEqual(exit_code, 0)
        search.assert_called_once_with(Path("unused.sqlite"), "topic", limit=10, search_mode="any_terms")
        self.assertEqual(json.loads(output.getvalue()), {"search_mode": "any_terms", "no_results": True, "results": []})


if __name__ == "__main__":
    unittest.main()
