import csv
import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.verifier import apply_verification, review_unverified_manifest


class VerifierTests(unittest.TestCase):
    def test_review_accepts_exact_doi_from_markdown_fulltext(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "paper.md"
            markdown.write_text(
                '---\ntitle: "Title"\n---\n\n'
                "# Title\n\nJane Smith 2024. DOI: https://doi.org/10.1000/ABC.",
                encoding="utf-8",
            )
            manifest = root / "manifest.csv"
            _write_manifest(manifest, markdown, doi="10.1000/abc")

            reviews = review_unverified_manifest(manifest, run_dir=root, agent_batch_size=10)

            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0].decision, "accept")
            self.assertEqual(reviews[0].review_rule, "auto_accept_doi_exact")
            self.assertIn("doi", reviews[0].matched_fields)
            self.assertTrue((root / "review.jsonl").exists())
            self.assertTrue((root / "review.csv").exists())

    def test_review_rejects_conflicting_doi_with_weak_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "paper.md"
            markdown.write_text(
                "# Completely Different Article\n\nDOI: 10.2000/wrong. Other content.",
                encoding="utf-8",
            )
            manifest = root / "manifest.csv"
            _write_manifest(
                manifest,
                markdown,
                title="Bayesian Psychometrics for Longitudinal Response Processes",
                doi="10.1000/right",
            )

            reviews = review_unverified_manifest(manifest, run_dir=root, agent_batch_size=1)

            self.assertEqual(reviews[0].decision, "reject")
            self.assertEqual(reviews[0].review_rule, "auto_reject_conflicting_doi_low_title")
            batch_files = list((root / "agent_batches").glob("*.jsonl"))
            self.assertEqual(len(batch_files), 1)

    def test_apply_verification_combines_base_manifest_and_promoted_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_manifest = root / "base.csv"
            base_markdown = root / "base.md"
            accepted_markdown = root / "accepted.md"
            base_markdown.write_text("base", encoding="utf-8")
            accepted_markdown.write_text("accepted", encoding="utf-8")
            _write_base_manifest(base_manifest, base_markdown)
            review = root / "review.jsonl"
            review.write_text(
                json.dumps(
                    {
                        "decision": "accept",
                        "confidence": 0.96,
                        "review_rule": "agent_accept_title_author_year",
                        "zotero_parent_key": "PARENT2",
                        "zotero_attachment_key": "ATTACH2",
                        "item_type": "journalArticle",
                        "title": "Accepted",
                        "creators": "Jane Smith",
                        "year": "2024",
                        "doi": "10.1000/accepted",
                        "citation_key": "smithAccepted2024",
                        "source_path": str(root / "accepted.pdf"),
                        "markdown_path": str(accepted_markdown),
                        "extraction_tool": "pymupdf4llm.to_markdown",
                        "page_count": "7",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "decision": "manual_review",
                        "confidence": 0.5,
                        "zotero_attachment_key": "ATTACH3",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "trusted.csv"

            summary = apply_verification(review, output, base_manifest=base_manifest, min_confidence=0.92)

            self.assertEqual(summary["promoted_rows"], 1)
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["zotero_attachment_key"], "ATTACH2")
            self.assertEqual(rows[1]["classification"], "mapped_verified")
            self.assertEqual(rows[1]["identity_status"], "fulltext_verified")
            self.assertIn("fulltext_review:agent_accept_title_author_year", rows[1]["identity_rule"])

    def test_apply_verification_skips_article_level_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_manifest = root / "base.csv"
            base_markdown = root / "base.md"
            accepted_markdown = root / "accepted.md"
            base_markdown.write_text("base", encoding="utf-8")
            accepted_markdown.write_text("accepted", encoding="utf-8")
            _write_base_manifest(base_manifest, base_markdown)
            review = root / "review.jsonl"
            review.write_text(
                "\n".join(
                    [
                        json.dumps(_accepted_review(root, "ATTACH_PARENT_DUP", parent="PARENT")),
                        json.dumps(_accepted_review(root, "ATTACH_DOI_DUP", doi="https://doi.org/10.1000/base")),
                        json.dumps(_accepted_review(root, "ATTACH_CITE_DUP", citation_key="smithTitle2024")),
                        json.dumps(_accepted_review(root, "ATTACH_NEW", title="Accepted", doi="10.1000/new")),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "trusted.csv"

            summary = apply_verification(review, output, base_manifest=base_manifest)

            self.assertEqual(summary["promoted_rows"], 1)
            self.assertEqual(summary["skipped_duplicate"], 3)
            self.assertEqual(summary["skipped_duplicate_by_reason"]["parent"], 1)
            self.assertEqual(summary["skipped_duplicate_by_reason"]["doi"], 1)
            self.assertEqual(summary["skipped_duplicate_by_reason"]["citation_key"], 1)
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["zotero_attachment_key"] for row in rows], ["ATTACH", "ATTACH_NEW"])

    def test_apply_verification_allows_title_only_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_manifest = root / "base.csv"
            base_markdown = root / "base.md"
            accepted_markdown = root / "accepted.md"
            base_markdown.write_text("base", encoding="utf-8")
            accepted_markdown.write_text("accepted", encoding="utf-8")
            _write_base_manifest(base_manifest, base_markdown)
            review = root / "review.jsonl"
            review.write_text(
                json.dumps(
                    _accepted_review(
                        root,
                        "ATTACH_TITLE_ONLY",
                        parent="PARENT2",
                        title="Base",
                        doi="10.1000/volume-2",
                        citation_key="smithTitle2024b",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "trusted.csv"

            summary = apply_verification(review, output, base_manifest=base_manifest)

            self.assertEqual(summary["promoted_rows"], 1)
            self.assertEqual(summary["skipped_duplicate"], 0)


def _write_manifest(
    path: Path,
    markdown: Path,
    *,
    title: str = "Title",
    doi: str = "10.1000/test",
) -> None:
    fieldnames = [
        "status",
        "extraction_tool",
        "zotero_parent_key",
        "zotero_attachment_key",
        "item_type",
        "title",
        "creators",
        "year",
        "doi",
        "citation_key",
        "source_path",
        "output_path",
        "page_count",
        "classification",
        "identity_status",
        "identity_rule",
        "error",
    ]
    row = {
        "status": "converted",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "zotero_parent_key": "PARENT",
        "zotero_attachment_key": "ATTACH",
        "item_type": "journalArticle",
        "title": title,
        "creators": "Jane Smith",
        "year": "2024",
        "doi": doi,
        "citation_key": "smithTitle2024",
        "source_path": str(path.with_suffix(".pdf")),
        "output_path": str(markdown),
        "page_count": "3",
        "classification": "mapped_unverified",
        "identity_status": "unverified",
        "identity_rule": "insufficient_evidence",
        "error": "",
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


def _write_base_manifest(path: Path, markdown: Path) -> None:
    _write_manifest(path, markdown, title="Base", doi="10.1000/base")


def _accepted_review(
    root: Path,
    attachment: str,
    *,
    parent: str = "PARENT2",
    title: str = "Accepted",
    doi: str = "10.1000/accepted",
    citation_key: str = "smithAccepted2024",
) -> dict[str, object]:
    return {
        "decision": "accept",
        "confidence": 0.96,
        "review_rule": "agent_accept_title_author_year",
        "zotero_parent_key": parent,
        "zotero_attachment_key": attachment,
        "item_type": "journalArticle",
        "title": title,
        "creators": "Jane Smith",
        "year": "2024",
        "doi": doi,
        "citation_key": citation_key,
        "source_path": str(root / f"{attachment}.pdf"),
        "markdown_path": str(root / f"{attachment}.md"),
        "extraction_tool": "pymupdf4llm.to_markdown",
        "page_count": "7",
    }


if __name__ == "__main__":
    unittest.main()
