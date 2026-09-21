import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.artifacts import (
    publish_generation,
    stage_generation,
    validate_generation,
    write_jsonl_from_existing,
)
from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.library import (
    ALL_STATUSES,
    ELIGIBLE_IDENTITY_STATUSES,
    METADATA_KEYS,
    STATUS_CURRENT,
    STATUS_DUPLICATE_KEY,
    STATUS_METADATA_CHANGED,
    STATUS_MISSING_MARKDOWN,
    STATUS_MISSING_SOURCE,
    STATUS_ORPHANED_INDEX,
    STATUS_SOURCE_CHANGED,
    STATUS_STALE_MARKDOWN,
    STATUS_UNINDEXED,
    STATUS_UNVERIFIED_INDEXED,
    ItemObservation,
    LibraryAuditError,
    audit_library,
    canonical_image_dir,
    canonical_markdown_path,
    classify_item,
    is_canonical_eligible,
    library_status,
    load_index_records,
    load_mapping_snapshot,
    slugify_title,
    validate_attachment_key,
)

ELIGIBLE = {"classification": "mapped_verified", "identity_status": "verified"}


def _config(root: Path) -> ProjectConfig:
    return ProjectConfig(
        zotero_root=root / "zotero",
        zotero_data_directory=root / "zotero" / "data",
        linked_attachments=root / "linked",
        output_root=root / "out",
    )


def _observation(key: str = "AAAA1111", **overrides: object) -> ItemObservation:
    """An indexed, eligible, healthy item. Tests override only the fields they are about."""
    base: dict[str, object] = {
        "in_mapping": True,
        "classification": "mapped_verified",
        "identity_status": "verified",
        "in_index": True,
        "index_row_count": 1,
        "source_exists": True,
        "markdown_exists": True,
        "markdown_sha256_current": "md-hash",
        "indexed_markdown_sha256": "md-hash",
        "source_sha256_current": "src-hash",
        "indexed_source_sha256": "src-hash",
        "mapping_metadata": {"title": "A title", "doi": "10.1000/x", "citation_key": "k"},
        "indexed_metadata": {"title": "A title", "doi": "10.1000/x", "citation_key": "k"},
    }
    base.update(overrides)
    return ItemObservation(attachment_key=key, **base)  # type: ignore[arg-type]


class CanonicalPathTests(unittest.TestCase):
    def test_markdown_path_is_keyed_on_attachment_key_with_cosmetic_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            path = canonical_markdown_path(config, "ABCD1234", "A Bayesian Review!")
            self.assertEqual(path.parent, config.output_root / "library" / "markdown")
            self.assertEqual(path.name, "ABCD1234--a-bayesian-review.md")

    def test_markdown_path_without_title_is_just_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            self.assertEqual(canonical_markdown_path(config, "ABCD1234").name, "ABCD1234.md")

    def test_image_dir_ignores_title_so_a_retitle_cannot_orphan_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            first = canonical_image_dir(config, "ABCD1234")
            self.assertEqual(first, config.output_root / "library" / "images" / "ABCD1234")
            # The image directory is derived from the key alone, never from the Markdown
            # filename, so renaming the item leaves the images exactly where they were.
            retitled = canonical_markdown_path(config, "ABCD1234", "A Totally Different Title")
            self.assertNotEqual(retitled.stem, "ABCD1234")
            self.assertEqual(canonical_image_dir(config, "ABCD1234"), first)

    def test_title_never_changes_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            for title in ("One title", "Another title", ""):
                self.assertTrue(
                    canonical_markdown_path(config, "ABCD1234", title).name.startswith("ABCD1234")
                )

    def test_invalid_attachment_keys_are_rejected_before_any_path_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            for bad in ("../etc", "ABCD/1234", "", "abcd1234", "TOOLONG12", "SHORT"):
                with self.subTest(key=bad):
                    with self.assertRaises(ValueError):
                        canonical_markdown_path(config, bad)
                    with self.assertRaises(ValueError):
                        canonical_image_dir(config, bad)

    def test_validate_attachment_key_returns_the_trimmed_key(self):
        self.assertEqual(validate_attachment_key("  ABCD1234 "), "ABCD1234")

    def test_slugify_is_ascii_only_and_bounded(self):
        self.assertEqual(slugify_title("Ünïcödé Títle"), "unicode-title")
        self.assertEqual(slugify_title(""), "")
        self.assertLessEqual(len(slugify_title("word " * 100)), 60)
        self.assertFalse(slugify_title("word " * 100).endswith("-"))


class EligibilityTests(unittest.TestCase):
    def test_requires_both_classification_and_identity(self):
        for identity in sorted(ELIGIBLE_IDENTITY_STATUSES):
            with self.subTest(identity=identity):
                self.assertTrue(
                    is_canonical_eligible(
                        {"classification": "mapped_verified", "identity_status": identity}
                    )
                )
        self.assertFalse(
            is_canonical_eligible(
                {"classification": "mapped_unverified", "identity_status": "verified"}
            )
        )
        self.assertFalse(
            is_canonical_eligible(
                {"classification": "mapped_verified", "identity_status": "unverified"}
            )
        )

    def test_accepts_objects_as_well_as_dicts(self):
        self.assertTrue(is_canonical_eligible(_observation()))
        self.assertFalse(
            is_canonical_eligible(_observation(identity_status="unverified"))
        )


