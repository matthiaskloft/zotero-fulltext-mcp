import csv
import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from zotero_pdf_text import artifacts
from zotero_pdf_text._library_publish import PUBLICATION_DIR, stage_library_files
from zotero_pdf_text.artifacts import (
    current_generation_jsonl,
    read_current_pointer,
    recover_pending_publication,
    stage_and_publish,
    stage_generation,
    write_jsonl_from_existing,
    write_jsonl_from_manifest_keeping_current,
)
from zotero_pdf_text.fts import search_fts
from zotero_pdf_text.identity import strip_front_matter
from zotero_pdf_text.indexer import _record_from_manifest_row
from zotero_pdf_text.library import locate_images_dir, read_extracted_image
from zotero_pdf_text.pages import read_page_map_sidecar, write_page_map_sidecar


KEY = "AAAA1111"


def conversion(root, key=KEY, *, body="New evidence", identity="verified"):
    markdown = root / "run" / "markdown" / f"0001_{key}.md"
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(f"---\nzotero_attachment_key: {key}\n---\n\n{body}\n", encoding="utf-8")
    row = {
        "output_path": str(markdown), "zotero_attachment_key": key,
        "zotero_parent_key": "PPPP1111", "classification": "mapped_verified",
        "identity_status": identity, "extraction_tool": "pymupdf4llm.to_markdown",
    }
    return asdict(_record_from_manifest_row(row)), markdown


def publish(root, records, **kwargs):
    source = root / "records.jsonl"
    source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
    return stage_and_publish(root / "index", write_jsonl_from_existing(source), command="test", **kwargs)


def seed(root, keys=(KEY,)):
    records = []
    for key in keys:
        markdown = root / "library" / key / f"{key}.md"
        markdown.parent.mkdir(parents=True)
        markdown.write_text("Old evidence", encoding="utf-8")
        images = markdown.parent / "images"
        images.mkdir()
        (images / "old.png").write_bytes(b"old image")
        (markdown.parent / "reader.txt").write_text("Keep this sibling", encoding="utf-8")
        row, _ = conversion(root, key)
        row.update(markdown_path=str(markdown), markdown_sha256=hashlib.sha256(markdown.read_bytes()).hexdigest(),
                   text="Old evidence", char_count=12, word_count=2)
        records.append(row)
    return publish(root, records)


def current(root):
    return [json.loads(line) for line in current_generation_jsonl(root / "index").read_text(encoding="utf-8").split("\n") if line]


