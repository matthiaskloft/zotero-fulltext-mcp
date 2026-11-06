"""Internal extraction-evaluation artifact schemas (roadmap E1, package A).

Benchmark-side only: nothing here ships in the installed package, and ``jsonschema`` is a test
extra, not a runtime dependency. Three record kinds stay structurally separate (see
``docs/extraction-quality-plan.md``):

- ``expectation``: reviewed ground truth, authored by a human against page images.
- ``observation``: automatic extractor output; never ground truth.
- ``run``: pinned configurations/inputs, per-result hashes, failures, costs and metric versions.

``olmocr_export`` is a thin one-way adapter that turns a reviewed expectation into olmOCR-bench
checks; it imports nothing from olmOCR (``docs/olmocr-bench-feasibility.md``). It is deliberately
not re-exported here so that ``python -m extraction.olmocr_export`` runs cleanly.

Public API: ``load_record``, ``load_reviewed_expectation``, ``validate_record``, ``schema_path``,
``sha256_bytes``/``sha256_text``/``options_sha256``, ``verify_output_spans`` and ``RecordError``.
"""

from .records import (
    RECORD_KINDS,
    SCHEMA_VERSION,
    RecordError,
    load_record,
    load_reviewed_expectation,
    options_sha256,
    schema_path,
    sha256_bytes,
    sha256_text,
    validate_record,
    verify_output_spans,
)

__all__ = [
    "RECORD_KINDS",
    "SCHEMA_VERSION",
    "RecordError",
    "load_record",
    "load_reviewed_expectation",
    "options_sha256",
    "schema_path",
    "sha256_bytes",
    "sha256_text",
    "validate_record",
    "verify_output_spans",
]
