"""fetch_pdf: URL policy, per-hop address checks, size cap and PDF validation, without network.

Every test injects a fake resolver and a fake connection factory, so no socket is ever opened.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from zotero_pdf_text.pdf_fetch import (
    FetchedPdf,
    PdfFetchError,
    check_url,
    fetch_pdf,
    refusal_reason,
    resolve_public_addresses,
)

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 1000 + b"\n%%EOF\n"


@dataclass
class FakeResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def getheader(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def read(self, amt: int | None = None) -> bytes:
        size = len(self.body) if amt is None else amt
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


class FakeWeb:
    """Hosts, their DNS answers and their responses; records every connection attempt."""

    def __init__(self) -> None:
        self.dns: dict[str, list[str]] = {}
        self.routes: dict[tuple[str, str], FakeResponse] = {}
        self.connections: list[tuple[str, int, str]] = []
        self.requests: list[tuple[str, str]] = []

    def serve(self, host: str, path: str, response: FakeResponse, address: str = PUBLIC_V4) -> None:
        self.dns.setdefault(host, [address])
        self.routes[(host, path)] = response

    def resolver(self, host: str, port: int) -> list[str]:
        if host not in self.dns:
            raise OSError("name not known")
        return self.dns[host]

    def connect(self, host: str, port: int, address: str, timeout: float) -> FakeConnection:
        self.connections.append((host, port, address))
        return FakeConnection(self, host)

    def fetch(self, url: str, dest: Path, max_bytes: int = 10_000, **kwargs: object) -> FetchedPdf:
        return fetch_pdf(
            url, dest, max_bytes, resolver=self.resolver, connection_factory=self.connect, **kwargs  # type: ignore[arg-type]
        )


class FakeConnection:
    def __init__(self, web: FakeWeb, host: str) -> None:
        self.web = web
        self.host = host
        self.path = ""

    def request(self, method: str, url: str, body: None = None, headers: object = None) -> None:
        self.path = url
        self.web.requests.append((self.host, url))

    def getresponse(self) -> FakeResponse:
        return self.web.routes.get((self.host, self.path), FakeResponse(404))

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# URL and address policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, fragment",
    [
        ("http://example.org/paper.pdf", "only https"),
        ("ftp://example.org/paper.pdf", "only https"),
        ("file:///etc/passwd", "only https"),
        ("example.org/paper.pdf", "only https"),
        ("https://user:secret@example.org/paper.pdf", "user name or password"),
        ("https://user@example.org/paper.pdf", "user name or password"),
        ("https:///paper.pdf", "no host"),
        ("https://example.org:99999/paper.pdf", "invalid URL"),
    ],
)
def test_url_policy_refuses(url: str, fragment: str) -> None:
    with pytest.raises(PdfFetchError, match=fragment):
        check_url(url)


def test_url_policy_accepts_plain_https() -> None:
    assert check_url("https://example.org/a/paper.pdf?download=1").hostname == "example.org"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback: the Zotero connector and debug-bridge listen here
        "127.8.9.10",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.10",
        "169.254.169.254",  # link-local, cloud metadata
        "100.64.0.1",  # shared address space
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",
        "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
        "2001:db8::1",
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert refusal_reason(address)


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6, "1.1.1.1"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert refusal_reason(address) == ""


def test_host_is_refused_when_any_resolved_address_is_private() -> None:
    with pytest.raises(PdfFetchError, match="refusing host"):
        resolve_public_addresses("mixed.example", 443, lambda host, port: [PUBLIC_V4, "10.1.2.3"])


def test_unresolvable_host_is_an_error() -> None:
    def fail(host: str, port: int) -> list[str]:
        raise OSError("name not known")

    with pytest.raises(PdfFetchError, match="could not resolve"):
        resolve_public_addresses("nowhere.example", 443, fail)


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def test_downloads_pdf_with_hash_and_pins_the_checked_address(tmp_path: Path) -> None:
    web = FakeWeb()
    web.serve("papers.example", "/paper.pdf", FakeResponse(200, {"Content-Type": "application/pdf"}, PDF_BYTES))

    fetched = web.fetch("https://papers.example/paper.pdf", tmp_path)

    assert fetched.path == tmp_path / "download.pdf"
    assert fetched.path.read_bytes() == PDF_BYTES
    assert fetched.size == len(PDF_BYTES)
    assert fetched.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert fetched.final_url == "https://papers.example/paper.pdf"
    assert fetched.content_type == "application/pdf"
    assert web.connections == [("papers.example", 443, PUBLIC_V4)]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["download.pdf"]


def test_follows_a_relative_redirect_and_reports_the_final_url(tmp_path: Path) -> None:
    web = FakeWeb()
    web.serve("papers.example", "/landing", FakeResponse(302, {"Location": "/files/paper.pdf"}))
    web.serve("papers.example", "/files/paper.pdf", FakeResponse(200, {}, PDF_BYTES))

    fetched = web.fetch("https://papers.example/landing", tmp_path)

    assert fetched.final_url == "https://papers.example/files/paper.pdf"


def test_redirect_to_a_private_address_is_refused_before_connecting(tmp_path: Path) -> None:
    web = FakeWeb()
    web.serve("papers.example", "/paper.pdf", FakeResponse(302, {"Location": "https://intranet.example/x.pdf"}))
    web.serve("intranet.example", "/x.pdf", FakeResponse(200, {}, PDF_BYTES), address="10.0.0.5")

    with pytest.raises(PdfFetchError, match="10.0.0.5 is a private address"):
        web.fetch("https://papers.example/paper.pdf", tmp_path)

    assert [c[0] for c in web.connections] == ["papers.example"]
    assert list(tmp_path.iterdir()) == []


def test_redirect_to_localhost_debug_bridge_is_refused(tmp_path: Path) -> None:
    web = FakeWeb()
    web.serve(
        "papers.example", "/paper.pdf",
        FakeResponse(301, {"Location": "https://localhost:23119/debug-bridge/execute"}),
    )
    web.dns["localhost"] = ["127.0.0.1", "::1"]

    with pytest.raises(PdfFetchError, match="loopback"):
        web.fetch("https://papers.example/paper.pdf", tmp_path)
    assert len(web.connections) == 1


def test_redirect_to_http_is_refused(tmp_path: Path) -> None:
    web = FakeWeb()
    web.serve("papers.example", "/paper.pdf", FakeResponse(307, {"Location": "http://papers.example/paper.pdf"}))

    with pytest.raises(PdfFetchError, match="only https"):
        web.fetch("https://papers.example/paper.pdf", tmp_path)


def _redirect_chain(web: FakeWeb, hops: int) -> None:
    for hop in range(hops):
        web.serve("papers.example", f"/r{hop}", FakeResponse(302, {"Location": f"/r{hop + 1}"}))
    web.serve("papers.example", f"/r{hops}", FakeResponse(200, {}, PDF_BYTES))


def test_five_redirects_are_followed(tmp_path: Path) -> None:
    web = FakeWeb()
    _redirect_chain(web, 5)

    assert web.fetch("https://papers.example/r0", tmp_path).final_url.endswith("/r5")


def test_more_than_five_redirects_fail(tmp_path: Path) -> None:
    web = FakeWeb()
    _redirect_chain(web, 6)

    with pytest.raises(PdfFetchError, match="more than 5 redirects"):
        web.fetch("https://papers.example/r0", tmp_path)
    assert len(web.connections) == 6


def test_redirect_without_location_fails(tmp_path: Path) -> None:
    web = FakeWeb()
    web.serve("papers.example", "/paper.pdf", FakeResponse(302))

    with pytest.raises(PdfFetchError, match="without a Location"):
        web.fetch("https://papers.example/paper.pdf", tmp_path)


def test_http_error_status_fails(tmp_path: Path) -> None:
    web = FakeWeb()
    web.dns["papers.example"] = [PUBLIC_V4]

    with pytest.raises(PdfFetchError, match="HTTP 404"):
        web.fetch("https://papers.example/missing.pdf", tmp_path)


@pytest.mark.parametrize("body", [b"<!DOCTYPE html><html>paywall</html>", b"%PD", b"", b" %PDF-1.7"])
def test_body_that_is_not_a_pdf_is_rejected_and_nothing_is_kept(tmp_path: Path, body: bytes) -> None:
    web = FakeWeb()
    web.serve("papers.example", "/paper.pdf", FakeResponse(200, {"Content-Type": "application/pdf"}, body))

    with pytest.raises(PdfFetchError, match="not a PDF"):
        web.fetch("https://papers.example/paper.pdf", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_over_the_cap_is_aborted_and_the_part_file_removed(tmp_path: Path) -> None:
    web = FakeWeb()
    web.serve("papers.example", "/big.pdf", FakeResponse(200, {}, b"%PDF-" + b"x" * 200_000))

    with pytest.raises(PdfFetchError, match="exceeds the 100000-byte limit"):
        web.fetch("https://papers.example/big.pdf", tmp_path, max_bytes=100_000)
    assert list(tmp_path.iterdir()) == []


def test_declared_length_over_the_cap_is_refused_before_reading(tmp_path: Path) -> None:
    web = FakeWeb()
    response = FakeResponse(200, {"Content-Length": "5000000"}, PDF_BYTES)
    web.serve("papers.example", "/big.pdf", response)

    with pytest.raises(PdfFetchError, match="over the 10000-byte limit"):
        web.fetch("https://papers.example/big.pdf", tmp_path)
    assert response.body == PDF_BYTES  # never read
    assert list(tmp_path.iterdir()) == []


def test_file_exactly_at_the_cap_is_accepted(tmp_path: Path) -> None:
    web = FakeWeb()
    web.serve("papers.example", "/paper.pdf", FakeResponse(200, {}, PDF_BYTES))

    assert web.fetch("https://papers.example/paper.pdf", tmp_path, max_bytes=len(PDF_BYTES)).size == len(PDF_BYTES)
