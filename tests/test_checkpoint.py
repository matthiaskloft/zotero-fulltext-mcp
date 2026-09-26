import contextlib
import csv
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.checkpoint import (
    CHECKPOINT_FILENAME,
    ConversionCheckpoint,
    classify_entry,
    read_checkpoint,
)
from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.converter import convert_verified

FIELDNAMES = [
    "classification",
    "source_path",
    "safe_folder_id",
    "zotero_parent_key",
    "zotero_attachment_key",
    "title",
    "creators",
    "year",
    "doi",
    "citation_key",
    "page_count",
    "identity_status",
    "identity_rule",
]


def _write_report(path: Path, attachments: list[tuple[str, Path]], *, citation_suffix: str = "2024") -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for key, pdf in attachments:
            writer.writerow(
                {
                    "classification": "mapped_verified",
                    "source_path": str(pdf),
                    "safe_folder_id": f"zotero_{key}",
                    "zotero_parent_key": f"PARENT{key}",
                    "zotero_attachment_key": key,
                    "title": f"Title {key}",
                    "creators": "Jane Smith",
                    "year": "2024",
                    "doi": "",
                    "citation_key": f"smith{key}{citation_suffix}",
                    "page_count": "1",
                    "identity_status": "verified",
                    "identity_rule": "doi_exact",
                }
            )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Extractor:
    """Fake extractor subprocess: records which PDFs were extracted, optionally interrupts one."""

    def __init__(self, interrupt_on: str | None = None) -> None:
        self.extracted: list[str] = []
        self.interrupt_on = interrupt_on
        self._lock = threading.Lock()

    def __call__(self, args, **kwargs):
        source = Path(args[3])
        with self._lock:
            self.extracted.append(source.stem)
        if source.stem == self.interrupt_on:
            raise KeyboardInterrupt
        Path(args[4]).write_text(f"# Text of {source.stem}\n\n{source.read_bytes()!r}", encoding="utf-8")


