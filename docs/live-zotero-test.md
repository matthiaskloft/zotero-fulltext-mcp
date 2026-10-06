# Live Zotero test

For a live independent LLM agent performing acquisition, conversion and retrieval, see
[manual-agent-workflow.md](manual-agent-workflow.md). It documents the fresh-context prompts,
captured tools, automated seven-step verifier, evidence retention and observed Luna run. The
default container suite below validates integration without dispatching an LLM.

For complete fresh-clone setup, prerequisites and a portable command helper, see
[dev-test-setup.md](dev-test-setup.md).

## Portable container environment (recommended)

Install Docker with a Linux container engine and Docker Compose, then run from a checkout:

```console
docker compose -f compose.live-zotero.yml run --build --rm zotero-test
```

This builds an independent Linux Zotero installation with a virtual display, debug-bridge,
ZotMoov, Python and the locked project dependencies. No host Zotero installation, profile,
config, account or PDF folder is mounted. No ports are published. The Docker build context
uses an allowlist so personal configs and local environments are not sent to the builder.
The container runs as an unprivileged user, with automatic sync and application updates disabled.
Zotero 9.0.6, debug-bridge 3.0.1 and ZotMoov 1.2.32 are pinned and checksum-verified. The Python
base image is pinned by digest. Update the versions and checksums in the Dockerfile together.

The default tests seed two synthetic papers through the **real Zotero API**, import real PDFs,
wait for real ZotMoov to turn them into linked attachments, check duplicate detection through
the CLI, convert and index the PDFs, and query a real MCP server. Assertions cover expected
body matches, author filtering despite an author appearing in another paper's reference list,
year filtering, citation-key lookup, hash-verified passages and repeat-run behavior. Expected
results come from the seeded corpus rather than being inferred from the resulting index.
These tests have no DOI/publisher dependency; the Compose runtime network blocks outbound
access while allowing the localhost bridge. Downloads happen during the image build only.

The same command works on Windows, Linux and macOS with Linux containers. The image uses
`linux/amd64`; ARM development machines need Docker's x86 emulation, so native ARM support
is not claimed. A change to source or tests requires rebuilding; the command above does this.

The named volume `zotero-test-state` keeps the test profile, PDFs, converted output and bridge
token across runs. Seeding is repeatable and reuses existing synthetic items. To start another
independent environment without deleting the current one, choose a new Compose project name:

```console
docker compose -p zotero-experiment -f compose.live-zotero.yml run --build --rm zotero-test
```

The default service runs tests and exits. To leave Zotero running for experiments instead
(no desktop viewer is bundled):

```console
docker compose -f compose.live-zotero.yml run --name zotero-experiment --rm zotero-test serve
```

In another terminal, use `docker exec -it zotero-experiment bash`. The test config is
`/work/config.live-test.json`. Read the bridge token from `/work/bridge-token.txt` inside the
container when invoking write CLI commands; never copy it into the repository. Paths supplied
to Zotero and the CLI are container paths. Ordinary CLI conversion/search uses explicit
`--config /work/config.live-test.json`; the sidecar index is under `/work/converted_text/index`.
Stop the experiment with Ctrl+C. No normal library is involved.

### GitHub Actions

`.github/workflows/live-zotero.yml` builds the same image on an Ubuntu runner and starts it with
`--network none`. It runs on relevant pull requests and pushes to `master`, and can also be
started manually. It needs no Zotero account, token secret or private data: a local bridge token
is generated inside the container. Startup or missing plugins fail the job before pytest, rather
than silently skipping live tests. JUnit results and the Zotero startup log are uploaded; the
profile, token and database are not. The regular three-OS offline suite remains outside Docker
and explicitly excludes `live_zotero`, `live_zotero_container` and `live_zotero_corpus` tests. Extraction, OCR,
corpus-quality, mock bridge and configuration-guard tests run on the host whenever they do
not require Zotero. See [workflow-test-corpus.md](workflow-test-corpus.md) for public PDF
sources, coverage gaps and user stories with testable acceptance criteria.

This verifies real Zotero on Linux; native host Zotero tests are disabled. It does not
measure extraction/search quality on real research papers. For realistic quality assessment,
copy selected public or locally authorized papers into a separate container experiment and
use a private question set as described in [search-quality.md](search-quality.md).

### Optional network acquisition tests

The older DOI/Find Available PDF test below remains an optional check of translator/resolver
behavior. It needs internet access and can fail because a publisher or resolver changes.
Run it separately, outside the isolated Compose network:

```console
docker build --platform linux/amd64 -f containers/live-zotero/Dockerfile -t zotero-live-test .
docker run --platform linux/amd64 --rm --init --shm-size 256m zotero-live-test network
```

That run uses a fresh disposable container and performs real DOI imports and PDF downloads.
It is excluded from the deterministic GitHub job.

The `corpus` mode does the same for every public-corpus entry with a DOI
([uses 3-5](public-corpus-uses.md)) through `import-doi --with-pdf` with the pinned URL as
fallback, and checks that the item ends up with exactly one readable PDF. It takes long and depends on many publishers, so pass pytest arguments to narrow
it, for example `-k "plos or jmlr"`. A copy other than the pinned one, or no PDF, is recorded in
`/work/corpus_acquisition.json` rather than failed; the pinned corpus for quality work still comes
from `tools/fetch_public_pdf_corpus.py` on the host.

```console
docker run --platform linux/amd64 --rm --init --shm-size 256m zotero-live-test corpus
```

First full run (2026-10-06, Zotero 9.0.6, 91 DOIs, 12 minutes): all passed. The import itself
attached a PDF for 74 items (58 identical to the pin), Find Available PDF or the pinned URL supplied
13 more, and four publishers (JMIR, De Gruyter) refused the download. Seventeen of the PDFs differ
from the pinned bytes, as expected for repository or regenerated copies.

## Host test isolation

Live Zotero tests require a Linux Docker container and the fixed `/work` test configuration.
Setting live-test environment variables on a developer machine does not enable native tests.
Host tests run with a temporary working directory/config, without inherited Zotero settings
or credentials, and block connections to Zotero's port. They never use a private library or
an existing MCP registration. All real Zotero workflows run inside the isolated image.

The offline suite verifies captured bridge payloads, generated scripts against a fake Zotero,
configuration guards, conversion, indexing and MCP behavior with test fixtures.
