# Search Quality

`benchmarks/retrieval.py` scores search on a question set you write yourself. It answers one
question: *for the papers I know should come back, how high does search rank them?* Any change to
search defaults, ranking, tokenization or chunking should be judged against these numbers, not
against a handful of remembered queries.

## What it measures

Each question has a query and the papers that should be found. The script runs the query through
`search_fts` and ranks the hits **per attachment** (search keeps one best chunk per attachment).

- **recall@k** for a question is the share of its expected papers found in the top k (defaults
  5, 10, 20). Reported as the mean over questions.
- **MRR** is the mean of 1 / rank of the first expected paper found, 0 when none is found.
- Both are reported overall and per question `type`, with the number of questions `n`.
- A **configuration** is one index (`--db`) crossed with one search mode (`--mode`). The first
  configuration is the baseline; every other one lists the questions whose outcome changed
  against it, split into *improved*, *regressed* and *mixed*. A question changed when the rank of
  its first hit, the number of expected papers found, or its recall at any cutoff differs; a lower
  first rank and a higher recall are better, no hit is worst. *Mixed* means some of these got
  better and others worse. Each row lists the cutoffs whose recall changed, so a recall drop is
  never hidden behind an unchanged first-hit rank.

Output is Markdown by default, JSON with `--json`, on stdout only. It contains question ids,
ranks and aggregates, never query text, keys, titles or paths, so it can be pasted into a pull
request without exposing the library.

The script is read-only. It opens indexes through the same read-only connection as the server and
never touches Zotero, the configuration, or any converted file.

## Running it

```powershell
python benchmarks\retrieval.py --questions <elsewhere>\my.questions.json `
  --db <output_root>\index\generations\<generation_id>\index.sqlite
```

Options:

- `--db` may repeat to compare index generations; `--label` names them (default `A`, `B`, ...).
- `--mode` may repeat: `per-question` (default: each question's own `search_mode`, else
  `all_terms`), `all_terms`, `any_terms`, `phrase`.
- `--k 5,10,20` sets the cutoffs. The search limit is the largest k (at most 100).

The current published generation is scored by pointing `--db` at it. To score a change that
needs a rebuilt index, build it into a scratch output root and compare, exactly as
`docs/performance-baselines.md` does for timings:

```powershell
zotero-pdf-text rebuild-index --output-root <scratch> --from-jsonl <current index.jsonl>
python benchmarks\retrieval.py --questions my.questions.json `
  --db <current generation>\index.sqlite --db <scratch generation>\index.sqlite `
  --label current --label candidate --mode per-question
```

Never point `rebuild-index` at the real output root for this.

## Writing a good question set

The file is JSON:

```json
{
  "format": "zotero-fulltext-retrieval-questions",
  "version": 1,
  "synthetic": false,
  "questions": [
    {"id": "q01", "type": "conceptual", "query": "...", "search_mode": "phrase",
     "expected": [{"citation_key": "..."}, {"attachment_key": "..."}], "note": "optional"}
  ]
}
```

`type` (free text) and `search_mode` are optional; questions without a type are grouped as
`untyped`. `note` is for you and is never printed. Each expected entry has exactly one of
`citation_key` or `attachment_key`.

- Aim for **20 to 40 questions**, spread over query types (for example `conceptual`, `author`,
  `exact_phrase`, `citation`) so a change that helps one kind and hurts another shows up.
- **Write the expected papers before you look at search results.** Judgments made after seeing
  the ranking just confirm the ranking.
- Prefer `citation_key` over `attachment_key`: it survives reconversion and covers every
  attachment of the paper, counting once. An attachment key is exact but changes with the file.
- Keep the file **outside the repository**. `*.questions.json` is gitignored and
  `tests/test_no_personal_data.py` fails if one is tracked anywhere but `tests/fixtures/retrieval/`
  or lacks `"synthetic": true`. The fixtures there are the only question sets that belong in git.

## Reading `not_in_index`

An expected paper that is absent from an index still counts as a miss in the metrics, and its
question id is listed under `not_in_index` for that configuration. A nonzero count means the
question tests coverage, not ranking: the paper was never converted, the key is wrong, or the
index is older than the paper. Fix the question or the coverage before drawing conclusions from
that question; when comparing generations, a paper present in one and absent in the other
explains the change, not the ranking. `invalid_queries` lists questions that search rejected.

## Using the numbers

Later search work (tokenization, chunking, filters, semantic discovery) attaches before/after
output of this script, on the same question set, to its pull request. Report the overall table
and the changed-question lists; do not paste queries or keys.

## Passage evaluation and chunking experiments (S1a)

The paper-level numbers above say whether the right *paper* comes back. Choosing chunk sizes,
boundaries, overlap and context assembly also needs to know whether the right *passage* comes
back, and at what cost in returned text. The same script does this in **passage mode**, switched
on by `--chunking`, `--sweep` or `--passages`. Design and rationale:
[chunking-research-proposal.md](chunking-research-proposal.md); issue
[#107](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/107).

### Judgments (private)

Add optional fields to a question in your private `*.questions.json` (same file, same rules: keep
it outside the repository). `expected` may then be omitted; it defaults to the papers named by
`evidence`.

```json
{"id": "q07", "type": "definition_context", "split": "heldout", "query": "...",
 "evidence": [{"citation_key": "...", "quote": "exact text copied from the paper", "grade": 3},
              {"attachment_key": "...", "start_char": 1200, "end_char": 1480, "grade": 1}],
 "traps": [{"citation_key": "...", "quote": "a reference-list entry that matches the words"}],
 "qualifiers": [{"citation_key": "...", "quote": "the condition that must travel with the answer"}]}
