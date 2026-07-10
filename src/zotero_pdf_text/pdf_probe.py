from __future__ import annotations

from pathlib import Path


def extract_early_text(path: Path, *, pages: int, max_page_chars: int) -> tuple[str, int]:
    import fitz

    chunks: list[str] = []
    with fitz.open(path) as doc:
        page_count = int(doc.page_count)
        for index in range(min(pages, page_count)):
            text = doc.load_page(index).get_text("text") or ""
            chunks.append(text[:max_page_chars])
    return "\n".join(chunks), page_count
