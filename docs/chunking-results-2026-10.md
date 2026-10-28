# Chunking results: fixed windows versus structure-based chunks

Measured on 2026-10-28 on the maintainer's library (2,033 records), comparing the pre-#80 fixed
6,000/500-character windows with the #80 structure-based chunker (1,500-character budget, 150
overlap). It is the first local evidence for the design in
[chunking-research-proposal.md](chunking-research-proposal.md). Only aggregate numbers are recorded
here; no question text, quotes, keys or titles.

## Question set

70 private questions (48 dev, 22 held out), drafted by an LLM from randomly sampled records,
reviewed by an independent agent, not yet reviewed by the library's owner:

| Type | n | Evidence |
|---|---:|---|
| Paraphrase, rare term, definition, numeric and similar | 26 | One sentence of 120–320 characters |
| Long passage | 13 | A whole paragraph of 1,700–3,200 characters |
| Table | 12 | The whole Markdown table |
| Cross-section | 9 | A result using an abbreviation defined far earlier (definition credited at grade 1) |
| Reference trap | 10 | The paper's own passage; another record's reference entry for it is a trap |

The first draft (30 questions, all single sentences picked by cue words such as "significant" or
"defined as") was biased toward small chunks by construction: every evidence span was shorter than
one new chunk. Long, table, cross-section and trap questions were added to balance it.

## Results

### Fixed queries, `all_terms` (the server default)

| | Old | New |
|---|---:|---:|
| Paper recall@20 | 0.29 | 0.19 |
| Span recall, top 20 | 0.26 | 0.13 |
| Span recall, 1,000-token budget | 0.00 | 0.10 |

Over 70% of questions found nothing with either chunker: the queries were phrases, and `all_terms`
requires every word in one chunk. This mainly measures query wording; it is harder to satisfy in a
smaller chunk, so it also tilts toward large chunks. Do not read it as a chunking result.

### Fixed queries, `any_terms`

| | Old | New |
|---|---:|---:|
| Paper recall@20 / MRR | 0.94 / 0.76 | 0.96 / 0.79 |
| Span recall, top 20 | 0.86 | 0.81 |
| Returned tokens for 20 hits | ~26,600 | ~4,650 |
| Span recall, 1,000-token budget | 0.00 | 0.66 |
| Span recall, 2,000-token budget | 0.39 | 0.78 |

At equal top-k the difference is within noise (paired 95% interval for span recall −0.11 to
+0.01; 6 losses, 2 wins). At equal returned-token budgets the new chunker wins clearly (1,000
tokens: +0.66, interval +0.55 to +0.76, 48 wins, 0 losses). Losses: two cross-section, one long
passage, one table, one reference trap, one paraphrase.

### Blind agents choosing their own keywords

Each held-out question was given to a fresh Haiku agent with only a search/read tool over one index
(at most 8 calls), once per index. It chose its own keywords and search mode and recorded up to
three passages. Scoring was mechanical: a hit counts when the answered chunk overlaps the judged
span.

| | Old | New |
|---|---:|---:|
| Judged passage answered | 12 / 22 | 11 / 22 |
| Right paper answered | 17 / 22 | 16 / 22 |
| Judged passage seen in any result | 12 / 22 | 13 / 22 |
| Mean calls | 3.1 | 3.6 |
| Mean characters read | 10,436 | 4,790 |

Passage wins were split 6 (old) to 5 (new); the rest tied. New chunks gave the same outcome for
about half the text read.

## Learnings

1. **Smaller chunks did not cost findability here, and halved what an agent reads.** Every
   measurement that controls for returned text favours the new chunker; at equal top-k it is a tie.
   This supports keeping #80's chunker.
2. **Natural-language queries and `all_terms` don't mix.** A benchmark with fixed phrase queries
   in the default mode measures wording, not retrieval. Either write keyword queries, score in
   `any_terms`, or let an agent formulate the search (the agent test above). Agents fell back to
   `any_terms` on their own after empty `all_terms` results.
3. **One judged span per question is too strict.** Agents found equivalent statements in the same
   paper a few hundred characters away, or the same finding in another paper, and were scored as
   misses. Judge several acceptable spans (grade 1–2 for weaker ones) and check the corpus for other
   papers that answer a question before freezing it.
4. **Duplicate attachments break `citation_key` judgments.** One citation key mapped to three
   attachments with identical text; the benchmark rejects such a key. Use `attachment_key` and
   consider deduplicating the library.
5. **LLM-drafted questions copy the evidence's wording** (mean word overlap about 0.65 before
   rewording) and favour short, cue-word sentences. They are a fast start, not a substitute for the
   owner's own questions; three draft questions were unanswerable by any agent.
6. **Metadata is copied into every chunk's FTS row** (title 8×, citation key 6×), so a paper's title
   words match all its chunks; the new chunker multiplies those rows by about five. `heading_path`,
   `page_start` and `page_end` are stored but not searchable. Agents often searched for section-like
   words ("simulation", "table", "method"), which is a case for #81 and #108.
7. **Tool-restricted agents need enforcement, not instructions.** About a third of the Haiku agents
   made one read-only call to a tool their brief excluded. For a repeatable agent benchmark, give the
   agents a tool set that contains only the search tool.
8. **A JSONL corpus must be split on `\n` only.** `str.splitlines()` also splits on U+2028 and other
   separators that `json.dumps(..., ensure_ascii=False)` leaves inside strings; the benchmark's corpus
   loader did this and could not read the real export.

## Not yet done

- The owner's review of the question set, and questions needing several passages from different
  papers, author/title filters and equations.
- The agent test on all 70 questions, and with the production MCP tools instead of a wrapper.
- Sweeps of the chunk budget and overlap, and parent/neighbour expansion (#108).
