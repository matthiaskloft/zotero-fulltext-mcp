import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from zotero_pdf_text.orphan_candidates import (
    OrphanCandidate,
    append_master_candidates,
    find_candidate,
    list_candidates,
    mark_status,
)


def _candidate(
    orphan_sha256: str = "SHA256VALUE",
    candidate_parent_key: str = "PARENTKEY",
    title_score: int = 90,
) -> OrphanCandidate:
    return OrphanCandidate(
        orphan_source_path="/path/to/1-s2.0-generic-main.pdf",
        orphan_sha256=orphan_sha256,
        orphan_safe_folder_id=f"sha256_{orphan_sha256}",
        orphan_page_count=8,
        candidate_parent_key=candidate_parent_key,
        candidate_item_type="journalArticle",
        candidate_title="A Generically Named Paper",
        candidate_creators="Jane Smith",
        candidate_year="2024",
        candidate_doi="10.1000/orphan",
        candidate_citation_key="smithGeneric2024",
        candidate_had_stale_attachment=False,
        title_score=title_score,
        author_evidence=True,
        year_evidence=True,
        observed_dois="10.1000/orphan",
        confidence_tier="high",
        identity_rule="title_author_or_year",
        detected_at=datetime.now().isoformat(timespec="seconds"),
    )


class MatchKeyTests(unittest.TestCase):
    def test_match_key_combines_orphan_and_candidate(self):
        candidate = _candidate("SHA1", "PARENT1")
        self.assertEqual(candidate.match_key, "SHA1:PARENT1")


class AppendMasterCandidatesTests(unittest.TestCase):
    def test_new_pairing_becomes_pending_with_occurrence_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])

            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "pending")
            self.assertEqual(records[0]["occurrence_count"], 1)
            self.assertEqual(records[0]["first_detected_at"], records[0]["last_detected_at"])

    def test_existing_pending_pairing_increments_and_preserves_first_detected_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            first_record = json.loads(master_path.read_text(encoding="utf-8").splitlines()[0])

            append_master_candidates(master_path, [_candidate(title_score=95)])
            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["occurrence_count"], 2)
            self.assertEqual(records[0]["first_detected_at"], first_record["first_detected_at"])
            self.assertEqual(records[0]["title_score"], 95)

    def test_skipped_pairing_is_left_untouched_by_a_later_automatic_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            mark_status(master_path, "SHA256VALUE:PARENTKEY", status="skipped", extra_fields={"skip_reason": "not a match"})

            append_master_candidates(master_path, [_candidate(title_score=99)])
            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "skipped")
            self.assertEqual(records[0]["title_score"], 90)  # unchanged, not reopened

    def test_resolved_pairing_is_left_untouched_by_a_later_automatic_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            mark_status(master_path, "SHA256VALUE:PARENTKEY", status="resolved", extra_fields={"resolved_via": "link-pdf"})

            append_master_candidates(master_path, [_candidate(title_score=99)])
            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "resolved")

    def test_empty_candidate_list_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [])
            self.assertFalse(master_path.exists())

    def test_corrupt_line_in_master_file_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate("GOODSHA", "GOODPARENT")])
            with master_path.open("a", encoding="utf-8") as handle:
                handle.write("{not valid json\n")

            append_master_candidates(master_path, [_candidate("GOODSHA", "GOODPARENT"), _candidate("SECONDSHA", "SECONDPARENT")])
            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({r["candidate_parent_key"] for r in records}, {"GOODPARENT", "SECONDPARENT"})

    def test_non_dict_json_line_in_master_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            master_path.parent.mkdir(parents=True, exist_ok=True)
            master_path.write_text('["just a list, not a record"]\n', encoding="utf-8")

            append_master_candidates(master_path, [_candidate("GOODSHA", "GOODPARENT")])
            records = [json.loads(line) for line in master_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["candidate_parent_key"] for r in records], ["GOODPARENT"])


class FindAndListCandidatesTests(unittest.TestCase):
    def test_missing_master_file_reads_as_empty_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            self.assertEqual(list_candidates(master_path, status=None), [])

    def test_find_candidate_raises_for_unknown_match_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            with self.assertRaises(KeyError):
                find_candidate(master_path, "MISSING:MISSING")

    def test_find_candidate_returns_stored_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            record = find_candidate(master_path, "SHA256VALUE:PARENTKEY")
            self.assertEqual(record["candidate_parent_key"], "PARENTKEY")

    def test_list_candidates_filters_by_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate("SHA1", "A"), _candidate("SHA2", "B")])
            mark_status(master_path, "SHA1:A", status="skipped", extra_fields={})

            pending = list_candidates(master_path, status="pending")
            self.assertEqual([r["candidate_parent_key"] for r in pending], ["B"])

            everything = list_candidates(master_path, status=None)
            self.assertEqual(len(everything), 2)


class MarkStatusTests(unittest.TestCase):
    def test_mark_status_raises_for_unknown_match_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            with self.assertRaises(KeyError):
                mark_status(master_path, "MISSING:MISSING", status="skipped", extra_fields={})

    def test_mark_status_updates_extra_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "index" / "orphan_candidates.jsonl"
            append_master_candidates(master_path, [_candidate()])
            mark_status(
                master_path,
                "SHA256VALUE:PARENTKEY",
                status="resolved",
                extra_fields={"resolved_via": "link-pdf", "resolved_at": "now"},
            )
            record = find_candidate(master_path, "SHA256VALUE:PARENTKEY")
            self.assertEqual(record["status"], "resolved")
            self.assertEqual(record["resolved_via"], "link-pdf")


if __name__ == "__main__":
    unittest.main()
