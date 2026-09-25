import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.cli import main

EXE_NAME = "zotero-fulltext-mcp.exe" if os.name == "nt" else "zotero-fulltext-mcp"


class InstallMcpPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # resolve(): on macOS the temp dir is under /var, a symlink that os.getcwd() reports resolved.
        self.root = Path(tmp.name).resolve()
        (self.root / "config.json").write_text(
            json.dumps(
                {
                    "zotero_root": str(self.root),
                    "zotero_data_directory": str(self.root),
                    "linked_attachments": str(self.root),
                    "output_root": "converted_text",
                }
            ),
            encoding="utf-8",
        )
        self.codex_config = self.root / "codex.toml"
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def _run(self, *args: str, executable: str | None = None) -> str:
        stdout = io.StringIO()
        with patch("sys.executable", executable or str(self.root / "Scripts" / "python.exe")), redirect_stdout(
            stdout
        ), redirect_stderr(io.StringIO()):
            exit_code = main(["install-mcp", "--codex-config", str(self.codex_config), *args])
        self.assertEqual(exit_code, 0)
        return stdout.getvalue()

    def _codex_servers(self, output: str) -> dict:
        lines = output.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("[mcp_servers."))
        end = next(i for i in range(start, len(lines)) if not lines[i].strip())
        return tomllib.loads("\n".join(lines[start:end]))["mcp_servers"]

    def test_relative_config_db_and_output_root_are_registered_absolute(self):
        output = self._run("--config", "config.json", "--db", os.path.join("idx", "..", "custom.sqlite"))

        args = self._codex_servers(output)["zotero_fulltext"]["args"]
        self.assertEqual(args[args.index("--config") + 1], str(self.root / "config.json"))
        self.assertEqual(args[args.index("--db") + 1], str(self.root / "custom.sqlite"))

    def test_relative_output_root_gives_absolute_default_db(self):
        output = self._run("--config", "config.json")

        args = self._codex_servers(output)["zotero_fulltext"]["args"]
        expected = self.root / "converted_text" / "index" / "zotero_text_index.sqlite"
        self.assertEqual(args[args.index("--db") + 1], str(expected))

    def test_dotted_server_name_is_a_quoted_key_and_found_by_the_drift_check(self):
        output = self._run("--config", "config.json", "--server-name", "zotero.fulltext")
        servers = self._codex_servers(output)
        self.assertIn("zotero.fulltext", servers)
        self.assertNotIn("zotero", servers)

        block_start = output.index("[mcp_servers.")
        block = output[block_start : output.index("\n\n", block_start)] + "\n"
        self.codex_config.write_text(block, encoding="utf-8")
        rerun = self._run("--config", "config.json", "--server-name", "zotero.fulltext")

        self.assertIn("is current", rerun)

    @unittest.skipIf(os.name == "nt", "POSIX venvs symlink bin/python; Windows venvs copy python.exe")
    def test_symlinked_venv_interpreter_keeps_the_venv_scripts_dir(self):
        base_bin = self.root / "base" / "bin"
        venv_bin = self.root / "venv" / "bin"
        base_bin.mkdir(parents=True)
        venv_bin.mkdir(parents=True)
        (base_bin / "python3").write_text("")
        (venv_bin / "python").symlink_to(base_bin / "python3")

        output = self._run("--config", "config.json", executable=str(venv_bin / "python"))

        self.assertEqual(self._codex_servers(output)["zotero_fulltext"]["command"], str(venv_bin / EXE_NAME))


if __name__ == "__main__":
    unittest.main()