class ClassifyItemTests(unittest.TestCase):
    def assertStatuses(self, observation: ItemObservation, expected: set[str]) -> None:
        self.assertEqual(set(classify_item(observation)), expected)

    # --- the exclusive status -------------------------------------------------------------

    def test_healthy_item_is_current(self):
        self.assertStatuses(_observation(), {STATUS_CURRENT})

    def test_current_never_coexists_with_a_problem_status(self):
        problems = [
            _observation(index_row_count=2),
            _observation(markdown_sha256_current="other"),
            _observation(source_sha256_current="other"),
            _observation(source_exists=False),
            _observation(markdown_exists=False),
            _observation(indexed_metadata={"title": "Changed", "doi": "", "citation_key": ""}),
        ]
        for index, observation in enumerate(problems):
            with self.subTest(case=index):
                statuses = classify_item(observation)
                self.assertNotIn(STATUS_CURRENT, statuses)
                self.assertTrue(statuses, "expected at least one problem status")

    # --- structural facts -----------------------------------------------------------------

    def test_duplicate_key(self):
        self.assertStatuses(_observation(index_row_count=2), {STATUS_DUPLICATE_KEY})

    def test_orphaned_index_row(self):
        self.assertStatuses(
            _observation(in_mapping=False, mapping_metadata={}, indexed_metadata={}),
            {STATUS_ORPHANED_INDEX},
        )

    def test_missing_source_and_missing_markdown(self):
        self.assertStatuses(
            _observation(source_exists=False, source_sha256_current=None),
            {STATUS_MISSING_SOURCE},
        )
        self.assertStatuses(
            _observation(markdown_exists=False, markdown_sha256_current=None),
            {STATUS_MISSING_MARKDOWN},
        )

    def test_unknown_existence_is_never_reported_as_missing(self):
        """`None` means 'not checked' and must not be conflated with `False`.

        An item with no known source path (an index row that records none) has source_exists
        None. A falsy test would report it as missing_source, asserting a file is gone when the
        audit never looked for one.
        """
        for field in ("source_exists", "markdown_exists"):
            with self.subTest(field=field):
                statuses = classify_item(_observation(**{field: None}))
                self.assertNotIn(STATUS_MISSING_SOURCE, statuses)
                self.assertNotIn(STATUS_MISSING_MARKDOWN, statuses)

    def test_unknown_existence_is_not_missing(self):
        """None means 'not checked' and must never be read as False.

        An unindexed item has no indexed markdown_path to check, so treating None as missing
        would report every unindexed item twice and inflate missing_markdown library-wide.
        """
        self.assertStatuses(
            _observation(
                in_index=False,
                index_row_count=0,
                markdown_exists=None,
                markdown_sha256_current=None,
                indexed_markdown_sha256="",
                indexed_source_sha256="",
                indexed_metadata={},
            ),
            {STATUS_UNINDEXED},
        )

    # --- comparisons ----------------------------------------------------------------------

    def test_stale_markdown_when_disk_differs_from_index(self):
        self.assertStatuses(
            _observation(markdown_sha256_current="disk-hash"), {STATUS_STALE_MARKDOWN}
        )

    def test_source_changed_when_both_hashes_known_and_differ(self):
        self.assertStatuses(
            _observation(source_sha256_current="new", indexed_source_sha256="old"),
            {STATUS_SOURCE_CHANGED},
        )

    def test_absent_hashes_never_report_drift(self):
        """Absence is not inequality.

        Without a full audit source_sha256_current is None, and records converted before the
        provenance field existed carry indexed_source_sha256 == "". An unguarded != would report
        every such item as source_changed -- a library-wide false positive on the one status
        meant to justify a high-risk migration.
        """
        cases = [
            ("no full audit", {"source_sha256_current": None}),
            ("pre-upgrade record", {"indexed_source_sha256": ""}),
            ("neither known", {"source_sha256_current": None, "indexed_source_sha256": ""}),
        ]
        for name, overrides in cases:
            with self.subTest(case=name):
                self.assertStatuses(_observation(**overrides), {STATUS_CURRENT})

    def test_missing_source_cannot_also_be_source_changed(self):
        """Falls out of the evidence model rather than needing a suppression rule."""
        self.assertStatuses(
            _observation(
                source_exists=False,
                source_sha256_current=None,
                indexed_source_sha256="old",
            ),
            {STATUS_MISSING_SOURCE},
        )

    def test_metadata_changed_fires_in_both_directions(self):
        enriched = _observation(
            mapping_metadata={"title": "T", "doi": "10.1000/x", "citation_key": "k"},
            indexed_metadata={"title": "T", "doi": "", "citation_key": "k"},
        )
        removed = _observation(
            mapping_metadata={"title": "T", "doi": "", "citation_key": "k"},
            indexed_metadata={"title": "T", "doi": "10.1000/x", "citation_key": "k"},
        )
        self.assertStatuses(enriched, {STATUS_METADATA_CHANGED})
        self.assertStatuses(removed, {STATUS_METADATA_CHANGED})

    def test_metadata_comparison_ignores_creators_and_year(self):
        """Routine Zotero tidying must not light up the library as drifted."""
        self.assertNotIn("creators", METADATA_KEYS)
        self.assertNotIn("year", METADATA_KEYS)

    def test_metadata_changed_needs_both_sides(self):
        self.assertNotIn(
            STATUS_METADATA_CHANGED,
            classify_item(
                _observation(
                    in_index=False,
                    index_row_count=0,
                    markdown_exists=None,
                    markdown_sha256_current=None,
                    indexed_markdown_sha256="",
                    indexed_source_sha256="",
                    indexed_metadata={},
                )
            ),
        )

    # --- eligibility ----------------------------------------------------------------------

    def test_eligible_but_not_indexed_is_unindexed(self):
        self.assertStatuses(
            _observation(
                in_index=False,
                index_row_count=0,
                markdown_exists=None,
                markdown_sha256_current=None,
                indexed_markdown_sha256="",
                indexed_source_sha256="",
                indexed_metadata={},
            ),
            {STATUS_UNINDEXED},
        )

    def test_ineligible_and_not_indexed_reports_nothing(self):
        """Quarantine is the correct state, not a gap to be worked.

        Reporting these as unindexed would manufacture a backlog that should never be actioned --
        the exact false signal that would wrongly push the gated migration forward.
        """
        self.assertStatuses(
            _observation(
                identity_status="unverified",
                in_index=False,
                index_row_count=0,
                markdown_exists=None,
                markdown_sha256_current=None,
                indexed_markdown_sha256="",
                indexed_source_sha256="",
                indexed_metadata={},
            ),
            set(),
        )

    def test_ineligible_but_indexed_is_unverified_indexed(self):
        self.assertStatuses(
            _observation(identity_status="unverified"), {STATUS_UNVERIFIED_INDEXED}
        )

    def test_unverified_indexed_is_distinct_from_orphaned_index(self):
        """They name different repairs: verify the identity vs. drop the row."""
        unverified = classify_item(_observation(identity_status="unverified"))
        orphaned = classify_item(
            _observation(in_mapping=False, mapping_metadata={}, indexed_metadata={})
        )
        self.assertIn(STATUS_UNVERIFIED_INDEXED, unverified)
        self.assertNotIn(STATUS_ORPHANED_INDEX, unverified)
        self.assertIn(STATUS_ORPHANED_INDEX, orphaned)
        self.assertNotIn(STATUS_UNVERIFIED_INDEXED, orphaned)

    def test_ineligible_items_still_report_structural_facts(self):
        statuses = classify_item(
            _observation(identity_status="unverified", index_row_count=2, source_exists=False)
        )
        self.assertIn(STATUS_DUPLICATE_KEY, statuses)
        self.assertIn(STATUS_MISSING_SOURCE, statuses)

    # --- the set, as a set ------------------------------------------------------------------

    def test_statuses_accumulate_rather_than_taking_precedence(self):
        statuses = classify_item(
            _observation(
                index_row_count=2,
                markdown_sha256_current="disk",
                source_sha256_current="new",
                indexed_source_sha256="old",
                indexed_metadata={"title": "Old", "doi": "", "citation_key": ""},
            )
        )
        self.assertEqual(
            statuses,
            frozenset(
                {
                    STATUS_DUPLICATE_KEY,
                    STATUS_STALE_MARKDOWN,
                    STATUS_SOURCE_CHANGED,
                    STATUS_METADATA_CHANGED,
                }
            ),
        )

    def test_every_returned_status_is_declared(self):
        observations = [
            _observation(),
            _observation(index_row_count=2),
            _observation(in_mapping=False),
            _observation(identity_status="unverified"),
            _observation(source_exists=False),
            _observation(markdown_exists=False),
            _observation(in_index=False, index_row_count=0),
        ]
        for observation in observations:
            self.assertLessEqual(set(classify_item(observation)), set(ALL_STATUSES))


