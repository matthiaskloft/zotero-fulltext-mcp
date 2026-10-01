# Chunking for lexical and semantic retrieval

Research date: 2026-09-30; updated 2026-10-01 with POMA chunksets. This is a proposal, not an implemented change or a benchmark of this library. The review covers primary research through July 2026, plus the original rank-fusion paper and engineering references. It is a targeted review rather than an exhaustive systematic review.

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

## Proposed design

### 1. Source-preserving chunks

The current implementation in `src/zotero_pdf_text/fts.py` slices at 6,000 characters with 500-character overlap. It strips surrounding whitespace but does not seek headings, paragraph endings or sentence boundaries. FTS5 uses `unicode61` and BM25. The roadmap already plans structural chunks and optional semantic discovery.

For S4, parse Markdown into ordered blocks with exact character offsets. Track heading ancestry, section type and available page mappings. Group consecutive paragraphs into a target of **384 tokens**, with an initial **512-token ceiling** for ordinary prose. These are proposed starting values, not established optima. Count tokens with a pinned tokenizer and reserve the embedding model's required prefix budget.

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
