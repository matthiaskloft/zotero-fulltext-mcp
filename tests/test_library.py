import json
import shutil
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    STATUS_MAPPING_AMBIGUOUS,
    STATUS_METADATA_CHANGED,
    STATUS_MISSING_MARKDOWN,
    STATUS_MISSING_SOURCE,
    STATUS_MEMBERSHIP_UNCHECKED,
    STATUS_ORPHANED_INDEX,
    STATUS_SOURCE_CHANGED,
    STATUS_SOURCE_UNCHECKED,
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
    """An indexed, eligible, healthy item. Tests override only the fields they are about.

    `in_zotero` and `inventory_available` are part of "healthy" rather than left at their
    dataclass defaults. Omitting them described an item whose membership nothing had checked,
    and every test that meant "a normal library" was quietly asserting against an audit run
    with Zotero unreadable -- a state in which no membership conclusion is sound.
    """
    base: dict[str, object] = {
        "in_zotero": True,
        "inventory_available": True,
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
        "zotero_metadata": {"title": "A title", "doi": "10.1000/x", "citation_key": "k"},
        "indexed_metadata": {"title": "A title", "doi": "10.1000/x", "citation_key": "k"},
    }
    base.update(overrides)
    return ItemObservation(attachment_key=key, **base)  # type: ignore[arg-type]


