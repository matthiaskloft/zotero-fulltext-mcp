import contextlib
import csv
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_library import _index_record, _write_zotero_inventory

from zotero_pdf_text.artifacts import (
    current_generation_jsonl,
    read_current_pointer,
    stage_and_publish,
    write_jsonl_from_existing,
)
from zotero_pdf_text.checkpoint import CHECKPOINT_FILENAME, read_checkpoint
from zotero_pdf_text.cli import main
from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.library import audit_library
from zotero_pdf_text.provenance_reconvert import (
    APPLY_LOG_FILENAME,
    BUCKET_ELIGIBLE,
    BUCKET_IDENTITY_UNCERTAIN,
    BUCKET_MISSING_PDF,
    BUCKET_NOT_IN_ZOTERO,
    OUTCOME_ALREADY_RESOLVED,
    OUTCOME_CONVERSION_FAILED,
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_PLAN_STALE,
    OUTCOME_PUBLISHED,
    ProvenancePlanError,
    apply_plan,
    build_plan,
    default_plan_dir,
    load_plan,
    write_plan,
)

ELIGIBLE_KEYS = ["AAAA1111", "BBBB2222", "CCCC3333"]
MISSING = "DDDD4444"
UNVERIFIED = "EEEE5555"
RELINKED = "FFFF6666"
RETIRED = "GGGG7777"
KNOWN = "HHHH8888"
ALL_KEYS = [*ELIGIBLE_KEYS, MISSING, UNVERIFIED, RELINKED, RETIRED, KNOWN]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Extractor:
    """Fake extractor subprocess: records extractions, can fail or interrupt one attachment."""

    def __init__(self, *, fail: str | None = None, interrupt: str | None = None) -> None:
        self.extracted: list[str] = []
        self.fail = fail
        self.interrupt = interrupt
        self._lock = threading.Lock()

    def __call__(self, args, **kwargs):
        source = Path(args[3])
        with self._lock:
            self.extracted.append(source.stem)
        if source.stem == self.interrupt:
            raise KeyboardInterrupt
        if source.stem == self.fail:
            raise subprocess.CalledProcessError(1, args, stderr="extractor broke")
        Path(args[4]).write_text(f"# Reconverted {source.stem}\n\nFresh body {source.stem}", encoding="utf-8")


class ProvenanceFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = ProjectConfig(
            zotero_root=self.root / "zotero",
            zotero_data_directory=self.root / "zotero" / "data",
            linked_attachments=self.root / "linked",
            output_root=self.root / "out",
        )
        self.index_root = self.config.output_root / "index"
        pdf_dir = self.root / "pdfs"
        legacy = self.root / "legacy"
        pdf_dir.mkdir()
        legacy.mkdir()
        self.pdfs = {key: pdf_dir / f"{key}.pdf" for key in ALL_KEYS}
        for key, pdf in self.pdfs.items():
            if key != MISSING:
                pdf.write_bytes(f"%PDF {key}".encode())
        relinked_target = pdf_dir / "relinked.pdf"
        relinked_target.write_bytes(b"%PDF another document")

        records = []
        for key in ALL_KEYS:
            markdown = legacy / f"{key}.md"
            markdown.write_text(f"Old body {key}", encoding="utf-8")
            records.append(
                _index_record(
                    key,
                    source_path=str(self.pdfs[key]),
                    markdown_path=str(markdown),
                    markdown_sha256=_sha(markdown),
                    source_sha256=_sha(self.pdfs[key]) if key == KNOWN else "",
                    text=f"Old body {key}",
                )
            )
        seed = self.root / "seed.jsonl"
        seed.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        stage_and_publish(self.index_root, write_jsonl_from_existing(seed), command="test")

        inventory = {key: str(self.pdfs[key]) for key in ALL_KEYS if key != RETIRED}
        inventory[RELINKED] = str(relinked_target)
        _write_zotero_inventory(self.config, inventory)

        snapshot_rows = []
        for key in ALL_KEYS:
            unverified = key == UNVERIFIED
            snapshot_rows.append(
                {
                    "zotero_attachment_key": key,
                    "zotero_parent_key": f"P{key}",
                    "source_path": str(self.pdfs[key]),
                    "sha256": "",
                    "classification": "mapped_unverified" if unverified else "mapped_verified",
                    "identity_status": "candidate" if unverified else "verified",
                    "identity_rule": "doi_exact",
                    "safe_folder_id": f"zotero_{key}",
                    "title": "A title",
                    "creators": "Jane Smith",
                    "year": "2024",
                    "doi": "10.1000/x",
                    "citation_key": f"key-{key}",
                    "page_count": 3,
                }
            )
        self.snapshot = self.root / "mapping" / "mapping_report.jsonl"
        self.snapshot.parent.mkdir()
        self.snapshot.write_text("".join(json.dumps(row) + "\n" for row in snapshot_rows), encoding="utf-8")

    def plan(self) -> Path:
        plan = build_plan(self.config, self.snapshot, plan_id="testplan")
        return write_plan(plan, default_plan_dir(self.config, plan.plan_id))

    def apply(self, plan_path: Path, extractor: _Extractor, **kwargs):
        with patch("zotero_pdf_text.converter.subprocess.run", side_effect=extractor), contextlib.redirect_stderr(io.StringIO()):
            return apply_plan(self.config, plan_path, **kwargs)

    def records(self) -> dict[str, dict]:
        lines = current_generation_jsonl(self.index_root).read_text(encoding="utf-8").splitlines()
        return {record["zotero_attachment_key"]: record for record in map(json.loads, lines)}

    def unknown_keys(self) -> set[str]:
        audit = audit_library(self.config, self.snapshot, index_root=self.index_root)
        keys = {
            item.attachment_key
            for item in audit.items
            if item.observation.in_index and not item.observation.indexed_source_sha256
        }
        self.assertEqual(len(keys), audit.source_provenance_unknown)
        return keys


