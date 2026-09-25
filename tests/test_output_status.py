import json
from pathlib import Path

from zotero_pdf_text.cli import main
from zotero_pdf_text.output_status import output_status
from zotero_pdf_text.config import ProjectConfig
from zotero_pdf_text.mcp_contract import latest_mapping_snapshot


def test_output_status_groups_active_markdown_by_folder(tmp_path: Path, capsys):
    root = tmp_path / "output"
    index_root = root / "index"
    current_id = "20260925T120000Z-1234abcd"
    previous_id = "20260924T120000Z-1234abcd"
    first = root / "verified" / "first" / "markdown" / "a.md"
    second = root / "verified" / "second" / "markdown" / "b.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    for generation_id, paths in ((current_id, [first, second]), (previous_id, [first])):
        generation = index_root / "generations" / generation_id
        generation.mkdir(parents=True)
        (generation / "index.jsonl").write_text(
            "".join(json.dumps({"markdown_path": str(path)}) + "\n" for path in paths), encoding="utf-8"
        )
    (index_root / "current.json").write_text(
        json.dumps({"current_generation": current_id, "previous_generation": previous_id}), encoding="utf-8"
    )

    report = output_status(root, list_files=True)
    assert report["current"]["records"] == 2
    assert len(report["current"]["folders"]) == 2
    assert all(folder["physical_path"] == folder["path"] for folder in report["current"]["folders"])
    assert report["previous"]["records"] == 1
    assert report["current"]["markdown_files"] == sorted([str(first), str(second)])

    assert main(["output-status", "--output-root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["current"]["generation_id"] == current_id


def test_latest_mapping_snapshot_accepts_new_and_old_names(tmp_path: Path):
    root = tmp_path / "output"
    config = ProjectConfig(tmp_path, tmp_path, tmp_path, root)
    old = root / "runs" / "old" / "mapping_report.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text("", encoding="utf-8")
    assert latest_mapping_snapshot(config) == old
    new = root / "mapping-runs" / "new" / "mapping_report.jsonl"
    new.parent.mkdir(parents=True)
    new.write_text("", encoding="utf-8")
    assert latest_mapping_snapshot(config) == new
