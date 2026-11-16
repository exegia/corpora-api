# Operating the pipeline

How to operate Corpora API's inherited template release flow. Rules live in
[`BRANCH-AND-RELEASE-POLICY.md`](./BRANCH-AND-RELEASE-POLICY.md), stages in [`LIFECYCLE.md`](./LIFECYCLE.md).

The root stack is Python; the Astro/Fumapress site is nested in `docs/`. The setup
action installs Node 24 for that nested package. `make install` also runs
`npm ci --prefix docs`, and `make ci` type-checks and builds the site.
After an endpoint or response-model change, run `uv run python scripts/export_openapi.py`
and commit the generated `docs/openapi.json` with the code.

The root `Dockerfile.vercel` builds the pinned core checks, TEI/EPUB/PDF
converters, OCR tools, and dictionaries on Vercel's container runtime. The API
project serves `/v1/*` at its own Vercel origin. A project route on the existing
`corpora-py` project forwards only `api.exegia.co/v1/*`; do not move the whole
domain or alter its older `/convert`, `/ingest`, or `/health` routes. Set
`CORPORA_API_TOKEN` in the API project before a public production deployment.
Verify `/v1/health`, `/v1/openapi.json`, an authenticated check, one conversion,
and the older API health route after publishing the route.

## 1. Bootstrap a new repository

1. Create the repo from the template (`Use this template`). Push `main`.
2. Locally: `make doctor`, then `make setup`.
3. Create the **automation GitHub App** (see Secrets) and install it on the repo.
4. Apply settings, labels and rulesets in one go:

   ```bash
   export BYPASS_ACTORS_JSON='[{"actor_id":<APP_ID>,"actor_type":"Integration","bypass_mode":"always"}]'
   .github/scripts/bootstrap-repo.sh          # or: make repo-bootstrap
   ```

   It creates `dev` and `next` from `main`, makes `dev` the default branch, applies `.github/settings.yml`
   (squash/merge options, auto-merge, delete-branch-on-merge), switches on Dependabot alerts and security
   updates, secret scanning with push protection, and private vulnerability reporting, creates the labels, and
   applies `.github/rulesets/*.json`. `DRY_RUN=1` previews. Re-running is safe.

5. Set the secrets and variables below, then `make secrets-doctor`.
6. `make dependabot` once the first stack manifest exists, then replace the `REPLACE_ME` values in
   `SECURITY.md`, `CODE_OF_CONDUCT.md`, `LICENSE` and `.github/CODEOWNERS`.

Rulesets are inert until applied. `make rulesets-diff` shows what GitHub has; `make rulesets-apply` pushes the
files, matching by `name` (keep names, or a second ruleset is created). Apply them **after** the first release
lands if you want to seed `main` directly.

## 2. Secrets and variables

Secrets (repository level unless an environment is named):

| Name                                              | Needed for                                          |
| ------------------------------------------------- | --------------------------------------------------- |
| `AUTOMATION_APP_ID`, `AUTOMATION_APP_PRIVATE_KEY` | **Required.** Opens PRs, cuts branches, pushes tags |
| `CLAUDE_CODE_OAUTH_TOKEN`                         | Optional AI review job                              |
| `NPM_TOKEN` (env `npm`)                           | `PUBLISH_NPM`                                       |
| `CARGO_REGISTRY_TOKEN` (env `crates`)             | `PUBLISH_CRATES`                                    |
| `EXPO_TOKEN` (env `eas`)                          | `EAS_BUILD_PROFILE`                                 |
| provider tokens (env `preview` / `production`)    | `PREVIEW_DEPLOY_COMMAND` / `DEPLOY_COMMAND`         |

PyPI uses trusted publishing (OIDC): register the repo and the `pypi` environment on pypi.org, no secret.

The GitHub App needs **Contents: write**, **Pull requests: write**, **Workflows: write** and **Metadata: read**.
The App must be the actor that creates tags: tags pushed with `GITHUB_TOKEN` do not start `publish.yml`, and PRs
opened with it never get their required checks.

Variables:

