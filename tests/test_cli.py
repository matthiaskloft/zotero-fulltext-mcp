import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.cli import _shell_quote, build_parser, main
from zotero_pdf_text.config import resolve_config_path
from zotero_pdf_text.fts import ChunkNotFoundError, SearchResult
from zotero_pdf_text.math_ocr import ReconvertResult


class ReconvertMathCliTests(unittest.TestCase):
    def test_parser_defaults(self):
        args = build_parser().parse_args(["reconvert-math", "--key", "ABCD1234"])
        self.assertEqual(args.command, "reconvert-math")
        self.assertEqual(args.key, "ABCD1234")
        self.assertEqual(args.config, resolve_config_path())
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


class RetryTimeoutCliTests(unittest.TestCase):
    def test_parser_requires_exactly_one_of_skip_or_retry(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["retry-timeout", "--key", "ABCD1234"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["retry-timeout", "--key", "ABCD1234", "--skip", "--retry"])

    def test_parser_defaults_for_retry(self):
        args = build_parser().parse_args(["retry-timeout", "--key", "ABCD1234", "--retry"])
        self.assertEqual(args.command, "retry-timeout")
        self.assertEqual(args.key, "ABCD1234")
        self.assertTrue(args.retry)
        self.assertFalse(args.skip)
        self.assertIsNone(args.timeout_seconds)
        self.assertIsNone(args.multiplier)

    def test_parser_defaults_for_skip(self):
        args = build_parser().parse_args(["retry-timeout", "--key", "ABCD1234", "--skip"])
        self.assertTrue(args.skip)
        self.assertEqual(args.reason, "")

    def test_skip_dispatch_calls_skip_timeout_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                '{"zotero_root": "%s", "zotero_data_directory": "%s", '
                '"linked_attachments": "%s", "output_root": "%s"}'
                % (root, root, root, root / "output"),
                encoding="utf-8",
            )
            from zotero_pdf_text.config import ProjectConfig
            from zotero_pdf_text.retry_timeout import RetryTimeoutResult

            success = RetryTimeoutResult(
                ok=True,
                action="skip",
                attachment_key="ABCD1234",
                previous_status="pending",
                new_status="skipped",
                timeout_seconds_used=None,
                extraction_tool="",
                markdown_path="",
                error="",
                resolved_at="2026-07-15T00:00:00",
            )
            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch("zotero_pdf_text.retry_timeout.skip_timeout_candidate", return_value=success) as mock_skip:
                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(
                    ["retry-timeout", "--key", "ABCD1234", "--skip", "--reason", "too slow", "--config", str(config_path)]
                )

            self.assertEqual(exit_code, 0)
            mock_skip.assert_called_once()
            _, kwargs = mock_skip.call_args
            self.assertEqual(kwargs["reason"], "too slow")

    def test_retry_dispatch_calls_retry_timeout_candidate_and_returns_expected_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from zotero_pdf_text.config import ProjectConfig
            from zotero_pdf_text.retry_timeout import RetryTimeoutResult

            failure = RetryTimeoutResult(
                ok=False,
                action="retry",
                attachment_key="ABCD1234",
                previous_status="pending",
                new_status="pending",
                timeout_seconds_used=1200,
                extraction_tool="",
                markdown_path="",
                error="No timeout candidate found",
                resolved_at="",
            )
            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch("zotero_pdf_text.retry_timeout.retry_timeout_candidate", return_value=failure) as mock_retry:
                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(["retry-timeout", "--key", "ABCD1234", "--retry", "--multiplier", "2.0"])

            self.assertEqual(exit_code, 1)
            mock_retry.assert_called_once()
            _, kwargs = mock_retry.call_args
            self.assertEqual(kwargs["multiplier"], 2.0)
            self.assertIsNone(kwargs["timeout_seconds"])

    def test_rejects_timeout_seconds_and_multiplier_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from zotero_pdf_text.config import ProjectConfig

            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch("zotero_pdf_text.cli.validate_config"):
                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(
                    ["retry-timeout", "--key", "ABCD1234", "--retry", "--timeout-seconds", "1000", "--multiplier", "2.0"]
                )

            self.assertEqual(exit_code, 2)


