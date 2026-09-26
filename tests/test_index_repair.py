import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_library import _index_record, _write_zotero_inventory
from test_provenance_reconvert import _Extractor

from zotero_pdf_text import index_repair
from zotero_pdf_text.artifacts import (
    ArtifactError,
    current_generation_jsonl,
    read_current_pointer,
    stage_and_publish,
    write_jsonl_applying_repairs,
    write_jsonl_from_existing,
)
from zotero_pdf_text.cli import main
from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.index_repair import (
    GROUP_RECONVERT,
    GROUP_REVIEW,
    GROUP_SAFE,
    OUTCOME_METADATA_REFRESHED,
    OUTCOME_REMOVED,
    IndexRepairError,
    apply_plan,
    build_plan,
    default_plan_dir,
    load_plan,
    write_plan,
)
from zotero_pdf_text.indexer import TextIndexRecord
from zotero_pdf_text.library import audit_library
from zotero_pdf_text.provenance_reconvert import (
    OUTCOME_ALREADY_RESOLVED,
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_NOT_IN_PLAN,
    OUTCOME_PLAN_STALE,
    OUTCOME_PUBLISHED,
    OUTCOME_ZOTERO_CHANGED,
)

SAFE = "AAAA1111"  # Zotero retitled the item
ENRICHED = "BBBB2222"  # retitled, and its indexed text is an enriched (math-OCR) version
STALE = "CCCC3333"  # the Markdown on disk no longer matches the indexed hash
CHANGED = "DDDD4444"  # the PDF changed since extraction, and Zotero retitled it too
MISSING_PDF = "EEEE5555"
MISSING_MD = "FFFF6666"
ORPHAN = "GGGG7777"  # Zotero no longer lists it
UNVERIFIED = "HHHH8888"
MANUAL = "LLLL1111"  # stale Markdown, but a manual_accepted identity may not be replaced
CURRENT = ("JJJJ9999", "KKKK0000")  # healthy, converted in a different run
ALL_KEYS = [SAFE, ENRICHED, STALE, CHANGED, MISSING_PDF, MISSING_MD, ORPHAN, UNVERIFIED, MANUAL, *CURRENT]
RUN_TWO = {MISSING_MD, *CURRENT}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _set_zotero_title(config: ProjectConfig, key: str, title: str) -> None:
    con = sqlite3.connect(config.zotero_sqlite)
    try:
        con.execute(
            "UPDATE itemDataValues SET value = ? WHERE valueID = ("
            "SELECT d.valueID FROM itemData d JOIN items i ON i.itemID = d.itemID "
            "WHERE i.key = ? AND d.fieldID = 1)",
            (title, f"P{key}"),
        )
        con.commit()
    finally:
        con.close()


