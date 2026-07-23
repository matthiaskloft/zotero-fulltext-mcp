"""Tests for the OCR runtime client.

Runs a real HTTP server from the standard library on an ephemeral loopback port rather than
mocking urllib: the failure modes worth testing here (connection refused, retryable status
codes, malformed bodies) live in the transport, which a mock would paper over. No network, no
model, no GPU -- milliseconds on every supported platform.
"""

import base64
import json
import socket
import struct
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from zotero_pdf_text._ollama_client import OllamaError, generate, probe


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _png(width: int = 10, height: int = 10) -> bytes:
    ihdr = struct.pack(">II", width, height) + bytes([8, 2, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


class FakeOllama:
    """A scripted Ollama stand-in; `generate_script` is consumed one entry per request."""

    def __init__(self, *, models: list[str], generate_script: list[tuple[int, object]] | None = None):
        self.models = models
        self.generate_script = list(generate_script or [])
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: A003 - silence the default stderr logging
                pass

            def _send(self, status: int, payload: object):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/version":
                    self._send(200, {"version": "0.14.2"})
                elif self.path == "/api/tags":
                    self._send(200, {"models": [{"name": name} for name in outer.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(json.loads(self.rfile.read(length).decode("utf-8")))
                status, payload = (
                    outer.generate_script.pop(0)
                    if outer.generate_script
                    else (200, {"response": "default"})
                )
                self._send(status, payload)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ProbeTests(unittest.TestCase):
    def test_server_down_is_reported_as_unreachable(self):
        status = probe(f"http://127.0.0.1:{_free_port()}", "glm-ocr:q8_0", timeout=1.0)

        self.assertFalse(status.server_running)
        self.assertFalse(status.ok)
        self.assertIn("not reachable", status.detail)

    def test_server_up_with_the_model_present_is_ok(self):
        with FakeOllama(models=["glm-ocr:q8_0", "llama3:8b"]) as fake:
            status = probe(fake.base_url, "glm-ocr:q8_0")

        self.assertTrue(status.ok)
        self.assertTrue(status.server_running)
        self.assertTrue(status.model_present)
        self.assertEqual(status.version, "0.14.2")

    def test_server_up_without_the_model_family_says_how_to_pull_it(self):
        with FakeOllama(models=["llama3:8b"]) as fake:
            status = probe(fake.base_url, "glm-ocr:q8_0")

        self.assertTrue(status.server_running)
        self.assertFalse(status.model_present)
        self.assertFalse(status.ok)
        self.assertIn("ollama pull glm-ocr:q8_0", status.detail)

    def test_a_different_tag_of_the_same_family_is_reported_specifically(self):
        # Distinguishing "wrong tag" from "not installed" saves pulling a model already on disk.
        with FakeOllama(models=["glm-ocr:bf16"]) as fake:
            status = probe(fake.base_url, "glm-ocr:q8_0")

        self.assertFalse(status.model_present)
        self.assertIn("glm-ocr:bf16", status.detail)
        self.assertIn("image_ocr.model", status.detail)

    def test_probe_never_runs_an_inference(self):
        # On a CPU-only machine a real generation takes minutes; an availability check that did
        # one would be indistinguishable from a hang.
        with FakeOllama(models=["glm-ocr:q8_0"]) as fake:
            probe(fake.base_url, "glm-ocr:q8_0")
            self.assertEqual(fake.requests, [])


class GenerateTests(unittest.TestCase):
    def test_sends_the_image_base64_encoded_with_the_task_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            crop = Path(tmp) / "crop.png"
            crop.write_bytes(_png())

            with FakeOllama(
                models=["glm-ocr:q8_0"], generate_script=[(200, {"response": "E = mc^2"})]
            ) as fake:
                text = generate(
                    fake.base_url, "glm-ocr:q8_0", "Formula Recognition:", crop, timeout=5
                )
                sent = fake.requests[0]

        self.assertEqual(text, "E = mc^2")
        self.assertEqual(sent["prompt"], "Formula Recognition:")
        self.assertEqual(sent["model"], "glm-ocr:q8_0")
        self.assertFalse(sent["stream"])
        self.assertEqual(base64.b64decode(sent["images"][0]), _png())

    def test_decoding_is_deterministic(self):
        # A resumed run must splice the same notation the interrupted one produced.
        with tempfile.TemporaryDirectory() as tmp:
            crop = Path(tmp) / "crop.png"
            crop.write_bytes(_png())
            with FakeOllama(models=["glm-ocr:q8_0"]) as fake:
                generate(fake.base_url, "glm-ocr:q8_0", "Formula Recognition:", crop, timeout=5)

        self.assertEqual(fake.requests[0]["options"]["temperature"], 0)

    def test_retries_a_retryable_status_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            crop = Path(tmp) / "crop.png"
            crop.write_bytes(_png())

            with FakeOllama(
                models=["glm-ocr:q8_0"],
                generate_script=[
                    (503, {"error": "loading model"}),
                    (200, {"response": "recovered"}),
                ],
            ) as fake:
                text = generate(
                    fake.base_url, "glm-ocr:q8_0", "Formula Recognition:", crop, timeout=5
                )

            self.assertEqual(text, "recovered")
            self.assertEqual(len(fake.requests), 2)

    def test_does_not_retry_a_non_retryable_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            crop = Path(tmp) / "crop.png"
            crop.write_bytes(_png())

            with FakeOllama(
                models=["glm-ocr:q8_0"], generate_script=[(404, {"error": "model not found"})]
            ) as fake:
                with self.assertRaises(OllamaError) as caught:
                    generate(fake.base_url, "glm-ocr:q8_0", "Formula Recognition:", crop, timeout=5)

            self.assertEqual(len(fake.requests), 1)
            # Ollama's own error body is passed through; it names the actual problem.
            self.assertIn("model not found", str(caught.exception))

    def test_gives_up_after_exhausting_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            crop = Path(tmp) / "crop.png"
            crop.write_bytes(_png())

            with FakeOllama(
                models=["glm-ocr:q8_0"],
                generate_script=[(500, {"error": "boom"})] * 3,
            ) as fake:
                with self.assertRaises(OllamaError):
                    generate(
                        fake.base_url,
                        "glm-ocr:q8_0",
                        "Formula Recognition:",
                        crop,
                        timeout=5,
                        attempts=3,
                    )

            self.assertEqual(len(fake.requests), 3)

    def test_a_response_without_the_expected_field_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            crop = Path(tmp) / "crop.png"
            crop.write_bytes(_png())

            with FakeOllama(
                models=["glm-ocr:q8_0"], generate_script=[(200, {"unexpected": "shape"})]
            ) as fake:
                with self.assertRaises(OllamaError):
                    generate(fake.base_url, "glm-ocr:q8_0", "Formula Recognition:", crop, timeout=5)

    def test_a_non_json_body_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            crop = Path(tmp) / "crop.png"
            crop.write_bytes(_png())

            with FakeOllama(
                models=["glm-ocr:q8_0"], generate_script=[(200, b"<html>not json</html>")]
            ) as fake:
                with self.assertRaises(OllamaError):
                    generate(fake.base_url, "glm-ocr:q8_0", "Formula Recognition:", crop, timeout=5)


if __name__ == "__main__":
    unittest.main()
