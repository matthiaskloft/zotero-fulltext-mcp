"""Portable runner safety and failure behavior, without a Docker daemon."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("dev_tests", Path(__file__).resolve().parents[1] / "tools/dev_tests.py")
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_child_commands_use_checkout_and_strip_zotero_settings(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout with spaces"
    monkeypatch.setattr(runner, "ROOT", checkout)
    monkeypatch.setenv("ZOTERO_DEBUG_BRIDGE_TOKEN", "synthetic-token")
    monkeypatch.setenv("UV_ENV_FILE", "unused-private-env")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "unused-other-environment")
    monkeypatch.setenv("ZOTERO_PDF_TEXT_CONFIG", "unused-personal-config")
    def execute(arguments, **kwargs):
        assert arguments == ["fixture", "argument with spaces"]
        assert kwargs["cwd"] == checkout
        assert not any(key.startswith("ZOTERO_") for key in kwargs["env"])
        assert "UV_ENV_FILE" not in kwargs["env"]
        assert Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]) == checkout / ".venv"
        assert Path(kwargs["env"]["UV_PYTHON_INSTALL_DIR"]).is_relative_to(checkout)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", execute)
    runner.run(["fixture", "argument with spaces"])


def test_failed_live_run_preserves_status_and_cleans_only_its_container(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "build", lambda executable: None)
    monkeypatch.setattr(runner.uuid, "uuid4", lambda: SimpleNamespace(hex="synthetic123456789"))
    calls = []
    def execute(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=7 if arguments[1] == "run" else 0)
    monkeypatch.setattr(runner, "run", execute)
    assert runner.live("docker-fixture") == 7
    command = calls[0]
    assert command[command.index("--network") + 1] == "none"
    assert not any(option in command for option in ("--volume", "-v", "--publish", "-p"))
    assert calls[-1] == ["docker-fixture", "rm", "-f", "zotero-dev-test-synthetic123"]


def test_windows_docker_engine_is_refused(monkeypatch):
    monkeypatch.setattr(runner, "require", lambda name: name)
    monkeypatch.setattr(runner, "run", lambda *a, **k: SimpleNamespace(stdout="windows", returncode=0))
    with pytest.raises(RuntimeError, match="Linux container engine"):
        runner.docker()
