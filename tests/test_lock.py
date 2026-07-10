import json
import tempfile
import time
import unittest
from pathlib import Path

from zotero_pdf_text.lock import LOCK_FILENAME, PipelineLockedError, pipeline_write_lock


class PipelineLockTests(unittest.TestCase):
    def test_lock_file_created_and_removed_on_clean_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / LOCK_FILENAME
            with pipeline_write_lock(root, command="convert-new"):
                self.assertTrue(lock_path.exists())
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["command"], "convert-new")
            self.assertFalse(lock_path.exists())

    def test_lock_file_removed_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / LOCK_FILENAME
            with self.assertRaises(ValueError):
                with pipeline_write_lock(root):
                    raise ValueError("boom")
            self.assertFalse(lock_path.exists())

    def test_second_acquisition_refused_while_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with pipeline_write_lock(root, command="convert-new"):
                with self.assertRaises(PipelineLockedError):
                    with pipeline_write_lock(root, command="build-fts"):
                        pass

    def test_stale_lock_is_treated_as_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / LOCK_FILENAME
            stale_started_at = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 7 * 60 * 60)
            )
            lock_path.write_text(
                json.dumps(
                    {
                        "hostname": "other-machine",
                        "pid": 1,
                        "started_at": stale_started_at,
                        "command": "convert-new",
                    }
                ),
                encoding="utf-8",
            )
            with pipeline_write_lock(root, command="build-fts"):
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["command"], "build-fts")

    def test_corrupt_lock_file_is_treated_as_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / LOCK_FILENAME
            lock_path.write_text("not json", encoding="utf-8")
            with pipeline_write_lock(root, command="build-fts"):
                self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
