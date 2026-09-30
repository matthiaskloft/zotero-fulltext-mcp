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
  against it, split into *improved* and *regressed*. An outcome is the rank of the first hit and
  the number of expected papers found; a lower first rank is better, no hit is worst.

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
