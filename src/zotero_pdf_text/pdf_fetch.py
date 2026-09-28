"""Download one PDF from an untrusted URL into a local directory, safely.

The URL may come from paper text an agent read, so it is treated as hostile:

* ``https`` only, no ``user:password@`` part.
* The host is resolved here and every address must be public. Loopback, private, link-local,
  shared, reserved, multicast and unspecified addresses are refused (IPv4 and IPv6, including
  IPv4-mapped/6to4/Teredo forms), so a URL cannot reach the Zotero connector, debug-bridge or
  anything else on the local network. The connection then goes to the address that was checked,
  not to a second DNS answer, with TLS still verified against the host name.
* Redirects are followed by hand, at most ``max_redirects`` of them, and each hop gets the same
  checks.
* The body is streamed to a ``.part`` file under a byte cap and must start with ``%PDF-``; only
  then is it renamed into place. Content-Type is recorded but not trusted.

``fetch_pdf`` writes only inside ``dest_dir`` and removes its partial file on any failure. It has
no Zotero dependency, so a future write server can reuse it unchanged.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_MAX_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 30.0  # per connect/read
DEFAULT_TOTAL_SECONDS = 300.0  # whole download, all hops
PDF_MAGIC = b"%PDF-"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHUNK = 64 * 1024
_USER_AGENT = "zotero-pdf-text (+https://github.com/matthiaskloft/zotero-fulltext-mcp)"

Resolver = Callable[[str, int], list[str]]


class PdfFetchError(Exception):
    """The URL was refused or the download did not produce a valid PDF. Nothing is left behind."""


@dataclass(frozen=True)
class FetchedPdf:
    path: Path
    size: int
    sha256: str
    final_url: str
    content_type: str


class _Response(Protocol):
    status: int

    def getheader(self, name: str) -> str | None: ...

    def read(self, amt: int | None = None) -> bytes: ...


class _Connection(Protocol):
    def request(
        self, method: str, url: str, body: None = None, headers: Mapping[str, str] = ...
    ) -> None: ...

    def getresponse(self) -> _Response: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, int, str, float], _Connection]


def check_url(url: str) -> urllib.parse.SplitResult:
    """Syntactic URL policy: https, a host, no userinfo. Raises PdfFetchError."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise PdfFetchError(f"invalid URL: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise PdfFetchError(f"only https URLs are allowed, got scheme {parts.scheme or '(none)'!r}")
    if "@" in parts.netloc:
        raise PdfFetchError("URLs with a user name or password are not allowed")
    if not parts.hostname:
        raise PdfFetchError("URL has no host")
    if port == 0:
        raise PdfFetchError("URL has an invalid port")
    return parts