class RepairFixture(unittest.TestCase):
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
        pdf_dir.mkdir()
        # The index was assembled from two separate conversion runs; a repair must keep both.
        runs = {
            1: self.config.output_root / "conversion-runs" / "verified" / "run1",
            2: self.config.output_root / "conversion-runs" / "verified" / "run2",
        }
        for run in runs.values():
            run.mkdir(parents=True)
        self.pdfs = {key: pdf_dir / f"{key}.pdf" for key in ALL_KEYS}
        self.markdown: dict[str, Path] = {}
        records = []
        for key in ALL_KEYS:
            if key != MISSING_PDF:
                self.pdfs[key].write_bytes(f"%PDF {key}".encode())
            run = runs[2 if key in RUN_TWO else 1]
            markdown = run / (f"{key}.enriched.md" if key == ENRICHED else f"{key}.md")
            body = f"Body {key} with $$E = mc^2$$ from OCR" if key == ENRICHED else f"Body {key}"
            markdown.write_text(body, encoding="utf-8")
            self.markdown[key] = markdown
            records.append(
                _index_record(
                    key,
                    source_path=str(self.pdfs[key]),
                    markdown_path=str(markdown),
                    markdown_sha256=_sha(markdown),
                    source_sha256="0" * 64 if key in (CHANGED, MISSING_PDF) else _sha(self.pdfs[key]),
                    extraction_tool="marker" if key == ENRICHED else "pymupdf4llm.to_markdown",
                    has_math=key == ENRICHED,
                    text=body,
                )
            )
        seed = self.root / "seed.jsonl"
        seed.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        stage_and_publish(self.index_root, write_jsonl_from_existing(seed), command="test")

        for key in (STALE, MANUAL):
            self.markdown[key].write_text(f"Body {key}, edited by hand", encoding="utf-8")
        self.markdown[MISSING_MD].unlink()

        self.write_inventory(with_orphan=False)
        for key in (SAFE, ENRICHED):
            _set_zotero_title(self.config, key, "New title")
        _set_zotero_title(self.config, CHANGED, "Renamed")

        snapshot_rows = []
        for key in ALL_KEYS:
            if key == MISSING_PDF:
                continue  # a file-walking dry-run never sees a PDF that is gone
            snapshot_rows.append(
                {
                    "zotero_attachment_key": key,
                    "zotero_parent_key": f"P{key}",
                    "source_path": str(self.pdfs[key]),
                    "sha256": _sha(self.pdfs[key]),
                    "classification": "mapped_unverified" if key == UNVERIFIED else "mapped_verified",
                    "identity_status": {UNVERIFIED: "candidate", MANUAL: "manual_accepted"}.get(key, "verified"),
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

    def write_inventory(self, *, with_orphan: bool) -> None:
        self.config.zotero_sqlite.unlink(missing_ok=True)
        _write_zotero_inventory(
            self.config, {key: str(self.pdfs[key]) for key in ALL_KEYS if with_orphan or key != ORPHAN}
        )

    def plan(self, plan_id: str = "testplan") -> Path:
        plan = build_plan(self.config, self.snapshot, plan_id=plan_id)
        return write_plan(plan, default_plan_dir(self.config, plan.plan_id))

    def apply(self, plan_path: Path, extractor: _Extractor | None = None, **kwargs):
        with (
            patch("zotero_pdf_text.converter.subprocess.run", side_effect=extractor or _Extractor()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return apply_plan(self.config, plan_path, **kwargs)

    def records(self) -> dict[str, dict]:
        lines = current_generation_jsonl(self.index_root).read_text(encoding="utf-8").splitlines()
        return {record["zotero_attachment_key"]: record for record in map(json.loads, lines)}

    def generation(self) -> str:
        return str(read_current_pointer(self.index_root)["current_generation"])

    def statuses(self) -> dict[str, tuple[str, ...]]:
        audit = audit_library(self.config, self.snapshot, index_root=self.index_root)
        return {item.attachment_key: item.statuses for item in audit.items}


class PlanTests(RepairFixture):
    def test_plan_groups_every_indexed_finding_and_changes_nothing(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        plan = build_plan(self.config, self.snapshot, plan_id="p")
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

        rows = {row.attachment_key: row for row in plan.rows}
        self.assertEqual(
            {key: (row.group, row.reason_code) for key, row in rows.items()},
            {
                SAFE: (GROUP_SAFE, "metadata_changed"),
                ENRICHED: (GROUP_SAFE, "metadata_changed"),
                STALE: (GROUP_RECONVERT, "stale_markdown"),
                CHANGED: (GROUP_RECONVERT, "source_changed"),
                MISSING_PDF: (GROUP_REVIEW, "missing_source"),
                MISSING_MD: (GROUP_REVIEW, "missing_markdown"),
                ORPHAN: (GROUP_REVIEW, "orphaned_index"),
                UNVERIFIED: (GROUP_REVIEW, "unverified_indexed"),
                MANUAL: (GROUP_REVIEW, "reconvert_blocked"),
            },
        )
        # The plan's findings are the audit's findings, status for status.
        audit = self.statuses()
        for key, row in rows.items():
            self.assertEqual(row.statuses, audit[key])
        for key in CURRENT:
            self.assertEqual(audit[key], ("current",))
            self.assertNotIn(key, rows)
        self.assertEqual([key for key, row in rows.items() if row.removable], [ORPHAN])
        self.assertEqual(rows[SAFE].target_metadata, {"title": "New title", "doi": "10.1000/x", "citation_key": f"key-{SAFE}"})
        self.assertEqual(rows[CHANGED].conversion_row["title"], "Renamed")
        self.assertEqual([rows[STALE].ordinal, rows[CHANGED].ordinal], [1, 2])
        self.assertIn("manual_accepted", rows[MANUAL].reason)
        self.assertEqual(plan.counts, {GROUP_SAFE: 2, GROUP_RECONVERT: 2, GROUP_REVIEW: 5})

    def test_diagnostics_are_bounded_and_the_plan_round_trips(self):
        with patch.object(index_repair, "EXAMPLES_PER_REASON", 1):
            plan = build_plan(self.config, self.snapshot, plan_id="p")
            safe = plan.summary()["diagnostics"][GROUP_SAFE]
        self.assertEqual(safe["count"], 2)
        self.assertEqual(safe["by_reason"]["metadata_changed"], {"count": 2, "examples": [SAFE]})
        self.assertNotIn("rows", plan.summary())

        plan_path = write_plan(plan, default_plan_dir(self.config, plan.plan_id))
        loaded, plan_dir = load_plan(plan_path.parent)
        self.assertEqual(loaded.rows, plan.rows)
        self.assertEqual(plan_dir, plan_path.parent)
        with self.assertRaises(IndexRepairError):
            write_plan(plan, plan_dir)

    def test_unreadable_zotero_makes_everything_review(self):
        self.config.zotero_sqlite.unlink()
        plan = build_plan(self.config, self.snapshot, plan_id="p")
        self.assertFalse(plan.inventory_available)
        self.assertEqual(plan.counts[GROUP_SAFE] + plan.counts[GROUP_RECONVERT], 0)
        self.assertEqual({row.reason_code for row in plan.rows}, {"membership_unchecked"})
        self.assertFalse(any(row.removable for row in plan.rows))


class ApplyTests(RepairFixture):
    def test_safe_refresh_keeps_text_hashes_enrichment_and_every_other_record(self):
        plan_path = self.plan()
        original = self.records()

        report = self.apply(plan_path, groups=[GROUP_SAFE])

        self.assertEqual({row.attachment_key: row.outcome for row in report.rows}, {SAFE: OUTCOME_METADATA_REFRESHED, ENRICHED: OUTCOME_METADATA_REFRESHED})
        records = self.records()
        self.assertEqual(set(records), set(original))  # records from both runs survive
        for key in (SAFE, ENRICHED):
            self.assertEqual(records[key], {**original[key], "title": "New title"})
        self.assertEqual(records[ENRICHED]["extraction_tool"], "marker")
        self.assertIn("E = mc^2", records[ENRICHED]["text"])
        for key in set(original) - {SAFE, ENRICHED}:
            self.assertEqual(records[key], original[key])
        # The resulting generation and a fresh audit agree: the repaired items are current.
        statuses = self.statuses()
        self.assertEqual(statuses[SAFE], ("current",))
        self.assertEqual(statuses[ENRICHED], ("current",))

    def test_reconvert_replaces_only_selected_rows_through_the_validated_path(self):
        plan_path = self.plan()
        original = self.records()

        extractor = _Extractor()
        report = self.apply(plan_path, extractor, keys=[STALE])

        self.assertEqual(extractor.extracted, [STALE])
        self.assertEqual([(row.attachment_key, row.outcome) for row in report.rows], [(STALE, OUTCOME_PUBLISHED)])
        records = self.records()
        self.assertIn(f"Fresh body {STALE}", records[STALE]["text"])
        self.assertEqual(records[STALE]["source_sha256"], _sha(self.pdfs[STALE]))
        self.assertEqual(records[CHANGED], original[CHANGED])
        self.assertEqual(self.statuses()[STALE], ("current",))
        # The hand-edited Markdown the old record pointed at is left where it was.
        self.assertTrue(self.markdown[STALE].is_file())

        report = self.apply(plan_path, groups=[GROUP_RECONVERT])
        outcomes = {row.attachment_key: row.outcome for row in report.rows}
        self.assertEqual(outcomes, {STALE: OUTCOME_ALREADY_RESOLVED, CHANGED: OUTCOME_PUBLISHED})
        record = self.records()[CHANGED]
        self.assertEqual(record["source_sha256"], _sha(self.pdfs[CHANGED]))
        self.assertEqual(record["title"], "Renamed")
        self.assertEqual(self.statuses()[CHANGED], ("current",))

    def test_unsafe_actions_are_rejected_and_nothing_is_published(self):
        plan_path = self.plan()
        before = self.generation()
        with self.assertRaises(IndexRepairError):
            self.apply(plan_path)

        report = self.apply(
            plan_path,
            keys=[MISSING_PDF, MISSING_MD, UNVERIFIED, MANUAL, ORPHAN, "ZZZZ9999"],
            remove_keys=[MISSING_PDF, MISSING_MD, UNVERIFIED, CURRENT[0]],
        )

        outcomes = [(row.attachment_key, row.outcome) for row in report.rows]
        for key in (MISSING_PDF, MISSING_MD, UNVERIFIED, MANUAL, ORPHAN):
            self.assertIn((key, OUTCOME_NOT_ELIGIBLE), outcomes)
        self.assertIn(("ZZZZ9999", OUTCOME_NOT_IN_PLAN), outcomes)
        self.assertIn((CURRENT[0], OUTCOME_NOT_IN_PLAN), outcomes)
        self.assertEqual(sum(1 for _key, outcome in outcomes if outcome == OUTCOME_NOT_ELIGIBLE), 8)
        self.assertIn("--remove-keys", next(row.reason for row in report.rows if row.attachment_key == ORPHAN))
        self.assertIsNone(report.generation_id)
        self.assertEqual(self.generation(), before)
        with self.assertRaises(ValueError):
            self.apply(plan_path, groups=[GROUP_REVIEW])

    def test_orphan_removal_needs_an_explicit_key_and_zotero_confirmation(self):
        plan_path = self.plan()
        before = self.generation()

        self.write_inventory(with_orphan=True)  # Zotero lists it again after planning
        report = self.apply(plan_path, remove_keys=[ORPHAN])
        self.assertEqual([row.outcome for row in report.rows], [OUTCOME_ZOTERO_CHANGED])
        self.assertEqual(self.generation(), before)

        self.write_inventory(with_orphan=False)
        report = self.apply(plan_path, remove_keys=[ORPHAN])
        self.assertEqual([row.outcome for row in report.rows], [OUTCOME_REMOVED])
        records = self.records()
        self.assertNotIn(ORPHAN, records)
        self.assertEqual(len(records), len(ALL_KEYS) - 1)
        self.assertTrue(self.markdown[ORPHAN].is_file())  # only the index record goes
        self.assertNotIn("orphaned_index", self.statuses().get(ORPHAN, ()))

    def test_zotero_or_record_changes_after_planning_are_refused_per_row(self):
        plan_path = self.plan()
        _set_zotero_title(self.config, SAFE, "Retitled again")
        seed = self.root / "seed2.jsonl"
        records = self.records()
        records[STALE]["markdown_sha256"] = "changed elsewhere"
        seed.write_text("".join(json.dumps(record) + "\n" for record in records.values()), encoding="utf-8")
        stage_and_publish(self.index_root, write_jsonl_from_existing(seed), command="test")

        extractor = _Extractor()
        report = self.apply(plan_path, extractor, groups=[GROUP_SAFE, GROUP_RECONVERT])

        outcomes = {row.attachment_key: row.outcome for row in report.rows}
        self.assertEqual(
            outcomes,
            {
                SAFE: OUTCOME_ZOTERO_CHANGED,
                ENRICHED: OUTCOME_METADATA_REFRESHED,
                STALE: OUTCOME_PLAN_STALE,
                CHANGED: OUTCOME_PUBLISHED,
            },
        )
        self.assertEqual(extractor.extracted, [CHANGED])
        self.assertEqual(self.records()[SAFE]["title"], "A title")

    def test_repeated_runs_are_idempotent_and_the_audit_agrees(self):
        plan_path = self.plan()
        report = self.apply(plan_path, groups=[GROUP_SAFE, GROUP_RECONVERT], remove_keys=[ORPHAN])
        self.assertIsNotNone(report.generation_id)
        after = self.generation()
        records = self.records()

        again = self.apply(plan_path, groups=[GROUP_SAFE, GROUP_RECONVERT], remove_keys=[ORPHAN])

        self.assertEqual({row.outcome for row in again.rows}, {OUTCOME_ALREADY_RESOLVED})
        self.assertEqual(len(again.rows), 5)
        self.assertIsNone(again.generation_id)
        self.assertEqual(self.generation(), after)
        self.assertEqual(self.records(), records)
        log = (plan_path.parent / index_repair.APPLY_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(log), 2)

        # A fresh plan finds nothing left to apply; only the human decisions remain.
        fresh = build_plan(self.config, self.snapshot, plan_id="again")
        self.assertEqual(fresh.counts[GROUP_SAFE] + fresh.counts[GROUP_RECONVERT], 0)
        self.assertEqual({row.attachment_key for row in fresh.rows}, {MISSING_PDF, MISSING_MD, UNVERIFIED, MANUAL})
        statuses = self.statuses()
        for key in (SAFE, ENRICHED, STALE, CHANGED, *CURRENT):
            self.assertEqual(statuses[key], ("current",))

    def test_interrupted_publication_leaves_the_previous_generation_current(self):
        plan_path = self.plan()
        before = self.generation()
        original = self.records()

        with patch("zotero_pdf_text.artifacts.build_fts_index", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan_path, groups=[GROUP_SAFE])
        self.assertEqual(self.generation(), before)
        self.assertEqual(self.records(), original)
        self.assertFalse((self.config.output_root / ".pipeline.lock").exists())

        # Interrupted after the new generation was staged, before the pointer swap.
        with patch("zotero_pdf_text.artifacts.publish_generation", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan_path, groups=[GROUP_SAFE])
        self.assertEqual(self.generation(), before)
        self.assertEqual(self.records(), original)

        report = self.apply(plan_path, groups=[GROUP_SAFE])
        self.assertEqual({row.outcome for row in report.rows}, {OUTCOME_METADATA_REFRESHED})
        self.assertNotEqual(self.generation(), before)

    def test_plan_outside_output_root_is_refused(self):
        plan = build_plan(self.config, self.snapshot, plan_id="outside")
        plan_path = write_plan(plan, self.root / "elsewhere")
        with self.assertRaises(IndexRepairError):
            apply_plan(self.config, plan_path, groups=[GROUP_SAFE])


class WriterTests(unittest.TestCase):
    def test_writer_refuses_missing_or_conflicting_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current.jsonl"
            current.write_text(json.dumps(_index_record("AAAA1111")) + "\n", encoding="utf-8")
            record = TextIndexRecord(**{k: v for k, v in _index_record("AAAA1111").items()})
            with self.assertRaises(ValueError):
                write_jsonl_applying_repairs(current, replacements={"AAAA1111": record}, removals={"AAAA1111"})
            writer = write_jsonl_applying_repairs(current, metadata_updates={"BBBB2222": {"title": "x"}})
            with self.assertRaises(ArtifactError):
                writer(Path(tmp) / "out.jsonl")


class CliTests(RepairFixture):
    def test_plan_then_apply_from_the_command_line(self):
        config_path = self.root / "config.json"
        config_path.write_text(
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

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["plan-index-repair", "--config", str(config_path), "--mapping-report", str(self.snapshot)])
        self.assertEqual(code, 0)
        self.assertIn("Index repair plan written:", output.getvalue())
        self.assertIn("orphaned_index", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["plan-index-repair", "--config", str(config_path), "--mapping-report", str(self.snapshot), "--json"])
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["counts"], {GROUP_SAFE: 2, GROUP_RECONVERT: 2, GROUP_REVIEW: 5})
        self.assertNotIn("rows", summary)

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["apply-index-repair", "--config", str(config_path), "--plan", summary["plan_path"]]), 2)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["apply-index-repair", "--config", str(config_path), "--plan", summary["plan_path"], "--group", "safe"])
        self.assertEqual(code, 0)
        self.assertIn("Published generation:", output.getvalue())
        self.assertEqual(self.records()[SAFE]["title"], "New title")


if __name__ == "__main__":
    unittest.main()