class SnapshotLoadingTests(unittest.TestCase):
    def test_missing_snapshot_names_the_recovery_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LibraryAuditError) as ctx:
                load_mapping_snapshot(Path(tmp) / "nope.jsonl")
            self.assertIn("dry-run", str(ctx.exception))

    def test_accepts_a_run_directory_or_the_file_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            path = run_dir / "mapping_report.jsonl"
            path.write_text(
                json.dumps({"zotero_attachment_key": "AAAA1111", "title": "T"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(set(load_mapping_snapshot(run_dir)), {"AAAA1111"})
            self.assertEqual(set(load_mapping_snapshot(path)), {"AAAA1111"})

    def test_rows_without_an_attachment_key_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.jsonl"
            path.write_text(
                json.dumps({"zotero_attachment_key": "AAAA1111"})
                + "\n"
                + json.dumps({"classification": "orphan", "source_path": "x.pdf"})
                + "\n\n",
                encoding="utf-8",
            )
            self.assertEqual(set(load_mapping_snapshot(path)), {"AAAA1111"})

    def test_malformed_json_names_the_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.jsonl"
            path.write_text('{"zotero_attachment_key": "A"}\nnot json\n', encoding="utf-8")
            with self.assertRaises(LibraryAuditError) as ctx:
                load_mapping_snapshot(path)
            self.assertIn(":2:", str(ctx.exception))

    def test_unpublished_index_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            generation_id, rows = load_index_records(Path(tmp) / "index")
            self.assertIsNone(generation_id)
            self.assertEqual(rows, {})


def _attachment_record(attachment_key: str, **overrides: object):
    """A Zotero attachment whose linked PDF is not on disk."""
    from zotero_pdf_text.zotero_db import AttachmentRecord

    fields: dict[str, object] = {
        "attachment_item_id": 1,
        "attachment_key": attachment_key,
        "parent_item_id": 2,
        "parent_key": f"P{attachment_key}",
        "link_mode": 2,
        "content_type": "application/pdf",
        "zotero_path": f"attachments:{attachment_key}.pdf",
        "item_type": "journalArticle",
        "title": "A title",
        "doi": "10.1000/x",
        "citation_key": f"key-{attachment_key}",
        "year": "2024",
        "venue": "A journal",
        "creators": ["Jane Smith"],
        "creator_surnames": ["Smith"],
    }
    fields.update(overrides)
    return AttachmentRecord(**fields)  # type: ignore[arg-type]


def _index_record(attachment_key: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "zotero_parent_key": f"P{attachment_key}",
        "zotero_attachment_key": attachment_key,
        "title": "A title",
        "creators": "Jane Smith",
        "year": "2024",
        "doi": "10.1000/x",
        "citation_key": f"key-{attachment_key}",
        "source_path": f"{attachment_key}.pdf",
        "markdown_path": f"{attachment_key}.md",
        "markdown_sha256": "md-hash",
        "extraction_tool": "pymupdf4llm.to_markdown",
        "char_count": 11,
        "word_count": 2,
        "page_count": "2",
        "classification": "mapped_verified",
        "identity_status": "verified",
        "identity_rule": "doi_exact",
        "has_math": False,
        "source_sha256": "src-hash",
        "indexed_at": "2026-01-01T00:00:00+00:00",
        "text": "hello world",
    }
    record.update(overrides)
    return record


class AuditEndToEndTests(unittest.TestCase):
    def _publish(self, index_root: Path, records: list[dict[str, object]]) -> str:
        index_root.mkdir(parents=True, exist_ok=True)
        source = index_root / "source.jsonl"
        source.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        info = stage_generation(index_root, write_jsonl_from_existing(source), command="test")
        validate_generation(index_root, info.generation_id)
        publish_generation(index_root, info.generation_id)
        return info.generation_id

    def _snapshot(self, run_dir: Path, rows: list[dict[str, object]]) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "mapping_report.jsonl"
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        return path

    def test_audit_joins_mapping_filesystem_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            index_root = root / "index"

            source_pdf = root / "AAAA1111.pdf"
            source_pdf.write_bytes(b"pdf bytes")
            markdown = root / "AAAA1111.md"
            markdown.write_text("hello world", encoding="utf-8")

            self._publish(
                index_root,
                [
                    _index_record(
                        "AAAA1111",
                        source_path=str(source_pdf),
                        markdown_path=str(markdown),
                        markdown_sha256=_sha256_text("hello world"),
                    ),
                    # An index row Zotero no longer represents.
                    _index_record("CCCC3333"),
                ],
            )
            snapshot = self._snapshot(
                root / "runs" / "r1",
                [
                    {
                        "zotero_attachment_key": "AAAA1111",
                        "title": "A title",
                        "doi": "10.1000/x",
                        "citation_key": "key-AAAA1111",
                        "source_path": str(source_pdf),
                        "sha256": "src-hash",
                        **ELIGIBLE,
                    },
                    # Eligible, mapped, but never indexed.
                    {
                        "zotero_attachment_key": "BBBB2222",
                        "title": "Another",
                        "source_path": str(root / "BBBB2222.pdf"),
                        **ELIGIBLE,
                    },
                ],
            )

            audit = audit_library(config, snapshot, index_root=index_root)

            self.assertEqual(audit.total_items, 3)
            self.assertEqual([item.attachment_key for item in audit.items],
                             ["AAAA1111", "BBBB2222", "CCCC3333"])
            self.assertEqual(
                {item.attachment_key: set(item.statuses) for item in audit.items},
                {
                    "AAAA1111": {STATUS_CURRENT},
                    "BBBB2222": {STATUS_UNINDEXED, STATUS_MISSING_SOURCE},
                    "CCCC3333": {STATUS_ORPHANED_INDEX, STATUS_MISSING_SOURCE,
                                 STATUS_MISSING_MARKDOWN},
                },
            )
            self.assertIsNotNone(audit.generation_id)

    def test_audit_reports_the_published_generation_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_root = root / "index"
            generation_id = self._publish(index_root, [_index_record("AAAA1111")])
            snapshot = self._snapshot(root / "runs" / "r1", [])
            audit = audit_library(_config(root), snapshot, index_root=index_root)
            self.assertEqual(audit.generation_id, generation_id)

    def test_union_of_keys_makes_orphans_visible(self):
        """Iterating the mapping alone would make orphaned_index structurally undetectable."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_root = root / "index"
            self._publish(index_root, [_index_record("ZZZZ9999")])
            snapshot = self._snapshot(root / "runs" / "r1", [])
            audit = audit_library(_config(root), snapshot, index_root=index_root)
            self.assertEqual(audit.total_items, 1)
            self.assertIn(STATUS_ORPHANED_INDEX, audit.items[0].statuses)

    def test_unbuilt_index_reports_everything_unindexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "AAAA1111.pdf"
            pdf.write_bytes(b"pdf")
            snapshot = self._snapshot(
                root / "runs" / "r1",
                [{"zotero_attachment_key": "AAAA1111", "source_path": str(pdf), **ELIGIBLE}],
            )
            audit = audit_library(_config(root), snapshot, index_root=root / "index")
            self.assertIsNone(audit.generation_id)
            self.assertEqual(audit.status_counts[STATUS_UNINDEXED], 1)

    def test_full_audit_hashes_the_source_and_detects_real_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            index_root = root / "index"
            pdf = root / "AAAA1111.pdf"
            pdf.write_bytes(b"replaced pdf bytes")
            markdown = root / "AAAA1111.md"
            markdown.write_text("hello world", encoding="utf-8")

            self._publish(
                index_root,
                [
                    _index_record(
                        "AAAA1111",
                        source_path=str(pdf),
                        markdown_path=str(markdown),
                        markdown_sha256=_sha256_text("hello world"),
                        source_sha256="hash-of-the-original-pdf",
                    )
                ],
            )
            snapshot = self._snapshot(
                root / "runs" / "r1",
                [
                    {
                        "zotero_attachment_key": "AAAA1111",
                        "title": "A title",
                        "doi": "10.1000/x",
                        "citation_key": "key-AAAA1111",
                        "source_path": str(pdf),
                        **ELIGIBLE,
                    }
                ],
            )

            cheap = audit_library(config, snapshot, index_root=index_root)
            self.assertEqual(cheap.status_counts[STATUS_SOURCE_CHANGED], 0)

            full = audit_library(config, snapshot, index_root=index_root, full_audit=True)
            self.assertEqual(full.status_counts[STATUS_SOURCE_CHANGED], 1)

    def test_pre_upgrade_records_are_counted_not_reported_as_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            index_root = root / "index"
            pdf = root / "AAAA1111.pdf"
            pdf.write_bytes(b"pdf")
            markdown = root / "AAAA1111.md"
            markdown.write_text("hello world", encoding="utf-8")

            self._publish(
                index_root,
                [
                    _index_record(
                        "AAAA1111",
                        source_path=str(pdf),
                        markdown_path=str(markdown),
                        markdown_sha256=_sha256_text("hello world"),
                        source_sha256="",
                    )
                ],
            )
            snapshot = self._snapshot(
                root / "runs" / "r1",
                [
                    {
                        "zotero_attachment_key": "AAAA1111",
                        "title": "A title",
                        "doi": "10.1000/x",
                        "citation_key": "key-AAAA1111",
                        "source_path": str(pdf),
                        **ELIGIBLE,
                    }
                ],
            )

            audit = audit_library(config, snapshot, index_root=index_root, full_audit=True)
            self.assertEqual(audit.status_counts[STATUS_SOURCE_CHANGED], 0)
            self.assertEqual(audit.source_provenance_unknown, 1)

    def test_ineligible_items_are_counted_so_silence_is_legible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = self._snapshot(
                root / "runs" / "r1",
                [
                    {
                        "zotero_attachment_key": "AAAA1111",
                        "classification": "mapped_unverified",
                        "identity_status": "unverified",
                        "source_path": str(root / "AAAA1111.pdf"),
                    }
                ],
            )
            audit = audit_library(_config(root), snapshot, index_root=root / "index")
            self.assertEqual(audit.total_items, 1)
            self.assertEqual(audit.ineligible_items, 1)
            self.assertNotIn(STATUS_UNINDEXED, audit.items[0].statuses)

    def test_serialization_declares_that_counts_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = self._snapshot(root / "runs" / "r1", [])
            audit = audit_library(_config(root), snapshot, index_root=root / "index")
            payload = audit.to_dict()
            self.assertTrue(payload["counts_overlap"])
            self.assertIn("ineligible_items", payload)
            self.assertIn("source_provenance_unknown", payload)
            self.assertEqual(set(payload["status_counts"]), set(ALL_STATUSES))
            self.assertEqual(payload["items"], [])

    def test_to_dict_can_omit_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = self._snapshot(root / "runs" / "r1", [])
            audit = audit_library(_config(root), snapshot, index_root=root / "index")
            self.assertNotIn("items", audit.to_dict(include_items=False))


class ReadOnlyTests(unittest.TestCase):
    """Read-only is the headline property claimed in four documents; pin it."""

    def test_full_audit_does_not_touch_anything_under_output_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            index_root = config.output_root / "index"
            index_root.mkdir(parents=True)

            pdf = root / "AAAA1111.pdf"
            pdf.write_bytes(b"pdf bytes")
            markdown = config.output_root / "AAAA1111.md"
            markdown.write_text("hello world", encoding="utf-8")

            source = index_root / "source.jsonl"
            source.write_text(json.dumps(_index_record("AAAA1111")) + "\n", encoding="utf-8")
            info = stage_generation(index_root, write_jsonl_from_existing(source), command="test")
            validate_generation(index_root, info.generation_id)
            publish_generation(index_root, info.generation_id)

            snapshot = root / "mapping_report.jsonl"
            snapshot.write_text(
                json.dumps(
                    {
                        "zotero_attachment_key": "AAAA1111",
                        "source_path": str(pdf),
                        "sha256": "src-hash",
                        **ELIGIBLE,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            def snapshot_tree(base: Path) -> dict[str, tuple[int, int]]:
                return {
                    str(p.relative_to(base)): (p.stat().st_size, p.stat().st_mtime_ns)
                    for p in sorted(base.rglob("*"))
                    if p.is_file()
                }

            before_output = snapshot_tree(config.output_root)
            before_pdf = (pdf.stat().st_size, pdf.stat().st_mtime_ns)

            audit_library(config, snapshot, index_root=index_root, full_audit=True)

            self.assertEqual(snapshot_tree(config.output_root), before_output)
            self.assertEqual((pdf.stat().st_size, pdf.stat().st_mtime_ns), before_pdf)

    def test_audit_acquires_no_pipeline_lock(self):
        """A read must never block, or be blocked by, a writer."""
        from zotero_pdf_text.lock import pipeline_write_lock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            config.output_root.mkdir(parents=True)
            snapshot = root / "mapping_report.jsonl"
            snapshot.write_text("", encoding="utf-8")

            with pipeline_write_lock(config.output_root, command="test-writer"):
                audit = audit_library(
                    config, snapshot, index_root=config.output_root / "index"
                )
            self.assertEqual(audit.total_items, 0)


class ProvenancePreservationTests(unittest.TestCase):
    """Neither OCR path may publish a record with no source provenance.

    Losing it permanently disables source_changed for that attachment, and because absence is
    correctly not read as drift, the loss is silent -- the attachment just moves into
    source_provenance_unknown with nothing recording why.

    The two paths reach that guarantee differently, and the difference is not cosmetic.
    `ocr-images` rewrites derived Markdown from images already extracted, so the recorded hash
    still describes the PDF the text came from and is carried forward. `reconvert-math` re-runs
    the extractor against whatever is at source_path now, so it must hash that file instead --
    carrying the old hash forward would attribute new text to the wrong PDF. This test asserts
    only that both supply the fields; see tests/test_math_ocr.py for the behaviour.
    """

    def test_both_ocr_paths_supply_source_sha256_and_indexed_at(self):
        import ast
        import inspect

        from zotero_pdf_text import image_ocr, math_ocr

        for module in (math_ocr, image_ocr):
            with self.subTest(module=module.__name__):
                tree = ast.parse(inspect.getsource(module))
                constructions = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "TextIndexRecord"
                ]
                self.assertTrue(constructions, "expected a TextIndexRecord construction")
                for call in constructions:
                    supplied = {kw.arg for kw in call.keywords}
                    self.assertIn("source_sha256", supplied)
                    self.assertIn("indexed_at", supplied)

    def test_preserved_hash_keeps_source_changed_detectable_after_reconversion(self):
        """The behavioural consequence, independent of how the record is built."""
        reconverted = _observation(
            indexed_markdown_sha256="new-md-hash",
            markdown_sha256_current="new-md-hash",
            indexed_source_sha256="src-hash",
            source_sha256_current="src-hash-changed",
        )
        self.assertIn(STATUS_SOURCE_CHANGED, classify_item(reconverted))

        erased = _observation(
            indexed_markdown_sha256="new-md-hash",
            markdown_sha256_current="new-md-hash",
            indexed_source_sha256="",
            source_sha256_mapping="",
            source_sha256_current="src-hash-changed",
        )
        self.assertNotIn(STATUS_SOURCE_CHANGED, classify_item(erased))


class MetadataProjectionTests(unittest.TestCase):
    """Provenance must survive the SQLite -> dict projection both OCR paths read through.

    A structural check that the keyword is passed is not enough: the value it passes came from
    `get_item_context`, and a field missing from that projection makes the keyword bind "".
    """

    def test_get_item_context_exposes_provenance_fields(self):
        from zotero_pdf_text.fts import build_fts_index, get_item_context

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            jsonl.write_text(
                json.dumps(_index_record("AAAA1111", source_sha256="real-source-hash"))
                + "\n",
                encoding="utf-8",
            )
            db = root / "index.sqlite"
            build_fts_index(jsonl, db)

            context = get_item_context(db, attachment_key="AAAA1111")
            record = context["records"][0]
            self.assertEqual(record.get("source_sha256"), "real-source-hash")
            self.assertIn("indexed_at", record)

    def test_projection_round_trip_preserves_a_real_hash(self):
        """The loss this guards against was invisible end to end, not at the call site."""
        from zotero_pdf_text.fts import build_fts_index, get_item_context
        from zotero_pdf_text.indexer import TextIndexRecord

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "index.jsonl"
            jsonl.write_text(
                json.dumps(_index_record("AAAA1111", source_sha256="real-source-hash")) + "\n",
                encoding="utf-8",
            )
            db = root / "index.sqlite"
            build_fts_index(jsonl, db)
            record = get_item_context(db, attachment_key="AAAA1111")["records"][0]

            # Exactly what image_ocr does when it rebuilds the record for its upsert.
            rebuilt = TextIndexRecord(
                zotero_parent_key=record["zotero_parent_key"],
                zotero_attachment_key=record["zotero_attachment_key"],
                title=record["title"],
                creators=record["creators"],
                year=record["year"],
                doi=record["doi"],
                citation_key=record["citation_key"],
                source_path=record["source_path"],
                markdown_path=record["markdown_path"],
                markdown_sha256=record["markdown_sha256"],
                extraction_tool=record["extraction_tool"],
                char_count=1,
                word_count=1,
                page_count=record["page_count"],
                classification=record["classification"],
                identity_status=record["identity_status"],
                identity_rule=record["identity_rule"],
                has_math=record["has_math"],
                source_sha256=record.get("source_sha256", ""),
                indexed_at="2026-01-01T00:00:00+00:00",
                text="text",
            )
            self.assertEqual(rebuilt.source_sha256, "real-source-hash")


class ZoteroInventoryTests(unittest.TestCase):
    """Membership comes from Zotero, not from the file-walking mapping snapshot."""

    def test_attachment_with_a_missing_pdf_is_reported_not_invisible(self):
        from zotero_pdf_text.library import build_observations

        observations = build_observations(
            _config(Path("/nonexistent")),
            mapping_rows={},
            index_rows={},
            inventory={"AAAA1111": _attachment_record("AAAA1111")},
            inventory_available=True,
        )
        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertTrue(observation.in_zotero)
        self.assertFalse(observation.in_mapping)
        self.assertIs(observation.source_exists, False)
        self.assertIn(STATUS_MISSING_SOURCE, classify_item(observation))

    def test_indexed_attachment_with_a_missing_pdf_is_not_called_orphaned(self):
        """Zotero still represents it; the PDF is what went missing."""
        observation = _observation(
            in_zotero=True,
            in_mapping=False,
            source_exists=False,
            source_sha256_current=None,
            mapping_metadata={},
        )
        statuses = classify_item(observation)
        self.assertIn(STATUS_MISSING_SOURCE, statuses)
        self.assertNotIn(STATUS_ORPHANED_INDEX, statuses)

    def test_index_row_absent_from_zotero_is_still_orphaned(self):
        observation = _observation(in_zotero=False, in_mapping=False, mapping_metadata={})
        self.assertIn(STATUS_ORPHANED_INDEX, classify_item(observation))

    def test_eligibility_falls_back_to_the_indexed_record(self):
        """A verified attachment that drops out of the snapshot must not read as unverified."""
        from zotero_pdf_text.library import build_observations

        observations = build_observations(
            _config(Path("/nonexistent")),
            mapping_rows={},
            index_rows={"AAAA1111": [_index_record("AAAA1111")]},
            inventory={"AAAA1111": _attachment_record("AAAA1111")},
            inventory_available=True,
        )
        self.assertTrue(is_canonical_eligible(observations[0]))
        self.assertNotIn(STATUS_UNVERIFIED_INDEXED, classify_item(observations[0]))


    def test_a_stale_mapping_row_cannot_vouch_for_a_deleted_attachment(self):
        """The snapshot proves membership as of the dry-run, not membership now.

        An attachment deleted from Zotero after the snapshot is still in `mapping_report.jsonl`
        and still in the index. Unioning the two sources would call it `current` -- the one
        answer that is certainly wrong, because search can return a document the library no
        longer contains.
        """
        observation = _observation(
            in_zotero=False,
            inventory_available=True,
            in_mapping=True,
            mapping_metadata={},
            indexed_metadata={},
        )
        statuses = classify_item(observation)
        self.assertIn(STATUS_ORPHANED_INDEX, statuses)
        self.assertNotIn(STATUS_CURRENT, statuses)

    def test_mapping_membership_still_counts_when_the_inventory_is_unavailable(self):
        """Without a readable inventory `in_zotero=False` is a shrug, not a denial."""
        observation = _observation(
            in_zotero=False,
            inventory_available=False,
            in_mapping=True,
            mapping_metadata={},
            indexed_metadata={},
        )
        statuses = classify_item(observation)
        self.assertNotIn(STATUS_ORPHANED_INDEX, statuses)
        self.assertIn(STATUS_CURRENT, statuses)

    def test_positive_zotero_membership_survives_a_missing_availability_flag(self):
        """`in_zotero=True` is evidence in its own right; only the fallback is conditional."""
        observation = _observation(
            in_zotero=True,
            inventory_available=False,
            in_mapping=False,
            mapping_metadata={},
            indexed_metadata={},
        )
        self.assertNotIn(STATUS_ORPHANED_INDEX, classify_item(observation))


class InventoryReadOnlyTests(unittest.TestCase):
    """Reading the live inventory must not rewrite the database or discard its WAL.

    `mode=ro` is not a formality here. A read-write connection lets SQLite checkpoint or recover
    on open and on close, so a connection that only ever issues SELECTs can still change the
    main database file and delete the -wal sidecar -- in a user's live Zotero library.

    What read-only does *not* promise is that no file appears: SQLite creates an empty -wal and
    a -shm for any reader of a WAL database, exactly as Zotero itself does. The claim under test
    is that the database and its WAL contents are never modified, not that nothing is created.
    """

    def _database_with_orphaned_wal(self, destination: Path) -> Path:
        """A database whose -wal is on disk with no connection owning it.

        This is what a crashed or force-quit Zotero leaves behind, and it is the only state in
        which the bug is visible: SQLite runs *recovery* when a read-write connection opens such
        a database, rewriting the main file and deleting the WAL. Holding a live connection open
        instead would hide it, because SQLite only checkpoints when the last connection closes.

        Built by copying the files out from under a still-open connection, which is
        deterministic where crashing a subprocess is not.
        """
        import shutil
        import sqlite3

        source_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, source_dir, True)
        source = source_dir / "zotero.sqlite"
        keeper = sqlite3.connect(source)
        try:
            keeper.execute("PRAGMA journal_mode=WAL")
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            keeper.execute("CREATE TABLE t (a INTEGER)")
            keeper.execute("INSERT INTO t VALUES (1)")
            keeper.commit()
            for suffix in ("", "-wal", "-shm"):
                sidecar = source.with_name(source.name + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, destination.with_name(destination.name + suffix))
        finally:
            keeper.close()
        return destination

    def test_loading_the_inventory_leaves_the_database_and_wal_untouched(self):
        from zotero_pdf_text.library import load_attachment_inventory

        with tempfile.TemporaryDirectory() as tmp:
            db = self._database_with_orphaned_wal(Path(tmp) / "zotero.sqlite")
            wal = db.with_name(db.name + "-wal")
            self.assertTrue(wal.exists(), "fixture failed to leave an orphaned WAL")
            before = db.read_bytes()
            wal_before = wal.read_bytes()

            # The schema is not Zotero's, so the query fails. That is deliberate: recovery
            # happens on open and on close, so the failure path must be as read-only as the
            # success path. A read-write connection rewrites the database here even though
            # nothing but a failing SELECT was ever issued.
            with self.assertRaises(Exception):
                load_attachment_inventory(db)

            self.assertEqual(db.read_bytes(), before, "main database was rewritten")
            self.assertTrue(wal.exists(), "the WAL was discarded")
            self.assertEqual(wal.read_bytes(), wal_before, "the WAL was rewritten")

    def test_a_missing_database_is_never_created(self):
        from zotero_pdf_text.library import load_attachment_inventory

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "zotero.sqlite"
            with self.assertRaises(LibraryAuditError):
                load_attachment_inventory(db)
            self.assertFalse(db.exists())


class InventoryAvailabilityReportingTests(unittest.TestCase):
    def test_audit_reports_when_the_inventory_could_not_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "mapping_report.jsonl"
            snapshot.write_text("", encoding="utf-8")
            # No zotero.sqlite exists under this config.
            audit = audit_library(_config(root), snapshot, index_root=root / "index")
            self.assertFalse(audit.inventory_available)
            self.assertFalse(audit.to_dict()["inventory_available"])


class SnapshotHashFallbackTests(unittest.TestCase):
    """The mapper hashes every PDF during dry-run; the audit should use that, not ignore it."""

    def test_source_changed_fires_without_a_full_audit(self):
        self.assertIn(
            STATUS_SOURCE_CHANGED,
            classify_item(
                _observation(
                    source_sha256_current=None,
                    source_sha256_mapping="hash-from-snapshot",
                    indexed_source_sha256="hash-at-index-time",
                )
            ),
        )

    def test_a_freshly_computed_hash_wins_over_the_snapshot(self):
        agreeing_now = _observation(
            source_sha256_current="indexed-hash",
            source_sha256_mapping="stale-snapshot-hash",
            indexed_source_sha256="indexed-hash",
        )
        self.assertNotIn(STATUS_SOURCE_CHANGED, classify_item(agreeing_now))

    def test_still_silent_when_the_index_has_no_provenance(self):
        self.assertNotIn(
            STATUS_SOURCE_CHANGED,
            classify_item(
                _observation(
                    source_sha256_current=None,
                    source_sha256_mapping="hash-from-snapshot",
                    indexed_source_sha256="",
                )
            ),
        )


class LibraryStatusTests(unittest.TestCase):
    def test_reports_health_and_publication_rather_than_row_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_root = root / "index"
            index_root.mkdir(parents=True)
            source = index_root / "source.jsonl"
            source.write_text(json.dumps(_index_record("AAAA1111")) + "\n", encoding="utf-8")
            info = stage_generation(index_root, write_jsonl_from_existing(source), command="test")
            validate_generation(index_root, info.generation_id)
            publish_generation(index_root, info.generation_id)

            run_dir = root / "runs" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "mapping_report.jsonl").write_text("", encoding="utf-8")

            status = library_status(
                _config(root), run_dir / "mapping_report.jsonl", index_root=index_root
            )

            self.assertEqual(status["generation_id"], info.generation_id)
            self.assertIsNotNone(status["last_published_at"])
            self.assertEqual(set(status["health"]), set(ALL_STATUSES))
            self.assertTrue(status["counts_overlap"])
            self.assertIn("counts_overlap_note", status)
            # "coverage" is the overstatement this function exists to replace.
            self.assertNotIn("coverage", status)


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
