"""Minimal Ollama client for image OCR, built only on the standard library.

Deliberately not the vendor `glmocr` SDK. That SDK exists to run layout analysis -- to *find*
regions in a page -- and pulls torch/transformers to do it. The images this project sends are
already single cropped regions, extracted by pymupdf4llm during normal conversion, so there is
nothing left to lay out. What remains is one HTTP POST per crop, which `urllib` covers.

Uses Ollama's native ``/api/generate`` rather than its OpenAI-compatible ``/v1`` surface: the
GLM-OCR vendor documentation calls out known vision-payload failures on the compatible path.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROBE_TIMEOUT_SECONDS = 2.0
DEFAULT_ATTEMPTS = 3
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class OllamaError(RuntimeError):
    """A request to the local Ollama server failed in a way the caller should surface."""


@dataclass(frozen=True)
class RuntimeStatus:
    """Whether the OCR runtime is usable, and if not, what the operator should do about it.

    Three distinct failure modes need three distinct remedies (start the server, pull the
    model, pull a different tag), so this reports them separately rather than as one bool.
    """

    server_running: bool
    model_present: bool
    detail: str
    version: str = ""

    @property
    def ok(self) -> bool:
        return self.server_running and self.model_present


def probe(
    base_url: str, model: str, *, timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS
) -> RuntimeStatus:
    """Check server reachability and model availability without running an inference.

    Never probes by generating: on a CPU-only machine a single real OCR call can take minutes,
    which would make an availability check indistinguishable from a hang.
    """
    try:
        version_payload = _get_json(f"{base_url}/api/version", timeout=timeout)
    except OllamaError as exc:
        return RuntimeStatus(
            server_running=False,
            model_present=False,
            detail=f"Ollama is not reachable at {base_url} ({exc}). Start Ollama and retry.",
        )
    version = str(version_payload.get("version", "")) if isinstance(version_payload, dict) else ""

    try:
        tags_payload = _get_json(f"{base_url}/api/tags", timeout=timeout)
    except OllamaError as exc:
        return RuntimeStatus(
            server_running=True,
            model_present=False,
            detail=f"Ollama responded at {base_url} but the model list could not be read ({exc}).",
            version=version,
        )

    names = _model_names(tags_payload)
    if model in names:
        return RuntimeStatus(True, True, f"Ollama {version or '(unknown version)'}; model '{model}' available.", version)

    family = model.split(":", 1)[0]
    related = sorted(name for name in names if name.split(":", 1)[0] == family)
    if related:
        detail = (
            f"Ollama is running but '{model}' is not pulled. Installed tags for '{family}': "
            f"{', '.join(related)}. Either pull '{model}' or set image_ocr.model to one of these."
        )
    else:
        detail = f"Ollama is running but no '{family}' model is installed. Run: ollama pull {model}"
    return RuntimeStatus(server_running=True, model_present=False, detail=detail, version=version)


def generate(
    base_url: str,
    model: str,
    prompt: str,
    image_path: Path,
    *,
    timeout: float,
    attempts: int = DEFAULT_ATTEMPTS,
) -> str:
    """Run one OCR request against one image and return the model's raw response text."""
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image_path.read_bytes()).decode("ascii")],
        "stream": False,
        # Deterministic decoding: the same crop must not produce different notation between
        # runs, or a resumed run would splice inconsistently with the part already committed.
        "options": {"temperature": 0},
    }
    body = _post_json(f"{base_url}/api/generate", payload, timeout=timeout, attempts=attempts)
    response = body.get("response")
    if not isinstance(response, str):
        raise OllamaError("Ollama returned no 'response' field.")
    return response


def _model_names(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    models = payload.get("models")
    if not isinstance(models, list):
        return set()
    names: set[str] = set()
    for entry in models:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("model")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _get_json(url: str, *, timeout: float) -> dict[str, object]:
    request = urllib.request.Request(url, method="GET")
    return _read_json(request, timeout=timeout)


def _post_json(
    url: str, payload: dict[str, object], *, timeout: float, attempts: int
) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8")
    delay = 0.5
    last_error: OllamaError | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, data=data, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            return _read_json(request, timeout=timeout)
        except OllamaError as exc:
            last_error = exc
            if not _is_retryable(exc) or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise last_error if last_error is not None else OllamaError("Request failed.")


def _is_retryable(error: OllamaError) -> bool:
    status = getattr(error, "status_code", None)
    return status in RETRY_STATUS_CODES


def _read_json(request: urllib.request.Request, *, timeout: float) -> dict[str, object]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Pass Ollama's own error body through: it is explicit about unsupported model formats
        # and missing models in ways a generic status line is not.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001 - a body we cannot read must not mask the HTTP error
            pass
        error = OllamaError(f"HTTP {exc.code}{': ' + detail[:500] if detail else ''}")
        error.status_code = exc.code  # type: ignore[attr-defined]
        raise error from exc
    except urllib.error.URLError as exc:
        raise OllamaError(str(exc.reason)) from exc
    except TimeoutError as exc:
        raise OllamaError(f"timed out after {timeout}s") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OllamaError("response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise OllamaError("response was not a JSON object")
    return parsed
