# Developer test setup

Use a checkout containing the test infrastructure on every developer machine. Nothing is
copied from another researcher's Zotero installation: synthetic fixtures and reviewed corpus
metadata live in Git; profiles, tokens, indexes and downloaded PDFs are created locally.
No Zotero account, host Zotero, personal config or MCP client registration is required.

## Prerequisites

Install Git, Python 3.11 or later, [uv](https://docs.astral.sh/uv/getting-started/installation/),
[Node.js](https://nodejs.org/en/download), and Docker with a running Linux container engine.
Use uv 0.11.28 to match CI and the image. Node runs the offline bridge-script tests; the helper
requires it so these tests do not silently skip. Docker is needed only for live Zotero tests.
On Windows, use [Docker Desktop's Linux engine](https://docs.docker.com/desktop/setup/install/windows-install/).
On macOS use Docker Desktop; on Linux use Docker Engine or Desktop.

The image is Linux AMD64. Apple Silicon and other ARM machines require Docker's AMD64
emulation; the helper explicitly selects that platform. ARM performance and execution have
not been verified here. Internet is required for initial dependency/image downloads, public
PDF downloads and the agent acquisition workflow. The synthetic container tests run offline.

## Fresh checkout

```console
git clone https://github.com/matthiaskloft/zotero-fulltext-mcp.git
cd zotero-fulltext-mcp
python tools/dev_tests.py doctor
python tools/dev_tests.py setup
python tools/dev_tests.py host
python tools/dev_tests.py live
```

On macOS/Linux, use `python3` if `python` is unavailable. On Windows, `py -3` is another
option. `doctor` reports missing executables and an unavailable or non-Linux Docker engine;
it does not install software. `setup` installs Python 3.11 through uv and syncs the local
`.venv` from `uv.lock`, with the MCP and test extras. It does not change the lockfile.
The managed Python runtime and uv cache stay in the ignored `test-results/tool-runtime`
folder; setup does not register Python globally or add executable links to a user bin directory.
Use a fresh clone if an existing `.venv` contains unrelated work.

`host` runs all tests that do not require Zotero outside Docker, with temporary test configs
and no inherited Zotero settings. `live` builds the pinned image, starts a fresh disposable
container with no host data mounts or published ports, runs the real Zotero tests with
`--network none`, saves its JUnit XML and startup log, then removes that container.
Host XML is at `test-results/host.xml`; live diagnostics have a unique subdirectory there.
These files are gitignored. Exit status is nonzero on failed setup or tests; XML can be read
by CI test viewers. There is no HTML report.

The helper works from any working directory when invoked with its absolute path: it resolves
the checkout from its own location and uses argument arrays, including for paths with spaces.
It strips inherited `ZOTERO_*` settings from child commands. Do not mount host Zotero folders
or supply a personal config to the container.

## Public corpus and independent agent

```console
python tools/dev_tests.py corpus
python tools/dev_tests.py agent-start
```

`corpus` downloads the checksum-pinned public PDFs to the ignored host cache; Zotero is not
needed. It includes the large historical scan. For a single paper, use
`uv run python tools/fetch_public_pdf_corpus.py --id data-sharing`.

`agent-start` builds the image and starts a fresh network-enabled isolated container, waits
for Zotero readiness, and prepares the baseline. It prints the ordinary task and a unique
container/tool prefix. Give those to an independent agent and follow
[manual-agent-workflow.md](manual-agent-workflow.md) for checkpoint, repeat, verification and
evidence export. Any agent client that can invoke the supplied tools can be used; a Codex
installation or account is not a requirement for the infrastructure. Dispatch is manual;
there is no automated Ollama runner yet. The container is retained until you stop/remove it
after exporting evidence. A startup failure stops and retains it for diagnosis.

## CI and troubleshooting

Regular CI runs locked host tests on Windows, macOS and Linux. The separate live Zotero
workflow builds the same image on Linux and runs the synthetic tests offline. Neither job
requires an LLM, account or private library. Publisher acquisition and agent runs are optional
network-dependent checks. See [live-zotero-test.md](live-zotero-test.md) for direct Docker and
Compose commands; Compose is optional when using the helper.

If setup reports `uv`/`node` missing, install it and reopen the terminal so PATH updates apply.
If Docker cannot connect, start its engine; on Windows select Linux containers. Git privacy
checks require a checkout owned by the account running the tests; if Git reports
"dubious ownership", clone under that account rather than disabling the privacy checks. A checksum
failure requires reviewing the changed upstream artifact, rather than bypassing validation.
A failed live test leaves its XML/log in `test-results`; check the log for bridge/plugin startup
errors. Re-run `live` to get a fresh environment; it does not reuse prior Zotero state.

The helper, locked Python 3.11 setup in a separate checkout with spaces in its path, and
Linux container tests/startup were exercised from Windows. The fresh environment passed
1,228 host tests; Git ownership blocked seven privacy checks when switching between the
sandbox and desktop accounts. Those privacy checks passed separately under the checkout
owner, without changing global Git trust settings. Host CI is configured for
three operating systems; a new GitHub run and physical macOS/Linux developer setup have not
been verified by this local validation.
