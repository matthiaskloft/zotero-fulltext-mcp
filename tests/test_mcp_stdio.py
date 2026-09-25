from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from zotero_pdf_text.mcp_contract import marker_dependency_available
from test_mcp_server import _build_index, _write_config

# Simulates a dependency that prints diagnostics when an optional tool lazily imports it (the
# PyMuPDF `fitz` deprecation notice): both a Python-level print and a raw fd-1 write.
NOISY_SERVER = textwrap.dedent(
    """
    import os, sys
    from zotero_pdf_text import mcp_contract, mcp_server

    original = mcp_contract.validate_config

    def noisy_validate_config(config):
        print("noise from a lazily imported dependency")
        sys.stdout.flush()
        os.write(1, b"raw fd noise\\n")
        return original(config)

    mcp_contract.validate_config = noisy_validate_config
    sys.exit(mcp_server.main(sys.argv[1:]))
    """
)


@unittest.skipUnless(importlib.util.find_spec("mcp"), "requires the optional MCP extra")
class McpStdioTests(unittest.TestCase):
    def test_lazy_dependency_output_stays_off_the_protocol_stream(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory() as tmp:
            root, sqlite_path, config = _build_index(Path(tmp))
            config_path = root / "config.json"
            _write_config(config_path, config)
            script = root / "noisy_server.py"
            script.write_text(NOISY_SERVER, encoding="utf-8")
            src = str(Path(__file__).resolve().parents[1] / "src")
            env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
            args = [str(script), "--db", str(sqlite_path), "--config", str(config_path), "--enable-retry-timeout"]
            calls = {
                "skip_timeout_extraction": {"attachment_key": "NOSUCHKEY", "reason": "test", "confirm": "skip_timeout"},
                "retry_timeout_extraction": {"attachment_key": "NOSUCHKEY", "confirm": "retry_timeout"},
            }
            if marker_dependency_available():
                args.append("--enable-reconvert")
                calls["reconvert_with_math_ocr"] = {"attachment_key": "NOSUCHKEY", "confirm": "reconvert"}
            params = StdioServerParameters(command=sys.executable, args=args, env=env)
            stderr_path = root / "stderr.log"

            async def run():
                with stderr_path.open("w", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            return {name: await session.call_tool(name, arguments) for name, arguments in calls.items()}

            # The client logs every stdout line that is not a JSON-RPC message on this logger.
            with self.assertNoLogs("mcp.client.stdio", level="ERROR"):
                results = asyncio.run(asyncio.wait_for(run(), timeout=60))
            stderr = stderr_path.read_text(encoding="utf-8")

        self.assertEqual(set(results), set(calls))
        for name, result in results.items():
            self.assertTrue(result.isError, name)
            self.assertNotIn(str(root), str(result.content), name)
        self.assertIn("noise from a lazily imported dependency", stderr)
        self.assertIn("raw fd noise", stderr)
