# Markdown and reference extraction quality

Decision record, 2026-11-05. Implementation is planned under E1–E4 in the
[active roadmap](../ROADMAP.md#extraction-and-reference-quality). Existing page maps, heuristic
quality scores and synthetic OCR checks are implemented; the reviewed artifact benchmark is not.

### Internal artifact and evaluation contract

Use a small internal JSON Schema, not evidence-ledger-schema or evidence-ledger-mcp now.
Keep reviewed expectations, automatically proposed observations, and run/results records separate.
Record PDF hash/title/physical page count; page dimensions and render identity; stable block IDs,
types, bounding boxes with explicit units/origin, reading order, meaningful body start/end anchors,
text/table cells, equations, graphics and caption links. Distinguish physical pages from printed
labels. Record annotation tool/model/version, human review, uncertainty and run configurations,
input/output hashes, metric versions, failures/timeouts, latency and resource costs.

Reference expectations use the same envelope: raw entry, ordinal/page, reviewed bibliographic
fields and citation-marker links. Score segmentation, field parsing, printed identifier recovery,
and reference-to-library resolution separately; missing source DOIs are not parsing failures.
Strict checks preserve signs, decimals, operators, case, exact spans and hashes; tolerant checks
are separate. A normalized quotation hash cannot establish scientific transcription fidelity.

Start with 20 diverse public-corpus pages and a few complete papers for cross-page/reference
behavior. Establish expectations by PDF inspection. Include injected missing signs, swapped table
values, omitted text and reordered columns to verify evaluator sensitivity. Keep the existing
synthetic corpus for cheap deterministic CI; real PDFs/annotations follow corpus rights rules.

Evaluate [olmOCR-bench](https://github.com/allenai/olmocr/blob/main/olmocr/bench/README.md)
first in a pinned isolated environment, exporting compatible presence/order/table/math checks
from the internal artifact. Do not duplicate answer keys or adopt its OCR model merely to use its
scorer. Its tolerant page-level checks do not verify exact location, our hashes or chunk coverage;
retain those internally. Consider [OmniDocBench](https://github.com/opendatalab/OmniDocBench#evaluation)
later for richer table/formula metrics; its full formula evaluation adds system dependencies.
Neither evaluator has been installed or validated here, so adapter feasibility is an E1 deliverable.

A cheap visual model may compare page images with corresponding Markdown and propose localized
omission/addition/order/table/equation/caption findings or abstain. Calibrate false passes and
false alarms against human labels and injected defects; pin prompt/model/version and record cost.
Model grades are triage, never ground truth. No model provider is selected and no uploads authorized.

### Reuse and dependency decisions

Benchmark current pymupdf4llm plus existing opt-in crop OCR first. Compare Docling as the first
alternative extractor; OpenDataLoader PDF is a second candidate with a Java runtime and distinct
hybrid path. Keep structured output/provenance alongside Markdown. Selective Unicode repairs must
preserve original mappings; do not add automatic generative rewriting or blanket dehyphenation.
No mandatory RAGFlow service or return of Marker's removed dependency tree. Optional local services
remain separate from ordinary conversion/search and require measured benefit before adoption.

For Markdown structure, evaluate markdown-it-py with built-in tables and selected mdit-py-plugins
math rules. Its block line ranges need verified conversion to original character spans. Use the
chosen embedding model's tokenizer (Hugging Face tokenizers where compatible; tiktoken for matching
encodings), not word counts, for complete input budgets. Tokenization measures size; it does not
require independent lexical and embedding source boundaries. Tree-sitter is a later option if
finer ranges justify byte-to-character mapping and packaging complexity.

Manuscript Verification System already supplies GROBID/TEI integration, raw-reference preservation,
citation-marker links, deterministic fallback ideas and separate parsing/DOI metrics. Inspect its
current provenance/license constraints before code reuse; do not copy private manuscripts, results
or paths here. Its recorded limited-domain comparison favored GROBID over the tested ensemble,
not a universal superiority claim over the deterministic parser. Prefer an explicitly configured
GROBID service on the embedding machine, cache by PDF hash plus service/model/options identity,
and retain engine/error provenance. Deterministic fallback remains a candidate, not an ensemble
requirement. GROBID TEI paragraph offsets are not Markdown offsets: align and verify before making
passage locators; ambiguous links remain unresolved. Core FTS works without GROBID or embeddings.

### Order and cross-track boundaries

E1 schema/pilot plus E2 reviewed baseline precede extraction-default changes. Compare extractor
settings, engine changes and cleanup separately. Then freeze/pin the resulting Markdown for S1a
boundary/context sweeps. Chunker prototyping can proceed in parallel; promoting production defaults
requires a reviewed extraction baseline and downstream held-out retrieval evidence.

Store authors/year/venue/DOI as exact metadata/filter fields. Paper embeddings start from
title/abstract/keywords; passage title/heading prefixes are ablations. Reference links belong to
S7's explicit graph, not wholesale bibliography prefixes on every passage. Distinguish shared
authorship, shared references (bibliographic coupling), and being cited together (co-citation);
future graph-assisted ranking must be separately measured and return verified source links.