class ResumeFixture(unittest.TestCase):
    keys = ["AAA", "BBB", "CCC", "DDD"]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pdfs = {}
        for key in self.keys:
            pdf = self.root / f"{key}.pdf"
            pdf.write_bytes(f"%PDF {key} original".encode())
            self.pdfs[key] = pdf
        self.report = self.root / "mapping_report.csv"
        _write_report(self.report, list(self.pdfs.items()))
        self.config = ProjectConfig(self.root, self.root, self.root, self.root / "output")
        self.run_dir = self.root / "output" / "run"

    def convert(self, extractor: _Extractor, *, workers: int = 1, resume: bool = True) -> str:
        stderr = io.StringIO()
        with patch("zotero_pdf_text.converter.subprocess.run", side_effect=extractor), contextlib.redirect_stderr(stderr):
            convert_verified(self.config, self.report, output_dir=self.run_dir, resume=resume, workers=workers)
        return stderr.getvalue()

    def interrupted(self, key: str, *, workers: int = 1) -> _Extractor:
        extractor = _Extractor(interrupt_on=key)
        with patch("zotero_pdf_text.converter.subprocess.run", side_effect=extractor), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                convert_verified(self.config, self.report, output_dir=self.run_dir, workers=workers)
        return extractor

    def manifest(self) -> list[dict]:
        lines = (self.run_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def markdown(self, key: str) -> Path:
        return next((self.run_dir / "markdown").glob(f"*_zotero_{key}.md"))


class InterruptedRunTests(ResumeFixture):
    def test_interrupted_run_checkpoints_completed_rows_before_any_manifest(self):
        self.interrupted("CCC")

        self.assertFalse((self.run_dir / "manifest.csv").exists())
        entries = read_checkpoint(self.run_dir / CHECKPOINT_FILENAME)
        self.assertEqual(sorted(entry.zotero_attachment_key for entry in entries.values()), ["AAA", "BBB"])
        for entry in entries.values():
            key = entry.zotero_attachment_key
            self.assertEqual(entry.source_sha256, _sha(self.pdfs[key]))
            self.assertEqual(entry.output_sha256, _sha(self.markdown(key)))
            self.assertEqual(entry.result["status"], "converted")

    def test_resume_reuses_validated_rows_and_reconstructs_the_manifest(self):
        self.interrupted("CCC")
        original_hashes = {key: _sha(pdf) for key, pdf in self.pdfs.items()}
        # Today's PDF differs from the one the checkpointed text was extracted from.
        self.pdfs["AAA"].write_bytes(b"%PDF AAA replaced after extraction")

        extractor = _Extractor()
        stderr = self.convert(extractor)

        self.assertEqual(sorted(extractor.extracted), ["CCC", "DDD"])
        rows = self.manifest()
        self.assertEqual([row["zotero_attachment_key"] for row in rows], self.keys)
        self.assertTrue(all(row["status"] == "converted" for row in rows))
        # The extraction-time hash survives the resume; today's file is never hashed into it.
        self.assertEqual(rows[0]["source_sha256"], original_hashes["AAA"])
        self.assertNotEqual(rows[0]["source_sha256"], _sha(self.pdfs["AAA"]))
        self.assertEqual(rows[1]["source_sha256"], original_hashes["BBB"])
        summary = (self.run_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("Reused from checkpoint without re-extraction: 2", summary)
        self.assertIn("Of those, source PDF modified since extraction: 1", summary)
        self.assertIn("[1/4] AAA: reused from checkpoint (source PDF modified since extraction)", stderr)
        self.assertIn("[2/4] BBB: reused from checkpoint [pymupdf4llm.to_markdown]", stderr)
        self.assertIn("CCC: converted [pymupdf4llm.to_markdown]", stderr)

        # A second resume re-extracts nothing and yields the same manifest.
        again = _Extractor()
        self.convert(again)
        self.assertEqual(again.extracted, [])
        self.assertEqual(self.manifest(), rows)

    def test_multiple_workers_checkpoint_every_completed_row(self):
        self.interrupted("BBB", workers=3)

        keys = {entry.zotero_attachment_key for entry in read_checkpoint(self.run_dir / CHECKPOINT_FILENAME).values()}
        self.assertEqual(keys, {"AAA", "CCC", "DDD"})
        extractor = _Extractor()
        self.convert(extractor, workers=3)
        self.assertEqual(extractor.extracted, ["BBB"])
        rows = self.manifest()
        self.assertEqual([row["zotero_attachment_key"] for row in rows], self.keys)
        self.assertTrue(all(len(row["source_sha256"]) == 64 for row in rows))

    def test_torn_final_line_is_tolerated_and_terminated(self):
        self.interrupted("CCC")
        checkpoint_path = self.run_dir / CHECKPOINT_FILENAME
        with checkpoint_path.open("ab") as handle:
            handle.write(b'{"checkpoint_version": 1, "output": "markdown/0003')

        extractor = _Extractor()
        self.convert(extractor)

        self.assertEqual(sorted(extractor.extracted), ["CCC", "DDD"])
        entries = read_checkpoint(checkpoint_path)
        self.assertEqual({entry.zotero_attachment_key for entry in entries.values()}, set(self.keys))


class ProvenanceValidationTests(ResumeFixture):
    def test_bare_markdown_without_checkpoint_stays_provenance_unknown(self):
        self.convert(_Extractor(), resume=False)
        (self.run_dir / CHECKPOINT_FILENAME).unlink()

        extractor = _Extractor()
        self.convert(extractor)

        self.assertEqual(extractor.extracted, [])
        rows = self.manifest()
        self.assertTrue(all(row["status"] == "skipped_existing" for row in rows))
        self.assertTrue(all(row["source_sha256"] == "" for row in rows))

    def test_markdown_modified_after_checkpoint_is_not_certified(self):
        self.convert(_Extractor(), resume=False)
        markdown = self.markdown("AAA")
        markdown.write_text(markdown.read_text(encoding="utf-8") + "\nedited elsewhere\n", encoding="utf-8")

        extractor = _Extractor()
        self.convert(extractor)

        self.assertEqual(extractor.extracted, [])
        rows = {row["zotero_attachment_key"]: row for row in self.manifest()}
        self.assertEqual(rows["AAA"]["status"], "skipped_existing")
        self.assertEqual(rows["AAA"]["source_sha256"], "")
        self.assertIn("edited elsewhere", markdown.read_text(encoding="utf-8"))
        self.assertEqual(rows["BBB"]["status"], "converted")

    def test_entry_for_another_source_forces_reextraction(self):
        self.convert(_Extractor(), resume=False)
        # Row 1 now points at a different document with different bytes, so the existing
        # 0001 Markdown is known to describe another source and must not be reused.
        other = self.root / "EEE.pdf"
        other.write_bytes(b"%PDF EEE")
        _write_report(self.report, [("AAA", other)] + [(key, self.pdfs[key]) for key in self.keys[1:]])

        extractor = _Extractor()
        self.convert(extractor)

        self.assertEqual(extractor.extracted, ["EEE"])
        row = self.manifest()[0]
        self.assertEqual(row["status"], "converted")
        self.assertEqual(row["source_sha256"], _sha(other))
        self.assertIn("Text of EEE", self.markdown("AAA").read_text(encoding="utf-8"))

    def test_moved_source_with_identical_bytes_is_reused(self):
        self.convert(_Extractor(), resume=False)
        moved = self.root / "moved"
        moved.mkdir()
        relocated = {}
        for key, pdf in self.pdfs.items():
            relocated[key] = moved / pdf.name
            relocated[key].write_bytes(pdf.read_bytes())
        _write_report(self.report, list(relocated.items()))

        extractor = _Extractor()
        self.convert(extractor)

        self.assertEqual(extractor.extracted, [])
        rows = self.manifest()
        self.assertTrue(all(row["status"] == "converted" for row in rows))
        self.assertEqual(rows[0]["source_path"], str(relocated["AAA"]))

    def test_metadata_refresh_keeps_provenance_and_rerecords_output_hash(self):
        self.convert(_Extractor(), resume=False)
        original = self.manifest()
        _write_report(self.report, list(self.pdfs.items()), citation_suffix="2025")

        extractor = _Extractor()
        self.convert(extractor)

        self.assertEqual(extractor.extracted, [])
        rows = self.manifest()
        self.assertEqual(rows[0]["citation_key"], "smithAAA2025")
        self.assertEqual(rows[0]["source_sha256"], original[0]["source_sha256"])
        self.assertIn('citation_key: "smithAAA2025"', self.markdown("AAA").read_text(encoding="utf-8"))
        entry = read_checkpoint(self.run_dir / CHECKPOINT_FILENAME)[f"markdown/{self.markdown('AAA').name}"]
        self.assertEqual(entry.output_sha256, _sha(self.markdown("AAA")))
        # And the refreshed file stays reusable on the next resume.
        self.convert(extractor)
        self.assertEqual(extractor.extracted, [])

    def test_failed_publication_is_never_checkpointed(self):
        with patch("zotero_pdf_text._atomic.replace_with_retry", side_effect=OSError("disk full")):
            self.convert(_Extractor(), resume=False)

        self.assertEqual(read_checkpoint(self.run_dir / CHECKPOINT_FILENAME), {})
        self.assertTrue(all(row["status"] == "error" for row in self.manifest()))

    def test_checkpoint_write_failure_does_not_fail_the_conversion(self):
        with patch("zotero_pdf_text.checkpoint.os.fsync", side_effect=OSError("sync failed")):
            stderr = self.convert(_Extractor(), resume=False)

        self.assertTrue(all(row["status"] == "converted" for row in self.manifest()))
        self.assertIn("could not update conversion checkpoint", stderr)


class CheckpointFileTests(unittest.TestCase):
    def test_malformed_and_foreign_lines_are_skipped_and_last_entry_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            checkpoint = ConversionCheckpoint(run_dir)
            output = run_dir / "markdown" / "0001_x.md"
            common = {"source_size": 1, "source_mtime_ns": 2, "source_sha256": "s", "output_sha256": "o1"}
            checkpoint.record(output, result={"zotero_attachment_key": "K", "source_path": "p"}, **common)
            with checkpoint.path.open("a", encoding="utf-8") as handle:
                handle.write("not json\n")
                handle.write(json.dumps({"checkpoint_version": 99, "output": "markdown/0001_x.md"}) + "\n")
                handle.write("[1, 2]\n")
            checkpoint.record(output, result={"zotero_attachment_key": "K", "source_path": "p"}, **{**common, "output_sha256": "o2"})

            entries = read_checkpoint(checkpoint.path)
            self.assertEqual(list(entries), ["markdown/0001_x.md"])
            self.assertEqual(entries["markdown/0001_x.md"].output_sha256, "o2")

    def test_classify_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            entry = ConversionCheckpoint(run_dir).record(
                run_dir / "markdown" / "a.md",
                result={"zotero_attachment_key": "K", "source_path": "p"},
                source_size=None,
                source_mtime_ns=None,
                source_sha256="s",
                output_sha256="o",
            )
        self.assertEqual(classify_entry(entry, attachment_key="K", source_path="p", output_sha256="o"), "reuse")
        self.assertEqual(classify_entry(entry, attachment_key="X", source_path="p", output_sha256="o"), "foreign")
        self.assertEqual(classify_entry(entry, attachment_key="K", source_path="q", output_sha256="o"), "foreign")
        self.assertEqual(
            classify_entry(entry, attachment_key="K", source_path="q", output_sha256="o", current_source_sha256="s"),
            "reuse",
        )
        self.assertEqual(classify_entry(entry, attachment_key="K", source_path="p", output_sha256="z"), "output_changed")


if __name__ == "__main__":
    unittest.main()
