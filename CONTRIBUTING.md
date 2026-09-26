# Contributing

Thanks for helping improve `zotero-fulltext-mcp`. Bug reports, installation feedback, feature
requests and documentation suggestions are welcome as issues.

Implementation is done by the maintainer. Pull requests are limited to invited collaborators, so
please describe the problem or proposal in an issue rather than sending code. A good issue --
what you ran, what you expected, what happened -- is the most useful contribution.

## Before opening an issue

- Check the existing issues and `docs/troubleshooting.md`.
- Include your operating system, Python version, installation method, and the command that failed.
- Redact local paths, Zotero keys, paper titles, database contents, tokens, and configuration
  values. Never attach a real `zotero.sqlite`, converted library, or personal config.
- For a security or privacy problem, follow `SECURITY.md` instead of opening a public report.

## Development setup

Python 3.11+ is required. The reproducible contributor environment uses the committed lockfile:

```bash
uv sync --extra mcp --extra test --locked
uv run pytest -q
```

Alternatively, create a virtual environment outside the checkout and install
`pip install -e '.[mcp,test]'`.

Work on a topic branch and keep changes narrowly scoped. Source lives in `src/zotero_pdf_text`,
tests in `tests`, and user-facing behavior is documented in `README.md` and `docs`.

## Pull-request expectations (collaborators)

- Add or update tests for behavior changes.
- Run the focused tests first, then the full suite.
- Update README/docs when CLI commands, MCP tools, configuration, or installation behavior changes.
- Preserve read-only Zotero database access and the default read-only MCP surface.
- Do not commit real configs, PDFs, converted Markdown, Zotero databases, indexes, or credentials.

By contributing code, you agree that your contribution is licensed under the repository's MIT License.
