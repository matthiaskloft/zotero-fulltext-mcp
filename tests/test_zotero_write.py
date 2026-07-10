import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from zotero_pdf_text.bibtex import JavaScriptResult
from zotero_pdf_text.cli import main
from zotero_pdf_text.ingestion import ExistingItem
from zotero_pdf_text.zotero_write import (
    WritePlanRecord,
    apply_write_plan,
    approve_write_plan_rows,
    build_write_plan,
    generate_zotero_javascript,
    load_write_plan,
    validate_write_plan,
    write_plan,
)


class ZoteroWriteTests(unittest.TestCase):
    def test_build_write_plan_creates_pending_item_with_linked_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            candidates = root / "candidates.jsonl"
            candidates.write_text(
                json.dumps(
                    {
                        "doi": "10.1000/new",
                        "title": "New Paper",
                        "authors": "Jane Smith; John Doe",
                        "year": "2026",
                        "pdf_path": str(pdf),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "write_plan.jsonl"
            db = root / "zotero.sqlite"
            db.write_bytes(b"snapshot")

            with patch("zotero_pdf_text.zotero_write.load_existing_items", return_value=[]):
                records = build_write_plan(candidates, db, output)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].operation, "create_item_with_linked_pdf")
            self.assertEqual(records[0].approval_status, "pending")
            self.assertEqual(records[0].risk_level, "medium")
            self.assertEqual(load_write_plan(output)[0].candidate.title, "New Paper")

    def test_build_write_plan_keeps_duplicate_and_review_as_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = root / "candidates.jsonl"
            candidates.write_text(
                "\n".join(
                    [
                        json.dumps({"doi": "10.1000/existing", "title": "Existing Paper", "year": "2024"}),
                        json.dumps({"title": "Ambiguous Paper"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            existing = [
                ExistingItem("PARENT1", "Existing Paper", "10.1000/existing", "2024", ""),
                ExistingItem("PARENT2", "Ambiguous Paper", "", "2023", ""),
            ]
            db = root / "zotero.sqlite"
            db.write_bytes(b"snapshot")

            with patch("zotero_pdf_text.zotero_write.load_existing_items", return_value=existing):
                records = build_write_plan(candidates, db, root / "write_plan.jsonl")

            self.assertEqual([record.operation for record in records], ["no_op", "no_op"])
            self.assertEqual(records[0].dedupe["action"], "skip_existing")
            self.assertEqual(records[1].dedupe["action"], "needs_review")

    def test_build_write_plan_can_create_item_and_find_available_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = root / "candidates.jsonl"
            candidates.write_text(
                json.dumps(
                    {
                        "doi": "10.1000/find-pdf",
                        "title": "Find PDF Paper",
                        "pdf_strategy": "find_available_pdf",
                        "zotmoov_expected": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            db = root / "zotero.sqlite"
            db.write_bytes(b"snapshot")

            with patch("zotero_pdf_text.zotero_write.load_existing_items", return_value=[]):
                records = build_write_plan(candidates, db, root / "write_plan.jsonl")

            self.assertEqual(records[0].operation, "create_item_and_find_pdf")
            self.assertEqual(records[0].pdf_strategy, "find_available_pdf")
            self.assertEqual(records[0].metadata_strategy, "zotero_identifier")
            self.assertTrue(records[0].zotmoov_expected)

    def test_build_write_plan_can_find_pdf_for_existing_exact_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = root / "candidates.jsonl"
            candidates.write_text(
                json.dumps(
                    {
                        "zotero_parent_key": "ABC123",
                        "pdf_strategy": "find_available_pdf",
                        "title": "Existing Item",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            db = root / "zotero.sqlite"
            db.write_bytes(b"snapshot")

            with patch("zotero_pdf_text.zotero_write.load_existing_items", return_value=[]):
                records = build_write_plan(candidates, db, root / "write_plan.jsonl")

            self.assertEqual(records[0].operation, "find_pdf_for_item")
            self.assertEqual(records[0].target["zotero_parent_key"], "ABC123")

    def test_validate_requires_existing_pdf_and_approval_for_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "write_plan.jsonl"
            write_plan(
                plan,
                [
                    WritePlanRecord(
                        operation="create_item_with_linked_pdf",
                        approval_status="pending",
                        risk_level="medium",
                        candidate=_candidate(title="New Paper", pdf_path=str(root / "missing.pdf")),
                        dedupe={"action": "add_candidate"},
                    )
                ],
            )

            validation = validate_write_plan(plan, require_approved=True)

            self.assertFalse(validation.ok)
            self.assertTrue(any("approval_status='approved'" in error for error in validation.errors))
            self.assertTrue(any("PDF path does not exist" in error for error in validation.errors))

    def test_apply_requires_approve_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "write_plan.jsonl"
            write_plan(plan, [WritePlanRecord("no_op", "not_required", "low", dedupe={"action": "skip_existing"})])

            with self.assertRaises(PermissionError):
                apply_write_plan(plan, root / "apply.js", approve=False)

    def test_approve_write_plan_rows_updates_only_selected_write_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "write_plan.jsonl"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            write_plan(
                plan,
                [
                    WritePlanRecord("no_op", "not_required", "low", dedupe={"action": "skip_existing"}),
                    WritePlanRecord(
                        "create_item_with_linked_pdf",
                        "pending",
                        "medium",
                        candidate=_candidate(title="New Paper", pdf_path=str(pdf)),
                        dedupe={"action": "add_candidate"},
                    ),
                ],
            )

            result = approve_write_plan_rows(plan, [2])
            records = load_write_plan(plan)

            self.assertEqual(result["approved_rows"], [2])
            self.assertEqual(records[0].approval_status, "not_required")
            self.assertEqual(records[1].approval_status, "approved")

    def test_approve_write_plan_rows_rejects_no_op_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "write_plan.jsonl"
            write_plan(plan, [WritePlanRecord("no_op", "not_required", "low", dedupe={"action": "skip_existing"})])

            with self.assertRaises(ValueError):
                approve_write_plan_rows(plan, [1])

    def test_validate_trash_requires_exact_key_and_destructive_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "write_plan.jsonl"
            write_plan(
                plan,
                [
                    WritePlanRecord(
                        operation="trash_item",
                        approval_status="approved",
                        risk_level="high",
                        dedupe={"action": "manual"},
                    )
                ],
            )

            validation = validate_write_plan(plan, require_approved=True)

            self.assertFalse(validation.ok)
            self.assertTrue(any("risk_level='destructive'" in error for error in validation.errors))
            self.assertTrue(any("exact Zotero key" in error for error in validation.errors))

    def test_validate_find_pdf_requires_metadata_or_exact_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "write_plan.jsonl"
            write_plan(
                plan,
                [
                    WritePlanRecord(
                        operation="create_item_and_find_pdf",
                        approval_status="approved",
                        risk_level="medium",
                        pdf_strategy="find_available_pdf",
                        metadata_strategy="supplied_metadata",
                        dedupe={"action": "add_candidate"},
                    ),
                    WritePlanRecord(
                        operation="find_pdf_for_item",
                        approval_status="approved",
                        risk_level="medium",
                        pdf_strategy="find_available_pdf",
                        metadata_strategy="supplied_metadata",
                        dedupe={"action": "manual"},
                    ),
                ],
            )

            validation = validate_write_plan(plan, require_approved=True)

            self.assertFalse(validation.ok)
            self.assertTrue(any("candidate title or DOI" in error for error in validation.errors))
            self.assertTrue(any("target.zotero_parent_key" in error for error in validation.errors))

    def test_generate_javascript_creates_links_and_trashes_without_permanent_delete(self):
        record = WritePlanRecord(
            operation="create_item_with_linked_pdf",
            approval_status="approved",
            risk_level="medium",
            candidate=_candidate(title="New Paper", authors="Jane Smith; John Doe", pdf_path="C:\\tmp\\paper.pdf"),
            dedupe={"action": "add_candidate"},
        )
        trash = WritePlanRecord(
            operation="trash_item",
            approval_status="approved",
            risk_level="destructive",
            target={"zotero_parent_key": "ABC123"},
            dedupe={"action": "manual"},
        )

        script = generate_zotero_javascript([record, trash])

        self.assertIn("Zotero.Attachments.linkFromFile", script)
        self.assertIn("splitCreators", script)
        self.assertIn("async function findDuplicate", script)
        self.assertIn("await Zotero.Items.getAll", script)
        self.assertIn("await findDuplicate", script)
        self.assertIn("item.deleted = true", script)
        self.assertNotIn("eraseTx", script)
        self.assertIn("SKIP existing attachment", script)

    def test_generate_javascript_can_find_available_pdf_and_use_identifier_metadata(self):
        record = WritePlanRecord(
            operation="create_item_and_find_pdf",
            approval_status="approved",
            risk_level="medium",
            candidate=_candidate(doi="10.1000/find", title="Find PDF Paper"),
            dedupe={"action": "add_candidate"},
            pdf_strategy="find_available_pdf",
            metadata_strategy="zotero_identifier",
            zotmoov_expected=True,
        )

        script = generate_zotero_javascript([record])

        self.assertIn("Zotero.Attachments.addAvailablePDF", script)
        self.assertIn("createItemsFromIdentifier", script)
        self.assertIn("identifier lookup unavailable", script)
        self.assertIn("ZotMoov may move/rename", script)
        self.assertNotIn("OS.File.move", script)

    def test_cli_zotero_write_status_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "write_plan.jsonl"
            write_plan(plan, [WritePlanRecord("no_op", "not_required", "low", dedupe={"action": "skip_existing"})])

            with redirect_stdout(StringIO()):
                self.assertEqual(main(["zotero-write", "status", "--plan", str(plan)]), 0)

    def test_apply_write_plan_auto_run_via_bbt_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "write_plan.jsonl"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            write_plan(
                plan,
                [
                    WritePlanRecord(
                        "create_item_with_linked_pdf",
                        "approved",
                        "medium",
                        candidate=_candidate(title="New Paper", doi="10.1000/test", pdf_path=str(pdf)),
                        dedupe={"action": "add_candidate"},
                    )
                ],
            )
            bbt_ok = JavaScriptResult(ok=True, result="CREATED item ABC12345", error="", endpoint="http://x")
            with patch("zotero_pdf_text.zotero_write.execute_javascript", return_value=bbt_ok):
                result = apply_write_plan(plan, root / "out.js", approve=True, auto_run=True)

            self.assertTrue(result.ok)
            self.assertTrue(result.auto_run_available)
            self.assertIn("debug-bridge", result.instructions)
            self.assertIn("ABC12345", result.instructions)

    def test_apply_write_plan_falls_back_to_file_when_bbt_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "write_plan.jsonl"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            write_plan(
                plan,
                [
                    WritePlanRecord(
                        "create_item_with_linked_pdf",
                        "approved",
                        "medium",
                        candidate=_candidate(title="New Paper", doi="10.1000/test", pdf_path=str(pdf)),
                        dedupe={"action": "add_candidate"},
                    )
                ],
            )
            bbt_fail = JavaScriptResult(ok=False, result=None, error="Connection refused", endpoint="http://x")
            with patch("zotero_pdf_text.zotero_write.execute_javascript", return_value=bbt_fail):
                result = apply_write_plan(plan, root / "out.js", approve=True, auto_run=True)

            self.assertTrue(result.ok)
            self.assertFalse(result.auto_run_available)
            self.assertIn("Connection refused", result.instructions)
            self.assertTrue((root / "out.js").exists())

    def test_apply_write_plan_skips_bbt_when_auto_run_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "write_plan.jsonl"
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            write_plan(
                plan,
                [
                    WritePlanRecord(
                        "create_item_with_linked_pdf",
                        "approved",
                        "medium",
                        candidate=_candidate(title="New Paper", doi="10.1000/test", pdf_path=str(pdf)),
                        dedupe={"action": "add_candidate"},
                    )
                ],
            )
            with patch("zotero_pdf_text.zotero_write.execute_javascript") as mock_bbt:
                result = apply_write_plan(plan, root / "out.js", approve=True, auto_run=False)

            mock_bbt.assert_not_called()
            self.assertFalse(result.auto_run_available)

    def test_cli_import_doi_detects_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            db_path = root / "data" / "zotero.sqlite"
            db_path.parent.mkdir(parents=True)
            db_path.write_bytes(b"")
            config_path.write_text(
                json.dumps({
                    "zotero_root": str(root),
                    "zotero_data_directory": str(root / "data"),
                    "linked_attachments": str(root),
                    "output_root": str(root / "out"),
                }),
                encoding="utf-8",
            )
            with (
                patch("zotero_pdf_text.zotero_db.find_item_by_doi", return_value="KEY00001"),
                redirect_stdout(StringIO()) as out,
            ):
                rc = main(["import-doi", "--doi", "10.1000/dup", "--config", str(config_path)])
            self.assertEqual(rc, 0)
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "already_in_library")
            self.assertEqual(result["key"], "KEY00001")

    def test_cli_find_pdf_reports_found_attachment(self):
        from zotero_pdf_text.bibtex import FindPdfResult

        find_result = FindPdfResult(
            ok=True, key="ABCD1234", found=True, attachment_key="WXYZ5678", error="", endpoint="http://x"
        )
        with (
            patch("zotero_pdf_text.cli.find_available_pdf_for_item", return_value=find_result),
            redirect_stdout(StringIO()) as out,
        ):
            rc = main(["find-pdf", "--key", "ABCD1234"])
        self.assertEqual(rc, 0)
        result = json.loads(out.getvalue())
        self.assertTrue(result["found"])
        self.assertEqual(result["attachment_key"], "WXYZ5678")

    def test_cli_find_pdf_reports_not_found_without_error_exit(self):
        from zotero_pdf_text.bibtex import FindPdfResult

        find_result = FindPdfResult(
            ok=True, key="ABCD1234", found=False, attachment_key="", error="", endpoint="http://x"
        )
        with (
            patch("zotero_pdf_text.cli.find_available_pdf_for_item", return_value=find_result),
            redirect_stdout(StringIO()) as out,
        ):
            rc = main(["find-pdf", "--key", "ABCD1234"])
        self.assertEqual(rc, 0)
        result = json.loads(out.getvalue())
        self.assertFalse(result["found"])


def _candidate(**kwargs):
    from zotero_pdf_text.ingestion import ImportCandidate

    return ImportCandidate(**kwargs)


if __name__ == "__main__":
    unittest.main()