def refusal_reason(address: str) -> str:
    """Why an address may not be fetched from, or "" if it is a public unicast address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return f"{address!r} is not an IP address"
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        # Addresses that embed an IPv4 address are judged by that address too.
        for embedded in (ip.ipv4_mapped, ip.sixtofour, ip.teredo[1] if ip.teredo else None):
            if embedded is not None:
                candidates.append(embedded)
    for candidate in candidates:
        for label, flag in (
            ("loopback", candidate.is_loopback),
            ("private", candidate.is_private),
            ("link-local", candidate.is_link_local),
            ("multicast", candidate.is_multicast),
            ("reserved", candidate.is_reserved),
            ("unspecified", candidate.is_unspecified),
        ):
            if flag:
                return f"{address} is a {label} address"
        if not candidate.is_global:
            return f"{address} is not a public address"
    return ""


def resolve_public_addresses(host: str, port: int, resolver: Resolver | None = None) -> list[str]:
    """Resolve ``host`` and require every address to be public. Raises PdfFetchError."""
    try:
        addresses = (resolver or _system_resolver)(host, port)
    except OSError as exc:
        raise PdfFetchError(f"could not resolve host {host!r}: {exc}") from exc
    if not addresses:
        raise PdfFetchError(f"host {host!r} resolved to no address")
    for address in addresses:
        reason = refusal_reason(address)
        if reason:
            raise PdfFetchError(f"refusing host {host!r}: {reason}")
    return addresses


def fetch_pdf(
    url: str,
    dest_dir: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    total_timeout: float = DEFAULT_TOTAL_SECONDS,
    file_name: str = "download.pdf",
    resolver: Resolver | None = None,
    connection_factory: ConnectionFactory | None = None,
) -> FetchedPdf:
    """Download the PDF at ``url`` to ``dest_dir / file_name``. Raises PdfFetchError."""
    if max_bytes <= 0:
        raise PdfFetchError("max_bytes must be positive")
    connect = connection_factory or _pinned_https_connection
    deadline = time.monotonic() + total_timeout
    current = url.strip()
    for _hop in range(max_redirects + 1):
        parts = check_url(current)
        host = parts.hostname or ""
        port = parts.port or 443
        address = resolve_public_addresses(host, port, resolver)[0]
        target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection = connect(host, port, address, timeout)
        try:
            try:
                connection.request(
                    "GET", target,
                    headers={
                        "User-Agent": _USER_AGENT,
                        "Accept": "application/pdf",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                raise PdfFetchError(f"request to {host} failed: {exc}") from exc
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location:
                    raise PdfFetchError(f"HTTP {response.status} redirect without a Location header")
                current = urllib.parse.urljoin(current, location.strip())
                continue
            if response.status != 200:
                raise PdfFetchError(f"HTTP {response.status} from {host}")
            return _stream_to_file(response, dest_dir, file_name, max_bytes, current, deadline)
        finally:
            connection.close()
    raise PdfFetchError(f"more than {max_redirects} redirects")


def _stream_to_file(
    response: _Response, dest_dir: Path, file_name: str, max_bytes: int, final_url: str, deadline: float
) -> FetchedPdf:
    content_type = response.getheader("Content-Type") or ""
    declared = (response.getheader("Content-Length") or "").strip()
    if declared.isdigit() and int(declared) > max_bytes:
        raise PdfFetchError(f"file is {int(declared)} bytes, over the {max_bytes}-byte limit")
    final_path = dest_dir / file_name
    part_path = dest_dir / (file_name + ".part")
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        with part_path.open("wb") as handle:
            while True:
                if time.monotonic() > deadline:
                    raise PdfFetchError("download took too long")
                try:
                    chunk = response.read(_CHUNK)
                except (OSError, http.client.HTTPException) as exc:
                    raise PdfFetchError(f"download interrupted: {exc}") from exc
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise PdfFetchError(f"file exceeds the {max_bytes}-byte limit")
                if len(head) < len(PDF_MAGIC):
                    head += chunk[: len(PDF_MAGIC) - len(head)]
                    if len(head) == len(PDF_MAGIC) and head != PDF_MAGIC:
                        raise _not_a_pdf(content_type)
                digest.update(chunk)
                handle.write(chunk)
        if head != PDF_MAGIC:
            raise _not_a_pdf(content_type)
        os.replace(part_path, final_path)
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
    return FetchedPdf(
        path=final_path, size=size, sha256=digest.hexdigest(), final_url=final_url, content_type=content_type
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _not_a_pdf(content_type: str) -> PdfFetchError:
    return PdfFetchError(
        "the response is not a PDF (it does not start with %PDF-"
        + (f"; Content-Type {content_type}" if content_type else "")
        + "). Supply the direct link to the PDF file, not a landing page."
    )


def _system_resolver(host: str, port: int) -> list[str]:
    addresses: list[str] = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to an address resolved and checked in advance, with TLS verified for ``host``."""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        self._tls = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._tls)
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


def _pinned_https_connection(host: str, port: int, address: str, timeout: float) -> _Connection:
    return _PinnedHTTPSConnection(host, port, address, timeout)
