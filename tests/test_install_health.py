import json
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
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


def _fake_sys(platform: str, executable: str = sys.executable):
    """Swap install_health's `sys` for a stand-in, so the real sys.platform never changes."""
    return patch.object(install_health, "sys", SimpleNamespace(platform=platform, executable=executable))


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
        self.assertTrue(status.source_is_other_project)
        self.assertFalse(status.stale)

    def test_unreadable_or_non_utf8_pyproject_is_unknown_not_foreign(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            missing = install_version_status(_editable_dist("0.2.0", source))
            (source / "pyproject.toml").write_bytes('[project]\nname = "zotero-fulltext-mcp"\n# ä\n'.encode("cp1252"))
            non_utf8 = install_version_status(_editable_dist("0.2.0", source))

        for status in (missing, non_utf8):
            self.assertIsNone(status.source_version)
            self.assertFalse(status.source_is_other_project)
            self.assertFalse(status.stale)

    def test_reinstall_command_follows_the_installer(self):
        source = Path("checkout")
        pip_status = InstallVersionStatus("0.2.0", True, source, "0.8.0", installer="pip")
        uv_status = InstallVersionStatus("0.2.0", True, source, "0.8.0", installer="uv")

        with _fake_sys("linux", "/opt/venv/bin/python"):
            pip_command = reinstall_command(pip_status, ["mcp", "marker"])
            uv_command = reinstall_command(uv_status, ["mcp"])

        self.assertEqual(pip_command, "/opt/venv/bin/python -m pip install -e 'checkout[mcp,marker]'")
        self.assertEqual(uv_command, "uv pip install --python /opt/venv/bin/python -e 'checkout[mcp]'")

    def test_windows_command_is_powershell_syntax(self):
        status = InstallVersionStatus("0.2.0", True, Path("checkout"), "0.8.0", installer="pip")
        with _fake_sys("win32", r"C:\Program Files\py\python.exe"):
            spaced = reinstall_command(status, [])
        with _fake_sys("win32", r"C:\venv\Scripts\python.exe"):
            plain = reinstall_command(status, ["mcp"])

        self.assertEqual(spaced, r"& 'C:\Program Files\py\python.exe' -m pip install -e checkout")
        self.assertEqual(plain, r"C:\venv\Scripts\python.exe -m pip install -e 'checkout[mcp]'")

    def test_shell_metacharacters_stay_literal(self):
        status = InstallVersionStatus("0.2.0", True, Path("it's $HOME"), "0.8.0", installer="pip")
        with _fake_sys("win32", r"C:\venv\Scripts\python.exe"):
            powershell = reinstall_command(status, [])
        with _fake_sys("linux", "/opt/venv/bin/python"):
            posix = reinstall_command(status, [])

        self.assertTrue(powershell.endswith("-e 'it''s $HOME'"))
        self.assertTrue(posix.endswith("""-e 'it'"'"'s $HOME'"""))

    def test_project_names_compare_like_packaging(self):
        for spelling in ("zotero_fulltext_mcp", "Zotero.Fulltext-MCP"):
            with self.subTest(spelling=spelling), tempfile.TemporaryDirectory() as tmp:
                _write_pyproject(Path(tmp), "0.8.0", name=spelling)

                status = install_version_status(_editable_dist("0.2.0", Path(tmp)))

                self.assertFalse(status.source_is_other_project)
                self.assertTrue(status.stale)

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
        with _fake_sys("win32"), patch.object(
            install_health.subprocess, "run", return_value=self._tasklist(rows)
        ):
            self.assertEqual(running_server_count(), 3)

    def test_no_match_info_line_counts_as_zero(self):
        with _fake_sys("win32"), patch.object(
            install_health.subprocess,
            "run",
            # German Windows, OEM code page: not decodable as UTF-8 or cp1252.
            return_value=self._tasklist("INFORMATION: Es werden keine Aufgaben ausgeführt.\r\n".encode("cp850")),
        ):
            self.assertEqual(running_server_count(), 0)

    def test_missing_output_counts_as_zero(self):
        with _fake_sys("win32"), patch.object(
            install_health.subprocess, "run", return_value=self._tasklist(None)
        ):
            self.assertEqual(running_server_count(), 0)

    def test_unknown_when_tasklist_fails(self):
        with _fake_sys("win32"), patch.object(
            install_health.subprocess, "run", side_effect=OSError("no tasklist")
        ):
            self.assertIsNone(running_server_count())

    def test_not_checked_off_windows(self):
        with _fake_sys("linux"), patch.object(
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

    def test_checkout_holding_another_project_passes(self):
        status = InstallVersionStatus("0.2.0", True, Path("checkout"), None, source_project="something-else")

        result = self._checks(status, running=None)["install_version"]

        self.assertTrue(result.ok)
        self.assertIn("another project", result.detail)

    def test_unreadable_checkout_warns(self):
        status = InstallVersionStatus("0.2.0", True, Path("checkout"), None)

        result = self._checks(status, running=None)["install_version"]

        self.assertFalse(result.ok)
        self.assertFalse(result.required)
        self.assertIn("could not read", result.detail)

    def test_stale_reinstall_keeps_the_test_extra(self):
        status = InstallVersionStatus("0.2.0", True, Path("checkout"), "0.8.0", installer="pip")
        installed = {"mcp", "pytest"}
        with patch("zotero_pdf_text.cli.importlib.util.find_spec", side_effect=lambda name: name if name in installed else None):
            detail = self._checks(status, running=None)["install_version"].detail

        self.assertIn("'checkout[mcp,test]'", detail)

    def test_pinned_install_passes_and_no_server_row_without_processes(self):
        results = self._checks(InstallVersionStatus("0.8.0", False), running=0)

        self.assertTrue(results["install_version"].ok)
        self.assertIn("not an editable install", results["install_version"].detail)
        self.assertNotIn("running_server", results)


if __name__ == "__main__":
    unittest.main()
