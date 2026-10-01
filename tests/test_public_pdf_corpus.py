"""Corpus preparation runs on the host and never needs a live Zotero instance."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "fetch_public_pdf_corpus", Path(__file__).resolve().parents[1] / "tools" / "fetch_public_pdf_corpus.py",
)
assert spec is not None and spec.loader is not None
corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus)


def test_cached_pdf_is_verified_without_network(tmp_path, monkeypatch):
    data = b"%PDF-corpus-fixture"
    target = tmp_path / "fixture.pdf"
    target.write_bytes(data)
    source = {"id": "fixture", "sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(corpus.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Unexpected download"))
    assert corpus.fetch(source, tmp_path) == target
    target.write_bytes(b"changed")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        corpus.fetch(source, tmp_path)


def test_changed_download_is_not_saved(tmp_path, monkeypatch):
    import io

    monkeypatch.setattr(corpus.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"changed upstream"))
    with pytest.raises(ValueError, match="Checksum mismatch"):
        corpus.fetch({"id": "fixture", "url": "https://example.org/fixture.pdf", "sha256": "0" * 64}, tmp_path)
    assert not (tmp_path / "fixture.pdf").exists()



def test_successful_download_writes_cache_and_reuses_verified_bytes(tmp_path, monkeypatch):
    import io
    data = b"%PDF-public-download-fixture"
    source = {"id": "fixture", "url": "https://example.org/fixture.pdf", "sha256": hashlib.sha256(data).hexdigest()}
    calls = []
    def download(request, timeout):
        calls.append(request.full_url)
        return io.BytesIO(data)
    monkeypatch.setattr(corpus.urllib.request, "urlopen", download)
    cache = tmp_path / "new-cache"
    target = corpus.fetch(source, cache)
    assert target.read_bytes() == data
    assert corpus.fetch(source, cache) == target
    assert calls == [source["url"]]
