from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .bibtex import (
    DEFAULT_BBT_ENDPOINT,
    DEFAULT_BBT_TRANSLATOR,
    DEFAULT_CONNECTOR_ENDPOINT,
    DEFAULT_DEBUG_BRIDGE_ENDPOINT,
    append_bibtex_entries,
    check_better_bibtex,
    export_bibtex_entries,
    find_available_pdf_for_item,
    find_item_key_via_connector,
    import_doi_via_connector,
    link_local_pdf,
)
from .config import load_config, resolve_config_path, validate_config
from .converter import convert_sample, convert_verified, default_worker_count
from .fts import ChunkNotFoundError, build_fts_index, coverage_report, get_fulltext, search_fts
from .ingestion import dry_run_ingest, ingest_approved
from .indexer import append_text_index, build_text_index, load_indexed_keys
from .lock import PipelineLockedError, pipeline_write_lock
from .mapper import run_dry_run
from .mcp_contract import (
    BIBTEX_MCP_TOOL_NAME,
    DEFAULT_MCP_TOOL_NAMES,
    RECONVERT_MCP_TOOL_NAME,
    RECONVERT_STARTUP_TIMEOUT_SECONDS,
    RECONVERT_TOOL_TIMEOUT_SECONDS,
    RETRY_TIMEOUT_MCP_TOOL_NAMES,
    configured_index_path,
    marker_dependency_available,
)
from .runtime import DEFAULT_ZOTERO_EXE, ensure_zotero_running
from . import orphan_discovery as orphan_discovery_module
from . import retry_timeout as retry_timeout_module
from .verifier import apply_verification, verify_unverified
from .zotero_write import (
    apply_write_plan,
    approve_write_plan_rows,
    build_write_plan,
    validate_write_plan,
    write_plan_status,
)


def _default_fts_db() -> Path:
    """Best-effort default for --db/--output flags that don't otherwise take --config.

    Tries ZOTERO_FULLTEXT_DB, then the current machine's resolved project config, before
    falling back to a plain relative path -- never a different machine's absolute path.
    """
    env = os.environ.get("ZOTERO_FULLTEXT_DB")
    if env:
        return Path(env)
    try:
        config = load_config(resolve_config_path())
        return config.output_root / "index" / "zotero_text_index.sqlite"
    except (FileNotFoundError, KeyError, ValueError, OSError):
        return Path("converted_text") / "index" / "zotero_text_index.sqlite"


DEFAULT_FTS_DB = _default_fts_db()