class VerifyUnverifiedCliTests(unittest.TestCase):
    def test_parser_defaults_index_jsonl_to_none(self):
        args = build_parser().parse_args(
            ["verify-unverified", "--mapping-report", "mapping_report.csv"]
        )
        self.assertEqual(args.command, "verify-unverified")
        self.assertIsNone(args.index_jsonl)

    def test_parser_accepts_explicit_index_jsonl(self):
        args = build_parser().parse_args(
            [
                "verify-unverified",
                "--mapping-report",
                "mapping_report.csv",
                "--index-jsonl",
                "custom_index.jsonl",
            ]
        )
        self.assertEqual(args.index_jsonl, Path("custom_index.jsonl"))

    def test_dispatch_forwards_index_jsonl_to_verify_unverified(self):
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
                    }
                ),
                encoding="utf-8",
            )
            (root / "zotero.sqlite").write_bytes(b"")
            mapping_report = root / "mapping_report.csv"
            mapping_report.write_text("classification\n", encoding="utf-8")
            captured = {}

            def _fake_verify_unverified(config, mapping_report_arg, **kwargs):
                captured.update(kwargs)
                run_dir = root / "output" / "unverified_review" / "run"
                run_dir.mkdir(parents=True, exist_ok=True)
                return run_dir

            with patch("zotero_pdf_text.cli.verify_unverified", side_effect=_fake_verify_unverified):
                exit_code = main(
                    [
                        "verify-unverified",
                        "--config",
                        str(config_path),
                        "--mapping-report",
                        str(mapping_report),
                        "--index-jsonl",
                        str(root / "custom_index.jsonl"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["index_jsonl"], root / "custom_index.jsonl")


class FindOrphanParentsCliTests(unittest.TestCase):
    def test_parser_defaults(self):
        args = build_parser().parse_args(["find-orphan-parents", "--mapping-report", "report.csv"])
        self.assertEqual(args.command, "find-orphan-parents")
        self.assertEqual(args.mapping_report, Path("report.csv"))
        self.assertEqual(args.config, resolve_config_path())
        self.assertIsNone(args.output_dir)
        self.assertIsNone(args.limit)

    def test_dispatch_calls_run_orphan_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from zotero_pdf_text.config import ProjectConfig

            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch(
                "zotero_pdf_text.orphan_discovery.run_orphan_discovery", return_value=root / "output" / "orphan_discovery" / "run1"
            ) as mock_run:
                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(["find-orphan-parents", "--mapping-report", str(root / "report.csv"), "--limit", "5"])

            self.assertEqual(exit_code, 0)
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs["limit"], 5)


class FindDuplicateAttachmentsCliTests(unittest.TestCase):
    def test_parser_defaults(self):
        args = build_parser().parse_args(["find-duplicate-attachments", "--mapping-report", "report.csv"])
        self.assertEqual(args.command, "find-duplicate-attachments")
        self.assertEqual(args.mapping_report, Path("report.csv"))
        self.assertEqual(args.config, resolve_config_path())
        self.assertIsNone(args.output_dir)

    def test_dispatch_calls_run_duplicate_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from zotero_pdf_text.config import ProjectConfig

            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch(
                "zotero_pdf_text.duplicate_attachments.run_duplicate_discovery",
                return_value=root / "output" / "duplicate_attachments" / "run1",
            ) as mock_run:
                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(["find-duplicate-attachments", "--mapping-report", str(root / "report.csv")])

            self.assertEqual(exit_code, 0)
            mock_run.assert_called_once()


class OrphanCandidateCliTests(unittest.TestCase):
    def test_parser_requires_exactly_one_of_skip_or_mark_resolved(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["orphan-candidate", "--orphan-sha256", "SHA1", "--parent-key", "PARENT1"]
            )
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["orphan-candidate", "--orphan-sha256", "SHA1", "--parent-key", "PARENT1", "--skip", "--mark-resolved"]
            )

    def test_skip_dispatch_calls_skip_orphan_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from zotero_pdf_text.config import ProjectConfig
            from zotero_pdf_text.orphan_discovery import OrphanResolutionResult

            success = OrphanResolutionResult(
                ok=True,
                action="skip",
                orphan_sha256="SHA1",
                candidate_parent_key="PARENT1",
                previous_status="pending",
                new_status="skipped",
                error="",
                resolved_at="2026-07-15T00:00:00",
            )
            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch("zotero_pdf_text.orphan_discovery.skip_orphan_candidate", return_value=success) as mock_skip:
                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(
                    [
                        "orphan-candidate",
                        "--orphan-sha256",
                        "SHA1",
                        "--parent-key",
                        "PARENT1",
                        "--skip",
                        "--reason",
                        "not a match",
                    ]
                )

            self.assertEqual(exit_code, 0)
            mock_skip.assert_called_once()
            _, kwargs = mock_skip.call_args
            self.assertEqual(kwargs["reason"], "not a match")

    def test_mark_resolved_dispatch_calls_mark_orphan_candidate_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from zotero_pdf_text.config import ProjectConfig
            from zotero_pdf_text.orphan_discovery import OrphanResolutionResult

            success = OrphanResolutionResult(
                ok=True,
                action="mark-resolved",
                orphan_sha256="SHA1",
                candidate_parent_key="PARENT1",
                previous_status="pending",
                new_status="resolved",
                error="",
                resolved_at="2026-07-15T00:00:00",
            )
            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch(
                "zotero_pdf_text.orphan_discovery.mark_orphan_candidate_resolved", return_value=success
            ) as mock_resolve:
                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(
                    ["orphan-candidate", "--orphan-sha256", "SHA1", "--parent-key", "PARENT1", "--mark-resolved"]
                )

            self.assertEqual(exit_code, 0)
            mock_resolve.assert_called_once()

    def test_failed_resolution_returns_nonzero_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from zotero_pdf_text.config import ProjectConfig
            from zotero_pdf_text.orphan_discovery import OrphanResolutionResult

            failure = OrphanResolutionResult(
                ok=False,
                action="skip",
                orphan_sha256="SHA1",
                candidate_parent_key="PARENT1",
                previous_status="",
                new_status="",
                error="No orphan candidate found for match key SHA1:PARENT1",
                resolved_at="",
            )
            with patch("zotero_pdf_text.cli.load_config") as mock_load_config, patch(
                "zotero_pdf_text.cli.validate_config"
            ), patch("zotero_pdf_text.orphan_discovery.skip_orphan_candidate", return_value=failure):
                mock_load_config.return_value = ProjectConfig(root, root, root, root / "output")
                exit_code = main(
                    ["orphan-candidate", "--orphan-sha256", "SHA1", "--parent-key", "PARENT1", "--skip"]
                )

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
    def test_cli_import_and_parser_do_not_require_mcp_dependency(self):
        code = (
            "import builtins\n"
            "real_import = builtins.__import__\n"
            "def blocked(name, *args, **kwargs):\n"
            "    if name == 'mcp' or name.startswith('mcp.'):\n"
            "        raise ImportError('blocked for test')\n"
            "    return real_import(name, *args, **kwargs)\n"
            "builtins.__import__ = blocked\n"
            "from zotero_pdf_text.cli import build_parser\n"
            "build_parser()\n"
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_parser_defaults(self):
        args = build_parser().parse_args(["install-mcp"])
        self.assertEqual(args.command, "install-mcp")
        self.assertEqual(args.server_name, "zotero-fulltext")
        self.assertIsNone(args.config)
        self.assertIsNone(args.db)
        self.assertFalse(args.enable_bibtex)
        self.assertFalse(args.enable_reconvert)
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
            self.assertNotIn("--enable-reconvert", printed)
            self.assertNotIn("reconvert_with_math_ocr", printed)

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

    def test_optional_reconversion_registration_forwards_flag_and_tool(self):
        import tomllib

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            (root / "zotero.sqlite").write_bytes(b"")
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
                "zotero_pdf_text.cli.marker_dependency_available", return_value=True
            ), patch("sys.stdout") as mock_stdout:
                exit_code = main(["install-mcp", "--config", str(config_path), "--enable-reconvert"])
            printed = "".join(call.args[0] for call in mock_stdout.write.call_args_list)
            self.assertEqual(exit_code, 0)
            self.assertIn("--enable-reconvert", printed)
            self.assertIn("reconvert_with_math_ocr", printed)

            toml_start = printed.index("[mcp_servers.zotero_fulltext]")
            server = tomllib.loads(printed[toml_start:])["mcp_servers"]["zotero_fulltext"]
            self.assertEqual(server["args"][-1], "--enable-reconvert")
            self.assertEqual(server["tool_timeout_sec"], 6000)
            self.assertEqual(
                server["enabled_tools"],
                [
                    "search_fulltext",
                    "get_fulltext_chunk",
                    "get_item_context",
                    "list_timeout_candidates",
                    "list_orphan_candidates",
                    "reconvert_with_math_ocr",
                ],
            )

    def test_optional_reconversion_apply_forwards_exact_claude_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "zotero.sqlite").write_bytes(b"")
            output_root = root / "converted_text"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(root),
                        "zotero_data_directory": str(root),
                        "linked_attachments": str(root),
                        "output_root": str(output_root),
                    }
                ),
                encoding="utf-8",
            )
            with patch("sys.executable", str(root / "Scripts" / "python.exe")), patch(
                "zotero_pdf_text.cli.shutil.which", return_value="C:/fake/claude.cmd"
            ), patch("zotero_pdf_text.cli.subprocess.run") as mock_run, patch(
                "zotero_pdf_text.cli.marker_dependency_available", return_value=True
            ):
                mock_run.return_value.returncode = 0
                exit_code = main(
                    ["install-mcp", "--config", str(config_path), "--enable-reconvert", "--apply"]
                )
            self.assertEqual(exit_code, 0)
            exe_name = "zotero-fulltext-mcp.exe" if os.name == "nt" else "zotero-fulltext-mcp"
            expected_exe = str((root / "Scripts" / exe_name).resolve())
            expected_db = str(output_root / "index" / "zotero_text_index.sqlite")
            self.assertEqual(
                mock_run.call_args[0][0],
                [
                    "C:/fake/claude.cmd",
                    "mcp", "add", "--scope", "user", "zotero-fulltext",
                    expected_exe,
                    "--", "--db", expected_db, "--config", str(config_path), "--enable-reconvert",
                ],
            )

    def test_optional_reconversion_rejects_invalid_config_and_database_mismatch(self):
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
            self.assertEqual(main(["install-mcp", "--config", str(config_path), "--enable-reconvert"]), 2)

            (root / "zotero.sqlite").write_bytes(b"")
            self.assertEqual(
                main(
                    [
                        "install-mcp",
                        "--config", str(config_path),
                        "--db", str(root / "different.sqlite"),
                        "--enable-reconvert",
                    ]
                ),
                2,
            )

    def test_optional_reconversion_rejects_missing_marker_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            (root / "zotero.sqlite").write_bytes(b"")
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
            with patch("zotero_pdf_text.cli.marker_dependency_available", return_value=False):
                self.assertEqual(main(["install-mcp", "--config", str(config_path), "--enable-reconvert"]), 2)

    def test_enable_retry_timeout_registration_forwards_flag_and_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            (root / "zotero.sqlite").write_bytes(b"")
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
                exit_code = main(["install-mcp", "--config", str(config_path), "--enable-retry-timeout"])
            printed = "".join(call.args[0] for call in mock_stdout.write.call_args_list)
            self.assertEqual(exit_code, 0)
            self.assertIn("--enable-retry-timeout", printed)
            self.assertIn("skip_timeout_extraction", printed)
            self.assertIn("retry_timeout_extraction", printed)

    def test_enable_retry_timeout_does_not_require_marker_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            (root / "zotero.sqlite").write_bytes(b"")
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
            with patch("zotero_pdf_text.cli.marker_dependency_available", return_value=False), patch(
                "sys.executable", str(root / "Scripts" / "python.exe")
            ), patch("sys.stdout"):
                exit_code = main(["install-mcp", "--config", str(config_path), "--enable-retry-timeout"])
            self.assertEqual(exit_code, 0)

    def test_enable_retry_timeout_rejects_database_config_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            (root / "zotero.sqlite").write_bytes(b"")
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
                main(
                    [
                        "install-mcp",
                        "--config", str(config_path),
                        "--db", str(root / "different.sqlite"),
                        "--enable-retry-timeout",
                    ]
                ),
                2,
            )

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


