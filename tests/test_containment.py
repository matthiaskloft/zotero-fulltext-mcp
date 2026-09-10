"""Adversarial tests for path containment on derived artifacts.

`resolve_generation_dir` is the single choke point through which every reader and writer turns a
generation identifier -- read from `current.json`, which is an ordinary editable file on disk --
into a directory it will open. Its ID pattern already excludes separators and traversal, so the
resolved-parent check behind it is defence in depth: these tests exist to keep that second layer
honest, including for inputs the pattern would reject anyway, so that loosening the pattern later
cannot quietly remove containment.

Symlink cases skip where the platform refuses to create one. On Windows that is the default for an
unelevated account without Developer Mode, so these run for real on CI's Linux and macOS legs.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.artifacts import (
    CurrentPointerError,
    GENERATIONS_DIRNAME,
    new_generation_id,
    resolve_generation_dir,
    resolve_reader_db_path,
)


def _symlinks_available(tmp: Path) -> bool:
    target = tmp / "_probe_target"
    target.mkdir()
    try:
        os.symlink(target, tmp / "_probe_link", target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


class GenerationIdContainmentTests(unittest.TestCase):
    def test_traversal_and_separator_ids_are_refused(self):
        """Every spelling that could escape `generations/` is rejected before any file is opened."""
        hostile = [
            "../../evil",
            "..",
            ".",
            "a/b",
            "a\\b",
            "/absolute",
            "C:\\windows",
            "\\\\server\\share",
            "gen\x00truncated",
            "gen\nsecond-line",
            " leading-space",
            "",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            (index_root / GENERATIONS_DIRNAME).mkdir()
            for generation_id in hostile:
                with self.subTest(generation_id=generation_id):
                    with self.assertRaises(CurrentPointerError):
                        resolve_generation_dir(index_root, generation_id)

    def test_refusal_precedes_any_filesystem_access(self):
        """A hostile ID must be refused even when the escape target exists and is readable.

        Rejecting only because a path happens to be missing would be an accident, not containment.
        """
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp) / "index"
            (index_root / GENERATIONS_DIRNAME).mkdir(parents=True)
            escaped = Path(tmp) / "evil"
            escaped.mkdir()
            (escaped / "index.sqlite").write_bytes(b"")
            with self.assertRaises(CurrentPointerError):
                resolve_generation_dir(index_root, f"..{os.sep}..{os.sep}evil")

    def test_a_valid_id_resolves_inside_the_managed_directory(self):
        """The complement: containment must not be achieved by refusing everything."""
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            generation_id = new_generation_id()
            (index_root / GENERATIONS_DIRNAME / generation_id).mkdir(parents=True)
            resolved = resolve_generation_dir(index_root, generation_id)
            self.assertEqual(resolved.parent.name, GENERATIONS_DIRNAME)
            self.assertEqual(resolved.name, generation_id)

    def test_generation_entry_symlinked_outside_is_refused(self):
        """A well-named generation whose directory is a symlink out of the tree is still refused.

        This is the case the ID pattern cannot see: the name is valid, and only resolving it
        reveals that it leaves the index root.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if not _symlinks_available(root):
                self.skipTest("this platform/account cannot create symlinks")
            generation_id = new_generation_id()
            outside = root / "outside" / generation_id
            outside.mkdir(parents=True)
            index_root = root / "index"
            (index_root / GENERATIONS_DIRNAME).mkdir(parents=True)
            os.symlink(outside, index_root / GENERATIONS_DIRNAME / generation_id, target_is_directory=True)

            with self.assertRaises(CurrentPointerError):
                resolve_generation_dir(index_root, generation_id)

    def test_symlinked_generations_directory_is_a_documented_boundary(self):
        """Replacing `generations/` itself with a symlink is NOT caught, by construction.

        The check compares a candidate's resolved parent against the resolved `generations/`, so
        redirecting `generations/` redirects both sides equally and they still match. That is a
        real limit of this layer, and pinning it here keeps it a known boundary rather than an
        assumed guarantee: anyone who can replace a directory inside `output_root` already has
        write access to the derived tree, which no path check can compensate for.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if not _symlinks_available(root):
                self.skipTest("this platform/account cannot create symlinks")
            generation_id = new_generation_id()
            elsewhere = root / "elsewhere"
            (elsewhere / generation_id).mkdir(parents=True)
            index_root = root / "index"
            index_root.mkdir()
            os.symlink(elsewhere, index_root / GENERATIONS_DIRNAME, target_is_directory=True)

            resolved = resolve_generation_dir(index_root, generation_id)
            self.assertEqual(resolved.name, generation_id)


class TamperedPointerTests(unittest.TestCase):
    def test_hostile_pointer_values_fail_loudly_through_the_reader(self):
        """Whatever `current.json` holds, a reader fails with a recovery instruction.

        The pointer is the untrusted input here: it is a plain file a user, a sync client, or a
        stray editor can rewrite. What must never happen is a low-level OSError or a silently
        wrong database.
        """
        hostile_pointers = [
            {"current_generation": "../../evil"},
            {"current_generation": ""},
            {"current_generation": None},
            {"current_generation": 17},
            {"current_generation": ["a-list"]},
            {"current_generation": {"nested": "object"}},
            {"no_pointer_key_at_all": True},
        ]
        for pointer in hostile_pointers:
            with self.subTest(pointer=pointer):
                with tempfile.TemporaryDirectory() as tmp:
                    index_root = Path(tmp)
                    (index_root / "current.json").write_text(json.dumps(pointer), encoding="utf-8")
                    with self.assertRaises(CurrentPointerError):
                        resolve_reader_db_path(index_root / "zotero_text_index.sqlite")

    def test_pointer_that_is_not_an_object_fails_loudly(self):
        for raw in ("[]", '"a string"', "null", "12", "{not json"):
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    index_root = Path(tmp)
                    (index_root / "current.json").write_text(raw, encoding="utf-8")
                    with self.assertRaises(CurrentPointerError):
                        resolve_reader_db_path(index_root / "zotero_text_index.sqlite")


if __name__ == "__main__":
    unittest.main()
