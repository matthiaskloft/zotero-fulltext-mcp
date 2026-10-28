"""Immutable library revisions coordinated with index publication."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import quote, unquote

from ._atomic import atomic_write_text, replace_with_retry
from .artifacts import ArtifactError
from .config import ProjectConfig
from .fts import image_destination_spans
from .identity import strip_front_matter
from .indexer import _sha256
from .library import _strip_link_title, images_dir_for, is_canonical_eligible, library_revision_markdown_path
from .pages import page_map_for_body, page_map_sidecar_path, validate_page_map, write_page_map_sidecar

PUBLICATION_DIR = "library_publication"


def _target(index_root: Path, key: str, generation_id: str) -> Path:
    root = index_root.parent.resolve()
    config = ProjectConfig(root, root, root, root)
    target = library_revision_markdown_path(config, key, generation_id).parent
    # A symlink/junction must not redirect a publication or rollback outside the library.
    if target.resolve() != root / "library" / key / "versions" / generation_id:
        raise ArtifactError(f"Library destination is redirected: {target}")
    return target


def canonical_image_links(text: str) -> str:
    for start, end in reversed(image_destination_spans(text)):
        destination = text[start:end]
        target = _strip_link_title(destination)
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", target):
            continue
        name = unquote(target.replace("\\", "/").rsplit("/", 1)[-1])
        if not name or name in {".", ".."} or any(char in name for char in "/\\\x00"):
            continue
        text = text[:start] + destination.replace(target, "images/" + quote(name), 1) + text[end:]
    return text


def stage_library_files(index_root: Path, jsonl_path: Path, current_jsonl: Path | None) -> None:
    """Copy only newly accepted, noncanonical records; leave retained records verbatim."""
    current = {}
    if current_jsonl is not None:
        current = {
            row["zotero_attachment_key"]: row
            for line in current_jsonl.read_text(encoding="utf-8").split("\n") if line.strip()
            for row in [json.loads(line)]
        }
    publication = jsonl_path.parent / PUBLICATION_DIR
    plan: list[dict[str, object]] = []
    lines = []
    seen = set()
    for line in jsonl_path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        record = json.loads(line)
        key = record["zotero_attachment_key"]
        old = current.get(key, {})
        if not is_canonical_eligible(record) or all(
            record.get(field) == old.get(field)
            for field in ("markdown_path", "markdown_sha256", "text")
        ):
            lines.append(line)
            continue
        target = _target(index_root, key, jsonl_path.parent.name)
        if target.exists():
            raise ArtifactError(f"Library revision already exists: {target}")
        source = Path(record["markdown_path"])
        if key in seen:
            raise ArtifactError(f"Duplicate library publication target: {key}")
        seen.add(key)
        markdown = source.read_text(encoding="utf-8")
        if _sha256(source) != record["markdown_sha256"] or strip_front_matter(markdown) != record["text"]:
            raise ArtifactError(f"Converted Markdown changed before publication: {key}")
        staged = publication / "staged" / key
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.mkdir()
        images = staged / "images"
        if images.exists():
            shutil.rmtree(images)
        source_images = images_dir_for(source)
        if source_images.is_dir():
            shutil.copytree(source_images, images)
        rebased = canonical_image_links(markdown)
        staged_markdown = staged / f"{key}.md"
        staged_markdown.write_text(rebased, encoding="utf-8", newline="\n")
        body = strip_front_matter(rebased)
        page_map = validate_page_map(record.get("page_map"), len(str(record["text"])))
        if page_map:
            original = str(record["text"])
            page_map = page_map_for_body(
                [(page, canonical_image_links(original[start:end])) for page, start, end in page_map], body
            )
        page_map_sidecar_path(staged_markdown).unlink(missing_ok=True)
        if page_map is not None:
            write_page_map_sidecar(staged_markdown, body, page_map)
        record.update(
            markdown_path=str(target / f"{key}.md"), markdown_sha256=_sha256(staged_markdown),
            text=body, char_count=len(body), word_count=len(body.split()), page_map=page_map,
        )
        plan.append({"key": key})
        lines.append(json.dumps(record, ensure_ascii=False))
    if plan:
        atomic_write_text(publication / "plan.json", json.dumps(plan))
        atomic_write_text(jsonl_path, "\n".join(lines) + "\n")


def _plan(index_root: Path, generation_dir: Path) -> list[dict[str, object]]:
    publication = generation_dir / PUBLICATION_DIR
    plan_path = publication / "plan.json"
    if not plan_path.exists():
        return []
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan, list):
        raise ArtifactError("Invalid library publication plan")
    seen = set()
    for row in plan:
        if not isinstance(row, dict):
            raise ArtifactError("Invalid library publication entry")
        key = row.get("key")
        if not isinstance(key, str) or key in seen:
            raise ArtifactError("Invalid or duplicate library publication key")
        _target(index_root, key, generation_dir.name)
        for folder in ("staged", "install"):
            path = publication / folder / key
            if path.resolve() != generation_dir.resolve() / PUBLICATION_DIR / folder / key:
                raise ArtifactError("Redirected library publication staging directory")
        seen.add(key)
    return plan


def install_library_files(index_root: Path, generation_dir: Path) -> None:
    publication = generation_dir / PUBLICATION_DIR
    plan = _plan(index_root, generation_dir)
    if not plan:
        return
    records = {
        record["zotero_attachment_key"]: record
        for line in (generation_dir / "index.jsonl").read_text(encoding="utf-8").split("\n") if line.strip()
        for record in [json.loads(line)]
    }
    # Finish every potentially expensive copy before touching any currently indexed files.
    for row in plan:
        key = str(row["key"])
        prepared = publication / "install" / key
        prepared.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(publication / "staged" / key, prepared)
        if _sha256(prepared / f"{key}.md") != records[key]["markdown_sha256"]:
            raise ArtifactError(f"Staged library Markdown changed before publication: {key}")
    for row in plan:
        key = str(row["key"])
        target = _target(index_root, key, generation_dir.name)
        prepared = publication / "install" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ArtifactError(f"Library revision already exists: {target}")
        replace_with_retry(prepared, target)


def rollback_library_files(index_root: Path, generation_dir: Path) -> None:
    for row in reversed(_plan(index_root, generation_dir)):
        key = str(row["key"])
        target = _target(index_root, key, generation_dir.name)
        if target.exists():
            shutil.rmtree(target)


def cleanup_library_files(generation_dir: Path) -> None:
    publication = generation_dir / PUBLICATION_DIR
    if publication.exists():
        if publication.resolve() != generation_dir.resolve() / PUBLICATION_DIR:
            raise ArtifactError("Redirected library publication directory")
        shutil.rmtree(publication)
