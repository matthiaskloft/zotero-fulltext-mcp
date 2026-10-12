"""Inspect release archives and smoke-test a wheel outside the source checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def check_members(names: list[str], *, sdist: bool) -> None:
    paths = [Path(name) for name in names]
    if sdist:
        paths = [Path(*path.parts[1:]) for path in paths]
    for path in paths:
        if any(part in {"test-results", ".git", "__pycache__"} or part.startswith(".venv")
               for part in path.parts):
            raise ValueError(f"Local runtime content in distribution: {path}")
        if path.name.startswith("config") and path.suffix == ".json" and path.name != "config.example.json":
            raise ValueError(f"Personal configuration in distribution: {path}")
    prefix = "src/" if sdist else ""
    for module in ("__init__.py", "cli.py", "mcp_server.py"):
        if Path(f"{prefix}zotero_pdf_text/{module}") not in paths:
            raise ValueError(f"Missing packaged module: {module}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    wheels = list(args.dist.glob("*.whl"))
    sdists = list(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("Expected exactly one wheel and one sdist")
    wheel = wheels[0].resolve()
    with zipfile.ZipFile(wheel) as archive:
        check_members(archive.namelist(), sdist=False)
    with tarfile.open(sdists[0]) as archive:
        check_members(archive.getnames(), sdist=True)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    with tempfile.TemporaryDirectory(prefix="zotero-wheel-check-") as tmp:
        work = Path(tmp)
        environment = work / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("ZOTERO_")}

        def run(*arguments: str) -> None:
            subprocess.run([str(python), *arguments], cwd=work, env=env, check=True)

        run("-m", "pip", "install", f"{wheel}[mcp]")
        run("-m", "pip", "check")
        run("-c", "import importlib.metadata as m; import zotero_pdf_text; "
            f"assert m.version('zotero-fulltext-mcp') == {version!r}; "
            f"assert not str(zotero_pdf_text.__file__).startswith({str(ROOT)!r}); "
            "print('Installed wheel:', zotero_pdf_text.__file__)")
        for command in ("zotero-pdf-text", "zotero-fulltext-mcp"):
            executable = scripts / (f"{command}.exe" if os.name == "nt" else command)
            subprocess.run([str(executable), "--help"], cwd=work, env=env, check=True)
    print(f"Distribution checks passed for {version}")


if __name__ == "__main__":
    main()