class PlanTests(ProvenanceFixture):
    def test_plan_buckets_every_provenance_unknown_record(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        plan = build_plan(self.config, self.snapshot, plan_id="p")

        # Read-only: building a plan changes nothing on disk.
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        buckets = {row.attachment_key: row.bucket for row in plan.rows}
        self.assertEqual(
            buckets,
            {
                "AAAA1111": BUCKET_ELIGIBLE,
                "BBBB2222": BUCKET_ELIGIBLE,
                "CCCC3333": BUCKET_ELIGIBLE,
                MISSING: BUCKET_MISSING_PDF,
                UNVERIFIED: BUCKET_IDENTITY_UNCERTAIN,
                RELINKED: BUCKET_IDENTITY_UNCERTAIN,
                RETIRED: BUCKET_NOT_IN_ZOTERO,
            },
        )
        self.assertNotIn(KNOWN, buckets)
        self.assertEqual(len(plan.rows), audit_library(self.config, self.snapshot).source_provenance_unknown)
        self.assertEqual([row.ordinal for row in plan.rows if row.bucket == BUCKET_ELIGIBLE], [1, 2, 3])
        rows = {row.attachment_key: row for row in plan.rows}
        self.assertIn("different PDF", rows[RELINKED].reason)
        self.assertIn("mapped_unverified", rows[UNVERIFIED].reason)
        self.assertIsNone(rows[UNVERIFIED].conversion_row)
        self.assertEqual(
            plan.estimate,
            {
                "eligible_rows": 3,
                "source_bytes": sum(self.pdfs[key].stat().st_size for key in ELIGIBLE_KEYS),
                "pages": 9,
                "rows_without_page_count": 0,
            },
        )

    def test_plan_round_trips_and_refuses_to_overwrite(self):
        plan_path = self.plan()
        plan, plan_dir = load_plan(plan_path.parent)
        self.assertEqual(plan_dir, plan_path.parent)
        self.assertEqual(plan.counts[BUCKET_ELIGIBLE], 3)
        with (plan_dir / "plan.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 7)
        with self.assertRaises(ProvenancePlanError):
            write_plan(plan, plan_dir)

    def test_unreadable_inventory_makes_nothing_eligible(self):
        self.config.zotero_sqlite.unlink()
        plan = build_plan(self.config, self.snapshot, plan_id="p")
        self.assertFalse(plan.inventory_available)
        self.assertEqual(plan.counts[BUCKET_ELIGIBLE], 0)
        self.assertEqual(plan.counts["membership_unchecked"], 7)


class ApplyTests(ProvenanceFixture):
    def test_partial_failure_publishes_successes_and_preserves_the_failed_record(self):
        plan_path = self.plan()
        original = self.records()

        report = self.apply(plan_path, _Extractor(fail="BBBB2222"))

        outcomes = {row.attachment_key: row.outcome for row in report.rows}
        self.assertEqual(
            outcomes,
            {"AAAA1111": OUTCOME_PUBLISHED, "BBBB2222": OUTCOME_CONVERSION_FAILED, "CCCC3333": OUTCOME_PUBLISHED},
        )
        self.assertIsNotNone(report.generation_id)
        records = self.records()
        for key in ("AAAA1111", "CCCC3333"):
            self.assertEqual(records[key]["source_sha256"], _sha(self.pdfs[key]))
            self.assertIn(f"Fresh body {key}", records[key]["text"])
        # The failed row and every row outside the selection are carried over verbatim.
        for key in ("BBBB2222", MISSING, UNVERIFIED, RELINKED, RETIRED, KNOWN):
            self.assertEqual(records[key], original[key])
        self.assertEqual(self.unknown_keys(), {"BBBB2222", MISSING, UNVERIFIED, RELINKED, RETIRED})

        # A later invocation retries only the failure; published rows are already resolved.
        extractor = _Extractor()
        report = self.apply(plan_path, extractor)
        self.assertEqual(extractor.extracted, ["BBBB2222"])
        self.assertEqual({row.outcome for row in report.rows if row.attachment_key == "BBBB2222"}, {OUTCOME_PUBLISHED})
        self.assertEqual(self.unknown_keys(), {MISSING, UNVERIFIED, RELINKED, RETIRED})
        log = (plan_path.parent / APPLY_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(log), 2)

    def test_interrupted_apply_publishes_nothing_and_resume_reuses_checkpointed_rows(self):
        plan_path = self.plan()
        before = read_current_pointer(self.index_root)["current_generation"]

        first = _Extractor(interrupt="CCCC3333")
        with self.assertRaises(KeyboardInterrupt):
            self.apply(plan_path, first)
        self.assertEqual(read_current_pointer(self.index_root)["current_generation"], before)
        self.assertFalse((self.config.output_root / ".pipeline.lock").exists())
        run_dir = Path(load_plan(plan_path)[0].run_dir)
        checkpointed = {entry.zotero_attachment_key for entry in read_checkpoint(run_dir / CHECKPOINT_FILENAME).values()}
        self.assertEqual(checkpointed, {"AAAA1111", "BBBB2222"})

        second = _Extractor()
        report = self.apply(plan_path, second)

        self.assertEqual(second.extracted, ["CCCC3333"])
        self.assertEqual([row.outcome for row in report.rows], [OUTCOME_PUBLISHED] * 3)
        records = self.records()
        for key in ELIGIBLE_KEYS:
            self.assertEqual(records[key]["source_sha256"], _sha(self.pdfs[key]))
        self.assertEqual(self.unknown_keys(), {MISSING, UNVERIFIED, RELINKED, RETIRED})

    def test_limit_batches_share_one_run_directory_with_stable_paths(self):
        plan_path = self.plan()
        first = self.apply(plan_path, _Extractor(), limit=1)
        self.assertEqual([row.attachment_key for row in first.rows], ["AAAA1111"])
        self.assertEqual(first.remaining_eligible, 2)
        second = self.apply(plan_path, _Extractor(), limit=5)
        self.assertEqual([row.attachment_key for row in second.rows if row.outcome == OUTCOME_PUBLISHED], ["BBBB2222", "CCCC3333"])
        paths = {key: Path(record["markdown_path"]).name for key, record in self.records().items() if key in ELIGIBLE_KEYS}
        self.assertEqual(paths, {"AAAA1111": "0001_zotero_AAAA1111.md", "BBBB2222": "0002_zotero_BBBB2222.md", "CCCC3333": "0003_zotero_CCCC3333.md"})
        # The first batch's Markdown, now referenced by the index, is still in place.
        self.assertTrue(Path(self.records()["AAAA1111"]["markdown_path"]).is_file())

    def test_source_replaced_after_extraction_is_rejected_and_old_record_kept(self):
        plan_path = self.plan()
        original = self.records()["AAAA1111"]

        # The PDF is replaced after the converter's post-extraction hash, before publication.
        with patch(
            "zotero_pdf_text.provenance_reconvert.convert_planned_rows",
            side_effect=self._convert_then_replace("AAAA1111"),
        ):
            report = self.apply(plan_path, _Extractor(), keys=["AAAA1111"])

        self.assertEqual(report.rows[0].outcome, "rejected")
        self.assertIn("changed since conversion", report.rows[0].reason)
        self.assertIsNone(report.generation_id)
        self.assertEqual(self.records()["AAAA1111"], original)

    def _convert_then_replace(self, key: str):
        from zotero_pdf_text.converter import convert_planned_rows

        def run(*args, **kwargs):
            result = convert_planned_rows(*args, **kwargs)
            self.pdfs[key].write_bytes(b"%PDF replaced after extraction")
            return result

        return run

    def test_keys_select_only_eligible_rows_and_stale_plans_are_refused_per_row(self):
        plan_path = self.plan()
        # The index record for BBBB2222 changes after planning (e.g. another reconversion).
        seed = self.root / "seed2.jsonl"
        records = self.records()
        records["BBBB2222"]["markdown_sha256"] = "changed"
        seed.write_text("".join(json.dumps(record) + "\n" for record in records.values()), encoding="utf-8")
        stage_and_publish(self.index_root, write_jsonl_from_existing(seed), command="test")

        extractor = _Extractor()
        report = self.apply(plan_path, extractor, keys=["BBBB2222", UNVERIFIED, "CCCC3333"])

        outcomes = {row.attachment_key: row.outcome for row in report.rows}
        self.assertEqual(
            outcomes, {UNVERIFIED: OUTCOME_NOT_ELIGIBLE, "BBBB2222": OUTCOME_PLAN_STALE, "CCCC3333": OUTCOME_PUBLISHED}
        )
        self.assertEqual(extractor.extracted, ["CCCC3333"])
        again = self.apply(plan_path, _Extractor(), keys=["CCCC3333"])
        self.assertEqual([row.outcome for row in again.rows], [OUTCOME_ALREADY_RESOLVED])
        self.assertIsNone(again.generation_id)

    def test_plan_outside_output_root_is_refused(self):
        plan = build_plan(self.config, self.snapshot, plan_id="outside")
        plan_path = write_plan(plan, self.root / "elsewhere")
        with self.assertRaises(ProvenancePlanError):
            apply_plan(self.config, plan_path)


class CliTests(ProvenanceFixture):
    def write_config(self) -> Path:
        path = self.root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "zotero_root": str(self.config.zotero_root),
                    "zotero_data_directory": str(self.config.zotero_data_directory),
                    "linked_attachments": str(self.config.linked_attachments),
                    "output_root": str(self.config.output_root),
                }
            ),
            encoding="utf-8",
        )
        self.config.linked_attachments.mkdir(exist_ok=True)
        return path

    def test_plan_then_apply_from_the_command_line(self):
        config_path = self.write_config()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["plan-provenance-reconvert", "--config", str(config_path), "--mapping-report", str(self.snapshot), "--json"])
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["counts"]["eligible"], 3)
        self.assertEqual(summary["counts"]["missing_pdf"], 1)
        self.assertNotIn("rows", summary)

        output = io.StringIO()
        with (
            patch("zotero_pdf_text.converter.subprocess.run", side_effect=_Extractor()),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = main(["apply-provenance-reconvert", "--config", str(config_path), "--plan", summary["plan_path"], "--limit", "2"])
        self.assertEqual(code, 0)
        self.assertIn("Published generation:", output.getvalue())
        self.assertEqual(self.unknown_keys(), {"CCCC3333", MISSING, UNVERIFIED, RELINKED, RETIRED})


if __name__ == "__main__":
    unittest.main()
