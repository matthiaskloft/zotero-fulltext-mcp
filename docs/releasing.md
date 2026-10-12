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
   `v*` **tags**, not branches named `v*`. Allow only the `master` **branch** for `testpypi`.
   These environments are already configured in this repository; check their settings when
   changing maintainers or copying this workflow elsewhere. The owner can approve their own
   production run, so a solo maintainer can still publish after explicitly reviewing it.
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

The publisher values must match exactly. The workflow filename is `release.yml`, not its
full repository path; the account/environment registrations are separate for each index.
No PyPI credentials are needed for build/test jobs.

## First-publication rehearsal

Authenticate GitHub CLI for the repository, then dispatch the merged workflow:

```console
gh workflow run release.yml --ref master -f destination=testpypi
gh run list --workflow release.yml --limit 5
```

Use `gh run watch RUN_ID` or open that run in GitHub Actions. `validate`, `ci`, `live-zotero`,
`build` and `testpypi` must succeed. `pypi` and `github-release` are skipped for a rehearsal.
Confirm the expected version's wheel and source archive appear on TestPyPI.

To test the uploaded wheel, use a fresh virtual environment. Download the package itself
from TestPyPI without dependencies, then install that local wheel with dependencies from
normal PyPI. This avoids mixing dependency resolution between two indexes:

```console
python -m pip download --no-deps --only-binary=:all: --index-url https://test.pypi.org/simple/ --dest test-results/testpypi-wheel "zotero-fulltext-mcp==X.Y.Z"
python -m pip install "test-results/testpypi-wheel/zotero_fulltext_mcp-X.Y.Z-py3-none-any.whl[mcp]"
python -m pip check
zotero-pdf-text --help
zotero-fulltext-mcp --help
```

Replace `X.Y.Z` with the prepared package version. Run the installed commands outside the
source checkout; they should print help without requiring a Zotero config. The first rehearsal
must succeed before the production tag is pushed. Repeating an upload with different bytes
under the same version is refused: prepare a new version if an uploaded artifact needs fixing.

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

In GitHub Actions, open the release run, select **Review deployments**, select `pypi`, and
approve it. All checks must have passed before this gate appears. After the run completes,
verify the version on PyPI and the GitHub release's tag, notes and attached distributions.
Test a fresh `python -m pip install "zotero-fulltext-mcp[mcp]==X.Y.Z"`, followed by
`python -m pip check` and both commands' `--help` outside the checkout. After the first
successful production publication, update README's installation guidance to make PyPI the
primary install path, while retaining tagged Git installation for users who need it.

TestPyPI is an optional rehearsal, not a dependency of every production release. Version
uploads cannot be replaced on either index; use a new version for changed artifacts. If PyPI
has accepted an upload but GitHub release creation fails, rerun only the failed GitHub release
job, or create it manually using the same tag, artifacts and notes. Do not re-upload a changed
build under the same version or move a published tag. Existing GitHub releases are preserved.

## Failure recovery

| Failure | Action |
| --- | --- |
| `validate` rejects the tag or metadata | Fix the release PR and prepare a correctly matching new tag; do not move a published tag. Manual rehearsals must use `master`. |
| CI, live Zotero, metadata or wheel-install checks fail | Inspect that job's logs. Fix and review the cause before publishing; upload jobs remain blocked. |
| Production waits for approval | Approve the `pypi` environment deployment after reviewing the checks. A queued approval is not a failed upload. |
| Trusted Publishing authentication fails | Check the index account's publisher owner, repository, `release.yml` filename and environment. Verify that the project name is available or belongs to that account. Fix the registration, then rerun the failed upload job if no files were accepted. |
| Index rejects an existing version/file | Check which files were accepted before retrying. Do not enable blanket skip-existing behavior to conceal differing artifacts; resolve partial uploads or use a new version. |
| PyPI succeeds but GitHub release creation fails | Use `gh run rerun RUN_ID --failed` to retry failed jobs only. The successful PyPI job must not be rerun. Alternatively create the GitHub release manually with the same tag, notes and published artifacts. |

Build artifacts are named `release-distributions` and `release-notes` on the workflow run.
Real-Zotero diagnostics are in `live-zotero-results`. Keep these available when investigating
failures. Do not merge unrelated fixes or replace artifacts while an existing release run is
waiting for approval; cancel that run and prepare another reviewed version if the release changes.

Until the first PyPI upload succeeds, use README's tagged Git install. After publication,
users can install `python -m pip install "zotero-fulltext-mcp[mcp]==X.Y.Z"`. Pip does not
use `uv.lock`; the `mcp` extra declares `PyJWT>=2.14.0` to enforce that minimum for pip users.

References: [PyPA publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/),
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
[GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