def test_publication_survives_run_removal_with_images_and_page_maps(tmp_path):
    seed(tmp_path)
    body = "First ![plot](../images/0001_AAAA1111/plot%20%281%29.png)\n\nSecond evidence"
    row, markdown = conversion(tmp_path, body=body)
    split = body.index("Second")
    write_page_map_sidecar(markdown, body, [(1, 0, split), (2, split, len(body))])
    row["page_map"] = read_page_map_sidecar(markdown, body)
    images = tmp_path / "run" / "images" / markdown.stem
    images.mkdir(parents=True)
    (images / "plot (1).png").write_bytes(b"new image")
    info = publish(tmp_path, [row], publish_library=True)
    shutil.rmtree(tmp_path / "run")

    record = current(tmp_path)[0]
    target = tmp_path / "library" / KEY / "versions" / info.generation_id / f"{KEY}.md"
    assert Path(record["markdown_path"]) == target
    assert "images/plot%20%281%29.png" in record["text"]
    assert strip_front_matter(target.read_text(encoding="utf-8")) == record["text"]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == record["markdown_sha256"]
    assert read_page_map_sidecar(target, record["text"]) == [tuple(p) for p in record["page_map"]]
    assert record["text"][record["page_map"][1][1]:] == "Second evidence"
    assert (target.parent / "images" / "plot (1).png").read_bytes() == b"new image"
    assert not (target.parent / "images" / "old.png").exists()
    assert (tmp_path / "library" / KEY / "reader.txt").is_file()
    assert search_fts(info.db_path, "evidence")
    assert not (info.directory / PUBLICATION_DIR).exists()
    assert f"{KEY}/versions/{info.generation_id}/" in (tmp_path / "library" / "catalog.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["build", "pointer", "install"])
def test_publication_failure_restores_all_previous_files(tmp_path, failure):
    first = seed(tmp_path, (KEY, "BBBB2222"))
    rows = [conversion(tmp_path, key)[0] for key in (KEY, "BBBB2222")]
    if failure == "build":
        context = patch.object(artifacts, "build_fts_index", side_effect=OSError("disk full"))
    elif failure == "pointer":
        original = artifacts._atomic_write_json

        def fail_pointer(path, payload):
            if path.name == "current.json":
                raise OSError("disk full")
            original(path, payload)

        context = patch.object(artifacts, "_atomic_write_json", side_effect=fail_pointer)
    else:
        from zotero_pdf_text import _library_publish
        original_replace = _library_publish.replace_with_retry

        def fail_install(source, target):
            if source.parent.name == "install" and source.name == "BBBB2222":
                raise OSError("disk full")
            original_replace(source, target)

        context = patch.object(_library_publish, "replace_with_retry", side_effect=fail_install)
    with context, pytest.raises(OSError, match="disk full"):
        publish(tmp_path, rows, publish_library=True)
    assert read_current_pointer(tmp_path / "index")["current_generation"] == first.generation_id
    assert recover_pending_publication(tmp_path / "index") is None
    for key in (KEY, "BBBB2222"):
        folder = tmp_path / "library" / key
        assert (folder / f"{key}.md").read_text(encoding="utf-8") == "Old evidence"
        assert (folder / "images" / "old.png").read_bytes() == b"old image"


@pytest.mark.parametrize("existing", [True, False])
def test_writer_recovers_hard_exit_before_pointer_swap(tmp_path, existing):
    first = seed(tmp_path) if existing else publish(tmp_path, [])
    row, _ = conversion(tmp_path)
    original = artifacts._atomic_write_json

    def fail_pointer(path, payload):
        if path.name == "current.json":
            raise KeyboardInterrupt()
        original(path, payload)

    # Suppress synchronous rollback to model a killed process after library installation.
    with patch.object(artifacts, "_atomic_write_json", side_effect=fail_pointer), patch(
        "zotero_pdf_text._library_publish.rollback_library_files", side_effect=KeyboardInterrupt
    ), pytest.raises(KeyboardInterrupt):
        publish(tmp_path, [row], publish_library=True)
    # Readers must remain consistent before any writer attempts recovery.
    assert read_current_pointer(tmp_path / "index")["current_generation"] == first.generation_id
    if existing:
        assert Path(current(tmp_path)[0]["markdown_path"]).read_text(encoding="utf-8") == "Old evidence"
    assert "rolled back" in recover_pending_publication(tmp_path / "index")
    assert read_current_pointer(tmp_path / "index")["current_generation"] == first.generation_id
    target = tmp_path / "library" / KEY / f"{KEY}.md"
    if existing:
        assert target.read_text(encoding="utf-8") == "Old evidence"
    else:
        assert not target.exists()
    assert recover_pending_publication(tmp_path / "index") is None


def test_committed_publication_is_kept_when_cleanup_is_interrupted(tmp_path):
    seed(tmp_path)
    row, _ = conversion(tmp_path)
    with patch("zotero_pdf_text._library_publish.cleanup_library_files", side_effect=OSError("busy")):
        info = publish(tmp_path, [row], publish_library=True)
    assert "completed" in recover_pending_publication(tmp_path / "index")
    assert read_current_pointer(tmp_path / "index")["current_generation"] == info.generation_id
    assert Path(current(tmp_path)[0]["markdown_path"]).read_text(encoding="utf-8").endswith("New evidence\n")


def test_exception_after_pointer_replacement_does_not_undo_committed_files(tmp_path):
    seed(tmp_path)
    row, _ = conversion(tmp_path)
    original = artifacts._atomic_write_json

    def fail_after_commit(path, payload):
        original(path, payload)
        if path.name == "current.json":
            raise OSError("interrupted after commit")

    with patch.object(artifacts, "_atomic_write_json", side_effect=fail_after_commit):
        info = publish(tmp_path, [row], publish_library=True)
    assert read_current_pointer(tmp_path / "index")["current_generation"] == info.generation_id
    assert "New evidence" in Path(current(tmp_path)[0]["markdown_path"]).read_text(encoding="utf-8")


def test_retained_records_and_unverified_reviews_are_not_promoted(tmp_path):
    old, markdown = conversion(tmp_path)
    publish(tmp_path, [old])
    unverified, _ = conversion(tmp_path, "BBBB2222", identity="unverified")
    publish(tmp_path, [old, unverified], publish_library=True)
    assert current(tmp_path) == [old, unverified]
    assert markdown.is_file()
    assert not (tmp_path / "library" / KEY).exists()
    assert not (tmp_path / "library" / "BBBB2222").exists()


def test_staging_never_overwrites_current_library(tmp_path):
    seed(tmp_path)
    row, _ = conversion(tmp_path)
    source = tmp_path / "new.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def writer(path):
        shutil.copyfile(source, path)
        stage_library_files(tmp_path / "index", path, current_generation_jsonl(tmp_path / "index"))

    stage_generation(tmp_path / "index", writer, command="test")
    target = tmp_path / "library" / KEY / f"{KEY}.md"
    assert target.read_text(encoding="utf-8") == "Old evidence"


def test_keep_current_preserves_better_extraction_and_its_images(tmp_path):
    seed(tmp_path)
    old = current(tmp_path)[0]
    old.update(extraction_tool="marker", source_sha256="a" * 64)
    publish(tmp_path, [old])
    row, markdown = conversion(tmp_path, body="Lower quality")
    manifest = tmp_path / "manifest.csv"
    manifest_row = {
        "status": "converted", "output_path": str(markdown), "zotero_attachment_key": KEY,
        "extraction_tool": "pymupdf.get_text", "source_sha256": "a" * 64,
    }
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(manifest_row))
        writer.writeheader()
        writer.writerow(manifest_row)
    writer, kept = write_jsonl_from_manifest_keeping_current(manifest, current_generation_jsonl(tmp_path / "index"))
    assert kept == 1
    stage_and_publish(tmp_path / "index", writer, command="test", publish_library=True)
    assert current(tmp_path) == [old]
    folder = tmp_path / "library" / KEY
    assert (folder / f"{KEY}.md").read_text(encoding="utf-8") == "Old evidence"
    assert (folder / "images" / "old.png").read_bytes() == b"old image"


