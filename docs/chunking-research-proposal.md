# Chunking for lexical and semantic retrieval

Research date: 2026-09-30; updated 2026-10-01 with POMA chunksets and 2026-11-05 with open-source implementation comparisons. This is a proposal, not an implemented change or a benchmark of this library. The review covers primary research through July 2026, plus the original rank-fusion paper and engineering references. It is a targeted review rather than an exhaustive systematic review.

## Recommendation

Use deterministic, structure-aware, size-bounded source chunks shared by lexical and semantic indexes. Make **POMA-style chunksets and deduplicated context assembly the first structural retrieval experiment**: retain small source units and their parent relationships, embed bounded units together with ancestor context, and assemble matching paths into readable evidence. Keep independent leaf embeddings as the comparison baseline. Evaluate late chunking on the same source spans as a separate embedding experiment.

Do not make sentence-similarity splitting, LLM-generated boundaries, or proposition rewriting the default. Their extra cost and complexity need evidence from this library. A semantic search index does not require a semantic boundary detector.

## What the evidence says

| Method | Primary evidence | Implication for this project |
|---|---|---|
| Fixed-size versus semantic boundaries | Qu et al. (2025), [Is Semantic Chunking Worth the Computational Cost?](https://aclanthology.org/2025.findings-naacl.114/), NAACL Findings, DOI `10.18653/v1/2025.findings-naacl.114`. Across document retrieval, evidence retrieval and answer generation, semantic breakpoints/clustering provide inconsistent gains. Some retrieval datasets stitch unrelated short documents together. | Keep a cheap, sentence-respecting baseline. This study does not establish that every semantic or hierarchical method fails. |
| Late chunking | Günther et al. (2024; revised July 2025), [Late Chunking](https://arxiv.org/abs/2409.04701), DOI `10.48550/arXiv.2409.04701`. Encode tokens with document context, then pool tokens within each chunk. Boundary selection remains a separate choice. | A promising experiment without generated source text. Requires access to token embeddings, compatible pooling and bounded long-context windows. Avoid silent document truncation. |
| Trained contextual embeddings | Conti et al. (2025), [Context is Gold to find the Gold Passage](https://aclanthology.org/2025.emnlp-main.1150/), EMNLP, DOI `10.18653/v1/2025.emnlp-main.1150`. ConTEB and InSeNT evaluate/train contextual embeddings using negatives from within the same document. Naive late chunking can dilute exact technical matches: COVID-QA nDCG@10 falls from 61.7 to 40.0 for their ModernBERT comparison. | Contextualization is model- and task-dependent. Retain original-text lexical search and compare independently embedded chunks with late and contextually trained embeddings. |
| Hierarchical chunking | Lu et al. (2026), [HiChunk](https://aclanthology.org/2026.acl-long.1372/), ACL, DOI `10.18653/v1/2026.acl-long.1372`. Hierarchical segmentation and Auto-Merge retrieval target questions needing multiple evidence passages. Comparisons use equal retrieval-token budgets; the strongest boundary predictor requires fine-tuning. | Store parent/child relationships now. First use extracted headings and paragraphs; do not assume that a simple hierarchy reproduces their trained system's gains. |
| Proposition retrieval | Chen et al. (2024), [Dense X Retrieval: What Retrieval Granularity Should We Use?](https://aclanthology.org/2024.emnlp-main.845/), EMNLP, DOI `10.18653/v1/2024.emnlp-main.845`. Atomic, self-contained propositions improve dense retrieval and QA in their experiments. The corpus is English Wikipedia. | Useful later for fact discovery, but generated propositions can change scientific qualifiers, equations and conditions. They must remain derived discovery records pointing to original source spans. |
| LLM boundary detection | Duarte et al. (2024), [LumberChunker: Long-Form Narrative Document Segmentation](https://aclanthology.org/2024.findings-emnlp.377/), EMNLP Findings, DOI `10.18653/v1/2024.findings-emnlp.377`. Iterative LLM topic-shift detection improves retrieval on GutenQA, built from narrative books. | Consider for long, weakly structured texts if inexpensive boundaries fail. Narrative-book results do not establish superiority for scientific PDFs. |
| Chunk-size selection | Bhat et al. (2025), [Rethinking Chunk Size For Long-Document Retrieval](https://arxiv.org/abs/2505.21700), preprint, DOI `10.48550/arXiv.2505.21700`. Preferred sizes vary with model and task: small chunks favor short facts; larger chunks can favor contextual answers. Several datasets concatenate documents and evaluate answer-string presence. | Treat size as a measured parameter. There is no evidence-backed universal 512-token optimum. |
| Adaptive method selection | de Moura Júnior et al. (2026), [Adaptive Chunking: Optimizing Chunking-Method Selection for RAG](https://arxiv.org/abs/2603.25333), preprint, DOI `10.48550/arXiv.2603.25333`. Document-specific scoring considers cohesion, references and block integrity. Evaluation is small and depends on automated judgment. | Adopt the practical block-integrity principle. Defer automatic strategy selection until a larger local evaluation justifies its preprocessing cost. |
| Academic-text evaluation | Kreileder et al. (2026), [Evaluating Chunking Strategies for Retrieval-Augmented Generation on Academic Texts](https://arxiv.org/abs/2607.01852), preprint, DOI `10.48550/arXiv.2607.01852`. Semantic clustering does not outperform simple alternatives, but the study has only 13 theses, ten queries and substantial metric failures. | Relevant caution, weak general evidence. Human evidence annotations should anchor the local benchmark. |

Two complementary references inform retrieval, rather than boundary selection:

- Cormack, Clarke, and Büttcher (2009), [Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods](https://doi.org/10.1145/1571941.1572114), SIGIR, DOI `10.1145/1571941.1572114`; [author-hosted full text](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf). Rank fusion combines rankings without comparing incompatible score scales. Use it as a baseline, not a claim of universally optimal hybrid weighting.
- Anthropic (2024), [Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval). Generated chunk-specific context is prepended for both embeddings and BM25. This vendor evaluation supports testing contextualization, but its reported gains are not predictions for this library. Deterministic title/heading context is a cheaper starting proposal and is not equivalent to their method.

### POMA: engineering approach and evidence

POMA changes the retrieval unit to a **chunkset**: leaf sentences together with their root-to-leaf structural lineage. Its full pipeline infers explicit and implicit hierarchy. At query time it assembles matching paths into a deduplicated per-document "cheatsheet." This provides context at retrieval and assembly time, beyond selecting better boundaries. See the supplied [chunking guide](https://www.poma-ai.com/docs/rag-chunking-strategies-text-splitters) and the focused [chunkset documentation](https://www.poma-ai.com/docs/learn/chunking/chunksets).

The public [POMA-OfficeQA benchmark](https://github.com/poma-ai/poma-officeqa) covers 20 table-lookup questions across 14 Treasury Bulletins, using identical embeddings across three ingestion/chunking pipelines. It measures the minimum retrieved context needed to recover all evidence and reports 77% fewer tokens for POMA. This is vendor-produced, narrow evidence; differing ingestion pipelines also prevent attributing the full gain to chunkset assembly alone. It is not a prediction of scientific-paper retrieval accuracy or downstream answer quality.

The MIT-licensed [poma-primecut-nano package](https://pypi.org/project/poma-primecut-nano/) offers Markdown chunking, ancestor-linked chunksets and context assembly, leaving search and embeddings to the caller. It depends on structured Markdown and does not include the full product's ingestion layer. Its documented chunk API supplies content, depth and parent IDs but no source offsets; citation compatibility needs verification before adoption. Evaluate it as a prototype option rather than introducing a dependency immediately.

## Open-source implementation review (2026-11-05)

These are source inspections at pinned revisions, not comparative retrieval benchmarks. Defaults
and algorithms below are engineering references, not evidence that they outperform this project.

| Project and inspected source | Observed implementation | Transfer and limitation |
|---|---|---|
| [Open Notebook chunking](https://github.com/lfnovo/open-notebook/blob/5e285b7a2e435895c4ee99ad7e4b8d2af64c3239/open_notebook/utils/chunking.py), [ingestion](https://github.com/lfnovo/open-notebook/blob/5e285b7a2e435895c4ee99ad7e4b8d2af64c3239/open_notebook/graphs/source.py) | Markdown headings followed by recursive token-counted splitting; defaults to 400 tokens and 15% overlap. Extraction delegates to content-core, with document-engine options including Docling. | Test heading-first splitting with a model-tokenizer size check. Do not copy its small-chunk filtering: short equations and qualifiers must survive in the authoritative source layer. |
| [NotebookLlama preprocessing](https://github.com/meta-llama/llama-cookbook/blob/2f22a9eb030f92d0e99227e57e9a1123af1f9532/end-to-end-use-cases/NotebookLlama/Step-1%20PDF-Pre-Processing-Logic.ipynb) | PyPDF2 extraction capped at 100,000 characters, roughly 1,000-character word-bounded pieces, then LLM rewriting for podcast preparation. | Not an evidence-preserving ingestion template. Do not introduce document truncation or generative rewriting into authoritative passages. |
| [AnythingLLM PDF ingestion](https://github.com/Mintplex-Labs/anything-llm/blob/cbaf3357c7d9810bfa1f91f075cae7f533554f80/collector/processSingleFile/convert/asPDF/index.js), [splitter](https://github.com/Mintplex-Labs/anything-llm/blob/cbaf3357c7d9810bfa1f91f075cae7f533554f80/server/utils/TextSplitter/index.js) | Page extraction, OCR when no documents are returned, then joined text. Recursive character splitting with optional metadata headers appended after splitting. | Test compact title/heading prefixes as derived embedding inputs; count them inside the model budget. Its headers can exceed the configured chunk size, so the sizing logic is not a model-token hard limit. |
| [Open WebUI retrieval](https://github.com/open-webui/open-webui/blob/c38885a1a737da4968f157092f304f3b5481d0bf/backend/open_webui/routers/retrieval.py), [PDF loader](https://github.com/open-webui/open-webui/blob/c38885a1a737da4968f157092f304f3b5481d0bf/backend/open_webui/retrieval/loaders/pdf.py) | Page/single-document PDF loading and optional image OCR; heading splitting plus character, tiktoken or Transformers-tokenizer splitting; embedding engine/model metadata. | Match token counting to the embedding model. Splitter-local start indices after preprocessing do not establish original-document provenance; retain our source mappings. |
| [Khoj PDF entries](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/processor/content/pdf/pdf_to_entries.py), [entry processing](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/processor/content/text_to_entries.py) | PyMuPDF page extraction, recursive splitting, content hashes to identify entries needing embeddings. Its 256-token split uses whitespace-separated words as the length function. | Reuse unchanged embedding inputs, but key reuse by exact input plus model/revision, tokenizer and preprocessing/template identity. Identical text in distinct attachments keeps distinct locators. Word counts are not model-token budgets. |
| [RAGFlow table splitting](https://github.com/infiniflow/ragflow/blob/8e3be03a2b9ff2a1a26e817977c9e7a95245e183/internal/ingestion/component/chunker/table_split.go), [media context](https://github.com/infiniflow/ragflow/blob/8e3be03a2b9ff2a1a26e817977c9e7a95245e183/internal/ingestion/component/chunker/title_media_context.go) | Structured tables split at complete rows and merged-cell boundaries; headers/captions repeat. Media context selects bounded neighboring sentences. | Prioritize Markdown row-aware splitting and caption/definition context. Its table data-row budget excludes repeated headers/captions and allows oversized atomic rows/spans: enforce our complete-input budget explicitly and report blocks requiring a larger view. |
| [Verba semantic chunker](https://github.com/weaviate/Verba/blob/70b6cfb8ef59c9f178ffccfaf3aadcf737757a18/goldenverba/components/chunking/SemanticChunker.py), [Markdown chunker](https://github.com/weaviate/Verba/blob/70b6cfb8ef59c9f178ffccfaf3aadcf737757a18/goldenverba/components/chunking/MarkdownChunker.py) | Embeds neighboring-sentence windows, splits at cosine-distance percentiles or a maximum sentence count; also offers heading splitting. Semantic output rejoins sentences; Markdown output has no implemented source offsets. | Keep semantic breakpoints as an experimental challenger. Select boundaries on original source spans rather than copying reconstructed-text offsets. |

### Implementation priorities and ownership

1. **S4-structure source blocks, tracked with the remaining work in #108:** preserve equations, captions
   and Markdown table rows before generic sentence splitting. Retain original offsets/hashes;
   repeated headers are derived context. #80's shipped character chunker remains the baseline.
2. **S4a / #108:** compare bounded caption/definition/neighbor context and deduplicated hierarchy
   assembly against leaf retrieval. This targets the table and cross-section losses in the
   [first local comparison](chunking-results-2026-10.md), without assuming an external project's gains.
3. **S1a / #107:** measure row/header/caption integrity, tiny meaningful blocks, malformed tables,
   oversized rows/equations, repeated text and exact locator round-trips. Charge all prefixes,
   repeated headers and assembly text to their respective input/returned-token budgets.
4. **S6 / #1 and S6a / #109:** add identity-aware embedding-input reuse, then compare leaf-only
   versus title/heading-prefixed inputs on the same spans. Semantic breakpoints stay a separate
   boundary experiment, not a prerequisite for semantic retrieval.

### Dependency decision

Do not add RAGFlow as a mandatory Python dependency. The inspected implementation places these
algorithms in Go `internal/` packages, and its [deployment instructions](https://github.com/infiniflow/ragflow/blob/8e3be03a2b9ff2a1a26e817977c9e7a95245e183/README.md)
describe a service deployment with separate storage/infrastructure. That is a different
operational contract from this project's Python CLI and local SQLite sidecar. Source files
under `internal/` are not a supported reusable Python chunking library.

Reuse focused libraries where they remove substantial work: evaluate model tokenizers for
accurate budgets, a Markdown parser for block recognition, and a document converter such as
Docling only if reviewed complex-PDF failures justify a conversion experiment. These are
evaluation candidates, not verified drop-in replacements or newly approved dependencies.
Our source-offset/hash mapping, attachment identity and atomic publication remain project code.

If complex layout extraction proves the bottleneck, benchmark an explicitly configured local
RAGFlow service as an optional conversion adapter against the existing extractor, using the
reviewed public corpus first. Verify API/version stability, page/source mapping, reading order,
table/equation fidelity, resource cost and platform support before implementation. Import only
derived results; never require it for lexical search or silently send PDFs to a remote service.
Keep conversion comparisons separate from chunking comparisons. Any copied implementation must
retain applicable license notices; RAGFlow's inspected files carry Apache-2.0 notices.

## Proposed design

### Extraction boundary (decision 2026-11-05)

[Extraction workstream](../ROADMAP.md#extraction-and-reference-quality)
owns PDF-to-Markdown/reference fidelity (see the [extraction-quality plan](extraction-quality-plan.md)), the internal validation artifact (#166), reviewed corpus
labels (#115) and cleanup candidates (#111). Evidence-ledger integration is deferred. The pilot
uses one internal answer key, with compatible checks exported to an isolated external evaluator
where useful. Human-reviewed expectations remain separate from automatic/model proposals.

This document and #107 own downstream Markdown-to-chunk coverage and retrieval. Freeze and hash
extraction outputs for each chunking comparison. A tokenizer measures the full embedding input;
start with shared source boundaries for lexical and embedding views, allowing separately mapped
subspans only when oversized blocks or measured requirements justify them. Paper metadata and
reference graph edges are separate retrieval channels; do not prepend entire bibliographies to
passage embeddings. Establish reviewed extraction quality before promoting production boundaries.

### 1. Source-preserving chunks

The production implementation in `src/zotero_pdf_text/fts.py` uses `structure-v1`: 1,500-character chunks with up to 150 characters of overlap, following headings, paragraphs and sentence boundaries. Page maps and heading paths shipped in v0.15.0. It does not yet provide explicit table/equation block hierarchy or production model-token budgets. The old 6,000/500-character windows remain a benchmark baseline. FTS5 uses `unicode61` and BM25.

For S4-structure, parse Markdown into ordered blocks with exact character offsets. Track heading ancestry, section type and available page mappings. Group consecutive paragraphs into a target of **384 tokens**, with an initial **512-token ceiling** for ordinary prose. These are proposed starting values, not established optima. Count tokens with a pinned tokenizer and reserve the embedding model's required prefix budget.

Split oversized prose at sentence boundaries, then use token boundaries only for an individual oversized sentence. Keep equations with nearby explanatory prose where the budget permits. Treat tables and figure captions as explicit blocks: split oversized tables by rows with repeated headers in a derived retrieval view, and retain the original table span. Never silently drop or truncate a block.

Start with no overlap between complete paragraphs. Compare one-sentence overlap, bounded to roughly 10–15% of the target, when a long paragraph must split. Overlap can inflate storage and duplicate search hits; it should earn its cost in evaluation.

Keep source text, character offsets and hashes authoritative. Title/heading prefixes, repeated table headers, summaries and propositions belong in separately identified retrieval fields. They are not quoted evidence and must not alter the source span's hash.

### 2. Two retrieval levels

Maintain paper metadata and passage retrieval separately. Title, abstract and tags support broad paper discovery; body chunks support evidence lookup. References and appendices receive explicit section labels, with a body-focused default and an option to search citations. Missing abstracts or extraction failures must remain visible.

Preserve parent sections and neighboring chunk identifiers without embedding every parent separately. Keep the storage unit distinct from the retrieval unit: source blocks remain individually addressable; a chunkset references the leaf block IDs plus selected ancestor IDs. The proposed initial hierarchy uses extracted headings and explicit blocks. It does not reproduce POMA's inferred sentence hierarchy or HiChunk's trained boundary predictor.

For example, a retrieved equation might carry `paper title → methods → prior specification → equation and explanatory paragraph`. When several hits share the same section, assemble their paths once in document order, retaining useful ancestor text and marking omitted passages with explicit gap markers. Expand neighboring paragraphs only when needed, under a returned-token budget. This assembled context is a derived view over original source text, not a generated summary.

**Citation contract:** paths can join non-adjacent text. Each assembled result must retain a list of original source locators and hashes, with a mapping from displayed evidence to its source spans. Do not assign one continuous character range or one existing chunk hash to the whole cheatsheet. Shared headings may be deduplicated for display without discarding the locators of individual evidence blocks.

Heading lineage cannot recover every dependency: definitions, assumptions and experimental conditions may occur elsewhere in a paper. Include these cases in evaluation and use additional passage retrieval where hierarchy alone is insufficient. For malformed or flat Markdown, retain the bounded sentence/paragraph baseline and expose hierarchy quality rather than inventing structure.

### 3. Hybrid search

Index original chunk bodies lexically. For semantic retrieval, compare independent leaf embeddings with chunkset embeddings composed from leaf content and selected ancestor context. Both representations map back to the same authoritative source blocks. For lexical search, keep metadata and headings in separate fields so repeated ancestors cannot masquerade as body matches. Preserve explicit phrase/all-term search semantics and metadata filters.

Bound the complete embedding input, including ancestors, within the model's token limit. Prefer compact title/heading context initially; measure richer ancestor prose as a separate variant. Never silently truncate evidence or assume that every root-to-leaf path fits. Long ancestor content can dilute leaf relevance, so evaluate leaf-only versus chunkset similarity and an optional blend before choosing a ranking default.

For a new hybrid discovery mode, run lexical and vector retrieval separately, map chunkset hits to their leaf evidence candidates, then fuse ranks at that common level using RRF. Start with 50 candidates per channel and `k=60` as experimental settings. Deduplicate by source span, optionally rerank the fused candidates, and only then assemble paths and expand context. Group paper results with a best-passage baseline so long papers do not win merely by producing more chunks. Report which channels matched.

Keep the embedding artifact optional and independently publishable. Bind it to source-generation hashes, chunking version, tokenizer and model revision. If it is unavailable or stale, serve lexical search with an explicit status. Reuse the project's atomic-generation publication and stale-locator behavior.

### 4. Separate contextual embedding experiment

Compare late chunking with independent embeddings using identical model weights, boundaries and source spans wherever possible. Encode a whole paper only when it fits; otherwise use explicit context windows with overlap and a deterministic ownership rule for each pooled chunk. Test contextual models trained for passage discrimination separately, since changing both model and embedding order confounds attribution.

Generated contextual prefixes and propositions are later challengers. Store model/prompt versions and source links, measure factual distortions, and keep derived text outside the original-body lexical index initially.

## Evaluation and delivery order

Follow S1 before promoting defaults, then S3/S4 for metadata, structural source spans, parent links and source-preserving assembly. In S6, compare independent leaf embeddings with POMA-style chunkset embeddings and hybrid retrieval. Test late embeddings separately afterward, keeping the structural assembly fixed to distinguish the gains. The same spans and hierarchy can support these retrieval views without another source re-chunk. Re-chunking needs an index rebuild; page provenance needs reconversion where the existing extraction lacks it.

Build a private set of roughly 80–120 questions covering exact identifiers, rare scientific terms, paraphrases, definitions requiring earlier context, equations, tables, multi-passage questions, author/title filters and reference-list traps. Annotate relevant papers and original evidence spans; keep a held-out split.

Compare the existing character slices, token-budgeted sentence-respecting chunks, structure-aware chunks, parent expansion, POMA-style chunksets with deduplicated assembly, semantic breakpoints and late embeddings. Ablate chunkset embeddings and assembly independently: an assembly improvement must not be credited to better retrieval. Sweep 128/256/384/512/768-token targets and zero versus bounded overlap. Control actual chunk-size distributions, embedding weights, candidate budget and reranking. Test lexical-only, semantic-only and hybrid channels independently.

Measure paper recall@k/MRR, graded passage nDCG@k, evidence-span recall and precision, duplicate rate, source-locator validity, build time, index size and p50/p95 query latency. Compare evidence retrieval at equal returned-token budgets as well as equal top-k; include ancestor and expansion tokens and distinguish exact-match queries from conceptual ones. For chunkset assembly, measure token savings at matched evidence recall, citation coverage and missing qualifiers; include bad-heading and cross-section-dependency cases. Use paired uncertainty estimates and inspect regressions. Promote a method only when held-out gains justify its cost and important exact-match behavior remains acceptable.

No retrieval implementation or existing index was changed for this research.

First local measurements after #80: [chunking-results-2026-10.md](chunking-results-2026-10.md).

## Delivery and evaluation contract (2026-11-05)

The design and research rationale above support this delivery sequence. Delivery order is
S1a evaluation → S4-structure source hierarchy → S4a context assembly → S6a chunkset embeddings/hybrid
retrieval → S6b late embeddings; S6 paper discovery can proceed independently using the shipped S3 metadata.

S1a already extends the lexical runner with a common ranked-result interface for lexical,
semantic and hybrid retrieval, using the same questions and paper/span judgments. Semantic runs
remain unavailable until an embedding backend exists; benchmark support does not imply a shipped
semantic retrieval implementation.

Evaluate pre-embedding preprocessing in two stages. Before any embedding call, measure source
coverage and dropped/duplicated text, offset/hash round-trips, heading/parent correctness, section
classification, equation/table/caption integrity, chunk-size distributions and complete embedding
input token budgets (including prefixes/ancestors). Use synthetic fixtures with known structure
and reviewed private examples; expose failures rather than silently truncating input. Then compare
each preprocessing variant through lexical, semantic and hybrid retrieval with source documents,
questions, embedding model and ranking settings held fixed. Record extraction/preprocessing,
chunking, tokenizer and model versions so retrieval gains can be attributed to the right stage.
S3a ([#111](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/111)) owns the cleanup
implementations and source mappings in the extraction workstream; E1/E2 own extraction fidelity checks and S1a owns downstream retrieval evaluation. Evaluate candidate transforms before changing the shipped structural defaults; page maps
are available from v0.15.0 conversions. Header/footer removal and indexing normalization were
measured and left unchanged (see [conversion decisions](conversion-text-decisions.md)).

The remaining S4 hierarchy work starts with extracted headings and explicit Markdown blocks, with a bounded sentence/paragraph
fallback for flat or malformed Markdown. Preserve equations, captions and table source spans.
A pinned tokenizer measures the proposed 384-token target and 512-token ordinary-prose ceiling;
these are starting candidates, not chosen defaults. S1a sweeps 128/256/384/512/768-token targets,
zero versus bounded overlap, and legacy fixed windows and the production structural chunker versus token-bounded sentence and structural boundaries.
Semantic breakpoints remain an experimental challenger; generated prefixes/propositions and
trained boundary predictors are deferred until simpler methods justify their cost.

Evaluate on private held-out questions with original evidence annotations. Alongside paper
recall/MRR, record passage nDCG, evidence-span recall/precision, duplicate rate, locator validity,
build/storage cost and p50/p95 latency. Compare equal top-k and equal returned-token budgets,
including ancestor and expansion text. Ablate embeddings and assembly independently, report
paired uncertainty, and inspect exact-match regressions, missing qualifiers, bad headings and
cross-section dependencies before promoting any default.

Source blocks and hashes remain authoritative. Assembled non-contiguous context carries multiple
original locators and a display-to-source mapping, never one fabricated continuous range or chunk
hash. Repeated headings/table headers and embedding prefixes are identified as derived retrieval
content. The optional semantic index records source generation, chunking/tokenizer versions and
model identity; lexical search remains available when it is missing or stale.
