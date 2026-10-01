"""Portable developer test setup. Uses no host Zotero config or registration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "zotero-live-test:dev"


def run(arguments, *, capture=False, check=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ZOTERO_")}
    env.pop("UV_ENV_FILE", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(ROOT / ".venv")
    env["UV_PYTHON_INSTALL_DIR"] = str(ROOT / "test-results" / "tool-runtime" / "python")
    env["UV_CACHE_DIR"] = str(ROOT / "test-results" / "tool-runtime" / "cache")
    return subprocess.run(arguments, cwd=ROOT, env=env, check=check, text=True,
                          capture_output=capture)


def require(name):
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"Install {name} and add it to PATH; see docs/dev-test-setup.md")
    return executable


def docker():
    executable = require("docker")
    result = run([executable, "info", "--format", "{{.OSType}}"], capture=True, check=False)
    if result.returncode:
        raise RuntimeError("Cannot reach Docker. Start its engine and check access permissions.")
    if result.stdout.strip() != "linux":
        raise RuntimeError("Docker must be running with a Linux container engine")
    return executable


def build(executable):
    run([executable, "build", "--platform", "linux/amd64", "-f",
         "containers/live-zotero/Dockerfile", "-t", IMAGE, "."])


def live(executable):
    build(executable)
    name = "zotero-dev-test-" + uuid.uuid4().hex[:12]
    evidence = ROOT / "test-results" / name
    evidence.mkdir(parents=True)
    try:
        result = run([executable, "run", "--platform", "linux/amd64", "--name", name,
                      "--init", "--network", "none", "--shm-size", "256m", IMAGE,
                      "test", "--junitxml=/work/junit.xml"], check=False)
        for filename in ("junit.xml", "zotero.log"):
            run([executable, "cp", f"{name}:/work/{filename}", str(evidence / filename)], check=False)
        print(f"Test diagnostics: {evidence}", flush=True)
        return result.returncode
    finally:
        # Only remove the unique disposable container created by this invocation.
        run([executable, "rm", "-f", name], check=False)


def agent_start(executable):
    build(executable)
    name = "zotero-agent-" + uuid.uuid4().hex[:12]
    run([executable, "run", "-d", "--platform", "linux/amd64", "--name", name,
         "--init", "--shm-size", "256m", IMAGE, "serve"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        logs = run([executable, "logs", name], capture=True)
        if "Ready: Zotero" in logs.stdout + logs.stderr:
            run([executable, "exec", name, "python", "containers/live-zotero/agent_workflow.py", "prepare"])
            print(f"Container: {name}\nTool prefix: docker exec {name} python containers/live-zotero/agent_workflow.py", flush=True)
            return 0
        state = run([executable, "inspect", "--format", "{{.State.Running}}", name], capture=True)
        if state.stdout.strip() != "true":
            break
        time.sleep(1)
    run([executable, "stop", name], check=False)
    raise RuntimeError(f"Zotero did not become ready; inspect docker logs {name}. Container retained.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["doctor", "setup", "host", "live", "corpus", "agent-start"])
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            failures = []
            for name in ("git", "uv", "node", "docker"):
                try:
                    executable = docker() if name == "docker" else require(name)
                    result = run([executable, "--version"], capture=True)
                    print(result.stdout.strip())
                except (RuntimeError, subprocess.CalledProcessError) as exc:
                    failures.append(str(exc))
            for failure in failures:
                print(failure, file=sys.stderr)
            return 1 if failures else 0
        if args.action in {"live", "agent-start"}:
            executable = docker()
            return live(executable) if args.action == "live" else agent_start(executable)
        if args.action == "corpus":
            run([sys.executable, "tools/fetch_public_pdf_corpus.py"])
            return 0
        uv = require("uv")
        require("git")
        if args.action == "setup":
            require("node")
            run([uv, "python", "install", "3.11", "--no-bin", *(["--no-registry"] if os.name == "nt" else [])])
            run([uv, "sync", "--python", "3.11", "--extra", "mcp", "--extra", "test", "--locked"])
        elif args.action == "host":
            require("node")
            evidence = ROOT / "test-results" / "host.xml"
            evidence.parent.mkdir(parents=True, exist_ok=True)
            run([uv, "run", "--locked", "--extra", "mcp", "--extra", "test", "pytest", "-q",
                 "-p", "no:cacheprovider", "-m", "not live_zotero and not live_zotero_container",
                 f"--junitxml={evidence}"])
        return 0
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
