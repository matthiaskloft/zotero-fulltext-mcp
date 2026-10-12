# Publishing releases

Release preparation stays in a reviewed PR. The `Release` workflow in
`.github/workflows/release.yml` runs automatically on a pushed `vX.Y.Z` tag.
It verifies the version, lockfile, citation, README pin and dated changelog, and refuses
commits outside `master`. Only stable three-part versions are supported.

The workflow reruns the three-OS CI and isolated real-Zotero tests against the actual
tagged commit. It builds the wheel and sdist, checks metadata with Twine, inspects their
contents for local runtime/config files, and installs the wheel with `[mcp]` into a clean
environment using ordinary pip resolution. Both console commands must run outside the
checkout and `pip check` must pass. Publishing downloads those same checked artifacts;
it does not rebuild them.

## One-time setup before the first upload

1. Create GitHub environments named `pypi` and `testpypi` under repository Settings →
   Environments. Require a reviewer for `pypi` and restrict production deployments to
   version tags. Allow `master` for `testpypi`.
2. Sign in to [PyPI](https://pypi.org/manage/account/publishing/) and register a pending
   Trusted Publisher (or add a publisher to the existing project if you own it):
   - Project: `zotero-fulltext-mcp`
   - Owner: `matthiaskloft`
   - Repository: `zotero-fulltext-mcp`
   - Workflow filename: `release.yml`
   - Environment: `pypi`
3. Register the same publisher on [TestPyPI](https://test.pypi.org/manage/account/publishing/),
   using environment `testpypi`. PyPI and TestPyPI accounts/publisher registrations are separate.
   A pending publisher does not reserve a package name; confirm availability or ownership.
4. Once the workflow PR is merged, run `Release` manually from `master` to rehearse the
   current version on TestPyPI. This runs all checks and uploads only to TestPyPI;
   it does not create a tag or GitHub release. Do not run from a release tag.

Trusted Publishing uses short-lived OIDC credentials. No API token belongs in repository
secrets. Only the isolated upload jobs have `id-token: write`; they do not check out code
or execute the build. Configure environment protection in GitHub Settings: declaring an
environment in YAML alone does not create reviewer rules. If your account/repository plan
does not support required reviewers, resolve the production approval policy before publishing.

## Each release

1. Prepare the changelog, `pyproject.toml` version, `uv.lock` (`uv lock`), `CITATION.cff`
   version/date and README's pinned install examples in a PR.
2. Merge after review and green CI, then wait for green `master` CI.
3. Tag that merged commit and push the tag: `git tag vX.Y.Z <merged-commit>` then
   `git push origin vX.Y.Z`. A human/normal Git credential tag push triggers the workflow;
   a tag pushed using a workflow's `GITHUB_TOKEN` does not start another workflow.
4. Review the completed checks and approve the `pypi` deployment. After PyPI accepts the
   upload, the workflow creates the GitHub release from that version's changelog and attaches
   the wheel and sdist. No second workflow is triggered by the release.

TestPyPI is an optional rehearsal, not a dependency of every production release. Version
uploads cannot be replaced on either index; use a new version for changed artifacts. If PyPI
has accepted an upload but GitHub release creation fails, rerun only the failed GitHub release
job, or create it manually using the same tag, artifacts and notes. Do not re-upload a changed
build under the same version or move a published tag. Existing GitHub releases are preserved.

Until the first PyPI upload succeeds, use README's tagged Git install. After publication,
users can install `python -m pip install "zotero-fulltext-mcp[mcp]==X.Y.Z"`. Pip does not
use `uv.lock`; the `mcp` extra declares `PyJWT>=2.14.0` to enforce that minimum for pip users.

References: [PyPA publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/),
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
[GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
