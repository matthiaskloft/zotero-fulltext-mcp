import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.cli import main


class CodexRegistrationDriftTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "zotero_root": str(self.root),
                    "zotero_data_directory": str(self.root),
                    "linked_attachments": str(self.root),
                    "output_root": str(self.root / "converted_text"),
                }
            ),
            encoding="utf-8",
        )
        # Always a temporary config.toml passed explicitly, never the developer's real Codex config.
        self.codex_config = self.root / "codex" / "config.toml"
        self.codex_config.parent.mkdir()

    def _run(self, *extra_args: str) -> str:
        stdout = io.StringIO()
        with patch("sys.executable", str(self.root / "Scripts" / "python.exe")), redirect_stdout(stdout):
            exit_code = main(
                ["install-mcp", "--config", str(self.config_path), "--codex-config", str(self.codex_config), *extra_args]
            )
        self.assertEqual(exit_code, 0)
        return stdout.getvalue()

    def _generated_block(self, output: str) -> str:
        lines = output.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("[mcp_servers."))
        end = next(i for i in range(start, len(lines)) if not lines[i].strip())
        return "\n".join(lines[start:end]) + "\n"

    def test_missing_config_file_reports_missing_registration(self):
        output = self._run()

        self.assertIn("no [mcp_servers.zotero_fulltext]", output)
        self.assertIn("restart Codex", output)
        self.assertFalse(self.codex_config.exists())

    def test_matching_registration_is_current_and_ignores_approval_overrides(self):
        block = self._generated_block(self._run())
        self.codex_config.write_text(
            '[mcp_servers.other]\ncommand = "other"\n\n'
            + block
            + '\n[mcp_servers.zotero_fulltext.tools.search_fulltext]\napproval_mode = "approve"\n',
            encoding="utf-8",
        )

        output = self._run()

        self.assertIn("[mcp_servers.zotero_fulltext] in", output)
        self.assertIn("is current", output)

    def test_drifted_registration_lists_differences_without_writing(self):
        self.codex_config.write_text(
            "[mcp_servers.zotero_fulltext]\n"
            'command = "old-venv/zotero-fulltext-mcp"\n'
            'args = ["--db", "old.sqlite"]\n'
            'enabled_tools = ["search_fulltext", "removed_tool"]\n'
            '\n[mcp_servers.zotero_fulltext.tools.search_fulltext]\napproval_mode = "approve"\n',
            encoding="utf-8",
        )
        before = self.codex_config.read_bytes()

        output = self._run()

        self.assertIn("differs from the generated block", output)
        self.assertIn("command: 'old-venv/zotero-fulltext-mcp' ->", output)
        self.assertIn("args: ['--db', 'old.sqlite'] ->", output)
        self.assertIn("enabled_tools missing: ", output)
        self.assertIn("get_fulltext_chunk", output)
        self.assertIn("enabled_tools no longer provided: removed_tool", output)
        self.assertIn("restart Codex", output)
        self.assertEqual(self.codex_config.read_bytes(), before)

    def test_newly_enabled_optional_tool_is_reported_as_missing(self):
        self.codex_config.write_text(self._generated_block(self._run()), encoding="utf-8")

        output = self._run("--enable-bibtex")

        self.assertIn("args: ", output)
        self.assertIn("enabled_tools missing: export_bibtex_entries_by_key", output)

    def test_disabled_registration_is_drift(self):
        block = self._generated_block(self._run()).replace("enabled = true", "enabled = false")
        self.codex_config.write_text(block, encoding="utf-8")

        output = self._run()

        self.assertIn("enabled = false", output.split("differs from the generated block", 1)[1])

    def test_hyphenated_server_name_entry_is_found(self):
        block = self._generated_block(self._run()).replace(
            "[mcp_servers.zotero_fulltext]", '[mcp_servers."zotero-fulltext"]'
        )
        self.codex_config.write_text(block, encoding="utf-8")

        output = self._run()

        self.assertIn("[mcp_servers.zotero-fulltext] in", output)
        self.assertIn("is current", output)

    def test_unreadable_config_is_reported_not_raised(self):
        self.codex_config.write_text("[mcp_servers\n", encoding="utf-8")

        output = self._run()

        self.assertIn("could not read", output)
        self.assertIn("nothing compared", output)

    def test_non_utf8_config_is_reported_not_raised(self):
        self.codex_config.write_bytes("# Kommentar mit ä\n".encode("cp1252"))

        output = self._run()

        self.assertIn("could not read", output)

    def test_malformed_enabled_tools_is_reported_as_drift(self):
        block = self._generated_block(self._run())
        for bad_value in ("5", '[["a"]]', '"search_fulltext"'):
            with self.subTest(bad_value=bad_value):
                lines = [
                    f"enabled_tools = {bad_value}" if line.startswith("enabled_tools") else line
                    for line in block.splitlines()
                ]
                self.codex_config.write_text("\n".join(lines) + "\n", encoding="utf-8")

                output = self._run()

                self.assertIn("enabled_tools: unexpected value", output)

    def test_disabled_tools_hiding_a_generated_tool_is_drift(self):
        block = self._generated_block(self._run())
        self.codex_config.write_text(
            block + 'disabled_tools = ["search_fulltext", "unrelated"]\n', encoding="utf-8"
        )

        output = self._run()

        self.assertIn("disabled_tools hides: search_fulltext", output)

    def test_second_stale_entry_under_hyphenated_name_is_reported(self):
        block = self._generated_block(self._run())
        stale = '[mcp_servers."zotero-fulltext"]\ncommand = "old-venv/zotero-fulltext-mcp"\nargs = []\n'
        self.codex_config.write_text(block + "\n" + stale, encoding="utf-8")

        output = self._run()

        self.assertIn("registers this server twice", output)
        self.assertIn("[mcp_servers.zotero_fulltext] in", output)
        self.assertIn("is current", output)
        self.assertIn("[mcp_servers.zotero-fulltext] in", output)
        self.assertIn("differs from the generated block", output)

    def test_equivalent_command_spelling_is_not_drift(self):
        block = self._generated_block(self._run())
        server_exe = tomllib.loads(block)["mcp_servers"]["zotero_fulltext"]["command"]
        respelled = os.path.join(os.path.dirname(server_exe), ".", os.path.basename(server_exe))
        if os.name == "nt":
            respelled = respelled.upper()
        respelled_block = block.replace(json.dumps(server_exe), json.dumps(respelled))
        self.assertNotEqual(respelled_block, block)
        self.codex_config.write_text(respelled_block, encoding="utf-8")

        output = self._run()

        self.assertIn("is current", output)

    def test_default_path_follows_codex_home(self):
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        stdout = io.StringIO()
        with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}), patch(
            "sys.executable", str(self.root / "Scripts" / "python.exe")
        ), redirect_stdout(stdout):
            main(["install-mcp", "--config", str(self.config_path)])

        self.assertIn(str(codex_home / "config.toml"), stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
