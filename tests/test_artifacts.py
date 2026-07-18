import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.artifacts import (
    ArtifactError,
    CurrentPointerError,
    GENERATION_DB_FILENAME,
    GENERATION_JSONL_FILENAME,
    GenerationValidationError,
    IndexPaths,
    ManagedIndexMissingError,
    chunking_params_from_current,
    current_generation_jsonl,
    is_valid_generation_id,
    new_generation_id,
    publish_generation,
    read_current_pointer,
    recover_pending_publication,
    resolve_generation_dir,
    resolve_reader_db_path,
    stage_generation,
    validate_generation,
    write_jsonl_appending_manifest,
    write_jsonl_from_conversion_manifest,
    write_jsonl_from_existing,
    write_jsonl_upserting_record,
)
from zotero_pdf_text.fts import search_fts
from zotero_pdf_text.indexer import TextIndexRecord


def _record(attachment_key: str, text: str, title: str = "A title") -> dict[str, object]:
    return {
        "zotero_parent_key": f"P{attachment_key}",
        "zotero_attachment_key": attachment_key,
        "title": title,
        "creators": "Jane Smith",
        "year": "2024",
        "doi": "10.1000/x",
        "citation_key": f"key{attachment_key}",
        "source_path": f"{attachment_key}.pdf",
        "markdown_path": f"{attachment_key}.md",
        "markdown_sha256": "abc",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "char_count": len(text),
        "word_count": len(text.split()),
        "page_count": "2",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": False,
        "text": text,
    }


def _write_source_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _stage_from_records(index_root: Path, records: list[dict[str, object]], command: str = "test"):
    source = index_root / "source.jsonl"
    _write_source_jsonl(source, records)
    return stage_generation(index_root, write_jsonl_from_existing(source), command=command)


def _write_conversion_manifest(root: Path, rows: list[dict[str, str]]) -> Path:
    manifest = root / "manifest.csv"
    fieldnames = [
        "status",
        "output_path",
        "zotero_parent_key",
        "zotero_attachment_key",
        "title",
        "creators",
        "year",
        "doi",
        "citation_key",
        "source_path",
        "extraction_tool",
        "page_count",
        "classification",
        "identity_status",
        "identity_rule",
        "has_math",
    ]
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return manifest


def _manifest_row(root: Path, attachment_key: str, text: str) -> dict[str, str]:
    markdown = root / f"{attachment_key}.md"
    markdown.write_text(text, encoding="utf-8")
    return {
        "status": "converted",
        "output_path": str(markdown),
        "zotero_parent_key": f"P{attachment_key}",
        "zotero_attachment_key": attachment_key,
        "title": f"Title {attachment_key}",
        "creators": "Jane Smith",
        "year": "2024",
        "doi": "10.1000/x",
        "citation_key": f"key{attachment_key}",
        "source_path": f"{attachment_key}.pdf",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "page_count": "2",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": "false",
    }


class GenerationIdTests(unittest.TestCase):
    def test_new_ids_are_valid_and_unique(self):
        first, second = new_generation_id(), new_generation_id()
        self.assertTrue(is_valid_generation_id(first))
        self.assertTrue(is_valid_generation_id(second))
        self.assertNotEqual(first, second)

    def test_invalid_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in ("", "..", "../evil", "gen/../../x", "20260101T000000Z-XYZ", 42, None):
                with self.subTest(bad=bad):
                    self.assertFalse(is_valid_generation_id(bad))
            with self.assertRaises(CurrentPointerError):
                resolve_generation_dir(root, "../escape")