@pytest.mark.parametrize("command", ["rebuild-index", "update-index"])
def test_manifest_commands_publish_accepted_reviews(tmp_path, command, capsys):
    from zotero_pdf_text.cli import main

    publish(tmp_path, [])
    _, markdown = conversion(tmp_path, body="Reviewed evidence")
    manifest = tmp_path / "trusted.csv"
    row = {
        "status": "skipped_existing", "output_path": str(markdown), "zotero_attachment_key": KEY,
        "classification": "mapped_verified", "identity_status": "fulltext_verified",
    }
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({name: str(tmp_path) for name in (
        "zotero_root", "zotero_data_directory", "linked_attachments", "output_root"
    )}), encoding="utf-8")
    assert main([command, "--config", str(config), "--manifest", str(manifest)]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 1
    shutil.rmtree(tmp_path / "run")
    record = current(tmp_path)[0]
    assert Path(record["markdown_path"]).read_text(encoding="utf-8").endswith("Reviewed evidence\n")
    assert record["identity_status"] == "fulltext_verified"


def test_readers_keep_their_own_equation_images_before_and_after_commit(tmp_path):
    old_image = b"\x89PNG\r\n\x1a\nold equation"
    new_image = b"\x89PNG\r\n\x1a\nnew equation"
    row, markdown = conversion(tmp_path, body="Old ![equation](../images/0001_AAAA1111/equation.png)")
    images = tmp_path / "run" / "images" / markdown.stem
    images.mkdir(parents=True)
    (images / "equation.png").write_bytes(old_image)
    first = publish(tmp_path, [row], publish_library=True)
    old = current(tmp_path)[0]

    def read_image(record):
        directory = locate_images_dir(Path(record["markdown_path"]), attachment_key=KEY, output_root=tmp_path)
        return read_extracted_image(directory, "images/equation.png", max_bytes=1024)[0]

    row, _ = conversion(tmp_path, body="New ![equation](../images/0001_AAAA1111/equation.png)")
    (images / "equation.png").write_bytes(new_image)
    original = artifacts._atomic_write_json
    observed = []

    def check_before_pointer(path, payload):
        if path.name == "current.json":
            assert current(tmp_path) == [old]
            assert read_image(old) == old_image
            assert "Old" in Path(old["markdown_path"]).read_text(encoding="utf-8")
            candidate = json.loads((tmp_path / "index" / "versions" / payload["current_generation"] / "index.jsonl").read_text(encoding="utf-8"))
            assert read_image(candidate) == new_image
            observed.append(True)
        original(path, payload)

    with patch.object(artifacts, "_atomic_write_json", side_effect=check_before_pointer):
        publish(tmp_path, [row], publish_library=True)
    assert observed
    assert read_image(old) == old_image
    assert read_image(current(tmp_path)[0]) == new_image
    # Sweeping the originating index generation must not delete an inherited asset revision.
    publish(tmp_path, current(tmp_path))
    assert not first.directory.exists()
    assert read_image(old) == old_image
    assert read_image(current(tmp_path)[0]) == new_image


def test_missing_revision_images_never_fall_back_to_canonical_images(tmp_path):
    seed(tmp_path)
    row, _ = conversion(tmp_path)
    publish(tmp_path, [row], publish_library=True)
    record = current(tmp_path)[0]
    misleading = tmp_path / Path(record["markdown_path"]).parent.name / "images"
    misleading.mkdir(parents=True)
    (misleading / "old.png").write_bytes(b"unrelated image")
    assert locate_images_dir(Path(record["markdown_path"]), attachment_key=KEY, output_root=tmp_path) is None


@pytest.mark.parametrize("command,options,expected", [
    ("rebuild-index", [], 0),
    ("rebuild-index", ["--keep-current"], 2),
    ("update-index", [], 2),
    ("update-index", ["--replace-existing"], 2),
])
def test_missing_current_jsonl_allows_only_independent_manifest_rebuild(tmp_path, command, options, expected, capsys):
    from zotero_pdf_text.cli import main

    first = seed(tmp_path)
    _, markdown = conversion(tmp_path, body="Recovered evidence")
    manifest = tmp_path / "manifest.csv"
    row = {
        "status": "converted", "output_path": str(markdown), "zotero_attachment_key": KEY,
        "classification": "mapped_verified", "identity_status": "verified",
    }
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    first.jsonl_path.unlink()

    assert main([command, "--output-root", str(tmp_path), "--manifest", str(manifest), *options]) == expected
    output = capsys.readouterr()
    if expected == 0:
        assert json.loads(output.out)["generation_id"] != first.generation_id
        record = current(tmp_path)[0]
        assert record["text"] == "Recovered evidence"
        assert Path(record["markdown_path"]).is_file()
    else:
        assert "JSONL file is missing" in output.err
        assert read_current_pointer(tmp_path / "index")["current_generation"] == first.generation_id
    assert (tmp_path / "library" / KEY / f"{KEY}.md").read_text(encoding="utf-8") == "Old evidence"
