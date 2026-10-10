from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from zotero_pdf_text.catalog import README_FILENAME, write_output_guides
from zotero_pdf_text.layout import new_run_dir, run_name


def _write_index(path: Path, *records: dict[str, str]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def test_run_names_lead_with_the_date_and_name_the_command():
    when = datetime(2026, 10, 10, 14, 15, 0)
    assert run_name("dry-run", now=when) == "2026-10-10_141500_dry-run"
    assert run_name("retry-timeout", detail="ABCD1234", now=when) == "2026-10-10_141500_retry-timeout_ABCD1234"


def test_a_second_run_in_the_same_second_gets_its_own_folder(tmp_path):
    first = new_run_dir(tmp_path, "dry-run")
    first.mkdir(parents=True)
    second = new_run_dir(tmp_path, "dry-run")
    assert second != first
    assert second.parent == tmp_path / "runs"


def test_catalog_links_resolve_relative_to_the_catalog(tmp_path):
    output_root = tmp_path / "converted_text"
    library = output_root / "library"
    run_markdown = output_root / "runs" / "2026-10-10_141500_verify-unverified" / "markdown" / "0001_paper.md"
    index = _write_index(
        tmp_path / "index.jsonl",
        {
            "zotero_attachment_key": "BBBB2222",
            "citation_key": "smith2020",
            "year": "2020",
            "creators": "Smith, Jane",
            "title": "A | piped [title]",
            "markdown_path": str(library / "BBBB2222" / "BBBB2222.md"),
        },
        {"zotero_attachment_key": "AAAA1111", "citation_key": "adams2019", "markdown_path": str(run_markdown)},
    )

    write_output_guides(output_root, index)

    with (library / "catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["citation_key"] for row in rows] == ["adams2019", "smith2020"]
    assert rows[1]["file"] == "BBBB2222/BBBB2222.md"
    assert rows[0]["file"] == "../runs/2026-10-10_141500_verify-unverified/markdown/0001_paper.md"
    for row in rows:
        assert (library / row["file"]).resolve() == Path(
            run_markdown if row["attachment_key"] == "AAAA1111" else library / "BBBB2222" / "BBBB2222.md"
        ).resolve()

    markdown = (library / "catalog.md").read_text(encoding="utf-8")
    assert "[A \\| piped \\[title\\]](BBBB2222/BBBB2222.md)" in markdown
    assert (output_root / README_FILENAME).is_file()


def test_unchanged_catalog_is_not_rewritten(tmp_path):
    output_root = tmp_path / "out"
    index = _write_index(tmp_path / "index.jsonl", {"zotero_attachment_key": "AAAA1111", "title": "T"})
    write_output_guides(output_root, index)
    catalog = output_root / "library" / "catalog.csv"
    before = catalog.stat().st_mtime_ns

    write_output_guides(output_root, index)

    assert catalog.stat().st_mtime_ns == before
