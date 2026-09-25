import json
import subprocess
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text import install_health
from zotero_pdf_text.cli import _install_health_checks
from zotero_pdf_text.install_health import (
    InstallVersionStatus,
    install_version_status,
    reinstall_command,
    running_server_count,
)


class FakeDistribution(metadata.Distribution):
    def __init__(self, version: str, files: dict[str, str]):
        self._files = {"METADATA": f"Metadata-Version: 2.1\nName: zotero-fulltext-mcp\nVersion: {version}\n", **files}

    def read_text(self, filename):
        return self._files.get(filename)

    def locate_file(self, path):
        return Path(path)


def _editable_dist(version: str, source_dir: Path, installer: str = "pip") -> FakeDistribution:
    direct_url = {"url": source_dir.resolve().as_uri(), "dir_info": {"editable": True}}
    return FakeDistribution(version, {"direct_url.json": json.dumps(direct_url), "INSTALLER": f"{installer}\n"})


def _write_pyproject(directory: Path, version: str, name: str = "zotero-fulltext-mcp") -> None:
    (directory / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8")


class InstallVersionStatusTests(unittest.TestCase):
    def test_current_editable_install_is_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pyproject(Path(tmp), "0.8.0")

            status = install_version_status(_editable_dist("0.8.0", Path(tmp)))

        self.assertTrue(status.editable)
        self.assertEqual(status.source_version, "0.8.0")
        self.assertFalse(status.stale)

    def test_stale_editable_install_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pyproject(Path(tmp), "0.8.0")

            status = install_version_status(_editable_dist("0.2.0", Path(tmp)))

        self.assertTrue(status.stale)
        self.assertEqual(status.source_dir, Path(tmp).resolve())

    def test_pinned_install_ignores_a_checkout_in_the_working_directory(self):
        pinned = FakeDistribution(
            "0.7.0",
            {"direct_url.json": json.dumps({"url": "https://github.com/x/y.git", "vcs_info": {"vcs": "git"}})},
        )
        with tempfile.TemporaryDirectory() as tmp:
            _write_pyproject(Path(tmp), "0.8.0")
            with patch("pathlib.Path.cwd", return_value=Path(tmp)):
                status = install_version_status(pinned)

        self.assertFalse(status.editable)
        self.assertFalse(status.stale)
        self.assertIsNone(status.source_version)

    def test_install_without_direct_url_is_not_editable(self):
        status = install_version_status(FakeDistribution("0.8.0", {}))

        self.assertFalse(status.editable)
        self.assertFalse(status.stale)

    def test_checkout_now_holding_another_project_is_not_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pyproject(Path(tmp), "9.9.9", name="something-else")

            status = install_version_status(_editable_dist("0.2.0", Path(tmp)))

        self.assertTrue(status.editable)
        self.assertIsNone(status.source_version)
        self.assertFalse(status.stale)

    def test_reinstall_command_follows_the_installer(self):
        source = Path("checkout")
        pip_status = InstallVersionStatus("0.2.0", True, source, "0.8.0", installer="pip")
        uv_status = InstallVersionStatus("0.2.0", True, source, "0.8.0", installer="uv")

        self.assertIn('-m pip install -e "checkout[mcp,marker]"', reinstall_command(pip_status, ["mcp", "marker"]))
        uv_command = reinstall_command(uv_status, ["mcp"])
        self.assertTrue(uv_command.startswith("uv pip install --python "))
        self.assertTrue(uv_command.endswith('-e "checkout[mcp]"'))

    def test_quoted_interpreter_path_is_runnable_in_powershell(self):
        status = InstallVersionStatus("0.2.0", True, Path("checkout"), "0.8.0", installer="pip")
        with patch.object(install_health.sys, "executable", r"C:\Program Files\py\python.exe"), patch.object(
            install_health.sys, "platform", "win32"
        ):
            self.assertTrue(reinstall_command(status, []).startswith(r'& "C:\Program Files\py\python.exe" -m pip'))
        with patch.object(install_health.sys, "executable", "/opt/venv/bin/python"):
            self.assertTrue(reinstall_command(status, []).startswith("/opt/venv/bin/python -m pip"))

    def test_unc_checkout_keeps_its_host(self):
        dist = FakeDistribution(
            "0.2.0", {"direct_url.json": json.dumps({"url": "file://server/share/repo", "dir_info": {"editable": True}})}
        )

        status = install_version_status(dist)

        self.assertEqual(status.source_dir.as_posix().lstrip("/").split("/")[:3], ["server", "share", "repo"])


class RunningServerCountTests(unittest.TestCase):
    def _tasklist(self, stdout: bytes | None):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=b"")

    def test_counts_matching_processes_on_windows(self):
        rows = b'"zotero-fulltext-mcp.exe","101","Console","1","50.000 K"\r\n' * 3
        with patch.object(install_health.sys, "platform", "win32"), patch.object(
            install_health.subprocess, "run", return_value=self._tasklist(rows)
        ):
            self.assertEqual(running_server_count(), 3)

    def test_no_match_info_line_counts_as_zero(self):
        with patch.object(install_health.sys, "platform", "win32"), patch.object(
            install_health.subprocess,
            "run",
            # German Windows, OEM code page: not decodable as UTF-8 or cp1252.
            return_value=self._tasklist("INFORMATION: Es werden keine Aufgaben ausgeführt.\r\n".encode("cp850")),
        ):
            self.assertEqual(running_server_count(), 0)

    def test_missing_output_counts_as_zero(self):
        with patch.object(install_health.sys, "platform", "win32"), patch.object(
            install_health.subprocess, "run", return_value=self._tasklist(None)
        ):
            self.assertEqual(running_server_count(), 0)

    def test_unknown_when_tasklist_fails(self):
        with patch.object(install_health.sys, "platform", "win32"), patch.object(
            install_health.subprocess, "run", side_effect=OSError("no tasklist")
        ):
            self.assertIsNone(running_server_count())

    def test_not_checked_off_windows(self):
        with patch.object(install_health.sys, "platform", "linux"), patch.object(
            install_health.subprocess, "run"
        ) as run:
            self.assertIsNone(running_server_count())
        run.assert_not_called()


