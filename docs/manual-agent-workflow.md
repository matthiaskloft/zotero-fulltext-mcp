# Live independent-agent acquisition test

This first version uses a manually prompted independent agent. The runner captures real CLI
and MCP calls; a deterministic verifier checks seven steps against live Zotero and the index.
No LLM judge or HTML report is involved. The task has been exercised by a fresh GPT-6 Luna
agent. Agent dispatch is currently manual; preparation, tool recording, snapshots and grading
are automated. Ollama automation is not implemented yet; a future runner can invoke the same
tools and verifier.

## Prerequisites and scope

Use a Linux Docker engine, internet access during the image build and article acquisition,
and a checkout of this repository. The image pins Zotero, its plugins and Python dependencies;
see [live-zotero-test.md](live-zotero-test.md) for isolation and platform details. No Zotero
account or private library is needed. This test uses real Zotero and real publisher/DOI services;
their availability is not deterministic even though the grading rules are deterministic.

The agent uses the existing write CLI for Zotero acquisition and the read-only full-text MCP
for search/retrieval. It does not add live metadata tools to this project's MCP server. Only
tests requiring Zotero run in the container; grader unit tests run on the host.

## Prepare a fresh run

The portable helper `python tools/dev_tests.py agent-start` builds, starts, waits for readiness
and prepares a fresh environment; see [dev-test-setup.md](dev-test-setup.md). Alternatively,
start a fresh isolated container with networking for DOI metadata and the public PDF:

```console
docker build --platform linux/amd64 -f containers/live-zotero/Dockerfile -t zotero-live-test .
docker run --platform linux/amd64 --name zotero-agent-task --init --shm-size 256m zotero-live-test serve
```

In another terminal, prepare a baseline and obtain the task prompt:

```console
docker exec zotero-agent-task python containers/live-zotero/agent_workflow.py prepare
```

Wait for the startup log to say `Ready: Zotero` before preparing. `prepare` refuses a reused
baseline, an existing target item, stale target index data, or an unexpected Zotero data directory.
Use a fresh container name per run. Do not use a personal config or mount a host Zotero folder.

## Prompt the agent

Start an independent agent with fresh context. For Codex subagents, use `fork_turns="none"`
and the requested model (the observed run used `gpt-6-luna`). Do not fork this development
conversation. Give the agent only the emitted task and these tool descriptions. Keep this document, verifier,
baseline and expectations outside its context. The article is the public PLOS data-sharing paper
from the pinned corpus; its DOI and PDF URL are ordinary task inputs. The runner always uses
the isolated config and internally reads its bridge token.

The initial task asks the agent to find the article, add it and its PDF if absent, make it
searchable, and return a supporting passage with a verifiable citation locator. Do not give
the agent an expected passage, item key, prescribed tool sequence or pass criteria.

Prefix each tool invocation with `docker exec zotero-agent-task python containers/live-zotero/agent_workflow.py`:

- `lookup-doi`: check whether the requested article is already in the library.
- `cli <command> <arguments>`: existing `import-doi`, `check-pdf`, `find-pdf`, `link-pdf` or
  `convert-new` commands. Their ordinary `--help` is supported; config/endpoints cannot be overridden.
- `mcp list`: list the full-text server's normal descriptions and argument schemas.
  Discovery uses the real server's registry and works before the first conversion; subsequent
  tool calls use real MCP stdio transport and require a published index.
- `mcp <tool-name> '<JSON arguments>'`: call a real full-text MCP tool. Quote the JSON for
  the shell, or invoke Docker with a Python argument array.

All actions must pass through this runner sequentially. The agent chooses the commands and
arguments; the operator must not execute missing steps on its behalf. Unrestricted shell or
filesystem access lets an agent inspect evaluator files, so this is context separation, not
a security boundary. A future automated agent should receive only these tools. An agent may
infer that the environment is an evaluation; ignorance cannot be guaranteed.

### Windows JSON arguments