class CanonicalPathTests(unittest.TestCase):
    def test_markdown_path_is_keyed_on_the_attachment_key_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            path = canonical_markdown_path(config, "ABCD1234")
            self.assertEqual(path.parent, config.output_root / "library" / "markdown")
            self.assertEqual(path.name, "ABCD1234.md")

    def test_image_dir_ignores_title_so_a_retitle_cannot_orphan_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            first = canonical_image_dir(config, "ABCD1234")
            self.assertEqual(first, config.output_root / "library" / "images" / "ABCD1234")
            # The image directory is derived from the key alone, never from the Markdown
            # filename, so renaming the item leaves the images exactly where they were.
            self.assertEqual(canonical_image_dir(config, "ABCD1234"), first)

    def test_a_retitle_resolves_to_exactly_the_same_file(self):
        """Equality, not a shared prefix.

        The previous version of this test asserted only `startswith(key)`, which the old
        title-slug implementation satisfied while still resolving `Old Title` and `New Title`
        to two different files -- so the test passed and the guarantee in the docstring was
        false. Any caller that checks existence needs the whole path to be stable.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            paths = {canonical_markdown_path(config, "ABCD1234") for _ in range(2)}
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths.pop().name, "ABCD1234.md")

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
            _observation(
                in_zotero=False,
                in_mapping=False,
                inventory_available=True,
                mapping_metadata={},
                zotero_metadata={},
                indexed_metadata={},
            ),
            {STATUS_ORPHANED_INDEX},
        )

    def test_an_index_only_row_is_unchecked_not_orphaned_without_the_inventory(self):
        """Only Zotero can retire a row.

        With no inventory, an attachment missing from the mapping snapshot may simply have lost
        its PDF -- the snapshot is built by walking files. Calling that `orphaned_index` would
        recommend dropping a row Zotero still lists.
        """
        statuses = classify_item(
            _observation(
                in_zotero=False,
                in_mapping=False,
                inventory_available=False,
                mapping_metadata={},
                zotero_metadata={},
                indexed_metadata={},
            )
        )
        self.assertIn(STATUS_MEMBERSHIP_UNCHECKED, statuses)
        self.assertNotIn(STATUS_ORPHANED_INDEX, statuses)
        self.assertNotIn(STATUS_CURRENT, statuses)

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
            (
                "no full audit, snapshot hash available",
                {"source_sha256_current": None, "source_sha256_mapping": "src-hash"},
                {STATUS_CURRENT},
            ),
            ("pre-upgrade record", {"indexed_source_sha256": ""}, {STATUS_CURRENT}),
            (
                "neither known",
                {"source_sha256_current": None, "indexed_source_sha256": ""},
                {STATUS_CURRENT},
            ),
            # Nothing to compare, but the index does record a hash. Reporting `current` here
            # would certify a file the audit never looked at, so it is `source_unchecked` --
            # still not `source_changed`, which is the false positive this test guards.
            (
                "no full audit, no snapshot hash",
                {"source_sha256_current": None},
                {STATUS_SOURCE_UNCHECKED},
            ),
        ]
        for name, overrides, expected in cases:
            with self.subTest(case=name):
                self.assertStatuses(_observation(**overrides), expected)

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
        """Adding a DOI and removing one are both drift, from either comparison source.

        Run against Zotero's live record and against the snapshot's fallback, because the two
        take different branches and only one of them is exercised on a healthy library.
        """
        with_doi = {"title": "T", "doi": "10.1000/x", "citation_key": "k"}
        without_doi = {"title": "T", "doi": "", "citation_key": "k"}
        sources: tuple[tuple[str, dict[str, object]], ...] = (
            ("live zotero record", {"in_zotero": True, "inventory_available": True}),
            (
                "snapshot fallback",
                {"in_zotero": False, "inventory_available": False, "zotero_metadata": {}},
            ),
        )
        for label, membership in sources:
            for direction, current, indexed in (
                ("doi added", with_doi, without_doi),
                ("doi removed", without_doi, with_doi),
            ):
                with self.subTest(source=label, change=direction):
                    statuses = classify_item(
                        _observation(
                            mapping_metadata=current,
                            **{
                                **membership,
                                **(
                                    {"zotero_metadata": current}
                                    if membership["in_zotero"]
                                    else {}
                                ),
                            },
                            indexed_metadata=indexed,
                        )
                    )
                    self.assertIn(STATUS_METADATA_CHANGED, statuses)

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
            _observation(
                in_zotero=False,
                in_mapping=False,
                inventory_available=True,
                mapping_metadata={},
                zotero_metadata={},
                indexed_metadata={},
            )
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

    def test_rows_sharing_an_attachment_key_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.jsonl"
            path.write_text(
                "\n".join(json.dumps({"zotero_attachment_key": "AAAA1111", "source_path": name})
                          for name in ("first.pdf", "second.pdf")) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [row["source_path"] for row in load_mapping_snapshot(path)["AAAA1111"]],
                ["first.pdf", "second.pdf"],
            )

    def test_malformed_json_names_the_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping_report.jsonl"
            path.write_text('{"zotero_attachment_key": "A"}\nnot json\n', encoding="utf-8")
            with self.assertRaises(LibraryAuditError) as ctx:
                load_mapping_snapshot(path)
            self.assertIn(":2:", str(ctx.exception))

    def test_unpublished_index_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            generation_id, published_at, rows = load_index_records(Path(tmp) / "index")
            self.assertIsNone(generation_id)
            self.assertEqual(rows, {})

    def test_a_publish_between_reads_cannot_mix_generation_and_rows(self):
        """The reported generation id and the reported rows must come from one pointer read.

        Reading `current.json` twice -- once for the id, once to locate the JSONL -- lets a
        publish landing in between pair generation A's id with generation B's contents, and the
        audit then serialises that false provenance as fact. Generation directories are
        immutable once published, so resolving the JSONL from the id already in hand makes the
        pair self-consistent: the report is a moment old, which is what a snapshot is.
        """
        from zotero_pdf_text import artifacts
        from zotero_pdf_text.artifacts import (
            publish_generation,
            stage_generation,
            validate_generation,
            write_jsonl_from_existing,
        )

        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp) / "index"
            index_root.mkdir(parents=True)

            def publish(key: str) -> str:
                source = index_root / f"{key}.jsonl"
                source.write_text(
                    json.dumps(_index_record(key)) + "\n", encoding="utf-8"
                )
                info = stage_generation(
                    index_root, write_jsonl_from_existing(source), command="test"
                )
                validate_generation(index_root, info.generation_id)
                publish_generation(index_root, info.generation_id)
                return info.generation_id

            first = publish("AAAA1111")
            second = publish("BBBB2222")
            # Point back at the first generation, then swap to the second the moment the
            # pointer is read -- the exact interleaving a concurrent publish produces.
            artifacts.publish_generation(index_root, first)

            real_read = artifacts.read_current_pointer
            calls = []

            def racing_read(root):
                pointer = real_read(root)
                calls.append(pointer)
                if len(calls) == 1:
                    artifacts.publish_generation(index_root, second)
                return pointer

            from unittest.mock import patch

            with patch.object(artifacts, "read_current_pointer", racing_read), patch(
                "zotero_pdf_text.library.read_current_pointer", racing_read
            ):
                generation_id, _published_at, rows = load_index_records(index_root)

            self.assertEqual(generation_id, first)
            self.assertEqual(
                sorted(rows),
                ["AAAA1111"],
                "rows came from a different generation than the reported id",
            )


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


def _write_zotero_inventory(config: ProjectConfig, attachments: dict[str, str]) -> Path:
    """Write a minimal Zotero-schema database listing `attachments` (key -> source path).

    End-to-end audit tests need this. Without a database at `config.zotero_sqlite` the audit
    cannot read membership, so every item comes back `membership_unchecked` and an assertion
    about `current` or `unindexed` is really an assertion about an audit that answered nothing.

    Title, DOI and citation key are written to match `_index_record`'s defaults, because the
    audit compares Zotero's live record against the indexed one. A fixture that disagreed with
    itself would report `metadata_changed` on a library that has not drifted, and a test
    written around that would be asserting the fixture rather than the rule.
    """
    db = config.zotero_sqlite
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.executescript(
            """
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
            CREATE TABLE itemAttachments (
                itemID INTEGER PRIMARY KEY,
                parentItemID INTEGER,
                linkMode INTEGER,
                contentType TEXT,
                path TEXT
            );
            CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
            CREATE TABLE itemTypesCombined (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
            CREATE TABLE fieldsCombined (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
            CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
            CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
            CREATE TABLE creators (
                creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT
            );
            INSERT INTO itemTypesCombined VALUES (1, 'journalArticle');
            INSERT INTO fieldsCombined VALUES (1, 'title');
            INSERT INTO fieldsCombined VALUES (2, 'DOI');
            INSERT INTO fieldsCombined VALUES (3, 'citationKey');
            """
        )
        item_id = 0
        value_id = 0
        for key, source_path in attachments.items():
            item_id += 1
            parent_id = item_id
            con.execute("INSERT INTO items VALUES (?, ?, 1)", (parent_id, f"P{key}"))
            for field_id, value in (
                (1, "A title"),
                (2, "10.1000/x"),
                (3, f"key-{key}"),
            ):
                value_id += 1
                con.execute("INSERT INTO itemDataValues VALUES (?, ?)", (value_id, value))
                con.execute(
                    "INSERT INTO itemData VALUES (?, ?, ?)", (parent_id, field_id, value_id)
                )
            item_id += 1
            con.execute("INSERT INTO items VALUES (?, ?, 2)", (item_id, key))
            con.execute(
                "INSERT INTO itemAttachments VALUES (?, ?, 2, 'application/pdf', ?)",
                (item_id, parent_id, source_path),
            )
        con.commit()
    finally:
        con.close()
    return db


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

    def test_shared_key_resolves_verified_row_in_either_order_for_audit_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            index_root = root / "index"
            pdf = root / "verified.pdf"
            other = root / "candidate.pdf"
            pdf.write_bytes(b"verified")
            other.write_bytes(b"candidate")
            markdown = root / "AAAA1111.md"
            markdown.write_text("hello world", encoding="utf-8")
            self._publish(index_root, [_index_record(
                "AAAA1111", source_path=str(pdf), source_sha256=_sha256_text("verified"),
                markdown_path=str(markdown), markdown_sha256=_sha256_text("hello world"),
            )])
            _write_zotero_inventory(config, {"AAAA1111": str(pdf)})
            verified = {
                "zotero_attachment_key": "AAAA1111", "source_path": str(pdf),
                "zotero_path": str(pdf), "sha256": _sha256_text("verified"), **ELIGIBLE,
            }
            candidate = {
                "zotero_attachment_key": "AAAA1111", "source_path": str(other),
                "zotero_path": str(other), "sha256": _sha256_text("candidate"),
                "classification": "mapped_unverified", "identity_status": "unverified",
            }
            for order in ((verified, candidate), (candidate, verified)):
                with self.subTest(last=order[-1]["classification"]):
                    snapshot = self._snapshot(root / "runs" / "r1", list(order))
                    audit = audit_library(config, snapshot, index_root=index_root)
                    item = audit.items[0]
                    self.assertEqual(item.observation.mapping_row_count, 2)
                    self.assertEqual(item.observation.mapping_match_count, 1)
                    self.assertEqual(item.observation.source_sha256_mapping, _sha256_text("verified"))
                    self.assertNotIn(STATUS_UNVERIFIED_INDEXED, item.statuses)
                    self.assertNotIn(STATUS_SOURCE_UNCHECKED, item.statuses)
                    self.assertNotIn(STATUS_MAPPING_AMBIGUOUS, item.statuses)
                    self.assertIn(STATUS_CURRENT, item.statuses)
                    status = library_status(config, snapshot, index_root=index_root)
                    self.assertEqual(status["health"][STATUS_UNVERIFIED_INDEXED], 0)
                    self.assertEqual(status["health"][STATUS_MAPPING_AMBIGUOUS], 0)

    def test_same_source_uses_zotero_path_then_reports_remaining_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            pdf = root / "shared.pdf"
            pdf.write_bytes(b"pdf")
            _write_zotero_inventory(config, {"AAAA1111": str(pdf)})
            index_root = root / "index"
            self._publish(index_root, [_index_record(
                "AAAA1111", source_path=str(pdf), source_sha256=_sha256_text("pdf"),
            )])
            verified = {
                "zotero_attachment_key": "AAAA1111", "source_path": str(pdf),
                "zotero_path": str(pdf), **ELIGIBLE,
            }
            candidate = {
                "zotero_attachment_key": "AAAA1111", "source_path": str(pdf),
                "zotero_path": str(root / "other.pdf"),
                "classification": "mapped_unverified", "identity_status": "unverified",
            }
            snapshot = self._snapshot(root / "runs" / "r1", [candidate, verified])
            resolved = audit_library(config, snapshot, index_root=index_root).items[0]
            self.assertEqual(resolved.observation.mapping_match_count, 1)
            self.assertTrue(resolved.canonical_eligible)

            snapshot = self._snapshot(root / "runs" / "r1", [verified, dict(verified)])
            audit = audit_library(config, snapshot, index_root=index_root)
            item = audit.items[0]
            self.assertIn(STATUS_MAPPING_AMBIGUOUS, item.statuses)
            self.assertNotIn(STATUS_CURRENT, item.statuses)
            self.assertNotIn(STATUS_UNVERIFIED_INDEXED, item.statuses)
            self.assertFalse(item.canonical_eligible)
            self.assertEqual(item.observation.classification, "")
            self.assertEqual(item.observation.identity_status, "")
            self.assertEqual(item.observation.mapping_match_count, 2)
            self.assertEqual(library_status(config, snapshot, index_root=index_root)["health"][STATUS_MAPPING_AMBIGUOUS], 1)

    def test_relinked_source_uses_current_mapping_row_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            old_pdf = root / "old.pdf"
            new_pdf = root / "new.pdf"
            old_pdf.write_bytes(b"old")
            new_pdf.write_bytes(b"new")
            _write_zotero_inventory(config, {"AAAA1111": str(new_pdf)})
            index_root = root / "index"
            self._publish(index_root, [_index_record(
                "AAAA1111", source_path=str(old_pdf), source_sha256=_sha256_text("old"),
            )])
            old_row = {
                "zotero_attachment_key": "AAAA1111", "source_path": str(old_pdf),
                "zotero_path": str(old_pdf), "sha256": _sha256_text("old"), **ELIGIBLE,
            }
            new_row = {
                "zotero_attachment_key": "AAAA1111", "source_path": str(new_pdf),
                "zotero_path": str(new_pdf), "sha256": _sha256_text("new"), **ELIGIBLE,
            }
            for order in ((old_row, new_row), (new_row, old_row)):
                with self.subTest(last=order[-1]["source_path"]):
                    snapshot = self._snapshot(root / "runs" / "r1", list(order))
                    audit = audit_library(config, snapshot, index_root=index_root)
                    item = audit.items[0]
                    self.assertEqual(item.observation.source_sha256_mapping, _sha256_text("new"))
                    self.assertIn(STATUS_SOURCE_CHANGED, item.statuses)
                    self.assertNotIn(STATUS_SOURCE_UNCHECKED, item.statuses)

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

            # Zotero lists the two live attachments and not CCCC3333, which is what makes
            # that row genuinely orphaned rather than merely unverifiable.
            _write_zotero_inventory(
                config,
                {
                    "AAAA1111": str(source_pdf),
                    "BBBB2222": str(root / "BBBB2222.pdf"),
                },
            )

            audit = audit_library(config, snapshot, index_root=index_root)

            self.assertTrue(audit.inventory_available)
            self.assertIsNone(audit.inventory_error)
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

    def test_union_of_keys_makes_index_only_rows_visible(self):
        """Iterating the mapping alone would make such a row structurally undetectable.

        There is no Zotero database under this config, so the audit cannot decide membership
        and reports `membership_unchecked` rather than `orphaned_index`. The property under
        test is that the row is *seen at all*; which of the two it earns is decided by the
        inventory and covered in `ZoteroInventoryTests`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_root = root / "index"
            self._publish(index_root, [_index_record("ZZZZ9999")])
            snapshot = self._snapshot(root / "runs" / "r1", [])
            audit = audit_library(_config(root), snapshot, index_root=index_root)
            self.assertEqual(audit.total_items, 1)
            self.assertFalse(audit.inventory_available)
            self.assertIn(STATUS_MEMBERSHIP_UNCHECKED, audit.items[0].statuses)
            self.assertNotIn(STATUS_ORPHANED_INDEX, audit.items[0].statuses)

    def test_unbuilt_index_reports_everything_unindexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "AAAA1111.pdf"
            pdf.write_bytes(b"pdf")
            snapshot = self._snapshot(
                root / "runs" / "r1",
                [{"zotero_attachment_key": "AAAA1111", "source_path": str(pdf), **ELIGIBLE}],
            )
            config = _config(root)
            _write_zotero_inventory(config, {"AAAA1111": str(pdf)})
            audit = audit_library(config, snapshot, index_root=root / "index")
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
        observation = _observation(
            in_zotero=False, inventory_available=True, in_mapping=False, mapping_metadata={}
        )
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

    def test_an_unreadable_inventory_withholds_every_membership_conclusion(self):
        """A mapping row proves historical membership, which is not the question being asked.

        The snapshot says the attachment existed at the last dry-run. Whether the user has
        deleted it since is exactly what an unreadable inventory leaves unknown, so neither
        `current` (it may be gone) nor `orphaned_index` (it may be fine) may be asserted.
        """
        for label, extra in (
            ("indexed", {"in_index": True}),
            ("not indexed", {"in_index": False}),
        ):
            with self.subTest(case=label):
                observation = _observation(
                    in_zotero=False,
                    inventory_available=False,
                    in_mapping=True,
                    mapping_metadata={},
                    indexed_metadata={},
                    **extra,
                )
                statuses = classify_item(observation)
                self.assertIn(STATUS_MEMBERSHIP_UNCHECKED, statuses)
                self.assertNotIn(STATUS_ORPHANED_INDEX, statuses)
                self.assertNotIn(STATUS_CURRENT, statuses)
                self.assertNotIn(STATUS_UNINDEXED, statuses)

    def test_file_level_findings_survive_an_unreadable_inventory(self):
        """Withholding membership must not silence facts about files on disk.

        A changed PDF is a changed PDF whether or not Zotero still lists the attachment. If
        these went quiet too, an audit run while Zotero was busy would look clean, and
        `membership_unchecked` on every row would read as the only thing wrong.

        `source_unchecked` is in here deliberately. An earlier version of this test asserted
        only on its two neighbours, and a mutation silencing `source_unchecked` under unknown
        membership survived -- the same near-miss scoping that has cost this branch several
        rounds.
        """
        unreadable = {"in_zotero": False, "inventory_available": False, "in_mapping": True}
        drifted = classify_item(
            _observation(
                **unreadable,
                source_sha256_current="moved-on",
                indexed_source_sha256="as-converted",
                markdown_sha256_current="edited",
                indexed_markdown_sha256="as-published",
            )
        )
        self.assertIn(STATUS_SOURCE_CHANGED, drifted)
        self.assertIn(STATUS_STALE_MARKDOWN, drifted)
        self.assertIn(STATUS_MEMBERSHIP_UNCHECKED, drifted)

        # Nothing hashed this PDF, so the index's provenance is unverified rather than fine.
        unchecked = classify_item(
            _observation(
                **unreadable,
                indexed_source_sha256="as-converted",
                source_sha256_current=None,
                source_sha256_mapping="",
            )
        )
        self.assertIn(STATUS_SOURCE_UNCHECKED, unchecked)
        self.assertIn(STATUS_MEMBERSHIP_UNCHECKED, unchecked)

        missing = classify_item(
            _observation(**unreadable, source_exists=False, source_sha256_current=None)
        )
        self.assertIn(STATUS_MISSING_SOURCE, missing)

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
    """Reading the live inventory must leave its directory byte-for-byte identical.

    Not "must not corrupt it" and not "must not rewrite the main file" -- *nothing changes and
    nothing appears*. That is what `audit-library` promises in four documents, and neither URI
    mode delivers it: a read-write connection recovers and checkpoints, `mode=ro` still creates
    the `-shm` every reader of a WAL database needs, and `immutable=1` creates nothing but
    cannot see the WAL at all. The audit therefore reads a copy.

    The assertions compare the whole directory tree, names and bytes, because the earlier
    version of this test compared only the database and its WAL and so had nothing to say about
    a sidecar appearing beside them.
    """

    def _tree(self, directory: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(directory)): p.read_bytes()
            for p in sorted(directory.rglob("*"))
            if p.is_file()
        }

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
            directory = Path(tmp)
            db = self._database_with_orphaned_wal(directory / "zotero.sqlite")
            wal = db.with_name(db.name + "-wal")
            self.assertTrue(wal.exists(), "fixture failed to leave an orphaned WAL")
            before = self._tree(directory)

            # The schema is not Zotero's, so the query fails. That is deliberate: recovery
            # happens on open and on close, so the failure path must be as untouched as the
            # success path. A read-write connection rewrites the database here even though
            # nothing but a failing SELECT was ever issued.
            with self.assertRaises(Exception):
                load_attachment_inventory(db)

            self.assertEqual(self._tree(directory), before)

    def test_reading_a_wal_database_creates_no_sidecar(self):
        """`mode=ro` passes every "was it modified" check and still fails this one.

        A WAL database with no `-shm` is the ordinary state of a Zotero library that is not
        currently running. Any reader needs an `-shm` to see the WAL, and a read-only connection
        creates it -- a new file in the user's Zotero folder, which the audit promises not to
        produce. Only reading a copy avoids it.
        """
        from zotero_pdf_text.library import load_attachment_inventory

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db = self._database_with_orphaned_wal(directory / "zotero.sqlite")
            shm = db.with_name(db.name + "-shm")
            shm.unlink(missing_ok=True)
            before = self._tree(directory)
            self.assertNotIn("zotero.sqlite-shm", before)

            with self.assertRaises(Exception):
                load_attachment_inventory(db)

            after = self._tree(directory)
            self.assertEqual(
                set(after) - set(before), set(), "reading the inventory created a sidecar"
            )
            self.assertEqual(after, before)

    def test_rows_committed_to_the_wal_are_still_visible(self):
        """The reason the audit copies rather than using `immutable=1`, which cannot see them."""
        from zotero_pdf_text.zotero_db import snapshot_for_reading

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db = self._database_with_orphaned_wal(directory / "zotero.sqlite")
            before = self._tree(directory)

            with snapshot_for_reading(db) as copy:
                con = sqlite3.connect(copy)
                try:
                    rows = con.execute("SELECT count(*) FROM t").fetchone()[0]
                finally:
                    con.close()

            self.assertEqual(rows, 1, "the uncheckpointed WAL row was not visible")
            self.assertEqual(self._tree(directory), before)

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


class RelinkedAttachmentTests(unittest.TestCase):
    """Zotero's current path wins over the one the snapshot and index remember.

    Relinking an attachment to a different PDF leaves the old path in both older views. Auditing
    there answers a question nobody asked: with the old file still present the source change is
    missed entirely, and with it gone the audit invents `missing_source` while the new PDF sits
    on disk intact.
    """

    def _observe(self, *, old_present: bool, full: bool = True):
        from zotero_pdf_text.library import build_observations

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        linked = root / "linked"
        linked.mkdir()
        old_pdf = linked / "old.pdf"
        new_pdf = linked / "new.pdf"
        new_pdf.write_bytes(b"%PDF new")
        if old_present:
            old_pdf.write_bytes(b"%PDF old")

        config = ProjectConfig(
            zotero_root=root / "zotero",
            zotero_data_directory=root / "zotero" / "data",
            linked_attachments=linked,
            output_root=root / "out",
        )
        # Zotero now points the key at new.pdf; both older views still name old.pdf.
        attachment = _attachment_record("AAAA1111", zotero_path="attachments:new.pdf")
        indexed = _index_record(
            "AAAA1111",
            source_path=str(old_pdf),
            source_sha256=_sha256_text("%PDF old"),
        )
        observations = build_observations(
            config,
            mapping_rows={
                "AAAA1111": [{
                    "zotero_attachment_key": "AAAA1111",
                    "source_path": str(old_pdf),
                    "sha256": _sha256_text("%PDF old"),
                    **ELIGIBLE,
                }]
            },
            index_rows={"AAAA1111": [indexed]},
            inventory={"AAAA1111": attachment},
            inventory_available=True,
            full_audit=full,
        )
        self.assertEqual(len(observations), 1)
        return observations[0], new_pdf, old_pdf

    def test_a_relinked_attachment_is_audited_at_its_current_path(self):
        observation, new_pdf, old_pdf = self._observe(old_present=True)
        self.assertEqual(observation.source_path, str(new_pdf))
        self.assertEqual(observation.indexed_source_path, str(old_pdf))
        self.assertIs(observation.source_exists, True)
        self.assertEqual(observation.source_sha256_current, _sha256_text("%PDF new"))
        statuses = classify_item(observation)
        self.assertIn(STATUS_SOURCE_CHANGED, statuses)
        self.assertNotIn(STATUS_CURRENT, statuses)
        self.assertNotIn(STATUS_MISSING_SOURCE, statuses)

    def test_a_relink_is_not_reported_as_a_missing_source(self):
        observation, new_pdf, _old = self._observe(old_present=False)
        self.assertEqual(observation.source_path, str(new_pdf))
        self.assertIs(observation.source_exists, True)
        self.assertNotIn(STATUS_MISSING_SOURCE, classify_item(observation))

    def test_a_known_relink_is_never_reported_as_current(self):
        """The default audit must not certify what it could not check.

        Clearing the stale snapshot hash stops the wrong bytes being compared, but silence is
        not neutral here: with no status at all the item falls through to `current`, which is
        the one answer known to be wrong. The path mismatch is known without hashing anything.
        """
        observation, _new, _old = self._observe(old_present=True, full=False)
        statuses = classify_item(observation)
        self.assertNotIn(STATUS_CURRENT, statuses)
        self.assertIn(STATUS_SOURCE_UNCHECKED, statuses)
        self.assertNotIn(STATUS_SOURCE_CHANGED, statuses)

    def test_a_full_audit_resolves_the_relink_to_source_changed(self):
        """`--full` hashes the file the audit can actually see, so it need not say "unknown"."""
        observation, _new, _old = self._observe(old_present=True, full=True)
        statuses = classify_item(observation)
        self.assertIn(STATUS_SOURCE_CHANGED, statuses)
        self.assertNotIn(STATUS_SOURCE_UNCHECKED, statuses)
        self.assertNotIn(STATUS_CURRENT, statuses)

    def test_the_snapshot_hash_does_not_vouch_for_a_different_file(self):
        """Without --full the snapshot hash is the fallback -- but not across a relink.

        It describes the old PDF. Matching it against `indexed_source_sha256` would report the
        item clean at exactly the moment it changed most, so it is dropped and the answer
        degrades to "unknown", which `source_provenance_unknown` already counts.
        """
        observation, _new, _old = self._observe(old_present=True)
        self.assertEqual(observation.source_sha256_mapping, "")

    def test_a_storage_attachment_keeps_the_recorded_path(self):
        """`storage:` paths do not resolve on disk, so there is no current path to prefer."""
        from zotero_pdf_text.library import build_observations

        observations = build_observations(
            _config(Path("/nonexistent")),
            mapping_rows={
                "AAAA1111": [{
                    "zotero_attachment_key": "AAAA1111",
                    "source_path": "recorded.pdf",
                    "sha256": "src-hash",
                    **ELIGIBLE,
                }]
            },
            index_rows={},
            inventory={"AAAA1111": _attachment_record("AAAA1111", zotero_path="storage:x.pdf")},
            inventory_available=True,
        )
        self.assertEqual(observations[0].source_path, "recorded.pdf")
        self.assertEqual(observations[0].source_sha256_mapping, "src-hash")


class LiveMetadataTests(unittest.TestCase):
    """`metadata_changed` compares against Zotero, not against the snapshot's memory of it.

    The snapshot's copy is only as current as the last dry-run, so a title, DOI or citation key
    edited in Zotero afterwards is invisible to it -- and an attachment that never reached the
    mapper carries no snapshot metadata to compare at all.
    """

    def _observe(self, *, in_mapping: bool, **attachment_overrides: object):
        from zotero_pdf_text.library import build_observations

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        linked = root / "linked"
        linked.mkdir()
        pdf = linked / "p.pdf"
        pdf.write_bytes(b"%PDF")
        config = ProjectConfig(
            zotero_root=root / "zotero",
            zotero_data_directory=root / "zotero" / "data",
            linked_attachments=linked,
            output_root=root / "out",
        )
        snapshot_metadata = {"title": "A title", "doi": "10.1000/x", "citation_key": "key-AAAA1111"}
        mapping_rows = {}
        if in_mapping:
            mapping_rows = {
                "AAAA1111": [{
                    "zotero_attachment_key": "AAAA1111",
                    "source_path": str(pdf),
                    "sha256": _sha256_text("%PDF"),
                    **snapshot_metadata,
                    **ELIGIBLE,
                }]
            }
        indexed = _index_record(
            "AAAA1111",
            source_path=str(pdf),
            source_sha256=_sha256_text("%PDF"),
            markdown_path="",
            markdown_sha256="",
            **snapshot_metadata,
        )
        observations = build_observations(
            config,
            mapping_rows=mapping_rows,
            index_rows={"AAAA1111": [indexed]},
            inventory={
                "AAAA1111": _attachment_record(
                    "AAAA1111", zotero_path="attachments:p.pdf", **attachment_overrides
                )
            },
            inventory_available=True,
            full_audit=True,
        )
        return observations[0]

    def test_metadata_edited_after_the_snapshot_is_detected(self):
        observation = self._observe(
            in_mapping=True, title="NEW title", doi="10.9999/new", citation_key="newKey"
        )
        self.assertEqual(observation.mapping_metadata["title"], "A title")
        self.assertEqual(observation.zotero_metadata["title"], "NEW title")
        self.assertIn(STATUS_METADATA_CHANGED, classify_item(observation))

    def test_an_indexed_inventory_only_attachment_is_compared(self):
        """No mapping row at all: the old rule required one and skipped the comparison."""
        observation = self._observe(in_mapping=False, title="NEW title")
        self.assertFalse(observation.in_mapping)
        self.assertIn(STATUS_METADATA_CHANGED, classify_item(observation))

    def test_matching_live_metadata_stays_current(self):
        observation = self._observe(in_mapping=True)
        self.assertEqual(sorted(classify_item(observation)), [STATUS_CURRENT])

    def test_snapshot_metadata_is_used_when_the_inventory_is_unavailable(self):
        """Without Zotero there is nothing newer to consult, so the snapshot is all there is."""
        observation = _observation(
            in_zotero=False,
            inventory_available=False,
            zotero_metadata={},
            mapping_metadata={"title": "Edited", "doi": "10.1000/x", "citation_key": "k"},
        )
        self.assertIn(STATUS_METADATA_CHANGED, classify_item(observation))

    def test_empty_current_metadata_never_reports_a_change(self):
        """"We know nothing" must not masquerade as "everything differs"."""
        observation = _observation(
            in_zotero=True, inventory_available=True, zotero_metadata={}, mapping_metadata={}
        )
        self.assertNotIn(STATUS_METADATA_CHANGED, classify_item(observation))


class SnapshotConsistencyTests(unittest.TestCase):
    """A file-level copy of a database being written is not a consistent snapshot.

    The files are copied one after another, so a *checkpoint* landing between them leaves a
    main database and a `-wal` that were never a matching pair. SQLite's per-frame checksums do
    not catch it: they validate each frame, not that the frames apply to the base beside them.
    The failure mode is a wrong answer rather than an unreadable file, so it has to be detected
    rather than relied upon to crash.

    A checkpoint, specifically -- not any write. An ordinary commit only appends to the `-wal`
    and leaves the main database untouched, so a copy spanning one is still a matching pair.
    Retrying on every write would be both slower and less honest about what is wrong.
    """

    def _wal_database(self) -> tuple[Path, Path, object]:
        """A live WAL database with a connection held open, as Zotero holds one.

        Cleanup order matters on Windows: `addCleanup` is LIFO, so registering the directory
        removal *first* makes it run last, after the connection is closed. The other way round
        leaves the file locked and the teardown raises.
        """
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        db = directory / "zotero.sqlite"
        keeper = sqlite3.connect(db)
        self.addCleanup(keeper.close)
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("CREATE TABLE t (a INTEGER)")
        keeper.execute("INSERT INTO t VALUES (1)")
        keeper.commit()
        return directory, db, keeper

    def _interfere_after_main_copy(self, keeper, action, *, limit: int | None = None):
        """Patch `copy2` so `action(keeper)` runs once the main database has been copied.

        That is the dangerous instant: the copy holds the old main file and is about to take a
        `-wal` that may no longer belong to it.
        """
        from zotero_pdf_text import zotero_db

        real_copy = zotero_db.shutil.copy2
        calls = {"n": 0}

        def copy_then_interfere(src, dst, *args, **kwargs):
            result = real_copy(src, dst, *args, **kwargs)
            if str(src).endswith("zotero.sqlite") and (limit is None or calls["n"] < limit):
                calls["n"] += 1
                action(keeper)
            return result

        return patch.object(zotero_db.shutil, "copy2", copy_then_interfere), calls

    @staticmethod
    def _checkpoint(keeper):
        keeper.execute("INSERT INTO t VALUES (2)")
        keeper.commit()
        keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def test_a_checkpoint_during_the_copy_is_detected_and_retried(self):
        """The copy no longer matches the source, so it is thrown away rather than queried."""
        from zotero_pdf_text import zotero_db

        _directory, db, keeper = self._wal_database()
        patcher, calls = self._interfere_after_main_copy(keeper, self._checkpoint, limit=1)

        with patcher:
            with zotero_db.snapshot_for_reading(db) as copy:
                con = sqlite3.connect(copy)
                try:
                    rows = sorted(r[0] for r in con.execute("SELECT a FROM t"))
                finally:
                    con.close()

        self.assertEqual(calls["n"], 1, "the fixture did not checkpoint during the first copy")
        self.assertEqual(rows, [1, 2], "the retry did not pick up the checkpointed state")

    def test_a_plain_commit_between_copies_does_not_force_a_retry(self):
        """An append-only WAL write leaves the pair coherent, so the first copy stands.

        Guards the precision of the check, not just its sensitivity. A stability test that
        fired on any write at all would make the audit retry three times and give up on a
        perfectly ordinary library that happens to be in use.
        """
        from zotero_pdf_text import zotero_db

        _directory, db, keeper = self._wal_database()

        def commit_only(k):
            k.execute("INSERT INTO t VALUES (2)")
            k.commit()

        patcher, calls = self._interfere_after_main_copy(keeper, commit_only)

        with patcher:
            with zotero_db.snapshot_for_reading(db) as copy:
                con = sqlite3.connect(copy)
                try:
                    rows = sorted(r[0] for r in con.execute("SELECT a FROM t"))
                finally:
                    con.close()

        self.assertEqual(calls["n"], 1, "exactly one copy round should have happened")
        self.assertEqual(rows, [1, 2])

    def test_a_database_that_never_settles_is_reported_unavailable(self):
        """Never a quiet guess: an undecidable inventory is a stated gap."""
        from zotero_pdf_text import zotero_db

        _directory, db, keeper = self._wal_database()
        patcher, _calls = self._interfere_after_main_copy(keeper, self._checkpoint)

        with patcher:
            with self.assertRaises(zotero_db.SnapshotUnstableError) as ctx:
                with zotero_db.snapshot_for_reading(db, attempts=2):
                    pass
        self.assertIn("Close Zotero", str(ctx.exception))

    def test_size_and_timestamp_alone_would_miss_a_checkpoint(self):
        """Why the check compares content: a checkpoint can leave the file size unchanged.

        An in-place `UPDATE` rewrites existing pages, so the main database changes content
        while keeping its exact byte count. Timestamp granularity is a filesystem property
        rather than a guarantee, which leaves content as the only dependable comparison.
        """
        from zotero_pdf_text import zotero_db

        _directory, db, keeper = self._wal_database()
        keeper.execute("CREATE TABLE u (a INTEGER PRIMARY KEY, b TEXT)")
        for index in range(200):
            keeper.execute("INSERT INTO u VALUES (?, ?)", (index, "x" * 50))
        keeper.commit()
        keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        before_size = db.stat().st_size
        before_digest = zotero_db._digests(db)
        keeper.execute("UPDATE u SET b = 'y' WHERE a = 5")
        keeper.commit()
        keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        self.assertEqual(db.stat().st_size, before_size, "the fixture changed the file size")
        self.assertNotEqual(zotero_db._digests(db), before_digest)

    def test_the_shm_is_never_copied_and_costs_no_visibility(self):
        """The `-shm` is a reconstructible index, not data, and it churns on plain reads.

        SQLite rebuilds the WAL index from the `-wal` when a database is first opened, so
        leaving it behind loses nothing -- including rows that live only in an uncheckpointed
        WAL, which is the case `immutable=1` cannot handle at all. Copying it would only feed
        reader-driven churn into the stability check.
        """
        from zotero_pdf_text import zotero_db

        self.assertNotIn("-shm", zotero_db._SNAPSHOT_SUFFIXES)
        self.assertIn("-journal", zotero_db._SNAPSHOT_SUFFIXES)

        _directory, db, keeper = self._wal_database()
        keeper.execute("INSERT INTO t VALUES (7)")
        keeper.commit()
        self.assertTrue(Path(f"{db}-wal").stat().st_size > 0, "fixture has no uncheckpointed WAL")
        self.assertTrue(Path(f"{db}-shm").is_file(), "fixture has no -shm to leave behind")

        with zotero_db.snapshot_for_reading(db) as copy:
            self.assertFalse(
                Path(f"{copy}-shm").exists(), "the -shm was copied into the snapshot"
            )
            con = sqlite3.connect(copy)
            try:
                rows = sorted(r[0] for r in con.execute("SELECT a FROM t"))
            finally:
                con.close()
        self.assertEqual(rows, [1, 7], "a WAL-only row was lost without the -shm")

    def test_an_uncommitted_rollback_transaction_is_not_exposed(self):
        """Rollback mode writes dirty pages into the main database *before* the commit.

        The `-journal` holds the bytes that undo them, so a copy taken without it shows a
        transaction that may never land -- and shows it as ordinary data: the file is
        structurally intact and `PRAGMA integrity_check` returns `ok`. Carried along, the
        journal is hot in the copy and SQLite rolls it back on open.

        The audit would otherwise report attachments the user never committed, or miss ones
        whose deletion was rolled back, with nothing anywhere saying the answer was provisional.
        """
        from zotero_pdf_text import zotero_db

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        db = directory / "zotero.sqlite"
        writer = sqlite3.connect(db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
        for index in range(500):
            writer.execute("INSERT INTO t VALUES (?, ?)", (index, "committed" + "x" * 200))
        # A page cache too small to hold the transaction, which is what forces the spill.
        writer.execute("PRAGMA cache_size=10")

        writer.execute("BEGIN")
        writer.execute("UPDATE t SET b = 'DIRTY' || substr(b, 10)")
        self.assertTrue(
            Path(f"{db}-journal").is_file(), "the fixture produced no rollback journal"
        )

        # Prove the hazard is actually staged before asserting it is handled. Whether the page
        # cache spills is a platform and build detail, and without this the test would pass
        # vacuously anywhere it does not -- green for the reason the code is wrong elsewhere.
        # An `UPDATE` rather than an `INSERT` is what makes the spill reachable at all: appended
        # pages sit past the boundary the current header describes, so a reader of the main file
        # alone cannot see them, while an update dirties pages that header already reaches.
        bare = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bare, True)
        bare_db = bare / "zotero.sqlite"
        shutil.copy2(db, bare_db)
        con = sqlite3.connect(bare_db)
        try:
            exposed = con.execute("SELECT count(*) FROM t WHERE b LIKE 'DIRTY%'").fetchone()[0]
        finally:
            con.close()
        if not exposed:
            writer.execute("ROLLBACK")
            self.skipTest(
                "this build did not spill uncommitted pages into the main database, so the "
                "rollback-journal hazard could not be staged here"
            )

        with zotero_db.snapshot_for_reading(db) as copy:
            con = sqlite3.connect(copy)
            try:
                self.assertEqual(
                    con.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                    "a corrupt copy would be a different bug; this one is silently wrong data",
                )
                dirty = con.execute(
                    "SELECT count(*) FROM t WHERE b LIKE 'DIRTY%'"
                ).fetchone()[0]
            finally:
                con.close()

        writer.execute("ROLLBACK")
        self.assertEqual(
            dirty,
            0,
            f"the snapshot exposed an uncommitted transaction ({dirty} rows); a bare copy of "
            f"the main file alone exposed {exposed}",
        )
        self.assertEqual(
            writer.execute("SELECT count(*) FROM t WHERE b LIKE 'DIRTY%'").fetchone()[0], 0
        )

    def test_a_leftover_journal_costs_a_wal_database_nothing(self):
        """Carrying the `-journal` must not cost the WAL path anything.

        A database that has been through a `journal_mode` change can leave a `-journal` beside
        a `-wal`. SQLite ignores it in WAL mode, but a snapshot that silently dropped
        WAL-resident rows here would be the previous bug wearing a new hat.
        """
        from zotero_pdf_text import zotero_db

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        db = directory / "zotero.sqlite"
        writer = sqlite3.connect(db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("CREATE TABLE t (a INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO t VALUES (1)")
        writer.execute("PRAGMA journal_mode=PERSIST")
        writer.execute("INSERT INTO t VALUES (2)")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO t VALUES (3)")

        with zotero_db.snapshot_for_reading(db) as copy:
            con = sqlite3.connect(copy)
            try:
                rows = sorted(row[0] for row in con.execute("SELECT a FROM t"))
            finally:
                con.close()
        self.assertEqual(rows, [1, 2, 3])

    # SQLite's rollback-journal magic, and the trailer layout `readMasterJournal` expects:
    # [name][4-byte name length][4-byte checksum][8-byte magic] at the end of the file.
    _JOURNAL_MAGIC = bytes([0xD9, 0xD5, 0x05, 0xF9, 0x20, 0xA1, 0x63, 0xD7])

    def _database_with_crafted_journal(self, directory: Path, names: Path) -> Path:
        """A valid database beside a journal whose trailer names `names`.

        Written by hand because SQLite only produces one during the commit of a transaction
        spanning attached databases, which lasts microseconds and cannot be caught reliably.
        The bytes are the same either way, and the bytes are what SQLite acts on.
        """
        db = directory / "zotero.sqlite"
        con = sqlite3.connect(db)
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("CREATE TABLE t (a INTEGER PRIMARY KEY)")
            con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
            con.commit()
            page_size = con.execute("PRAGMA page_size").fetchone()[0]
            page_count = con.execute("PRAGMA page_count").fetchone()[0]
        finally:
            con.close()

        header = (
            self._JOURNAL_MAGIC
            + struct.pack(">I", 0)           # nRec: no page records to replay
            + struct.pack(">I", 0)           # cksumInit
            + struct.pack(">I", page_count)  # database size, in pages
            + struct.pack(">I", 512)         # sector size
            + struct.pack(">I", page_size)
        ).ljust(512, b"\x00")
        name = str(names).encode("utf-8")
        trailer = (
            name
            + struct.pack(">I", len(name))
            + struct.pack(">I", sum(name) & 0xFFFFFFFF)
            + self._JOURNAL_MAGIC
        )
        Path(f"{db}-journal").write_bytes(header + trailer)
        return db

    def test_a_journal_naming_a_super_journal_cannot_delete_a_file_outside_the_snapshot(self):
        """A rollback journal is not inert data; it can name a file for SQLite to delete.

        The trailer can carry a super-journal pathname -- an absolute path -- and SQLite
        follows it when the journal is hot, deleting what it names once no database still
        references it. Copying the journal into staging does not confine that to staging,
        because the path travels inside the file. The copy reads back correctly and reports no
        error while the deletion happens, so nothing about the query itself would reveal it.
        """
        from zotero_pdf_text import zotero_db

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        outside = directory / "outside"
        outside.mkdir()

        # Two sentinels. The first proves this SQLite build actually performs the deletion;
        # the second is the one the guard has to save. Without the first, the test would pass
        # vacuously wherever SQLite declines to follow the name -- green for the reason the
        # code is wrong elsewhere.
        proof = outside / "PROOF"
        proof.write_bytes(b"\x00")
        staged = directory / "staged"
        staged.mkdir()
        db = self._database_with_crafted_journal(staged, proof)
        bare = directory / "bare"
        bare.mkdir()
        shutil.copy2(db, bare / "zotero.sqlite")
        shutil.copy2(f"{db}-journal", bare / "zotero.sqlite-journal")
        con = sqlite3.connect(bare / "zotero.sqlite")
        try:
            con.execute("SELECT count(*) FROM t").fetchone()
        except sqlite3.Error:
            pass
        finally:
            con.close()
        if proof.exists():
            self.skipTest(
                "this SQLite build did not follow the super-journal name, so the deletion "
                "could not be staged here"
            )

        guarded = outside / "GUARDED"
        guarded.write_bytes(b"\x00")
        live = directory / "live"
        live.mkdir()
        target = self._database_with_crafted_journal(live, guarded)

        with self.assertRaises(zotero_db.SnapshotUnsafeError):
            with zotero_db.snapshot_for_reading(target) as copy:
                sqlite3.connect(copy).close()

        self.assertTrue(
            guarded.exists(),
            "the snapshot let SQLite delete a file outside the staging directory",
        )

    def test_an_ordinary_rollback_journal_is_still_accepted(self):
        """The refusal must not swallow the journals the snapshot was taught to carry.

        A single-database transaction writes no super-journal trailer, so the guard has
        nothing to match on. Were it to match anyway, every rollback-mode database would
        report an unavailable inventory and the previous fix would be silently undone.
        """
        from zotero_pdf_text import zotero_db

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        db = directory / "zotero.sqlite"
        writer = sqlite3.connect(db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
        for index in range(200):
            writer.execute("INSERT INTO t VALUES (?, ?)", (index, "committed"))
        writer.execute("PRAGMA cache_size=10")
        writer.execute("BEGIN")
        writer.execute("UPDATE t SET b = 'DIRTY'")
        self.assertTrue(Path(f"{db}-journal").is_file(), "the fixture produced no journal")
        self.assertFalse(zotero_db._names_super_journal(Path(f"{db}-journal")))

        with zotero_db.snapshot_for_reading(db) as copy:
            con = sqlite3.connect(copy)
            try:
                dirty = con.execute("SELECT count(*) FROM t WHERE b = 'DIRTY'").fetchone()[0]
            finally:
                con.close()
        writer.execute("ROLLBACK")
        self.assertEqual(dirty, 0)

    def test_a_journal_that_cannot_be_read_is_refused_rather_than_assumed_safe(self):
        """Not being able to look is not the same as having looked and found nothing.

        An absent journal is the ordinary case for a WAL database and must stay cheap; a
        journal that exists but will not open is the one that has to fail closed.
        """
        from zotero_pdf_text import zotero_db

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        journal = directory / "zotero.sqlite-journal"

        self.assertFalse(zotero_db._names_super_journal(journal), "absent is not suspicious")
        journal.write_bytes(b"x" * 64)
        self.assertFalse(zotero_db._names_super_journal(journal), "no trailer, no refusal")
        with patch("builtins.open", side_effect=OSError("unreadable")):
            self.assertTrue(zotero_db._names_super_journal(journal))

    def test_a_trailer_that_names_nothing_is_not_a_super_journal_reference(self):
        """Precision, not just sensitivity: a zero-length name means no super-journal.

        SQLite's own `readMasterJournal` reads a zero length as "none" and goes on to open the
        database. Refusing on the magic alone would make this snapshot stricter than SQLite and
        report an unavailable inventory for a database SQLite would have read without ever
        reaching outside the snapshot.
        """
        from zotero_pdf_text import zotero_db

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        journal = directory / "zotero.sqlite-journal"
        journal.write_bytes(
            b"page data" * 8
            + struct.pack(">I", 0)
            + struct.pack(">I", 0)
            + self._JOURNAL_MAGIC
        )
        self.assertFalse(zotero_db._names_super_journal(journal))

    def test_an_unstable_database_still_lets_the_audit_finish(self):
        """End-to-end degradation only: the audit completes and flags the gap.

        This does not guard the stability check itself -- the fixture's schema is not Zotero's,
        so the inventory would be unavailable either way. `test_a_database_that_never_settles`
        is the test that fails when the check is removed.
        """
        root, db, keeper = self._wal_database()
        snapshot = root / "mapping_report.jsonl"
        snapshot.write_text("", encoding="utf-8")
        patcher, _calls = self._interfere_after_main_copy(keeper, self._checkpoint)

        config = ProjectConfig(
            zotero_root=root,
            zotero_data_directory=root,
            linked_attachments=root,
            output_root=root / "out",
        )
        self.assertEqual(config.zotero_sqlite, db)
        with patcher:
            audit = audit_library(config, snapshot, index_root=root / "index")
        self.assertFalse(audit.inventory_available)

    def test_the_audit_reports_why_the_inventory_was_unavailable(self):
        """The recovery instruction must survive the catch that keeps the audit running.

        `inventory_available: false` alone cannot tell a user whether retrying is worth
        anything. An unstable database is fixed by closing Zotero; a permission or schema
        failure never will be, and the two call for different next steps.
        """
        root, db, keeper = self._wal_database()
        snapshot = root / "mapping_report.jsonl"
        snapshot.write_text("", encoding="utf-8")
        patcher, _calls = self._interfere_after_main_copy(keeper, self._checkpoint)

        config = ProjectConfig(
            zotero_root=root,
            zotero_data_directory=root,
            linked_attachments=root,
            output_root=root / "out",
        )
        with patcher:
            audit = audit_library(config, snapshot, index_root=root / "index")

        self.assertIsNotNone(audit.inventory_error)
        self.assertIn("SnapshotUnstableError", audit.inventory_error or "")
        self.assertIn("Close Zotero", audit.inventory_error or "")
        self.assertEqual(audit.to_dict()["inventory_error"], audit.inventory_error)

    def test_a_readable_inventory_reports_no_error(self):
        """The reason field must stay empty on the happy path, or it is just noise."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            pdf = root / "AAAA1111.pdf"
            pdf.write_bytes(b"pdf")
            _write_zotero_inventory(config, {"AAAA1111": str(pdf)})
            snapshot = root / "mapping_report.jsonl"
            snapshot.write_text("", encoding="utf-8")
            audit = audit_library(config, snapshot, index_root=root / "index")
        self.assertTrue(audit.inventory_available)
        self.assertIsNone(audit.inventory_error)


class IncompletePublicationTests(unittest.TestCase):
    """Past the pointer, absence is corruption rather than an unbuilt library.

    `resolve_generation_dir` does not require the directory to exist, so treating a missing
    JSONL as "no rows" hands back the generation id with an empty index. Every eligible
    attachment then reports `unindexed` and a broken publication reads as a routine backlog --
    the interpretation most likely to send someone re-converting a library that is fine.
    """

    def _published(self, index_root: Path) -> str:
        from zotero_pdf_text.artifacts import (
            publish_generation,
            stage_generation,
            validate_generation,
            write_jsonl_from_existing,
        )

        index_root.mkdir(parents=True, exist_ok=True)
        source = index_root / "source.jsonl"
        source.write_text(json.dumps(_index_record("AAAA1111")) + "\n", encoding="utf-8")
        info = stage_generation(index_root, write_jsonl_from_existing(source), command="test")
        validate_generation(index_root, info.generation_id)
        publish_generation(index_root, info.generation_id)
        return info.generation_id

    def test_a_missing_jsonl_is_an_error_not_an_empty_index(self):
        from zotero_pdf_text.artifacts import CurrentPointerError, resolve_generation_dir

        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp) / "index"
            generation_id = self._published(index_root)
            (resolve_generation_dir(index_root, generation_id) / "index.jsonl").unlink()

            with self.assertRaises(CurrentPointerError) as ctx:
                load_index_records(index_root)
            self.assertIn("rebuild-index", str(ctx.exception))

    def test_a_missing_generation_directory_is_an_error(self):
        from zotero_pdf_text.artifacts import CurrentPointerError, resolve_generation_dir

        with tempfile.TemporaryDirectory() as tmp:
            index_root = Path(tmp) / "index"
            generation_id = self._published(index_root)
            shutil.rmtree(resolve_generation_dir(index_root, generation_id))

            with self.assertRaises(CurrentPointerError):
                load_index_records(index_root)

    def test_the_audit_surfaces_an_incomplete_publication(self):
        """The CLI turns this into a non-zero exit, not a report full of false `unindexed`."""
        from zotero_pdf_text.artifacts import ArtifactError, resolve_generation_dir

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_root = root / "index"
            generation_id = self._published(index_root)
            (resolve_generation_dir(index_root, generation_id) / "index.jsonl").unlink()
            snapshot = root / "mapping_report.jsonl"
            snapshot.write_text("", encoding="utf-8")

            with self.assertRaises(ArtifactError):
                audit_library(_config(root), snapshot, index_root=index_root)


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