class InstallHealthCheckResultTests(unittest.TestCase):
    def _checks(self, status: InstallVersionStatus, running: int | None):
        with patch("zotero_pdf_text.cli.install_version_status", return_value=status), patch(
            "zotero_pdf_text.cli.running_server_count", return_value=running
        ):
            return {result.name: result for result in _install_health_checks()}

    def test_stale_install_warns_with_reinstall_command_and_flags_running_server(self):
        status = InstallVersionStatus("0.2.0", True, Path("checkout"), "0.8.0", installer="pip")

        results = self._checks(status, running=2)

        version = results["install_version"]
        self.assertFalse(version.ok)
        self.assertFalse(version.required)
        self.assertIn("0.2.0", version.detail)
        self.assertIn("0.8.0", version.detail)
        self.assertIn("pip install -e", version.detail)
        self.assertFalse(results["running_server"].ok)
        self.assertFalse(results["running_server"].required)

    def test_running_server_is_informational_when_install_is_current(self):
        status = InstallVersionStatus("0.8.0", True, Path("checkout"), "0.8.0")

        results = self._checks(status, running=1)

        self.assertTrue(results["install_version"].ok)
        self.assertTrue(results["running_server"].ok)

    def test_pinned_install_passes_and_no_server_row_without_processes(self):
        results = self._checks(InstallVersionStatus("0.8.0", False), running=0)

        self.assertTrue(results["install_version"].ok)
        self.assertIn("not an editable install", results["install_version"].detail)
        self.assertNotIn("running_server", results)


if __name__ == "__main__":
    unittest.main()
