"""Test configuration and bridge isolation without contacting any real service."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


def test_host_defaults_and_child_environment_are_disposable(tmp_path):
    assert Path.cwd() == tmp_path
    assert Path(os.environ["ZOTERO_PDF_TEXT_CONFIG"]) == tmp_path / "config.json"
    assert "ZOTERO_DEBUG_BRIDGE_TOKEN" not in os.environ
    assert "ZOTERO_LIVE_CONTAINER" not in os.environ
    result = subprocess.run([sys.executable, "-c", "import os; print(os.environ['ZOTERO_PDF_TEXT_CONFIG']); print(os.getenv('ZOTERO_DEBUG_BRIDGE_TOKEN', 'absent'))"],
                            capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [str(tmp_path / "config.json"), "absent"]


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_host_zotero_connections_are_blocked_before_network(method):
    with socket.socket() as connection:
        with pytest.raises(OSError, match="Host tests cannot contact Zotero"):
            getattr(connection, method)(("127.0.0.1", 23119))