class ManagedIndexCliTests(unittest.TestCase):
    _MANIFEST_HEADER = (
        "status,output_path,zotero_attachment_key,zotero_parent_key,title,creators,year,doi,"
        "citation_key,source_path,extraction_tool,page_count,classification,identity_status,"
        "identity_rule,has_math\n"
    )

    def test_parser_defaults(self):
        args = build_parser().parse_args(["rebuild-index"])
        self.assertEqual(args.command, "rebuild-index")
        self.assertIsNone(args.manifest)
        self.assertIsNone(args.from_jsonl)
        args = build_parser().parse_args(["update-index", "--manifest", "new.csv"])
        self.assertEqual(args.command, "update-index")
        self.assertEqual(args.manifest, Path("new.csv"))

    def test_rebuild_migrates_legacy_jsonl_and_update_appends_new_rows(self):
        from zotero_pdf_text.artifacts import current_generation_jsonl, read_current_pointer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "converted_text"
            index_root = output_root / "index"
            index_root.mkdir(parents=True)
            existing_md = root / "existing.md"
            existing_md.write_text("---\nzotero_attachment_key: OLD1\n---\nOld body", encoding="utf-8")
            new_md = root / "new.md"
            new_md.write_text("---\nzotero_attachment_key: NEW1\n---\nNew body", encoding="utf-8")

            legacy_jsonl = index_root / "zotero_text_index.jsonl"
            legacy_jsonl.write_text(
                json.dumps(
                    {
                        "zotero_parent_key": "P1",
                        "zotero_attachment_key": "OLD1",
                        "title": "Old Title",
                        "creators": "",
                        "year": "",
                        "doi": "",
                        "citation_key": "",
                        "source_path": "",
                        "markdown_path": str(existing_md),
                        "markdown_sha256": "old",
                        "extraction_tool": "pymupdf",
                        "char_count": 8,
                        "word_count": 2,
                        "page_count": "1",
                        "classification": "mapped_verified",
                        "identity_status": "verified",
                        "identity_rule": "doi_exact",
                        "has_math": False,
                        "text": "Old body",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(["rebuild-index", "--output-root", str(output_root)])
            self.assertEqual(exit_code, 0, buffer.getvalue())
            pointer = read_current_pointer(index_root)
            self.assertIsNotNone(pointer)
            first_generation = pointer["current_generation"]

            new_manifest = root / "new_manifest.csv"
            new_manifest.write_text(
                self._MANIFEST_HEADER
                + f"converted,{new_md},NEW1,P2,New Title,,,,,,pymupdf,1,mapped_verified,fulltext_verified,"
                "fulltext_review:agent,false\n"
                + f"converted,{existing_md},OLD1,P1,Old Title,,,,,,pymupdf,1,mapped_verified,verified,doi_exact,false\n",
                encoding="utf-8",
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(
                    ["update-index", "--output-root", str(output_root), "--manifest", str(new_manifest)]
                )
            self.assertEqual(exit_code, 0, buffer.getvalue())
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["new_records"], 1)
            self.assertNotEqual(payload["generation_id"], first_generation)

            current_jsonl = current_generation_jsonl(index_root)
            keys = [
                json.loads(line)["zotero_attachment_key"]
                for line in current_jsonl.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(keys, ["OLD1", "NEW1"])
            # The legacy files are left untouched for the user to remove manually.
            self.assertTrue(legacy_jsonl.exists())

    def test_update_index_without_generation_directs_to_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "converted_text"
            manifest = Path(tmp) / "manifest.csv"
            manifest.write_text(self._MANIFEST_HEADER, encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(
                    ["update-index", "--output-root", str(output_root), "--manifest", str(manifest)]
                )
            self.assertEqual(exit_code, 2)

    def test_rebuild_index_reports_duplicate_attachment_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "converted_text"
            index_root = output_root / "index"
            index_root.mkdir(parents=True)
            record = {
                "zotero_parent_key": "P1",
                "zotero_attachment_key": "DUP1",
                "title": "Title",
                "creators": "",
                "year": "",
                "doi": "",
                "citation_key": "",
                "source_path": "",
                "markdown_path": "x.md",
                "markdown_sha256": "a",
                "extraction_tool": "pymupdf",
                "char_count": 4,
                "word_count": 1,
                "page_count": "1",
                "classification": "mapped_verified",
                "identity_status": "verified",
                "identity_rule": "doi_exact",
                "has_math": False,
                "text": "body",
            }
            legacy_jsonl = index_root / "zotero_text_index.jsonl"
            legacy_jsonl.write_text(
                json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8"
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(["rebuild-index", "--output-root", str(output_root)])
            self.assertEqual(exit_code, 2)
            # No generation was published.
            from zotero_pdf_text.artifacts import read_current_pointer

            self.assertIsNone(read_current_pointer(index_root))


class SearchCliTests(unittest.TestCase):
    def test_parser_exposes_the_explicit_search_modes(self):
        args = build_parser().parse_args(["search-fts", "--query", "topic"])
        self.assertEqual(args.search_mode, "all_terms")

        any_terms = build_parser().parse_args(["search-fts", "--query", "topic", "--search-mode", "any_terms"])
        self.assertEqual(any_terms.search_mode, "any_terms")

    def test_json_search_result_reports_mode_and_no_results(self):
        with patch("zotero_pdf_text.cli.search_fts", return_value=[]) as search, patch(
            "zotero_pdf_text.cli.resolve_reader_db_path", side_effect=lambda p: p
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["search-fts", "--db", "unused.sqlite", "--query", "topic", "--search-mode", "any_terms", "--json"]
                )

        self.assertEqual(exit_code, 0)
        search.assert_called_once_with(Path("unused.sqlite"), "topic", limit=10, search_mode="any_terms")
        self.assertEqual(json.loads(output.getvalue()), {"search_mode": "any_terms", "no_results": True, "results": []})

    def test_json_search_result_includes_content_hash_and_matching_fields(self):
        result = SearchResult(
            zotero_parent_key="PARENT1",
            zotero_attachment_key="ATTACH1",
            title="Title match",
            creators="Author",
            year="2026",
            doi="",
            citation_key="key2026",
            snippet="Title match",
            score=-1.0,
            chunk_index=0,
            start_char=0,
            end_char=10,
            markdown_sha256="abc123",
            matched_fields=["title"],
            source_path="paper.pdf",
            markdown_path="paper.md",
            extraction_tool="marker",
            classification="mapped_verified",
            identity_status="verified",
            identity_rule="doi_exact",
            has_math=False,
        )
        with patch("zotero_pdf_text.cli.search_fts", return_value=[result]), patch(
            "zotero_pdf_text.cli.resolve_reader_db_path", side_effect=lambda p: p
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["search-fts", "--db", "unused.sqlite", "--query", "title", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["results"][0]["markdown_sha256"], "abc123")
        self.assertEqual(payload["results"][0]["matched_fields"], ["title"])

    def test_get_fulltext_reports_out_of_range_chunk_index_as_a_clean_error(self):
        with patch(
            "zotero_pdf_text.cli.get_fulltext",
            side_effect=ChunkNotFoundError("Chunk 99 does not exist for attachment ATTACH1"),
        ), patch("zotero_pdf_text.cli.resolve_reader_db_path", side_effect=lambda p: p):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "get-fulltext",
                        "--db",
                        "unused.sqlite",
                        "--attachment-key",
                        "ATTACH1",
                        "--chunk-index",
                        "99",
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
