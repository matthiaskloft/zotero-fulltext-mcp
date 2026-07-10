import subprocess
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.runtime import ensure_zotero_running, is_zotero_running


class RuntimeTests(unittest.TestCase):
    def test_is_zotero_running_uses_process_output(self):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="zotero.exe                  123 Console", stderr="")

        self.assertTrue(is_zotero_running(run_process=fake_run))

    def test_ensure_zotero_launches_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            zotero_exe = Path(tmp) / "zotero.exe"
            zotero_exe.write_text("", encoding="utf-8")
            state = {"running": False, "launched": False}

            def fake_run(args, **kwargs):
                stdout = "zotero.exe 123" if state["running"] else "INFO: No tasks are running"
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

            def fake_popen(args, **kwargs):
                state["launched"] = True
                state["running"] = True
                return object()

            status = ensure_zotero_running(
                zotero_exe=zotero_exe,
                wait_seconds=0,
                run_process=fake_run,
                popen=fake_popen,
                connector_probe=lambda: (True, "ok"),
            )

            self.assertTrue(status.running)
            self.assertTrue(status.launched)
            self.assertTrue(state["launched"])
            self.assertTrue(status.connector_ok)

    def test_ensure_zotero_reports_missing_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "zotero.exe"

            def fake_run(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, stdout="INFO: No tasks are running", stderr="")

            status = ensure_zotero_running(
                zotero_exe=missing,
                wait_seconds=0,
                run_process=fake_run,
                connector_probe=lambda: (False, "not checked"),
            )

            self.assertFalse(status.running)
            self.assertIn("not found", status.troubleshooting[0])


if __name__ == "__main__":
    unittest.main()