Avoid losing JSON quotation marks through nested PowerShell/native-command parsing. A Python
argument array can carry a tool request unchanged; substitute the agent's selected tool/arguments:

```powershell
@'
import json, subprocess
subprocess.run([
    "docker", "exec", "zotero-agent-task", "python",
    "containers/live-zotero/agent_workflow.py", "mcp", "search_fulltext",
    json.dumps({"query": "data sharing"}),
], check=True)
'@ | .\.venv\Scripts\python.exe
```

## Record completion, repeat, and grade

When the agent completes the task, record the first end state:

```console
docker exec zotero-agent-task python containers/live-zotero/agent_workflow.py checkpoint
```

Give the agent the repeat task printed by that command. Let it act through the same tools,
in the same agent conversation so it retains ordinary task history. The operator runs
`checkpoint` and `verify`, not the agent. Do not repair the library or execute missing agent
steps between phases. Once the agent has finished the repeat task, run verification:

```console
docker exec zotero-agent-task python containers/live-zotero/agent_workflow.py verify
```

The verifier exits **0 only when all seven checks pass**. Failed or insufficient checks return
exit code **1**; startup, tool or infrastructure exceptions also fail the command. Preserve the
exit status in any future runner/CI integration. `PASS` requires both independently observed
state and the relevant successful tool calls. `FAIL` means a state/content condition was not
satisfied. `INSUFFICIENT_EVIDENCE` means the required trace observation was missing.

### Deterministic criteria (verifier version 3)

| Check | Required state | Required agent trace |
| --- | --- | --- |
| `identify_absence` | Baseline contains no matching DOI item | Successful live lookup returns no item before a successful import |
| `add_article` | Exactly one matching item with expected DOI/title | Import returns that item's key |
| `attach_correct_pdf` | Exactly one PDF under that item; file exists, opens, has pages, matches the pinned source SHA-256 | Acquisition call targets that item; automatic acquisition during import is allowed |
| `link_pdf` | PDF resolves within the isolated linked folder using Zotero's relative attachment path | Successful acquisition call; automatic ZotMoov linking is allowed |
| `convert_and_index` | Published index contains the matching parent, attachment and DOI | Successful `convert-new` |
| `retrieve_verified_passage` | Nonempty returned text matches the current indexed chunk and its hash | Earlier search returns the same attachment, chunk index and hash, followed by exact chunk retrieval |
| `avoid_duplicates` | Item and attachment identities/counts unchanged between checkpoint and final snapshot | Repeat live lookup returns the same item, or repeated import reports `already_in_library` |

The verifier follows the published index generation pointer rather than assuming the nominal
database filename exists. It re-fetches the chunk independently and compares it to the recorded
MCP response. Both global and within-paper search are valid. Exact prose is not graded: neither
semantic relevance, the final answer's interpretation, nor final citation formatting is graded
by this tool-use test. Grading does not consult another LLM. Establish rules before a new run;
do not change them or patch evidence to turn an observed failure into a pass.

## Evidence and cleanup

Evidence stays under `/work/agent-task`:

| File | Contents |
| --- | --- |
| `article.json` | Scenario DOI/title/source URL and expected PDF checksum |
| `before.json` | Initial live library and index snapshot |
| `after.json` | First completion snapshot, captured by `checkpoint` |
| `phase.json` | Current initial/repeat phase |
| `trace.json` | Ordered tool names, arguments, successful/failed results, timestamps and phase; acquisition calls also capture post-command state |
| `verdict.json` | Verifier version, overall Boolean result and per-step observations/statuses |

The final repeat snapshot is evaluated during `verify`; it is not stored as a separate snapshot
file. Tool traces do not store the agent's reasoning or entire conversation. Retain the agent's
final response separately when reviewing answer quality. No evidence is automatically uploaded
or committed, and no profile, bridge token or account information should be copied for review.

Stop the foreground container with Ctrl+C, or use `docker stop zotero-agent-task`. Evidence
remains in the stopped container. To copy only the trace and verdict to a chosen local directory
outside this repository, create that destination first and use:

```console
docker cp zotero-agent-task:/work/agent-task/trace.json <local-evidence-directory>/trace.json
docker cp zotero-agent-task:/work/agent-task/verdict.json <local-evidence-directory>/verdict.json
```

Replace the placeholder with an actual directory; it is not a literal shell argument. Capture
the Git revision/working-tree state, image ID, agent model/settings, date and exit status alongside
the files when comparing runs. The runner does not record model settings automatically. Remove
the disposable container only after saving needed evidence: `docker rm zotero-agent-task`.

Source byte changes fail the pinned PDF checksum and require review.
Network/resolver errors are actual failures or missing evidence, never silently mocked.
Preparation refuses an existing target or reused baseline. Use a new disposable container
rather than deleting library items to reset the task.

Host tests check grading with missing evidence, wrong PDFs/parents, stale hashes, duplicates
and missing repeat runs. Container tests check live snapshots and baseline refusal. They validate
the harness, not agent performance. A manual run succeeds only when its own trace passes `verify`.

## Regression commands and CI boundary

```console
uv run pytest -q tests/test_agent_workflow.py
docker compose -f compose.live-zotero.yml run --build --rm zotero-test
```

The first command tests the deterministic grader on the host. The second tests real Zotero
integration, including the harness's live snapshots and refusal of an existing baseline. It
does **not** dispatch an LLM. Regular CI runs host tests; the separate container job runs the
network-isolated synthetic integration suite. The agent/publisher workflow remains manually
triggered and is not claimed as a GitHub CI result. No automatic Ollama agent loop exists yet.

## Observed live run: 2026-10-01

A fresh `gpt-6-luna` agent received only the ordinary task/tool descriptions, with no inherited
conversation or grading criteria. It acquired the public article in a fresh Linux Zotero 9.0.6
container, converted/indexed its PDF, searched and retrieved a hash-verified passage, then
repeated the task by looking up and reusing the existing item and PDF. Verifier version 2,
fixed before this rerun, returned exit code 0 with **all seven checks passing**. The model's
answer was not used to determine the verdict.

This was a local development run from a modified checkout, not a release/CI guarantee. The
supporting checks at that time were 18 host grader tests and three real-Zotero container tests,
all passing. Evidence was retained in the stopped local container `zotero-luna-rerun`. That
container is machine-local and not available to other developers; reproduce with the procedure
above. One successful run does not establish success rates across models or repeated runs.

An earlier version rejected valid source wording and required a second import. Those criteria
were corrected and regression-tested before starting the fresh version-2 run; the earlier
verdict was not retroactively counted as a pass.

Verifier version 3 additionally checks the returned item key on repeat imports, normalizes
CLI `--option=value` arguments, and rejects abbreviated config or endpoint overrides.
The evaluation is scoped to correct, successful CLI/MCP tool use and source integrity.


A further fresh-context Luna run on 2026-10-01 used verifier version 3 after the independent
review fixes. Acquisition, pinned PDF verification, linking, conversion/indexing and duplicate
avoidance passed. Retrieval failed because both recorded chunk calls omitted `chunk_sha256`.
The MCP returned text, but the agent did not bind its retrieval request to the earlier search
locator. The original verdict is retained without overriding the failure. This is a tool-use
failure, not a judgment of the model's prose or interpretation.


A fresh `gpt-6.1-sol` agent subsequently received the same ordinary task and tool descriptions
in another clean container on 2026-10-01. It completed acquisition, linked-PDF verification,
conversion/indexing, search and exact chunk retrieval with the search locator's chunk hash.
On repeat it performed live lookup/PDF checks and reused the existing article and attachment.
The unchanged version-3 verifier returned exit code 0 with all seven checks passing. Its trace
and verdict were exported to ignored local evidence files; the failed Luna verdict remains
preserved separately. No model answer or semantic judge determined either result.