def _pipeline_lock_root(index_related_path: Path) -> Path:
    """Best-effort output_root for locking, for commands that take an explicit index path
    instead of --config.

    Index files conventionally live at <output_root>/index/<file> (see docs/operations.md).
    convert-new and reconvert-math derive lock_root from their loaded config.output_root
    directly; build-index/append-index/build-fts have no config, only an explicit --output/
    --index path, so they must derive the equivalent root the same way to lock the same file --
    otherwise two commands writing into the same pipeline output can race past each other's
    lock. Walk up past a literal "index" directory component when present; otherwise fall back
    to the immediate parent (locking a narrower, but still safe, scope).
    """
    parent = index_related_path.parent
    return parent.parent if parent.name == "index" else parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zotero-pdf-text")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_setup = subparsers.add_parser(
        "check-setup",
        help="Validate config paths and environment before running a conversion. Read-only.",
    )
    check_setup.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    check_setup.add_argument(
        "--require-mcp",
        action="store_true",
        help="Fail if the mcp extra isn't installed. Informational-only by default.",
    )
    check_setup.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    dry_run = subparsers.add_parser("dry-run", help="Map Zotero metadata to linked PDFs without conversion.")
    dry_run.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    sample = subparsers.add_parser("convert-sample", help="Convert a small mapped_verified PDF sample to Markdown.")
    sample.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    sample.add_argument("--mapping-report", type=Path, required=True, help="Path to mapping_report.csv from a dry-run.")
    sample.add_argument("--limit", type=int, default=10, help="Maximum number of mapped_verified PDFs to convert.")
    sample.add_argument("--output-dir", type=Path, default=None, help="Optional output folder for this sample run.")
    sample.add_argument("--force", action="store_true", help="Reconvert PDFs even when Markdown already exists.")
    sample.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"Number of parallel Markdown conversion workers. Default: max(1, CPU cores - 4), currently {default_worker_count()}.",
    )
    sample.add_argument("--timeout-seconds", type=int, default=600, help="Per-PDF extraction timeout in seconds.")
    verified = subparsers.add_parser("convert-verified", help="Convert mapped_verified PDFs to Markdown.")
    verified.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    verified.add_argument("--mapping-report", type=Path, required=True, help="Path to mapping_report.csv from a dry-run.")
    verified.add_argument("--limit", type=int, default=None, help="Optional maximum number of mapped_verified PDFs.")
    verified.add_argument("--output-dir", type=Path, default=None, help="Optional output folder for this conversion run.")
    verified.add_argument(
        "--resume",
        action="store_true",
        help="Reuse an existing output folder, reuse existing Markdown bodies, and refresh metadata front matter.",
    )
    verified.add_argument("--force", action="store_true", help="Reconvert PDFs even when Markdown already exists.")
    verified.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"Number of parallel Markdown conversion workers. Default: max(1, CPU cores - 4), currently {default_worker_count()}.",
    )
    verified.add_argument("--timeout-seconds", type=int, default=600, help="Per-PDF extraction timeout in seconds.")
    unverified = subparsers.add_parser(
        "verify-unverified",
        help="Convert mapped_unverified PDFs to quarantine Markdown and write full-text review decisions.",
    )
    unverified.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    unverified.add_argument("--mapping-report", type=Path, required=True, help="Path to mapping_report.csv from a dry-run.")
    unverified.add_argument("--limit", type=int, default=None, help="Optional maximum number of mapped_unverified PDFs.")
    unverified.add_argument("--output-dir", type=Path, default=None, help="Optional output folder for this review run.")
    unverified.add_argument("--resume", action="store_true", help="Reuse an existing output folder and Markdown bodies.")
    unverified.add_argument("--force", action="store_true", help="Reconvert PDFs even when quarantine Markdown already exists.")
    unverified.add_argument(
        "--include-possible-mismatch",
        action="store_true",
        help="Also review possible_mismatch rows; default reviews only mapped_unverified.",
    )
    unverified.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"Number of parallel Markdown conversion workers. Default: max(1, CPU cores - 4), currently {default_worker_count()}.",
    )
    unverified.add_argument("--timeout-seconds", type=int, default=600, help="Per-PDF extraction timeout in seconds.")
    unverified.add_argument(
        "--agent-batch-size",
        type=int,
        default=25,
        help="Rows per agent_batches JSONL file for cheap LLM review of ambiguous cases; 0 disables batches.",
    )
    unverified.add_argument(
        "--index-jsonl",
        type=Path,
        default=None,
        help=(
            "Sidecar full-text index to check for already-resolved attachments, which are skipped "
            "rather than reconverted and rescored. Default: <output_root>/index/zotero_text_index.jsonl."
        ),
    )
    apply_review = subparsers.add_parser(
        "apply-verification",
        help="Promote accepted unverified reviews into a conversion-manifest-compatible trusted manifest.",
    )
    apply_review.add_argument("--review", type=Path, required=True, help="review.jsonl or reviewed JSONL from agents.")
    apply_review.add_argument(
        "--base-manifest",
        type=Path,
        default=None,
        help="Existing trusted manifest.csv to prepend before promoted rows.",
    )
    apply_review.add_argument("--output-manifest", type=Path, required=True, help="Manifest path to write.")
    apply_review.add_argument(
        "--min-confidence",
        type=float,
        default=0.92,
        help="Minimum confidence required to promote an accepted review row.",
    )
    index = subparsers.add_parser("build-index", help="Build a JSONL sidecar index from a conversion manifest.")
    index.add_argument("--manifest", type=Path, required=True, help="Path to manifest.csv from a conversion run.")
    index.add_argument(
        "--output",
        type=Path,
        default=Path("converted_text/index/zotero_text_index.jsonl"),
        help="Path to the JSONL index to write.",
    )
    append_index = subparsers.add_parser(
        "append-index",
        help="Append a small manifest's rows into an existing JSONL index (skips already-indexed attachment keys) and rebuild FTS.",
    )
    append_index.add_argument(
        "--manifest", type=Path, required=True, help="Manifest.csv with the new rows to add (e.g. from apply-verification)."
    )
    append_index.add_argument(
        "--index",
        type=Path,
        default=Path("converted_text/index/zotero_text_index.jsonl"),
        help="Existing JSONL index to append into, in place.",
    )
    append_index.add_argument(
        "--fts-db",
        type=Path,
        default=None,
        help="SQLite FTS database to rebuild from the updated index. Defaults next to --index.",
    )
    ensure = subparsers.add_parser("ensure-zotero", help="Start Zotero if needed and report connector health.")
    ensure.add_argument("--zotero-exe", type=Path, default=DEFAULT_ZOTERO_EXE, help="Path to zotero.exe.")
    ensure.add_argument("--wait-seconds", type=int, default=15, help="Seconds to wait for Zotero startup.")
    ensure.add_argument("--no-launch", action="store_true", help="Only check status; do not launch Zotero.")
    ensure.add_argument("--require-connector", action="store_true", help="Return non-zero if the local connector ping fails.")
    ensure.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    fts = subparsers.add_parser("build-fts", help="Build a SQLite FTS index from the JSONL sidecar.")
    fts.add_argument("--index-jsonl", type=Path, required=True, help="Path to zotero_text_index.jsonl.")
    fts.add_argument("--output", type=Path, default=DEFAULT_FTS_DB, help="SQLite FTS database to write.")
    fts.add_argument("--chunk-chars", type=int, default=6000, help="Maximum characters per searchable chunk.")
    fts.add_argument("--overlap-chars", type=int, default=500, help="Characters of overlap between chunks.")
    search = subparsers.add_parser("search-fts", help="Search the SQLite FTS full-text index.")
    search.add_argument("--db", type=Path, default=DEFAULT_FTS_DB, help="SQLite FTS database path.")
    search.add_argument("--query", required=True, help="Search query.")
    search.add_argument(
        "--search-mode",
        choices=("all_terms", "any_terms", "phrase"),
        default="all_terms",
        help="Match all normalized terms (default), any term, or the normalized phrase.",
    )
    search.add_argument("--limit", type=int, default=10, help="Maximum results.")
    search.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    fulltext = subparsers.add_parser("get-fulltext", help="Fetch bounded converted full text for one attachment.")
    fulltext.add_argument("--db", type=Path, default=DEFAULT_FTS_DB, help="SQLite FTS database path.")
    fulltext.add_argument("--attachment-key", required=True, help="Zotero attachment key.")
    fulltext.add_argument("--chunk-index", type=int, default=None, help="Optional chunk index to fetch.")
    fulltext.add_argument("--max-chars", type=int, default=12000, help="Maximum text characters to print.")
    fulltext.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    coverage = subparsers.add_parser("coverage-report", help="Summarize SQLite FTS coverage.")
    coverage.add_argument("--db", type=Path, default=DEFAULT_FTS_DB, help="SQLite FTS database path.")
    coverage.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    bibtex_check = subparsers.add_parser("bibtex-check", help="Check Better BibTeX JSON-RPC availability.")
    bibtex_check.add_argument("--endpoint", default=DEFAULT_BBT_ENDPOINT, help="Better BibTeX JSON-RPC endpoint.")
    bibtex_export = subparsers.add_parser("bibtex-export", help="Export Better BibTeX/BibLaTeX entries by citation key.")
    _add_bibtex_key_args(bibtex_export)
    bibtex_export.add_argument("--translator", default=DEFAULT_BBT_TRANSLATOR, help="BBT translator name.")
    bibtex_export.add_argument("--endpoint", default=DEFAULT_BBT_ENDPOINT, help="Better BibTeX JSON-RPC endpoint.")
    bibtex_export.add_argument("--library-id", default=None, help="Optional Zotero library ID. Omit for My Library.")
    bibtex_export.add_argument("--output", type=Path, default=None, help="Optional .bib file to write instead of stdout.")
    bibtex_export.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    bibtex_add = subparsers.add_parser("bibtex-add", help="Append missing Better BibTeX entries to a references.bib file.")
    _add_bibtex_key_args(bibtex_add)
    bibtex_add.add_argument("--references-bib", type=Path, required=True, help="LaTeX project references.bib path.")
    bibtex_add.add_argument("--translator", default=DEFAULT_BBT_TRANSLATOR, help="BBT translator name.")
    bibtex_add.add_argument("--endpoint", default=DEFAULT_BBT_ENDPOINT, help="Better BibTeX JSON-RPC endpoint.")
    bibtex_add.add_argument("--library-id", default=None, help="Optional Zotero library ID. Omit for My Library.")
    bibtex_add.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    ingest = subparsers.add_parser("ingest-candidates", help="Dry-run dedupe for an LLM article import queue.")
    ingest.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    ingest.add_argument("--input", type=Path, required=True, help="Candidate queue JSONL or JSON array.")
    ingest.add_argument("--output", type=Path, default=None, help="Optional JSONL dry-run report to write.")
    ingest_approved_parser = subparsers.add_parser(
        "ingest-approved",
        help="Deprecated guarded write entrypoint; use zotero-write plan/validate/apply.",
    )
    ingest_approved_parser.add_argument("--input", type=Path, required=True, help="Approved candidate JSONL.")
    zotero_write = subparsers.add_parser("zotero-write", help="Approval-gated Zotero write-plan workflow.")
    write_subparsers = zotero_write.add_subparsers(dest="write_command", required=True)
    write_plan = write_subparsers.add_parser("plan", help="Create an audited Zotero write plan from candidates.")
    write_plan.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    write_plan.add_argument("--input", type=Path, required=True, help="Candidate queue JSONL or JSON array.")
    write_plan.add_argument("--output", type=Path, required=True, help="Write-plan JSONL to create.")
    write_validate = write_subparsers.add_parser("validate", help="Validate a Zotero write plan.")
    write_validate.add_argument("--plan", type=Path, required=True, help="Write-plan JSONL.")
    write_validate.add_argument(
        "--require-approved",
        action="store_true",
        help="Require approval_status='approved' for all write operations.",
    )
    write_approve = write_subparsers.add_parser("approve", help="Approve selected 1-based rows in a write plan.")
    write_approve.add_argument("--plan", type=Path, required=True, help="Write-plan JSONL to update in place.")
    write_approve.add_argument(
        "--rows",
        required=True,
        help="Comma-, semicolon-, or whitespace-separated 1-based row numbers to approve.",
    )
    write_apply = write_subparsers.add_parser("apply", help="Generate approved Zotero JavaScript from a write plan.")
    write_apply.add_argument("--plan", type=Path, required=True, help="Approved write-plan JSONL.")
    write_apply.add_argument("--approve", action="store_true", help="Required explicit approval gate.")
    write_apply.add_argument("--out-script", type=Path, required=True, help="Generated Zotero JavaScript path.")
    write_apply.add_argument(
        "--no-auto-run",
        action="store_true",
        help="Do not attempt local auto-run discovery; just write the JavaScript script.",
    )
    write_status = write_subparsers.add_parser("status", help="Summarize a Zotero write plan.")
    write_status.add_argument("--plan", type=Path, required=True, help="Write-plan JSONL.")
    import_doi = subparsers.add_parser(
        "import-doi",
        help="Add a reference to Zotero by DOI via the Zotero connector (no plugins required).",
    )
    import_doi.add_argument("--doi", required=True, help="DOI to import (e.g. 10.1037/xge0001375).")
    import_doi.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    import_doi.add_argument(
        "--connector-endpoint",
        default=DEFAULT_CONNECTOR_ENDPOINT,
        help="Zotero connector base URL.",
    )
    import_doi.add_argument(
        "--debug-bridge-endpoint",
        default=DEFAULT_DEBUG_BRIDGE_ENDPOINT,
        help="debug-bridge execute endpoint (requires plugin + ZOTERO_DEBUG_BRIDGE_TOKEN env var).",
    )
    import_doi.add_argument(
        "--debug-bridge-token",
        default="",
        help="debug-bridge Bearer token (overrides ZOTERO_DEBUG_BRIDGE_TOKEN env var).",
    )
    check_pdf = subparsers.add_parser(
        "check-pdf",
        help="Check whether a Zotero item has a PDF attachment (reads local SQLite, no connector required).",
    )
    check_pdf.add_argument("--key", required=True, help="Zotero item key (8-character alphanumeric).")
    check_pdf.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    check_pdf.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    find_pdf = subparsers.add_parser(
        "find-pdf",
        help=(
            "Trigger Zotero's own 'Find Available PDF' search for an item that has no PDF "
            "attachment yet (requires the debug-bridge plugin)."
        ),
    )
    find_pdf.add_argument("--key", required=True, help="Zotero item key (8-character alphanumeric).")
    find_pdf.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    find_pdf.add_argument(
        "--debug-bridge-endpoint",
        default=DEFAULT_DEBUG_BRIDGE_ENDPOINT,
        help="debug-bridge execute endpoint (requires plugin + ZOTERO_DEBUG_BRIDGE_TOKEN env var).",
    )
    find_pdf.add_argument(
        "--debug-bridge-token",
        default="",
        help="debug-bridge Bearer token (overrides ZOTERO_DEBUG_BRIDGE_TOKEN env var).",
    )
    link_pdf = subparsers.add_parser(
        "link-pdf",
        help=(
            "Link a local PDF to a Zotero item, then relocate it into the managed linked-attachments "
            "folder via the ZotMoov plugin (copies + renames it, matching Zotero's own auto-move "
            "behavior). Use this instead of manually attaching a PDF found outside Zotero's storage -- "
            "a plain link left at its original location becomes a dangling attachment if that location "
            "is ever cleaned up. Requires the debug-bridge and ZotMoov plugins."
        ),
    )
    link_pdf.add_argument("--key", required=True, help="Zotero parent item key (8-character alphanumeric).")
    link_pdf.add_argument("--file", required=True, help="Absolute path to the local PDF to attach.")
    link_pdf.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    link_pdf.add_argument(
        "--debug-bridge-endpoint",
        default=DEFAULT_DEBUG_BRIDGE_ENDPOINT,
        help="debug-bridge execute endpoint (requires plugin + ZOTERO_DEBUG_BRIDGE_TOKEN env var).",
    )
    link_pdf.add_argument(
        "--debug-bridge-token",
        default="",
        help="debug-bridge Bearer token (overrides ZOTERO_DEBUG_BRIDGE_TOKEN env var).",
    )
    convert_new = subparsers.add_parser(
        "convert-new",
        help=(
            "Incremental pipeline: run dry-run, detect new items not yet in the JSONL index, "
            "convert them, and merge into the index and FTS database."
        ),
    )
    convert_new.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    convert_new.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="JSONL index to update. Default: <output_root>/index/zotero_text_index.jsonl.",
    )
    convert_new.add_argument(
        "--fts-db",
        type=Path,
        default=None,
        help="SQLite FTS database to rebuild. Default: ZOTERO_FULLTEXT_DB env var or the standard path.",
    )
    convert_new.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"Parallel conversion workers. Default: {default_worker_count()}.",
    )
    convert_new.add_argument("--timeout-seconds", type=int, default=600, help="Per-PDF timeout in seconds.")
    reconvert_math = subparsers.add_parser(
        "reconvert-math",
        help=(
            "Re-extract a single paper's markdown using marker-pdf (LaTeX-aware equation and "
            "figure handling), overwriting the existing markdown file in place. Use when the "
            "default pymupdf4llm extraction produced garbled or missing equations. Requires the "
            "optional 'marker' extra (pip install -e .[marker])."
        ),
    )
    reconvert_math.add_argument("--key", required=True, help="Zotero attachment key.")
    reconvert_math.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    reconvert_math.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="JSONL index to update. Default: <output_root>/index/zotero_text_index.jsonl.",
    )
    reconvert_math.add_argument(
        "--fts-db",
        type=Path,
        default=None,
        help="SQLite FTS database to rebuild. Default: ZOTERO_FULLTEXT_DB env var or the standard path.",
    )
    reconvert_math.add_argument("--timeout-seconds", type=int, default=5400, help="marker-pdf timeout in seconds.")
    retry_timeout = subparsers.add_parser(
        "retry-timeout",
        help=(
            "Resolve a recorded extraction-timeout candidate: either permanently skip the primary "
            "extractor for it (no source change needed) or reconvert it with a longer timeout and "
            "promote a successful result into the live manifest/index."
        ),
    )
    retry_timeout.add_argument("--key", required=True, help="Zotero attachment key from timeout_candidates.jsonl.")
    retry_timeout.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    retry_timeout.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="JSONL index to update on a successful retry. Default: <output_root>/index/zotero_text_index.jsonl.",
    )
    retry_timeout.add_argument(
        "--fts-db",
        type=Path,
        default=None,
        help="SQLite FTS database to rebuild on a successful retry. Default: ZOTERO_FULLTEXT_DB env var or the standard path.",
    )
    retry_timeout_mode = retry_timeout.add_mutually_exclusive_group(required=True)
    retry_timeout_mode.add_argument(
        "--skip",
        action="store_true",
        help="Permanently skip the primary extractor for this attachment (writes timeout_skip_list.json); does not reconvert.",
    )
    retry_timeout_mode.add_argument(
        "--retry",
        action="store_true",
        help="Reconvert with a longer timeout and promote a successful result into the live index.",
    )
    retry_timeout.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help=(
            "Explicit retry timeout in seconds (--retry only); defaults to the candidate's "
            f"suggested_next_timeout_seconds. Hard-capped at {retry_timeout_module.MAX_RETRY_TIMEOUT_SECONDS}."
        ),
    )
    retry_timeout.add_argument(
        "--multiplier",
        type=float,
        default=None,
        help="Multiply the candidate's last attempted timeout instead of --timeout-seconds (--retry only).",
    )
    retry_timeout.add_argument("--reason", default="", help="Free-text note stored with a --skip decision.")
    find_orphan_parents = subparsers.add_parser(
        "find-orphan-parents",
        help=(
            "Scan a dry-run's orphan_pdf rows for plausible Zotero parents by PDF content (title/DOI/"
            "author/year in the early pages), not filename -- the reverse of dry-run's own filename-"
            "based metadata-candidate matching. Explicit opt-in; not part of dry-run. Reports only "
            "high-confidence (verified) matches by default; see --include-lower-confidence."
        ),
    )
    find_orphan_parents.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    find_orphan_parents.add_argument("--mapping-report", type=Path, required=True, help="Path to mapping_report.csv from a dry-run.")
    find_orphan_parents.add_argument("--output-dir", type=Path, default=None, help="Optional output folder for this discovery run.")
    find_orphan_parents.add_argument("--limit", type=int, default=None, help="Optional maximum number of orphan_pdf rows to scan.")
    find_orphan_parents.add_argument(
        "--include-lower-confidence",
        action="store_true",
        help=(
            "Also report 'medium'/'low' confidence tiers (a fuzzy title match classify_identity "
            "itself left unverified). Off by default: on real libraries these produce mostly noise "
            "(e.g. generic chapter titles like 'Index'/'Citations'/'Preface' from an edited volume "
            "scoring a high fuzzy match against nearly any PDF). Default output is 'high' confidence "
            "only (classify_identity's own verified status)."
        ),
    )
    orphan_candidate = subparsers.add_parser(
        "orphan-candidate",
        help=(
            "Resolve one suggested (orphan PDF, candidate parent) pairing from orphan_candidates.jsonl: "
            "either dismiss it, or record that you separately confirmed it and ran `link-pdf` yourself."
        ),
    )
    orphan_candidate.add_argument("--config", type=Path, default=Path("config.json"), help="Path to project config JSON.")
    orphan_candidate.add_argument("--orphan-sha256", required=True, help="orphan_sha256 from orphan_candidates.jsonl.")
    orphan_candidate.add_argument("--parent-key", required=True, help="candidate_parent_key from orphan_candidates.jsonl.")
    orphan_candidate_mode = orphan_candidate.add_mutually_exclusive_group(required=True)
    orphan_candidate_mode.add_argument("--skip", action="store_true", help="Dismiss this suggested pairing as not a match.")
    orphan_candidate_mode.add_argument(
        "--mark-resolved",
        action="store_true",
        help="Record that this pairing was confirmed and already attached via `link-pdf` (bookkeeping only; does not attach anything itself).",
    )
    orphan_candidate.add_argument("--reason", default="", help="Free-text note stored with a --skip decision.")
    orphan_candidate.add_argument("--note", default="", help="Free-text note stored with a --mark-resolved decision.")
    install_mcp = subparsers.add_parser(
        "install-mcp",
        help=(
            "Print (or apply) the MCP client registration for this machine's venv/config, "
            "generated from resolved paths instead of hand-edited JSON."
        ),
    )
    install_mcp.add_argument("--server-name", default="zotero-fulltext", help="MCP server name to register.")
    install_mcp.add_argument("--config", type=Path, default=None, help="Path to project config JSON. Default: resolved for this machine.")
    install_mcp.add_argument("--db", type=Path, default=None, help="SQLite FTS database path. Default: <output_root>/index/zotero_text_index.sqlite.")
    install_mcp.add_argument("--enable-bibtex", action="store_true", help="Enable the optional local Better BibTeX MCP integration.")
    install_mcp.add_argument(
        "--enable-reconvert",
        action="store_true",
        help="Enable single-attachment math OCR in MCP; the generated registration includes the explicit config.",
    )
    install_mcp.add_argument(
        "--enable-retry-timeout",
        action="store_true",
        help="Enable timeout skip/retry decisions in MCP; the generated registration includes the explicit config.",
    )
    install_mcp.add_argument(
        "--bibtex-endpoint",
        default=None,
        help="Optional local Better BibTeX endpoint to pass at server startup; requires --enable-bibtex.",
    )
    install_mcp.add_argument(
        "--apply",
        action="store_true",
        help="Also run 'claude mcp add' to register the server, instead of only printing it.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check-setup":
        results = run_setup_checks(args.config, require_mcp=args.require_mcp)
        if args.json:
            print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
        else:
            _print_setup_check_results(results)
        failed_required = [result for result in results if result.required and not result.ok]
        return 1 if failed_required else 0
    if args.command == "dry-run":
        config = load_config(args.config)
        validate_config(config)
        run_dir = run_dry_run(config)
        print(f"Dry-run complete: {run_dir}")
        return 0
    if args.command == "convert-sample":
        config = load_config(args.config)
        validate_config(config)
        try:
            with pipeline_write_lock(config.output_root, command="convert-sample"):
                run_dir = convert_sample(
                    config,
                    args.mapping_report,
                    limit=args.limit,
                    output_dir=args.output_dir,
                    workers=args.workers,
                    timeout_seconds=args.timeout_seconds,
                    force=args.force,
                )
        except PipelineLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Sample conversion complete: {run_dir}")
        return 0
    if args.command == "convert-verified":
        config = load_config(args.config)
        validate_config(config)
        try:
            with pipeline_write_lock(config.output_root, command="convert-verified"):
                run_dir = convert_verified(
                    config,
                    args.mapping_report,
                    limit=args.limit,
                    output_dir=args.output_dir,
                    resume=args.resume,
                    workers=args.workers,
                    timeout_seconds=args.timeout_seconds,
                    force=args.force,
                )
        except PipelineLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Verified conversion complete: {run_dir}")
        return 0
    if args.command == "verify-unverified":
        config = load_config(args.config)
        validate_config(config)
        try:
            with pipeline_write_lock(config.output_root, command="verify-unverified"):
                run_dir = verify_unverified(
                    config,
                    args.mapping_report,
                    limit=args.limit,
                    output_dir=args.output_dir,
                    resume=args.resume,
                    workers=args.workers,
                    timeout_seconds=args.timeout_seconds,
                    force=args.force,
                    include_possible_mismatch=args.include_possible_mismatch,
                    agent_batch_size=args.agent_batch_size,
                    index_jsonl=args.index_jsonl,
                )
        except PipelineLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Unverified full-text review complete: {run_dir}")
        return 0
    if args.command == "apply-verification":
        try:
            with pipeline_write_lock(args.output_manifest.parent, command="apply-verification"):
                summary = apply_verification(
                    args.review,
                    args.output_manifest,
                    base_manifest=args.base_manifest,
                    min_confidence=args.min_confidence,
                )
        except PipelineLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "build-index":
        try:
            with pipeline_write_lock(_pipeline_lock_root(args.output), command="build-index"):
                output = build_text_index(args.manifest, args.output)
        except PipelineLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Text index complete: {output}")
        return 0
    if args.command == "append-index":
        fts_db = args.fts_db or (args.index.parent / f"{args.index.stem}.sqlite")
        # append-index writes both --index and fts_db; a single _pipeline_lock_root(args.index)
        # only protects the JSONL side. If an explicit --fts-db resolves to a different canonical
        # root (e.g. a separate drive/share), locking just one leaves the other artifact
        # unprotected against a concurrent command writing the same FTS file from its own root --
        # refuse rather than silently guaranteeing less than the lock implies.
        index_lock_root = _pipeline_lock_root(args.index)
        fts_lock_root = _pipeline_lock_root(fts_db)
        if index_lock_root != fts_lock_root:
            print(
                f"--index ({args.index}) and --fts-db ({fts_db}) resolve to different pipeline "
                f"lock roots ({index_lock_root} vs {fts_lock_root}); place both under one "
                "output_root so this command's lock actually covers everything it writes.",
                file=sys.stderr,
            )
            return 2
        try:
            with pipeline_write_lock(index_lock_root, command="append-index"):
                before = len(load_indexed_keys(args.index))
                append_text_index(args.manifest, args.index, args.index)
                after = len(load_indexed_keys(args.index))
                summary = build_fts_index(args.index, fts_db)
        except PipelineLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        result = summary.to_dict()
        result["new_records"] = after - before
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "ensure-zotero":
        status = ensure_zotero_running(
            zotero_exe=args.zotero_exe,
            wait_seconds=args.wait_seconds,
            launch=not args.no_launch,
            require_connector=args.require_connector,
        )
        if args.json:
            print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_zotero_status(status.to_dict())
        return 0 if status.running and (status.connector_ok or not args.require_connector) else 1
    if args.command == "build-fts":
        try:
            with pipeline_write_lock(_pipeline_lock_root(args.output), command="build-fts"):
                summary = build_fts_index(
                    args.index_jsonl,
                    args.output,
                    chunk_chars=args.chunk_chars,
                    overlap_chars=args.overlap_chars,
                )
        except PipelineLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "search-fts":
        results = search_fts(args.db, args.query, limit=args.limit, search_mode=args.search_mode)
        if args.json:
            print(
                json.dumps(
                    {"search_mode": args.search_mode, "no_results": not results, "results": [result.to_dict() for result in results]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_search_results(results, search_mode=args.search_mode)
        return 0
    if args.command == "get-fulltext":
        try:
            result = get_fulltext(
                args.db,
                attachment_key=args.attachment_key,
                max_chars=args.max_chars,
                chunk_index=args.chunk_index,
            )
        except ChunkNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_fulltext_result(result.to_dict())
        return 0
    if args.command == "coverage-report":
        report = coverage_report(args.db)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_coverage_report(report)
        return 0
    if args.command == "bibtex-check":
        print(json.dumps(check_better_bibtex(args.endpoint), ensure_ascii=False, indent=2))
        return 0
    if args.command == "bibtex-export":
        export = export_bibtex_entries(
            _citation_keys_from_args(args),
            translator=args.translator,
            endpoint=args.endpoint,
            library_id=args.library_id,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(export.entry, encoding="utf-8", newline="\n")
        if args.json:
            print(json.dumps(export.to_dict(), ensure_ascii=False, indent=2))
        elif not args.output:
            print(export.entry, end="")
        else:
            print(f"BibTeX export complete: {args.output}")
        return 0
    if args.command == "bibtex-add":
        result = append_bibtex_entries(
            _citation_keys_from_args(args),
            args.references_bib,
            translator=args.translator,
            endpoint=args.endpoint,
            library_id=args.library_id,
        )
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"references.bib: {result.references_bib}")
            print(f"added: {', '.join(result.added_keys) if result.added_keys else '(none)'}")
            print(
                "skipped_existing: "
                + (", ".join(result.skipped_existing_keys) if result.skipped_existing_keys else "(none)")
            )
        return 0
    if args.command == "ingest-candidates":
        config = load_config(args.config)
        validate_config(config)
        decisions = dry_run_ingest(args.input, config.zotero_sqlite, args.output)
        print(json.dumps([decision.to_dict() for decision in decisions], ensure_ascii=False, indent=2))
        return 0
    if args.command == "ingest-approved":
        try:
            ingest_approved(args.input)
        except NotImplementedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    if args.command == "zotero-write":
        if args.write_command == "plan":
            config = load_config(args.config)
            validate_config(config)
            records = build_write_plan(args.input, config.zotero_sqlite, args.output)
            print(json.dumps(write_plan_status(args.output), ensure_ascii=False, indent=2))
            print(f"Write plan created: {args.output}")
            print(
                "Approve intended write rows with zotero-write approve --rows <row_numbers>, then run "
                "zotero-write validate --require-approved and zotero-write apply --approve."
            )
            return 0
        if args.write_command == "validate":
            result = validate_write_plan(args.plan, require_approved=args.require_approved)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0 if result.ok else 1
        if args.write_command == "approve":
            try:
                result = approve_write_plan_rows(args.plan, _row_numbers_from_arg(args.rows))
            except (ValueError, FileNotFoundError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.write_command == "apply":
            try:
                result = apply_write_plan(
                    args.plan,
                    args.out_script,
                    approve=args.approve,
                    auto_run=not args.no_auto_run,
                )
            except (PermissionError, ValueError, FileNotFoundError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.write_command == "status":
            print(json.dumps(write_plan_status(args.plan), ensure_ascii=False, indent=2))
            return 0
    if args.command == "import-doi":
        import time
        from .zotero_db import find_item_by_doi  # used only for pre-import dedup check
        config = load_config(args.config)
        validate_config(config)
        doi = args.doi.strip()

        existing_key = find_item_by_doi(doi, config.zotero_sqlite)
        if existing_key:
            print(json.dumps({"status": "already_in_library", "doi": doi, "key": existing_key},
                             ensure_ascii=False, indent=2))
            return 0

        result = import_doi_via_connector(
            doi,
            connector_endpoint=args.connector_endpoint,
            debug_bridge_endpoint=args.debug_bridge_endpoint,
            debug_bridge_token=args.debug_bridge_token,
        )
        if not result.ok:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

        # Poll Zotero connector REST API for the newly created item (connector returns 201 with no body).
        # The connector API reads live in-process data, avoiding SQLite WAL lag.
        new_key: str | None = None
        for _ in range(5):
            time.sleep(1)
            new_key = find_item_key_via_connector(
                doi, title_hint=result.title, connector_endpoint=args.connector_endpoint
            )
            if new_key:
                break

        print(json.dumps({
            "status": "imported",
            "doi": doi,
            "title": result.title,
            "item_type": result.item_type,
            "key": new_key,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "check-pdf":
        from .zotero_db import check_pdf_attachment
        config = load_config(args.config)
        validate_config(config)
        result = check_pdf_attachment(args.key, config.zotero_sqlite)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result["found"]:
            print(f"PDF attachment found for {args.key}:")
            for att in result["attachments"]:
                print(f"  key={att['key']}  path={att['path']}")
        else:
            print(f"No PDF attachment found for {args.key}.")
        return 0
    if args.command == "find-pdf":
        result = find_available_pdf_for_item(
            args.key,
            debug_bridge_endpoint=args.debug_bridge_endpoint,
            debug_bridge_token=args.debug_bridge_token,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    if args.command == "link-pdf":
        result = link_local_pdf(
            args.key,
            args.file,
            debug_bridge_endpoint=args.debug_bridge_endpoint,
            debug_bridge_token=args.debug_bridge_token,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    if args.command == "convert-new":
        config = load_config(args.config)
        validate_config(config)
        jsonl_path = args.jsonl or (config.output_root / "index" / "zotero_text_index.jsonl")
        fts_db = args.fts_db or (config.output_root / "index" / "zotero_text_index.sqlite")
        try:
            with pipeline_write_lock(config.output_root, command="convert-new"):
                print("Running dry-run to regenerate mapping report...")
                run_dir = run_dry_run(config)
                mapping_report = run_dir / "mapping_report.csv"
                indexed_keys = load_indexed_keys(jsonl_path)
                print(f"Existing index: {len(indexed_keys)} records")
                new_rows = _filter_new_mapping_rows(mapping_report, indexed_keys)
                if not new_rows:
                    print("No new mapped_verified items detected. Index is up to date.")
                    return 0
                print(f"New items to convert: {len(new_rows)}")
                filtered_report = run_dir / "new_items_mapping_report.csv"
                _write_filtered_mapping_csv(mapping_report, filtered_report, new_rows)
                conv_run_dir = convert_verified(
                    config,
                    filtered_report,
                    workers=args.workers,
                    timeout_seconds=args.timeout_seconds,
                )
                print(f"Conversion complete: {conv_run_dir}")
                new_manifest = conv_run_dir / "manifest.csv"
                append_text_index(new_manifest, jsonl_path, jsonl_path)
                print(f"JSONL index updated: {jsonl_path}")
                summary = build_fts_index(jsonl_path, fts_db)
        except PipelineLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "reconvert-math":
        from .math_ocr import reconvert_with_marker

        config = load_config(args.config)
        validate_config(config)
        jsonl_path = args.jsonl or (config.output_root / "index" / "zotero_text_index.jsonl")
        fts_db = args.fts_db or (config.output_root / "index" / "zotero_text_index.sqlite")
        result = reconvert_with_marker(
            args.key,
            db_path=fts_db,
            jsonl_path=jsonl_path,
            fts_db_path=fts_db,
            lock_root=config.output_root,
            timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    if args.command == "retry-timeout":
        if args.timeout_seconds is not None and args.multiplier is not None:
            print("--timeout-seconds and --multiplier are mutually exclusive.", file=sys.stderr)
            return 2
        config = load_config(args.config)
        validate_config(config)
        if args.skip:
            result = retry_timeout_module.skip_timeout_candidate(args.key, config=config, reason=args.reason)
        else:
            jsonl_path = args.jsonl or (config.output_root / "index" / "zotero_text_index.jsonl")
            fts_db = args.fts_db or (config.output_root / "index" / "zotero_text_index.sqlite")
            result = retry_timeout_module.retry_timeout_candidate(
                args.key,
                config=config,
                jsonl_path=jsonl_path,
                fts_db_path=fts_db,
                timeout_seconds=args.timeout_seconds,
                multiplier=args.multiplier,
            )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    if args.command == "find-orphan-parents":
        config = load_config(args.config)
        validate_config(config)
        run_dir = orphan_discovery_module.run_orphan_discovery(
            config,
            args.mapping_report,
            output_dir=args.output_dir,
            limit=args.limit,
            include_lower_confidence=args.include_lower_confidence,
        )
        print(f"Orphan-parent discovery complete: {run_dir}")
        return 0
    if args.command == "orphan-candidate":
        config = load_config(args.config)
        validate_config(config)
        if args.skip:
            result = orphan_discovery_module.skip_orphan_candidate(
                config, args.orphan_sha256, args.parent_key, reason=args.reason
            )
        else:
            result = orphan_discovery_module.mark_orphan_candidate_resolved(
                config, args.orphan_sha256, args.parent_key, note=args.note
            )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    if args.command == "install-mcp":
        return _install_mcp(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


def _install_mcp(args: argparse.Namespace) -> int:
    """Generate this machine's MCP client registration from resolved paths.

    Replaces a hand-edit-the-JSON-paths workflow: every path here comes from the current venv
    (sys.executable) and this machine's own resolved config, so a new machine is a run of this
    command, not a find-and-replace across a doc.

    Both --db and --config are baked into the generated registration explicitly. This matters
    once the server code and the config/data it points at live in different locations (e.g. the
    server installed from a cloned repo, config and converted-text data in a separate Zotero
    workspace folder) -- the server subprocess must not depend on inheriting the right cwd from
    whatever process launches it.
    """
    config_path = args.config if args.config is not None else resolve_config_path()
    if not config_path.exists():
        print(f"No project config found at {config_path}.", file=sys.stderr)
        print("Set ZOTERO_PDF_TEXT_CONFIG, pass --config, or create config.json for this machine.", file=sys.stderr)
        return 2
    config = load_config(config_path)
    expected_db_path = configured_index_path(config)
    db_path = args.db if args.db is not None else expected_db_path
    if args.enable_reconvert:
        try:
            validate_config(config)
        except (OSError, TypeError, ValueError):
            print("--enable-reconvert requires a valid project config.", file=sys.stderr)
            return 2
        if db_path.resolve(strict=False) != expected_db_path.resolve(strict=False):
            print("--enable-reconvert requires --db to be the index governed by the project config.", file=sys.stderr)
            return 2
        if not marker_dependency_available():
            print("--enable-reconvert requires the optional marker dependency (pip install -e .[marker]).", file=sys.stderr)
            return 2
    if args.enable_retry_timeout:
        try:
            validate_config(config)
        except (OSError, TypeError, ValueError):
            print("--enable-retry-timeout requires a valid project config.", file=sys.stderr)
            return 2
        if db_path.resolve(strict=False) != expected_db_path.resolve(strict=False):
            print("--enable-retry-timeout requires --db to be the index governed by the project config.", file=sys.stderr)
            return 2

    venv_scripts_dir = Path(sys.executable).resolve().parent
    exe_name = "zotero-fulltext-mcp.exe" if os.name == "nt" else "zotero-fulltext-mcp"
    server_exe = venv_scripts_dir / exe_name
    if not server_exe.exists():
        print(f"Warning: {server_exe} does not exist yet -- install with 'pip install -e .[mcp]' first.", file=sys.stderr)

    server_name = args.server_name
    server_args = ["--db", str(db_path), "--config", str(config_path)]
    if args.enable_bibtex:
        server_args.append("--enable-bibtex")
    if args.enable_reconvert:
        server_args.append("--enable-reconvert")
    if args.enable_retry_timeout:
        server_args.append("--enable-retry-timeout")
    if args.bibtex_endpoint:
        if not args.enable_bibtex:
            print("--bibtex-endpoint requires --enable-bibtex.", file=sys.stderr)
            return 2
        server_args.extend(["--bibtex-endpoint", args.bibtex_endpoint])
    claude_add_args = ["mcp", "add", "--scope", "user", server_name, str(server_exe), "--", *server_args]
    claude_cmd = "claude " + " ".join(_shell_quote(a) for a in claude_add_args)

    toml_name = server_name.replace("-", "_")
    enabled_tools = list(DEFAULT_MCP_TOOL_NAMES)
    if args.enable_bibtex:
        enabled_tools.append(BIBTEX_MCP_TOOL_NAME)
    if args.enable_reconvert:
        enabled_tools.append(RECONVERT_MCP_TOOL_NAME)
    if args.enable_retry_timeout:
        enabled_tools.extend(RETRY_TIMEOUT_MCP_TOOL_NAMES)
    # Use json.dumps for every embedded string, not Python repr(): repr() renders a backslash
    # as two characters, but TOML single-quoted (literal) strings treat backslashes literally,
    # so repr() output silently doubles every backslash in a Windows path once TOML parses it.
    # json.dumps's backslash/quote escaping matches TOML basic (double-quoted) string escaping.
    toml_args = ", ".join(json.dumps(arg) for arg in server_args)
    toml_tools = ", ".join(json.dumps(tool) for tool in enabled_tools)
    codex_block = (
        f"[mcp_servers.{toml_name}]\n"
        f"command = {json.dumps(str(server_exe))}\n"
        f"args = [{toml_args}]\n"
        "enabled = true\n"
        f"startup_timeout_sec = {RECONVERT_STARTUP_TIMEOUT_SECONDS if args.enable_reconvert else 30}\n"
        f"tool_timeout_sec = {RECONVERT_TOOL_TIMEOUT_SECONDS if args.enable_reconvert else 120}\n"
        f"enabled_tools = [{toml_tools}]"
    )

    print(f"Resolved config: {config_path}")
    print(f"Resolved db: {db_path}")
    print(f"Resolved server executable: {server_exe}")
    print()
    print("# Claude Code registration:")
    print(claude_cmd)
    if args.enable_reconvert:
        print()
        print(
            "# --enable-reconvert pulls in marker-pdf's torch/transformers dependencies, which "
            f"cold-import in ~{RECONVERT_STARTUP_TIMEOUT_SECONDS}s on a typical machine -- longer than "
            "Claude Code's default 30s MCP connection timeout. Before using this server, set (once, "
            "persistently, e.g. via `setx MCP_TIMEOUT " f"{RECONVERT_STARTUP_TIMEOUT_SECONDS * 1000}` "
            "on Windows or the shell-profile equivalent) so Claude Code waits long enough to connect:"
        )
        print(f"MCP_TIMEOUT={RECONVERT_STARTUP_TIMEOUT_SECONDS * 1000}")
    print()
    print("# Codex registration -- paste into your config.toml (this command does not edit it for you):")
    print(codex_block)

    if args.apply:
        print()
        print(f"Applying Claude Code registration for '{server_name}'...")
        # shutil.which resolves PATHEXT (e.g. claude.cmd) the way an interactive shell does;
        # subprocess.run(["claude", ...]) without shell=True does not, and fails with
        # FileNotFoundError on Windows even though "claude" works fine in the same shell.
        claude_exe = shutil.which("claude")
        if claude_exe is None:
            print("'claude' was not found on PATH -- run the printed command manually instead.", file=sys.stderr)
            return 2
        try:
            result = subprocess.run([claude_exe, *claude_add_args], check=False)
        except FileNotFoundError:
            print(
                f"'{claude_exe}' was found via PATH lookup but could not be launched -- "
                "run the printed command manually instead.",
                file=sys.stderr,
            )
            return 2
        if result.returncode != 0:
            print("claude mcp add failed; run the printed command manually.", file=sys.stderr)
            return result.returncode
        print(f"Applied. Verify with: claude mcp get {server_name}")
    return 0


_SHELL_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/\\-"
)


def _shell_quote(value: str) -> str:
    """Quote an arg for the printed command if it contains anything shell-meaningful.

    Double quotes are used rather than shlex.quote's POSIX single-quoting because the printed
    command is meant to be pasteable into PowerShell, cmd, or Git Bash alike -- all three accept
    double-quoted arguments with the same semantics for plain paths. Quoting on a safe-charset
    allowlist (rather than only whitespace) also covers shell metacharacters that are legal in
    Windows paths/server names, e.g. '&', '(', ')', which cmd and PowerShell both treat as command
    separators/operators when unquoted.
    """
    if value and all(c in _SHELL_SAFE_CHARS for c in value):
        return value
    return f'"{value}"'


def _print_zotero_status(status: dict[str, object]) -> None:
    print(f"Zotero executable: {status['zotero_exe']}")
    print(f"Running: {status['running']}")
    print(f"Launched: {status['launched']}")
    print(f"Connector OK: {status['connector_ok']}")
    print(f"Connector message: {status['connector_message']}")
    troubleshooting = status.get("troubleshooting") or []
    if troubleshooting:
        print("Troubleshooting:")
        for item in troubleshooting:
            print(f"- {item}")


def _add_bibtex_key_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--citation-key", action="append", default=[], help="Citation key to export; can be repeated.")
    parser.add_argument(
        "--citation-keys",
        default="",
        help="Comma-, semicolon-, or whitespace-separated citation keys.",
    )


def _citation_keys_from_args(args: argparse.Namespace) -> list[str]:
    return [*args.citation_key, args.citation_keys]


def _row_numbers_from_arg(value: str) -> list[int]:
    normalized = value.replace(",", " ").replace(";", " ")
    rows: list[int] = []
    for token in normalized.split():
        rows.append(int(token))
    return rows


def _configure_stdio() -> None:
    for stream in [sys.stdout, sys.stderr]:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _print_search_results(results: list[object], *, search_mode: str) -> None:
    print(f"search_mode: {search_mode}")
    if not results:
        print("No results.")
        return
    for index, result in enumerate(results, start=1):
        data = result.to_dict()
        print(f"{index}. {data['title']} ({data['year']})")
        print(f"   creators: {data['creators']}")
        print(f"   keys: parent={data['zotero_parent_key']} attachment={data['zotero_attachment_key']}")
        print(f"   citation_key: {data['citation_key']}")
        print(f"   score: {data['score']} chunk={data['chunk_index']} chars={data['start_char']}-{data['end_char']}")
        print(f"   extraction: {data['extraction_tool']} confidence={data['classification']}/{data['identity_status']}")
        print(f"   snippet: {data['snippet']}")
        print(f"   markdown: {data['markdown_path']}")


def _print_fulltext_result(result: dict[str, object]) -> None:
    print(f"# {result['title']}")
    print(f"zotero_parent_key: {result['zotero_parent_key']}")
    print(f"zotero_attachment_key: {result['zotero_attachment_key']}")
    print(f"citation_key: {result['citation_key']}")
    print(f"chunk_index: {result['chunk_index']}")
    print(f"chars: {result['start_char']}-{result['end_char']} of {result['total_chars']}")
    print(f"extraction_tool: {result['extraction_tool']}")
    print("")
    print(result["text"])


def _print_coverage_report(report: dict[str, object]) -> None:
    print(f"Records: {report['records']}")
    print(f"Chunks: {report['chunks']}")
    print(f"Total characters: {report['total_chars']}")
    print(f"Total words: {report['total_words']}")
    for field in ["by_classification", "by_identity_status", "by_extraction_tool"]:
        print(field + ":")
        for key, count in sorted(dict(report[field]).items()):
            print(f"- {key}: {count}")


@dataclass(frozen=True)
class SetupCheckResult:
    name: str
    ok: bool
    detail: str
    required: bool


def run_setup_checks(config_path: Path, *, require_mcp: bool = False) -> list[SetupCheckResult]:
    """Validate config, paths, and environment without performing any conversion work.

    Fails fast on the first missing piece a first-time user is likely to hit, rather than letting
    a bad path or missing dependency surface as a confusing failure partway through a long
    dry-run/conversion. Read-only: never writes to output_root beyond the writability probe in
    `_check_output_root_writable`, which itself never leaves a stray file behind.
    """
    results: list[SetupCheckResult] = [
        SetupCheckResult(
            "python_version",
            sys.version_info >= (3, 11),
            f"Python {'.'.join(str(part) for part in sys.version_info[:3])} (requires >=3.11)",
            required=True,
        )
    ]

    try:
        config = load_config(config_path)
    # TypeError covers structurally-malformed-but-syntactically-valid JSON: a top-level array
    # instead of an object, a non-dict entry in manually_accepted_mappings, a path field given as
    # a number, etc. -- load_config indexes/constructs Path() without validating shape first.
    except (FileNotFoundError, KeyError, ValueError, OSError, TypeError) as exc:
        results.append(SetupCheckResult("config", False, f"Failed to load {config_path}: {exc}", required=True))
        return results
    results.append(SetupCheckResult("config", True, f"Loaded {config_path}", required=True))

    # validate_config() covers everything below except output_root, which it doesn't check at
    # all since it's a write target rather than a required-to-exist input.
    for name, path in (
        ("zotero_root", config.zotero_root),
        ("zotero_data_directory", config.zotero_data_directory),
        ("linked_attachments", config.linked_attachments),
        ("zotero.sqlite", config.zotero_sqlite),
    ):
        results.append(SetupCheckResult(name, path.exists(), str(path), required=True))

    output_ok, output_detail = _check_output_root_writable(config.output_root)
    results.append(SetupCheckResult("output_root", output_ok, output_detail, required=True))

    for extra_name, module_name in (("mcp", "mcp"), ("zotero-write", "pyzotero"), ("marker", "marker")):
        available = importlib.util.find_spec(module_name) is not None
        detail = "installed" if available else "not installed (optional extra)"
        required = require_mcp and extra_name == "mcp"
        results.append(SetupCheckResult(f"extra:{extra_name}", available, detail, required=required))

    return results


def _check_output_root_writable(output_root: Path) -> tuple[bool, str]:
    """Probe writability by actually creating and removing a file, not just os.access.

    os.access(path, os.W_OK) doesn't guarantee file creation will succeed -- Windows ACLs,
    controlled-folder protection, quotas, and network filesystems can all permit the access-check
    bit while still rejecting a real write. Never creates output_root itself if it doesn't exist
    yet; the probe file is created in (and immediately removed from) the nearest existing
    ancestor instead, so this stays read-only with respect to the directory tree.
    """
    if output_root.exists() and not output_root.is_dir():
        return False, f"{output_root} exists but is not a directory"

    target = output_root
    while not target.exists():
        if target.parent == target:
            return False, f"{output_root} does not exist and no existing ancestor directory was found"
        target = target.parent

    try:
        fd, tmp_name = tempfile.mkstemp(dir=target, prefix=".zotero_pdf_text_write_check_")
    except OSError as exc:
        return False, f"Cannot create files under {target}: {exc}"
    os.close(fd)
    # Unlike the fts.py/indexer.py atomic-write cleanup (where a stray temp file next to a
    # successfully published index is harmless), a cleanup failure here is itself the finding:
    # this check's whole point is to prove nothing gets left behind, so failing to remove the
    # probe file must fail the check and report the leftover path -- not be silently suppressed.
    try:
        Path(tmp_name).unlink()
    except OSError as exc:
        return False, f"Created a write-check file under {target} but failed to remove it: {tmp_name} ({exc})"

    if output_root.exists():
        return True, f"{output_root} exists and is writable"
    return True, f"{output_root} does not exist yet but is creatable under {target}"


def _print_setup_check_results(results: list[SetupCheckResult]) -> None:
    for result in results:
        status = "OK" if result.ok else ("FAIL" if result.required else "WARN")
        print(f"[{status}] {result.name}: {result.detail}")


def _filter_new_mapping_rows(mapping_report: Path, indexed_keys: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with mapping_report.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("classification") != "mapped_verified":
                continue
            key = row.get("zotero_attachment_key", "")
            if key and key not in indexed_keys:
                rows.append(row)
    return rows


def _write_filtered_mapping_csv(source: Path, output: Path, rows: list[dict[str, str]]) -> None:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        fieldnames = csv.DictReader(handle).fieldnames or []
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