| Name                                                       | Effect                                                                   |
| ---------------------------------------------------------- | ------------------------------------------------------------------------ |
| `CI_RUNNERS`                                               | JSON array of runner labels for PR checks (default from stack detection) |
| `MATRIX_RUNNERS`                                           | Runners for `matrix.yml` (default ubuntu + macOS)                        |
| `XCODE_SCHEME`, `XCODE_DESTINATION`, `XCODE_UITEST_TARGET` | Swift/Xcode builds, tests and UI tests                                   |
| `VERSION_CMD`                                              | Replaces `scripts/version.sh set` at release cut (monorepos)             |
| `PUBLISH_PYPI`, `PUBLISH_NPM`, `PUBLISH_CRATES`            | `true` enables that publish job                                          |
| `EAS_BUILD_PROFILE`                                        | Queues an EAS build for that profile                                     |
| `PREVIEW_DEPLOY_COMMAND`, `DEPLOY_COMMAND`                 | Shell run after `next` pushes / production tags                          |
| `DEPENDENCY_REVIEW`, `CODEQL`                              | `false` switches that security job off                                   |

Locally: `make env-init` creates `.env` (mode 600) from `.env.example`; `make env-check` diffs keys;
`make secrets-push [ENV_FILE=.env.ci] [ENVIRONMENT=npm]` and `make secrets-vars-push` load dotenv files into
GitHub (names are listed for confirmation, values never printed); `make secrets-list` shows names only.

## 3. Day to day

```bash
git switch -c feat/123-add-parser dev      # branch off dev
make fmt && make ci                        # same thing CI runs
gh pr create --draft --base dev --title "feat: add parser"
gh pr ready                                # starts hygiene, test, check
```

Multiple feature branches are allowed only for related work in a dependent PR stack. Otherwise,
merge the current feature into `dev` before starting another feature.

The docs package runs `docs/scripts/patch-openapi-example.mjs` after install.
Keep its version guard aligned with the pinned `fumadocs-openapi` dependency.

Squash-merge into `dev`. Everything after that is automatic until the release PR: review the draft PR into
`main`, mark it ready, merge it with a merge commit.

The API container pins one `corpora-core` commit for its Rust binaries,
converter packages, and `corpora-validate` CLI. Update that pin only after the
core PR is merged. `/v1/validate` accepts bounded user-authenticated uploads;
the legacy sidecar retains server-path and conversion-job resolution.

## 4. Manual operations

```bash
make bump-info                  # latest tag, implied bump, next version
make next-version BUMP=minor    # what the next tag would be called
make changelog VERSION=1.2.0    # preview the CHANGELOG section
make bootstrap-lanes            # create origin/dev and origin/next if missing
make promote-pr VERSION=1.2.0 BUMP=minor
make cut-release VERSION=1.2.0  # normally run by next.yml
make cleanup-local              # prune local branches whose remote is gone
```

`promote-pr` skips matching `dev` and `next` trees. The release job runs `make pack` before
`make tag-release`, which attaches `dist/` files while creating the immutable GitHub Release.
An already published release is skipped; a draft is retried by uploading assets and publishing it.
Everything is parameterized on `TRUNK` (default `main`).

## 5. Troubleshooting

| Symptom                                     | Cause and fix                                                                                             |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `guard` fails on the title                  | Use `<type>(scope)!: summary`; edit the PR title, it re-runs on `edited`                                  |
| Required `check` never appears              | PR is still a draft, or it was opened with `GITHUB_TOKEN` instead of the App                              |
| `promote` / `next` fail at their first step | `AUTOMATION_APP_ID` / `AUTOMATION_APP_PRIVATE_KEY` missing: `make secrets-doctor`                         |
| Tag exists but `publish.yml` did not run    | The tag was pushed with `GITHUB_TOKEN`; create it as the App                                              |
| Scheduled workflows not firing              | They run from the default branch; make sure `dev` has the files                                           |
| `guard` says manifests disagree             | `scripts/version.sh check` lists them; delete the stale `VERSION` file once a real manifest has a version |
| Release PR hygiene rewrites `CHANGELOG.md`   | The generator must omit trailing blank lines; run `make selftest`, then refresh the release branch via PR |
| Cut-release cannot push workflow changes    | Grant the App **Workflows: write**                                                                        |
| Hook fired on a secret                      | Remove it, rotate it, never `--no-verify`; see `skills/secrets-hygiene`                                   |

## 6. Template maintenance

`make selftest` runs `tests/template/` (stack detection, SemVer, versions, PR guard, secrets, changelog, agent
assets). Keep it green when you change `scripts/`. Lint workflows with `actionlint` and scripts with
`shellcheck -S warning -x scripts/*.sh .github/scripts/*.sh` (both also run as pre-commit hooks).