class StagePublishResolveTests(unittest.TestCase):
    def test_stage_validate_publish_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp) / "index"
            index_root.mkdir()
            info = _stage_from_records(index_root, [_record("A1", "Cultural consensus theory models shared knowledge.")])

            manifest = validate_generation(index_root, info.generation_id)
            self.assertEqual(manifest["records"], 1)

            pointer = publish_generation(index_root, info.generation_id)
            self.assertEqual(pointer["current_generation"], info.generation_id)
            self.assertIsNone(pointer["previous_generation"])
            self.assertFalse(IndexPaths(index_root).journal_path.exists())

            legacy_db = index_root / "zotero_text_index.sqlite"
            resolved = resolve_reader_db_path(legacy_db)
            self.assertEqual(resolved, info.directory / GENERATION_DB_FILENAME)
            results = search_fts(resolved, "consensus", limit=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(
                current_generation_jsonl(index_root), info.directory / GENERATION_JSONL_FILENAME
            )

    def test_reader_requires_pointer_no_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy_db = Path(tmp) / "zotero_text_index.sqlite"
            legacy_db.write_bytes(b"not opened")
            with self.assertRaises(ManagedIndexMissingError) as ctx:
                resolve_reader_db_path(legacy_db)
            self.assertIn("rebuild-index", str(ctx.exception))
            self.assertIsNone(read_current_pointer(Path(tmp)))

    def test_corrupt_pointer_fails_loudly_not_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            (index_root / "current.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(CurrentPointerError):
                resolve_reader_db_path(index_root / "zotero_text_index.sqlite")
            (index_root / "current.json").write_text(
                json.dumps({"current_generation": "../../evil"}), encoding="utf-8"
            )
            with self.assertRaises(CurrentPointerError):
                resolve_reader_db_path(index_root / "zotero_text_index.sqlite")

    def test_pointer_naming_missing_generation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            (index_root / "current.json").write_text(
                json.dumps({"schema_version": 1, "current_generation": new_generation_id()}),
                encoding="utf-8",
            )
            with self.assertRaises(CurrentPointerError):
                resolve_reader_db_path(index_root / "zotero_text_index.sqlite")

    def test_failed_staging_removes_partial_directory_and_keeps_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            info = _stage_from_records(index_root, [_record("A1", "First body text.")])
            publish_generation(index_root, info.generation_id)
            pointer_before = read_current_pointer(index_root)

            def exploding_writer(jsonl_path: Path) -> None:
                jsonl_path.write_text("partial", encoding="utf-8")
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                stage_generation(index_root, exploding_writer, command="test")

            paths = IndexPaths(index_root)
            remaining = sorted(entry.name for entry in paths.generations_dir.iterdir())
            self.assertEqual(remaining, [info.generation_id])
            self.assertEqual(read_current_pointer(index_root), pointer_before)

    def test_validation_rejects_tampered_and_incomplete_generations(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            info = _stage_from_records(index_root, [_record("A1", "Some body text for the index.")])

            tampered = info.jsonl_path
            tampered.write_text(tampered.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaises(GenerationValidationError):
                validate_generation(index_root, info.generation_id)

            info2 = _stage_from_records(index_root, [_record("A1", "Some body text for the index.")])
            (info2.directory / GENERATION_DB_FILENAME).unlink()
            with self.assertRaises(GenerationValidationError):
                validate_generation(index_root, info2.generation_id)

            info3 = _stage_from_records(index_root, [_record("A1", "Some body text for the index.")])
            manifest = json.loads(info3.manifest_path.read_text(encoding="utf-8"))
            manifest["records"] = 99
            info3.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(GenerationValidationError):
                validate_generation(index_root, info3.generation_id)

            # Publishing a tampered generation must refuse and leave no pointer behind.
            with self.assertRaises(GenerationValidationError):
                publish_generation(index_root, info.generation_id)
            self.assertIsNone(read_current_pointer(index_root))

    def test_publish_retains_only_current_and_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            ids = []
            for n in range(3):
                info = _stage_from_records(index_root, [_record("A1", f"Body text revision {n}.")])
                publish_generation(index_root, info.generation_id)
                ids.append(info.generation_id)

            pointer = read_current_pointer(index_root)
            self.assertEqual(pointer["current_generation"], ids[2])
            self.assertEqual(pointer["previous_generation"], ids[1])
            remaining = sorted(entry.name for entry in IndexPaths(index_root).generations_dir.iterdir())
            self.assertEqual(remaining, sorted(ids[1:]))

    def test_chunking_params_survive_into_successors(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            source = index_root / "source.jsonl"
            _write_source_jsonl(source, [_record("A1", "word " * 50)])
            info = stage_generation(
                index_root,
                write_jsonl_from_existing(source),
                command="test",
                chunk_chars=40,
                overlap_chars=5,
            )
            publish_generation(index_root, info.generation_id)
            self.assertEqual(chunking_params_from_current(index_root), (40, 5))


class RecoveryTests(unittest.TestCase):
    def test_recovery_completes_interrupted_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            first = _stage_from_records(index_root, [_record("A1", "Original body text.")])
            publish_generation(index_root, first.generation_id)
            second = _stage_from_records(index_root, [_record("A1", "Updated body text.")])

            # Simulate a crash after the journal is written but before the pointer swap.
            paths = IndexPaths(index_root)
            journal = {
                "schema_version": 1,
                "state": "publishing",
                "generation_id": second.generation_id,
                "previous_pointer": read_current_pointer(index_root),
                "started_at": "2026-07-17T00:00:00+00:00",
            }
            paths.journal_path.write_text(json.dumps(journal), encoding="utf-8")

            action = recover_pending_publication(index_root)
            self.assertIn("completed", action)
            pointer = read_current_pointer(index_root)
            self.assertEqual(pointer["current_generation"], second.generation_id)
            self.assertEqual(pointer["previous_generation"], first.generation_id)
            self.assertFalse(paths.journal_path.exists())

    def test_recovery_rolls_back_incomplete_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            first = _stage_from_records(index_root, [_record("A1", "Original body text.")])
            publish_generation(index_root, first.generation_id)
            pointer_before = read_current_pointer(index_root)

            # A staged directory that never completed: JSONL only, no manifest.
            paths = IndexPaths(index_root)
            half_id = new_generation_id()
            half_dir = paths.generations_dir / half_id
            half_dir.mkdir()
            (half_dir / GENERATION_JSONL_FILENAME).write_text("{}", encoding="utf-8")
            journal = {
                "schema_version": 1,
                "state": "publishing",
                "generation_id": half_id,
                "previous_pointer": pointer_before,
                "started_at": "2026-07-17T00:00:00+00:00",
            }
            paths.journal_path.write_text(json.dumps(journal), encoding="utf-8")

            action = recover_pending_publication(index_root)
            self.assertIn("rolled back", action)
            self.assertEqual(read_current_pointer(index_root), pointer_before)
            self.assertFalse(half_dir.exists())
            self.assertFalse(paths.journal_path.exists())

    def test_recovery_cleans_journal_when_pointer_already_swapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            info = _stage_from_records(index_root, [_record("A1", "Body text.")])
            publish_generation(index_root, info.generation_id)
            paths = IndexPaths(index_root)
            journal = {
                "schema_version": 1,
                "state": "publishing",
                "generation_id": info.generation_id,
                "previous_pointer": None,
                "started_at": "2026-07-17T00:00:00+00:00",
            }
            paths.journal_path.write_text(json.dumps(journal), encoding="utf-8")

            action = recover_pending_publication(index_root)
            self.assertIn("completed", action)
            self.assertFalse(paths.journal_path.exists())
            self.assertEqual(read_current_pointer(index_root)["current_generation"], info.generation_id)

    def test_recovery_refuses_corrupt_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            paths = IndexPaths(index_root)
            paths.generations_dir.mkdir(parents=True)
            paths.journal_path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ArtifactError):
                recover_pending_publication(index_root)

    def test_recovery_sweeps_crash_orphaned_stagings(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            info = _stage_from_records(index_root, [_record("A1", "Body text.")])
            publish_generation(index_root, info.generation_id)
            paths = IndexPaths(index_root)
            orphan_id = new_generation_id()
            orphan_dir = paths.generations_dir / orphan_id
            orphan_dir.mkdir()
            (orphan_dir / GENERATION_JSONL_FILENAME).write_text("{}", encoding="utf-8")
            foreign_dir = paths.generations_dir / "not-a-generation"
            foreign_dir.mkdir()

            self.assertIsNone(recover_pending_publication(index_root))
            self.assertFalse(orphan_dir.exists())
            # Unknown directory names are never deleted.
            self.assertTrue(foreign_dir.exists())

    def test_post_commit_cleanup_failure_does_not_fail_the_publication(self):
        # The pointer swap is the commit point: once it succeeds, a journal-unlink or
        # retention-sweep failure must not escape as an exception, or callers roll back their
        # source artifacts while readers already resolve the new generation.
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            first = _stage_from_records(index_root, [_record("A1", "Original body text.")])
            publish_generation(index_root, first.generation_id)
            second = _stage_from_records(index_root, [_record("A1", "Updated body text.")])

            with patch(
                "zotero_pdf_text.artifacts._sweep_unreferenced_generations",
                side_effect=OSError("simulated sweep failure"),
            ):
                pointer = publish_generation(index_root, second.generation_id)

            self.assertEqual(pointer["current_generation"], second.generation_id)
            self.assertEqual(
                read_current_pointer(index_root)["current_generation"], second.generation_id
            )
            # Whatever cleanup skipped is repaired by the next recovery pass under the lock.
            recover_pending_publication(index_root)
            self.assertEqual(
                read_current_pointer(index_root)["current_generation"], second.generation_id
            )

    def test_publish_crash_between_journal_and_pointer_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp)
            first = _stage_from_records(index_root, [_record("A1", "Original body text.")])
            publish_generation(index_root, first.generation_id)
            second = _stage_from_records(index_root, [_record("A1", "Updated body text.")])
            paths = IndexPaths(index_root)

            from zotero_pdf_text.artifacts import _atomic_write_json as _original_atomic_write

            call_state = {"journal_written": False}

            def selective(path, payload):
                if path == paths.pointer_path and call_state["journal_written"]:
                    raise RuntimeError("simulated crash before pointer swap")
                if path == paths.journal_path:
                    call_state["journal_written"] = True
                return _original_atomic_write(path, payload)

            with patch("zotero_pdf_text.artifacts._atomic_write_json", side_effect=selective):
                with self.assertRaises(RuntimeError):
                    publish_generation(index_root, second.generation_id)

            # Reader still resolves the previous complete generation.
            resolved = resolve_reader_db_path(index_root / "zotero_text_index.sqlite")
            self.assertEqual(resolved, first.directory / GENERATION_DB_FILENAME)

            action = recover_pending_publication(index_root)
            self.assertIn("completed", action)
            self.assertEqual(
                read_current_pointer(index_root)["current_generation"], second.generation_id
            )


class StagingWriterTests(unittest.TestCase):
    def test_rebuild_from_conversion_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_root = root / "index"
            index_root.mkdir()
            manifest = _write_conversion_manifest(
                root,
                [
                    _manifest_row(root, "A1", "Alpha body text about consensus."),
                    _manifest_row(root, "A2", "Beta body text about response times."),
                ],
            )
            info = stage_generation(
                index_root, write_jsonl_from_conversion_manifest(manifest), command="rebuild-index"
            )
            self.assertEqual(info.summary.records, 2)

    def test_append_writer_skips_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current.jsonl"
            _write_source_jsonl(current, [_record("A1", "Existing body text.")])
            manifest = _write_conversion_manifest(
                root,
                [
                    _manifest_row(root, "A1", "Existing body text."),
                    _manifest_row(root, "A2", "Brand-new body text."),
                ],
            )
            writer, appended = write_jsonl_appending_manifest(current, manifest)
            self.assertEqual(appended, 1)
            out = root / "staged.jsonl"
            writer(out)
            keys = [json.loads(line)["zotero_attachment_key"] for line in out.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(keys, ["A1", "A2"])

    def test_upsert_writer_replaces_and_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current.jsonl"
            _write_source_jsonl(
                current,
                [_record("A1", "Old body text."), _record("A2", "Other body text.")],
            )
            new_record = TextIndexRecord(**_record("A1", "New body text."))
            writer = write_jsonl_upserting_record(current, "A1", new_record)
            out = root / "staged.jsonl"
            writer(out)
            lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["text"], "New body text.")
            self.assertEqual(lines[1]["zotero_attachment_key"], "A2")

            appender = write_jsonl_upserting_record(current, "A3", TextIndexRecord(**_record("A3", "Appended body text.")))
            out2 = root / "staged2.jsonl"
            appender(out2)
            lines2 = [json.loads(line) for line in out2.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(lines2), 3)
            self.assertEqual(lines2[2]["zotero_attachment_key"], "A3")


if __name__ == "__main__":
    unittest.main()
