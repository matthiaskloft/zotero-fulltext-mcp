import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from zotero_pdf_text.timeout_candidates import (
    MAX_SUGGESTED_TIMEOUT_SECONDS,
    TimeoutCandidate,
    add_to_skip_list,
    append_master_candidates,
    find_candidate,
    list_candidates,
    mark_status,
    suggested_next_timeout,
)


def _candidate(attachment_key: str = "ATTACH", attempted_timeout_seconds: int = 600) -> TimeoutCandidate:
    return TimeoutCandidate(
        zotero_parent_key="PARENT",
        zotero_attachment_key=attachment_key,
        item_type="attachment",
        title="Title",
        creators="Jane Smith",
        year="2024",
        doi="10.1000/test",
        citation_key="smithTitle2024",
        source_path="/path/to/paper.pdf",
        page_count="500",
        classification="mapped_verified",
        identity_status="verified",
        identity_rule="doi_exact",
        safe_folder_id="zotero_ATTACH",
        drawing_density=12.5,
        attempted_timeout_seconds=attempted_timeout_seconds,
        suggested_next_timeout_seconds=suggested_next_timeout(attempted_timeout_seconds),
        fallback_outcome="fallback_used",
        conversion_status="converted",
        detected_at=datetime.now().isoformat(timespec="seconds"),
    )


class SuggestedNextTimeoutTests(unittest.TestCase):
    def test_doubles_below_cap(self):
        self.assertEqual(suggested_next_timeout(600), 1200)
        self.assertEqual(suggested_next_timeout(3600), 7200)

    def test_capped_at_max(self):
        self.assertEqual(suggested_next_timeout(20000), MAX_SUGGESTED_TIMEOUT_SECONDS)
        self.assertEqual(suggested_next_timeout(100000), MAX_SUGGESTED_TIMEOUT_SECONDS)


class AppendMasterCandidatesTests(unittest.TestCase):
    def test_new_key_becomes_pending_with_occurrence_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])

            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "pending")
            self.assertEqual(records[0]["occurrence_count"], 1)
            self.assertEqual(records[0]["first_detected_at"], records[0]["last_detected_at"])

    def test_existing_pending_key_increments_and_preserves_first_detected_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            first_record = json.loads(master_path.read_text(encoding="utf-8").splitlines()[0])

            append_master_candidates(master_path, [_candidate(attempted_timeout_seconds=1200)])
            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["occurrence_count"], 2)
            self.assertEqual(records[0]["first_detected_at"], first_record["first_detected_at"])
            self.assertEqual(records[0]["attempted_timeout_seconds"], 1200)

    def test_skipped_key_is_left_untouched_by_a_later_automatic_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            mark_status(master_path, "ATTACH", status="skipped", extra_fields={"skip_reason": "too slow"})

            append_master_candidates(master_path, [_candidate(attempted_timeout_seconds=9999)])
            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "skipped")
            self.assertEqual(records[0]["attempted_timeout_seconds"], 600)  # unchanged, not reopened

    def test_resolved_key_is_left_untouched_by_a_later_automatic_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            mark_status(master_path, "ATTACH", status="resolved", extra_fields={"resolved_via": "retry"})

            append_master_candidates(master_path, [_candidate(attempted_timeout_seconds=9999)])
            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "resolved")

    def test_empty_candidate_list_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [])
            self.assertFalse(master_path.exists())


class FindAndListCandidatesTests(unittest.TestCase):
    def test_find_candidate_raises_for_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            with self.assertRaises(KeyError):
                find_candidate(master_path, "MISSING")

    def test_find_candidate_returns_stored_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            record = find_candidate(master_path, "ATTACH")
            self.assertEqual(record["zotero_attachment_key"], "ATTACH")

    def test_list_candidates_filters_by_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate("A"), _candidate("B")])
            mark_status(master_path, "A", status="skipped", extra_fields={})

            pending = list_candidates(master_path, status="pending")
            self.assertEqual([r["zotero_attachment_key"] for r in pending], ["B"])

            everything = list_candidates(master_path, status=None)
            self.assertEqual(len(everything), 2)


class MarkStatusTests(unittest.TestCase):
    def test_mark_status_raises_for_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            with self.assertRaises(KeyError):
                mark_status(master_path, "MISSING", status="skipped", extra_fields={})

    def test_mark_status_updates_extra_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "timeout_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            mark_status(master_path, "ATTACH", status="resolved", extra_fields={"resolved_via": "retry", "resolved_at": "now"})
            record = find_candidate(master_path, "ATTACH")
            self.assertEqual(record["status"], "resolved")
            self.assertEqual(record["resolved_via"], "retry")


class SkipListTests(unittest.TestCase):
    def test_add_to_skip_list_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            skip_list_path = Path(tmp) / "timeout_skip_list.json"
            add_to_skip_list(skip_list_path, "ATTACH", reason="too slow", title="A Book", citation_key="smith2024")

            data = json.loads(skip_list_path.read_text(encoding="utf-8"))
            self.assertEqual(data["entries"]["ATTACH"]["reason"], "too slow")
            self.assertEqual(data["entries"]["ATTACH"]["title"], "A Book")
            self.assertIn("added_at", data["entries"]["ATTACH"])

    def test_add_to_skip_list_preserves_existing_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            skip_list_path = Path(tmp) / "timeout_skip_list.json"
            add_to_skip_list(skip_list_path, "FIRST", reason="reason one")
            add_to_skip_list(skip_list_path, "SECOND", reason="reason two")

            data = json.loads(skip_list_path.read_text(encoding="utf-8"))
            self.assertEqual(set(data["entries"].keys()), {"FIRST", "SECOND"})

    def test_add_to_skip_list_leaves_no_stray_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            skip_list_path = Path(tmp) / "timeout_skip_list.json"
            add_to_skip_list(skip_list_path, "ATTACH", reason="too slow")
            leftover = list(Path(tmp).glob(".*.tmp-*"))
            self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
