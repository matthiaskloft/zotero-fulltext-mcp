import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
                    with pipeline_write_lock(root, command="rebuild-index"):
                        pass

    def test_stale_lock_fails_loudly_naming_holder(self):
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
            with self.assertRaises(PipelineLockedError) as ctx:
                with pipeline_write_lock(root, command="rebuild-index"):
                    pass
            message = str(ctx.exception)
            self.assertIn("other-machine", message)
            self.assertIn("stale", message)
            self.assertIn("delete the lock file manually", message)
            # The stale lock is never silently overwritten.
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["hostname"], "other-machine")

    def test_corrupt_lock_file_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / LOCK_FILENAME
            lock_path.write_text("not json", encoding="utf-8")
            with self.assertRaises(PipelineLockedError) as ctx:
                with pipeline_write_lock(root, command="rebuild-index"):
                    pass
            self.assertIn("unreadable or corrupt", str(ctx.exception))
            self.assertEqual(lock_path.read_text(encoding="utf-8"), "not json")


class LockOwnershipRaceTests(unittest.TestCase):
    """Concurrency tests for the ownership guarantee sequential tests cannot reach.

    The lock's whole purpose is that exactly one writer proceeds when several start at once. A
    sequential acquire-then-acquire proves the refusal message, not the atomicity underneath it:
    the check-then-write race this closes is only observable when the two attempts genuinely
    overlap.
    """

    def test_only_one_of_many_simultaneous_writers_acquires(self):
        """Exactly one writer proceeds when many attempt at once.

        The winner is held inside its critical section until every other contender has reported
        an outcome, so the assertion never depends on scheduler speed. A barrier alone is not
        enough: it makes every worker runnable, but does not make every worker *attempt* while
        the winner still holds the lock. A contender delayed past the winner's release would
        acquire legitimately and be counted as a second winner -- a locking failure reported for
        a run in which no two critical sections ever overlapped. Holding until all twelve
        outcomes are in removes the timing assumption rather than making it likelier to hold.

        Every wait is bounded and the release is set in a finally, so a genuine bug fails the
        test instead of hanging it.
        """
        writers = 12
        deadline_seconds = 30

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = threading.Barrier(writers, timeout=deadline_seconds)
            reported = threading.Condition()
            outcomes: list[tuple[str, str]] = []
            release_winner = threading.Event()

            def record(kind: str, name: str) -> None:
                with reported:
                    outcomes.append((kind, name))
                    reported.notify_all()

            def contend(index: int) -> None:
                name = f"writer-{index}"
                try:
                    ready.wait()
                    try:
                        with pipeline_write_lock(root, command=name):
                            record("acquired", name)
                            # Stay inside the critical section until the main thread has seen
                            # every contender's outcome.
                            release_winner.wait(timeout=deadline_seconds)
                    except PipelineLockedError:
                        record("refused", name)
                except BaseException as exc:  # noqa: BLE001 - surfaced as a test failure below
                    record("error", f"{name}: {exc!r}")

            threads = [threading.Thread(target=contend, args=(index,)) for index in range(writers)]
            try:
                for thread in threads:
                    thread.start()
                with reported:
                    all_reported = reported.wait_for(
                        lambda: len(outcomes) == writers, timeout=deadline_seconds
                    )
            finally:
                release_winner.set()
                for thread in threads:
                    thread.join(timeout=deadline_seconds)

            self.assertTrue(
                all_reported, f"only {len(outcomes)} of {writers} contenders reported: {outcomes}"
            )
            self.assertEqual([o for o in outcomes if o[0] == "error"], [])
            acquired = [name for kind, name in outcomes if kind == "acquired"]
            refused = [name for kind, name in outcomes if kind == "refused"]
            self.assertEqual(len(acquired), 1, f"expected exactly one winner, got {acquired}")
            self.assertEqual(len(refused), writers - 1)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            # The winner released cleanly, so the tree is usable by the next writer.
            self.assertFalse((root / LOCK_FILENAME).exists())
            with pipeline_write_lock(root, command="after"):
                pass

    def test_serial_writers_each_acquire_in_turn(self):
        """The complement: refusing concurrent writers must not leave the lock permanently held."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(5):
                with pipeline_write_lock(root, command=f"run-{index}"):
                    self.assertTrue((root / LOCK_FILENAME).exists())
                self.assertFalse((root / LOCK_FILENAME).exists())

    def test_holder_releasing_between_create_and_read_is_retried(self):
        """A lock released in the window between our failed create and our read is not an error.

        Reporting "locked" there would be a lie -- nobody holds it by the time we look. This is
        the retry branch in _acquire, unreachable without a race, so it is driven directly.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / LOCK_FILENAME
            real_open = __import__("os").open
            calls = {"count": 0}

            def racing_open(path, flags, *args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    # Simulate a competing holder that vanishes before we can read its file.
                    raise FileExistsError(lock_path)
                return real_open(path, flags, *args, **kwargs)

            with patch("zotero_pdf_text.lock.os.open", side_effect=racing_open):
                with pipeline_write_lock(root, command="retrier"):
                    self.assertTrue(lock_path.exists())

            self.assertEqual(calls["count"], 2)
            self.assertFalse(lock_path.exists())

    def test_a_persistent_racer_eventually_gives_up_rather_than_spinning(self):
        """If the create keeps failing against a vanishing holder, the attempt is bounded."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "zotero_pdf_text.lock.os.open", side_effect=FileExistsError(root / LOCK_FILENAME)
            ):
                with self.assertRaises(PipelineLockedError) as caught:
                    with pipeline_write_lock(root, command="spinner"):
                        pass
            self.assertIn("kept creating and releasing", str(caught.exception))

    def test_a_live_holders_lock_is_never_stolen_or_overwritten(self):
        """A fresh lock from another machine survives a refused attempt byte-for-byte."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / LOCK_FILENAME
            foreign = json.dumps(
                {
                    "hostname": "other-machine",
                    "pid": 4242,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "command": "convert-new",
                },
                indent=2,
            )
            lock_path.write_text(foreign, encoding="utf-8")

            with self.assertRaises(PipelineLockedError) as caught:
                with pipeline_write_lock(root, command="intruder"):
                    pass

            self.assertEqual(lock_path.read_text(encoding="utf-8"), foreign)
            # The refusal names the holder, which is what makes it actionable.
            self.assertIn("other-machine", str(caught.exception))
            self.assertIn("4242", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
