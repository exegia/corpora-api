# Copilot instructions

Full rules are in [`AGENTS.md`](../AGENTS.md); this is the short form. This repository is
Corpora API: FastAPI and FastMCP over the `corpora-core` JSON-lines executable, plus
bounded TEI, EPUB, and PDF conversion uploads. The API uses Vercel's container runtime;
the Astro/Fumapress documentation site is a separate project rooted in `docs/`.
`POST /v1/validate` accepts bounded archive uploads with verified user JWT auth.
It invokes `CORPORA_VALIDATE_BIN`; client paths and legacy job IDs are not valid
hosted targets.

## Commands (run from the repository root)

| Task                           | Command                                                        |
| ------------------------------ | -------------------------------------------------------------- |
| Check tools / first-time setup | `make doctor` / `make setup`                                   |
| Format and autofix             | `make fmt`                                                     |
| Lint, types, tests, build      | `make lint-check`, `make typecheck`, `make test`, `make build` |
| End-to-end tests               | `make e2e`                                                     |
| Everything CI runs on a PR     | `make ci`                                                      |
| Test the repo's own automation | `make selftest`                                                |
| Validate skills / instructions | `make agents-check`                                            |
| Scan for secrets               | `make secrets-scan`                                            |

Root targets detect Python from `pyproject.toml`; `make ci` also installs, type-checks, and
builds the nested `docs/` site. After changing routes or models, run
`uv run python scripts/export_openapi.py` and commit `docs/openapi.json`.
The docs install patches `fumadocs-openapi` 12.6.2 request examples; review
`docs/scripts/patch-openapi-example.mjs` before upgrading that dependency.
The hosted API owns `api.exegia.co/v1/*` through a narrow route on the existing
`corpora-py` project. Preserve all other routes on that domain.
Override a task with `<TASK>_CMD`, e.g. `make test TEST_CMD="..."`.
`copilot-setup-steps.yml` prepares the same toolchains for the coding agent.

## Conventions

- **Branches:** `<type>/<slug>` with type `feat|fix|chore|docs|ci|refactor|test|perf|build|style|revert|ai`,
  opened as PRs into `dev` (the default branch). Promotion: `dev` -> `next` -> `release/vX.Y.Z` -> `main`.
  Multiple feature branches require related work in a dependent PR stack; otherwise merge the current feature into `dev` before starting another.
- **PR titles:** Conventional Commits, `feat(scope)!: summary`. They set the SemVer bump (`!` major,
  `feat` minor, else patch). Do not edit versions, tags or `CHANGELOG.md` by hand.
- **Release changelog:** the generator must leave no trailing blank line; run `make selftest` after edits.
- **Release assets:** build `dist/` before `tag-release`; it attaches assets during release creation.
  Skip promotion when `dev` and `next` have the same tree.
- **Workflows:** `.github/workflows/*.yml` are plain workflows; steps call `make` targets. Pin third-party
  actions to a commit SHA with a version comment. Keep jobs least-privilege.
- **Docs move with automation:** update `AGENTS.md`, `README.md`, and `.github/WORKFLOW.md` in the same PR.
- **Shared automation stays reusable:** keep API and docs specifics in product code and guidance.
- **Skills:** `.github/skills/<name>/SKILL.md`, frontmatter `name` equal to the directory and a one-line
  `description`. `make agents-check` enforces it.

## Secrets

- Never commit credentials, tokens or keys, including in tests, fixtures, docs and examples.
- Real values belong in `.env` (gitignored); commit `.env.example` with `REPLACE_ME` placeholders.
- `gitleaks` runs as a pre-commit hook and as the required `Secret Scan` workflow. Never use
  `--no-verify`; if it fires, remove the secret and rotate it.
- Do not commit `.claude/`, `.agents/`, `.codex/`, `.worktrees/` or workflow logs.
