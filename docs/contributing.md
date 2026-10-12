# Contributing

Use a checkout and the locked environment rather than the README's tagged user install:

```bash
uv sync --extra mcp --extra test --locked
uv run pytest -q
```

See [Developer test setup](dev-test-setup.md) for prerequisites and the isolated live tests.

## Contributing hygiene

This repository must never contain anyone's real identity, machine, or credentials. After cloning,
enable the guard hook once:

```bash
git config core.hooksPath .githooks
```

Before each commit it does two things. It checks the author and committer address, because a
history rewrite can replace file contents while leaving every identity header untouched -- an
address committed here outlives the text that prompted the rewrite. Then it runs
`../tests/test_no_personal_data.py`, which scans tracked text files for real home directories, real
email addresses, institutional identifiers and credential shapes. It reads both the working tree
and, where they differ, the staged blob, since a commit publishes the index rather than whatever
happens to be on disk. The same
test runs in CI, but CI runs after the push -- and a push to a public repository is the moment the
content becomes public. Removing something afterwards means rewriting history, which is why the
cheap local check is worth the second it costs.

Documentation examples should use obviously fake placeholders (`C:\Users\you\...`, `jsmith`,
`someone@example.com`). The guard allows a short list of such names and rejects anything else, so
add new placeholders to `PLACEHOLDER_NAMES` rather than choosing realistic-looking ones.

### Portable developer tests

See [docs/dev-test-setup.md](dev-test-setup.md) for fresh-clone prerequisites and Windows,
macOS/Linux commands. With Python, uv, Node and a Linux Docker engine installed:

```console
python tools/dev_tests.py doctor
python tools/dev_tests.py setup
python tools/dev_tests.py host
python tools/dev_tests.py live
```

Host tests stay outside Docker; live tests use a fresh isolated container. No personal Zotero
config, library, account or MCP registration is needed. `corpus` fetches the public PDFs;
`agent-start` prepares a fresh environment for a manually prompted independent agent.

### Testing the Zotero bridge

The write commands (`import-doi`, `find-pdf`, `link-pdf`) run JavaScript inside Zotero. The
normal suite runs the generated `find-pdf` and `link-pdf --url` scripts in Node against a fake Zotero seeded from
recorded responses, so it needs `node` on `PATH`. Those tests are skipped without it. A
portable live test environment runs an independent Linux Zotero installation in Docker:
`docker compose -f compose.live-zotero.yml run --build --rm zotero-test`. It seeds synthetic
papers through real Zotero, exercises ZotMoov and the conversion/search pipeline, and also
runs in the dedicated GitHub Actions workflow. Native host Zotero tests are disabled; all live tests require the isolated container.
See [docs/live-zotero-test.md](live-zotero-test.md)
for the isolated environment. Ordinary tests run outside Docker; only tests requiring Zotero
run inside. See [docs/workflow-test-corpus.md](workflow-test-corpus.md) for checksum-pinned
public PDF sources, OCR coverage gaps and workflow acceptance criteria. Fetch a small sample
on the host with `uv run python tools/fetch_public_pdf_corpus.py --id data-sharing`.
For a manually prompted independent agent, use the
[acquisition workflow](manual-agent-workflow.md): capture its CLI/MCP actions and verify
each step against live library state, PDF checksums and retrieved passages.
That guide includes the seven deterministic pass criteria, initial/repeat prompt procedure,
evidence retention, CI boundary and the observed live Luna result.


## Repository history

This repository's history was rewritten on 2026-09-10 to remove personal identifiers, and the
GitHub repository was recreated so that pull-request refs could not keep pinning the old commits.
Two consequences are worth knowing when reading older commits:

- **Pull-request numbers restart at #1.** References like `(#31)` or `(#37)` inside commit messages
  point at pull requests in the *previous* repository, not the current one. As new pull requests are
  opened, those numbers will come to refer to unrelated changes. Read them as historical labels, not
  as links.
- **Commit SHAs changed**, so any SHA recorded elsewhere before that date will not resolve here, and
  commits before it are unsigned.

The pull-request and review history from before the rewrite was archived rather than lost. It is
kept privately by the maintainer, since it also contains the identifiers that were removed.
