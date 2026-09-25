import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.artifacts import resolve_reader_db_path, stage_and_publish, write_jsonl_from_existing
from zotero_pdf_text.cli import (
    _check_output_root_writable,
    _shell_quote,
    main,
    run_setup_checks,
)
from zotero_pdf_text.install_health import InstallVersionStatus


def setUpModule():
    # Setup checks also inspect the installed package and running processes; keep these tests
    # independent of the machine they run on (a localized tasklist, a stale dev venv).
    for target, value in (
        ("zotero_pdf_text.cli.install_version_status", InstallVersionStatus("0.0.0", editable=False)),
        ("zotero_pdf_text.cli.running_server_count", None),
    ):
        patcher = patch(target, return_value=value)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def tearDownModule():
    # pytest calls tearDownModule but not unittest's module cleanups; without this the patches
    # above would stay active for every test module collected after this one.
    unittest.case.doModuleCleanups()


class RunSetupChecksTests(unittest.TestCase):
    def test_missing_config_fails_and_stops_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "does_not_exist.json"

            results = run_setup_checks(config_path)

            names = [result.name for result in results]
            self.assertIn("python_version", names)
            self.assertIn("config", names)
            config_result = next(result for result in results if result.name == "config")
            self.assertFalse(config_result.ok)
            self.assertTrue(config_result.required)
            # No path checks are attempted once config loading itself fails.
            self.assertNotIn("zotero_data_directory", names)

    def test_structurally_malformed_config_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            # Syntactically valid JSON, wrong shape (array instead of object) -- load_config
            # indexes into it as a dict and raises TypeError, not a clean load_config error.
            config_path.write_text("[]", encoding="utf-8")

            results = run_setup_checks(config_path)

            config_result = next(result for result in results if result.name == "config")
            self.assertFalse(config_result.ok)
            self.assertTrue(config_result.required)

    def test_all_pass_for_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "project_root"
            project_root.mkdir()
            zotero_data = root / "zotero_data"
            zotero_data.mkdir()
            (zotero_data / "zotero.sqlite").write_text("")
            attachments = root / "attachments"
            attachments.mkdir()
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(project_root),
                        "zotero_data_directory": str(zotero_data),
                        "linked_attachments": str(attachments),
                        "output_root": str(root / "output"),
                    }
                ),
                encoding="utf-8",
            )

            results = run_setup_checks(config_path)

            required_results = [result for result in results if result.required]
            self.assertTrue(all(result.ok for result in required_results))

    def test_low_python_version_fails_required_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "does_not_exist.json"

            with patch("zotero_pdf_text.cli.sys.version_info", (3, 10, 0)):
                results = run_setup_checks(config_path)

            version_result = next(result for result in results if result.name == "python_version")
            self.assertFalse(version_result.ok)
            self.assertTrue(version_result.required)

    def test_missing_paths_are_reported_individually(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(root / "project_root"),
                        "zotero_data_directory": str(root / "missing_zotero_data"),
                        "linked_attachments": str(root / "missing_attachments"),
                        "output_root": str(root / "output"),
                    }
                ),
                encoding="utf-8",
            )

            results = run_setup_checks(config_path)

            failed_names = {result.name for result in results if not result.ok}
            self.assertIn("zotero_data_directory", failed_names)
            self.assertIn("linked_attachments", failed_names)
            self.assertIn("zotero.sqlite", failed_names)

    def test_require_mcp_makes_missing_mcp_extra_a_required_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "project_root"
            project_root.mkdir()
            zotero_data = root / "zotero_data"
            zotero_data.mkdir()
            (zotero_data / "zotero.sqlite").write_text("")
            attachments = root / "attachments"
            attachments.mkdir()
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(project_root),
                        "zotero_data_directory": str(zotero_data),
                        "linked_attachments": str(attachments),
                        "output_root": str(root / "output"),
                    }
                ),
                encoding="utf-8",
            )

            with patch("zotero_pdf_text.cli.importlib.util.find_spec", return_value=None):
                without_require = run_setup_checks(config_path, require_mcp=False)
                with_require = run_setup_checks(config_path, require_mcp=True)

            mcp_without = next(r for r in without_require if r.name == "extra:mcp")
            mcp_with = next(r for r in with_require if r.name == "extra:mcp")
            self.assertFalse(mcp_without.required)
            self.assertTrue(mcp_with.required)
            self.assertFalse(mcp_with.ok)


