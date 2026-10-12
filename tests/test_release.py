"""Publication guards must reject a mismatched identity or private artifact contents."""

from pathlib import Path
import importlib.util
import shutil
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_members = load_tool("check_distribution").check_members
release_notes = load_tool("release_notes").release_notes


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock", "CITATION.cff", "README.md", "CHANGELOG.md"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    return tmp_path


def test_release_notes_exclude_other_versions(release_root: Path) -> None:
    version = current_ref(release_root).removeprefix("refs/tags/v")
    (release_root / "CHANGELOG.md").write_text(
        f"## [Unreleased]\n\n## [{version}] - 2026-10-12\n\nCurrent release marker.\n\n"
        "## [0.0.0] - 2026-01-01\n\nEarlier release marker.\n",
        encoding="utf-8",
    )
    notes = release_notes(release_root, current_ref(release_root), "push")
    assert "Current release marker" in notes
    assert "Earlier release marker" not in notes


@pytest.mark.parametrize("ref,event", [
    ("refs/tags/v0.0.0", "push"),
    ("refs/heads/master", "push"),
    ("refs/heads/feature", "workflow_dispatch"),
])
def test_wrong_ref_rejected(release_root: Path, ref: str, event: str) -> None:
    with pytest.raises(ValueError):
        release_notes(release_root, ref, event)


def test_missing_notes_rejected(release_root: Path) -> None:
    (release_root / "CHANGELOG.md").write_text("## [Unreleased]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Missing dated"):
        release_notes(release_root, current_ref(release_root), "push")


def test_testpypi_rehearsal_from_master(release_root: Path) -> None:
    assert release_notes(release_root, "refs/heads/master", "workflow_dispatch")


def test_mismatched_lock_version_rejected(release_root: Path) -> None:
    version = current_ref(release_root).removeprefix("refs/tags/v")
    lock = release_root / "uv.lock"
    lock.write_text(lock.read_text(encoding="utf-8").replace(
        f'name = "zotero-fulltext-mcp"\nversion = "{version}"',
        'name = "zotero-fulltext-mcp"\nversion = "0.0.0"',
    ), encoding="utf-8")
    with pytest.raises(ValueError, match="Lockfile version"):
        release_notes(release_root, current_ref(release_root), "push")


def current_ref(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return f"refs/tags/v{project['version']}"


@pytest.mark.parametrize("private_path", [
    "test-results/config.example.json", "config.json", ".venv-release/data.txt",
])
def test_private_content_rejected(private_path: str) -> None:
    with pytest.raises(ValueError):
        check_members([private_path], sdist=False)


def test_missing_server_rejected() -> None:
    with pytest.raises(ValueError, match="Missing packaged module"):
        check_members(["zotero_pdf_text/__init__.py", "zotero_pdf_text/cli.py"], sdist=False)