```

- `evidence`: the original spans that answer the question. A span is a `quote` (resolved against
  `--corpus`; add `"occurrence": n` when the text repeats) or `start_char`/`end_char` offsets into
  the record text. `grade` is 1 to 3 (default 3) and sets the graded gain in nDCG.
- `traps`: lexically attractive spans that are not evidence (reference lists, repeated
  headings). `trap` is the share of returned passages overlapping one.
- `qualifiers`: spans that must be returned with the evidence (conditions, scope limits).
  `qual` is the share fully contained in the returned text; a miss is a missing qualifier.
- `split`: `dev` (default) or `heldout`. Tune on `dev`; look at `heldout` for decisions. The report
  lists both, and `--split heldout` scores only the held-out questions.
- Aim for 80 to 120 questions covering exact identifiers, rare terms, paraphrases, definitions that
  need earlier context, equations, tables, multi-passage questions, malformed headings,
  cross-section dependencies, author/title filters and reference traps (use `type` for these).
  Write the judgments before looking at results.

`--corpus` is the record text: an `index.jsonl` export (what `rebuild-index --from-jsonl` reads) or
a JSON list of records with `zotero_attachment_key`, `citation_key` and `text`. It is needed for
quotes, for locator validation and for building experimental indexes. It is read-only.

### Experimental indexes

`--chunking strategy:target[:overlap]` (repeatable) builds one scratch index per spec from
`--corpus`; `--sweep sentence` or `--sweep structural` expands to 128/256/384/512/768-token
targets, each with zero and bounded (12.5%) overlap. Strategies: `chars` (the current 6000/500
slicing, in characters), `sentence` (token-budgeted sentence packing) and `structural`
(heading-bounded paragraph, table-row and display-equation packing with a sentence fallback;
a chunk never crosses a heading). Every chunk is a contiguous slice of the original text, nothing
is dropped or truncated, and its hash is `sha256(text[start:end])`.

These indexes are separate derived files (`*.experiment.sqlite`) with their own small schema.
They never replace or modify a generation: the builder refuses to run inside any folder tree that
holds a `current.json` or a `generations` folder, and it replaces only a file it created itself.
The default location is a temporary folder deleted afterwards; `--experiment-dir` keeps them (the
files are gitignored like every `*.sqlite`). The current generation can be scored alongside with
`--db` (read-only) and `--passages`; it returns one best chunk per paper, whereas experimental
indexes rank every chunk.

### What is reported

For each configuration: chunk count, token-size distribution (min, p50, p95, max, mean) and how
many chunks exceed the target, tokenizer name and version, build time, index size, and p50/p95
retrieval latency. Then, at equal top-k (`--k`) and at equal returned-token budgets (`--budget`,
default 500,1000,2000):

- **paper recall@k and MRR**, with papers ranked by their best passage;
- **nDCG**: graded, each evidence span credited once to the first passage overlapping it, so
  repeated hits on the same evidence earn nothing;
- **spanR / charR**: evidence-span recall (a span counts when at least half of it is returned) and
  the share of evidence characters returned; **prec**: the share of returned characters that lie
  inside evidence; **dup**: the share of returned characters already returned by an earlier hit;
- **cited / locOK**: evidence recall counting only passages whose locators verify, and the share of
  locators that verify (in range, and the hash of that slice of the original text equals the
  result's hash; locators are only checked when `--corpus` provides the text);
- **trap**, **qual**, mean returned **tokens** and **hits**.

A budget counts the returned text plus any ancestor or expansion text (`--expand 1` returns each
hit with its neighbouring chunks as extra locators and charges their tokens). It stops at the first
passage that does not fit; a result is never cut to size, so a chunk larger than the budget
returns nothing and shows up as zero recall rather than silently truncated evidence.
`--tokenizer regex` (default, deterministic, pinned as version 1) counts word runs and punctuation;
`--tokenizer tiktoken:<encoding>` uses that model tokenizer if installed.

The first live configuration is the baseline. Every other one gets a **paired comparison** on the
questions judged in both: mean difference of nDCG and span recall per series with a 95% bootstrap
interval over questions (fixed seed, 2000 resamples), wins, losses, and the ids of the regressed
questions. An interval that includes zero is no evidence of a gain; inspect the listed regressions
(exact identifiers, equations, tables, missing qualifiers) before promoting any default, and base
the decision on the `heldout` split.

### Channels

`--channel lexical|semantic|hybrid` (repeatable) shares one ranked-result interface. Hybrid fuses
the ranks of its lexical and semantic inputs with reciprocal-rank fusion (k=60, 50 candidates per
channel). No embedding backend exists yet, so `semantic` and `hybrid` are reported as
`unavailable` with a reason and never scored; this is benchmark support, not a retrieval feature.
Parent/path assembly (S4a) and chunkset embeddings (S6a) plug in later as further retrievers that
return several original locators per result; the interface already charges their extra tokens.

```powershell
python benchmarks\retrieval.py --questions <elsewhere>\my.questions.json `
  --corpus <output_root>\index\index.jsonl --chunking chars:6000:500 `
  --sweep sentence --sweep structural --budget 500,1000,2000 --split heldout
```

Output stays aggregate: ids, ranks, metrics and configuration facts, never query text, quotes,
keys, titles, record text or paths. The synthetic fixtures
(`tests/fixtures/retrieval/synthetic_passages*.json`) are the only passage question set that
belongs in git.
