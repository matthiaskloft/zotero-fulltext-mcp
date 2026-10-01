"""Host tests must never inherit a developer's Zotero configuration or bridge."""

import os
import socket
import sys
from pathlib import Path

import pytest


def isolated_container():
    return (sys.platform == "linux" and Path("/.dockerenv").is_file()
            and os.environ.get("ZOTERO_LIVE_CONTAINER") == "1"
            and os.environ.get("ZOTERO_LIVE_TEST_CONFIG") == "/work/config.live-test.json"
            and Path("/work/config.live-test.json").is_file())


@pytest.fixture(autouse=True)
def isolated_host_environment(tmp_path, monkeypatch):
    if isolated_container():
        return
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("ZOTERO_", "ZOTERO_PDF_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZOTERO_PDF_TEXT_CONFIG", str(tmp_path / "config.json"))
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(sock, address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == 23119:
            raise OSError("Host tests cannot contact Zotero; use the isolated container")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == 23119:
            raise OSError("Host tests cannot contact Zotero; use the isolated container")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