class CheckOutputRootWritableTests(unittest.TestCase):
    def test_existing_writable_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = _check_output_root_writable(Path(tmp))
            self.assertTrue(ok)
            self.assertIn(tmp, detail)

    def test_nonexistent_but_creatable_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "output"
            ok, detail = _check_output_root_writable(target)
            self.assertTrue(ok)
            self.assertIn("does not exist yet", detail)

    def test_existing_path_that_is_a_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "not_a_dir"
            target.write_text("x")
            ok, detail = _check_output_root_writable(target)
            self.assertFalse(ok)
            self.assertIn("not a directory", detail)

    def test_probe_leaves_no_stray_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            _check_output_root_writable(Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_creation_failure_is_reported_as_not_writable(self):
        # os.access can report a directory as writable while an actual write still fails --
        # Windows ACLs, controlled-folder protection, quotas, network filesystems. Simulate that
        # by making the real write probe itself fail.
        with tempfile.TemporaryDirectory() as tmp:
            with patch("zotero_pdf_text.cli.tempfile.mkstemp", side_effect=OSError("access denied")):
                ok, detail = _check_output_root_writable(Path(tmp))
            self.assertFalse(ok)
            self.assertIn("Cannot create files", detail)

    def test_cleanup_failure_fails_the_check_and_reports_the_leftover_path(self):
        # Unlike the atomic-write helpers (where a stray temp file next to a successfully
        # published index is harmless), this check's entire point is to prove nothing gets left
        # behind -- a cleanup failure must fail the check, not be silently reported as success.
        with tempfile.TemporaryDirectory() as tmp:
            with patch("pathlib.Path.unlink", side_effect=OSError("cleanup failed")):
                ok, detail = _check_output_root_writable(Path(tmp))
            self.assertFalse(ok)
            self.assertIn("failed to remove", detail)


class CheckSetupCliTests(unittest.TestCase):
    def test_dispatch_returns_zero_for_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "project_root"
            project_root.mkdir()
            zotero_data = root / "zotero_data"
            zotero_data.mkdir()
            (zotero_data / "zotero.sqlite").write_text("")
            attachments = root / "attachments"
            attachments.mkdir()
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(project_root),
                        "zotero_data_directory": str(zotero_data),
                        "linked_attachments": str(attachments),
                        "output_root": str(root / "output"),
                    }
                ),
                encoding="utf-8",
            )

            with patch("zotero_pdf_text.cli.importlib.util.find_spec", return_value=None):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    exit_code = main(["check-setup", "--config", str(config_path)])

            # Missing optional extras are WARN, not FAIL, and must not affect the exit code.
            self.assertEqual(exit_code, 0)
            self.assertIn("[OK] config:", buffer.getvalue())
            self.assertIn("[WARN] extra:marker:", buffer.getvalue())

    def test_require_mcp_flag_reaches_run_setup_checks_through_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "project_root"
            project_root.mkdir()
            zotero_data = root / "zotero_data"
            zotero_data.mkdir()
            (zotero_data / "zotero.sqlite").write_text("")
            attachments = root / "attachments"
            attachments.mkdir()
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zotero_root": str(project_root),
                        "zotero_data_directory": str(zotero_data),
                        "linked_attachments": str(attachments),
                        "output_root": str(root / "output"),
                    }
                ),
                encoding="utf-8",
            )

            with patch("zotero_pdf_text.cli.importlib.util.find_spec", return_value=None):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    exit_code = main(["check-setup", "--config", str(config_path), "--require-mcp"])

            self.assertEqual(exit_code, 1)
            self.assertIn("[FAIL] extra:mcp:", buffer.getvalue())

    def test_dispatch_returns_nonzero_for_missing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "does_not_exist.json"

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(["check-setup", "--config", str(config_path)])

            self.assertEqual(exit_code, 1)
            self.assertIn("[FAIL] config:", buffer.getvalue())

    def test_json_output_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "does_not_exist.json"

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                main(["check-setup", "--config", str(config_path), "--json"])

            payload = json.loads(buffer.getvalue())
            self.assertIsInstance(payload, list)
            self.assertTrue(any(entry["name"] == "config" for entry in payload))


