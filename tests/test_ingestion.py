import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.ingestion import ExistingItem, ImportCandidate, dedupe_candidates, load_candidates


class IngestionTests(unittest.TestCase):
    def test_load_candidates_accepts_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.jsonl"
            path.write_text(
                json.dumps({"doi": "10.1000/example", "title": "Example Paper", "year": "2024"}) + "\n",
                encoding="utf-8",
            )

            candidates = load_candidates(path)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].doi, "10.1000/example")
            self.assertEqual(candidates[0].title, "Example Paper")

    def test_dedupe_candidates_marks_existing_and_new_items(self):
        existing = [
            ExistingItem(
                zotero_parent_key="PARENT",
                title="Existing Paper",
                doi="10.1000/existing",
                year="2024",
                url="",
            )
        ]
        candidates = [
            ImportCandidate(doi="https://doi.org/10.1000/existing", title="Existing Paper", year="2024"),
            ImportCandidate(doi="10.1000/new", title="New Paper", year="2025"),
        ]

        decisions = dedupe_candidates(candidates, existing)

        self.assertEqual(decisions[0].action, "skip_existing")
        self.assertEqual(decisions[0].reason, "doi_match")
        self.assertEqual(decisions[0].existing_zotero_parent_key, "PARENT")
        self.assertEqual(decisions[1].action, "add_candidate")


if __name__ == "__main__":
    unittest.main()