def _write_setup_config(root: Path) -> Path:
    for name in ("project_root", "zotero_data", "attachments"):
        (root / name).mkdir()
    (root / "zotero_data" / "zotero.sqlite").write_text("")
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "zotero_root": str(root / "project_root"),
                "zotero_data_directory": str(root / "zotero_data"),
                "linked_attachments": str(root / "attachments"),
                "output_root": str(root / "output"),
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _publish_fixture_generation(output_root: Path) -> Path:
    index_root = output_root / "index"
    index_root.mkdir(parents=True)
    jsonl_path = output_root / "fixture.jsonl"
    jsonl_path.write_text(
        json.dumps(
            {
                "zotero_parent_key": "PARENT1",
                "zotero_attachment_key": "ATTACH1",
                "title": "Fixture paper",
                "creators": "Someone",
                "year": "2026",
                "source_path": str(output_root / "fixture.pdf"),
                "markdown_path": str(output_root / "fixture.md"),
                "markdown_sha256": "abc123",
                "extraction_tool": "pymupdf4llm.to_markdown",
                "classification": "mapped_verified",
                "identity_status": "verified",
                "text": "Searchable fixture text.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stage_and_publish(index_root, write_jsonl_from_existing(jsonl_path), command="test")
    return resolve_reader_db_path(index_root / "zotero_text_index.sqlite")


class PublishedIndexCheckTests(unittest.TestCase):
    def _index_result(self, config_path: Path, *, require_mcp: bool = True):
        results = run_setup_checks(config_path, require_mcp=require_mcp)
        return next((r for r in results if r.name == "published_index"), None)

    def test_only_checked_with_require_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_setup_config(Path(tmp))
            self.assertIsNone(self._index_result(config_path, require_mcp=False))

    def test_current_schema_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_setup_config(root)
            _publish_fixture_generation(root / "output")

            result = self._index_result(config_path)

            self.assertTrue(result.ok, result.detail)
            self.assertTrue(result.required)
            self.assertNotIn(str(root / "output"), result.detail)

    def test_old_schema_fails_with_rebuild_index_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_setup_config(root)
            db_path = _publish_fixture_generation(root / "output")
            # Reproduce a generation published before these columns existed.
            con = sqlite3.connect(db_path)
            try:
                con.execute("ALTER TABLE metadata DROP COLUMN source_sha256")
                con.execute("ALTER TABLE metadata DROP COLUMN indexed_at")
                con.commit()
            finally:
                con.close()
            before = db_path.read_bytes()

            result = self._index_result(config_path)

            self.assertFalse(result.ok)
            self.assertTrue(result.required)
            self.assertIn(f"zotero-pdf-text rebuild-index --config {_shell_quote(str(config_path))}", result.detail)
            self.assertNotIn(str(root / "output"), result.detail)
            self.assertEqual(db_path.read_bytes(), before)

    def test_missing_generation_fails_with_distinct_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_setup_config(root)

            result = self._index_result(config_path)

            self.assertFalse(result.ok)
            self.assertIn("no published index generation", result.detail)
            self.assertIn(f"zotero-pdf-text convert-new --config {_shell_quote(str(config_path))}", result.detail)
            self.assertNotIn("rebuild-index", result.detail)
            self.assertFalse((root / "output").exists())

    def test_recovery_command_quotes_config_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "My Library"
            root.mkdir()
            config_path = _write_setup_config(root)

            result = self._index_result(config_path)

            self.assertIn(f'convert-new --config "{config_path}"', result.detail)


if __name__ == "__main__":
    unittest.main()
