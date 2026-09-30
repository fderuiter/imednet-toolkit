# Contributing

## Project scope
The SDK targets Python 3.10–3.12 and includes:

- Core SDK package under `packages/core/src/imednet/`
- Workflow orchestration package under `packages/plugins-workflows/src/imednet_workflows/`
- Airflow provider package under `packages/providers-airflow/src/apache_airflow_providers_imednet/`
- Docs, tests, examples, and tooling in their directories

`AGENTS.md` is the authoritative contributor contract for architecture boundaries,
quality gates, and release expectations.

## Workspace package boundaries
- `packages/core/src/imednet/`: SDK transport, endpoints, models, CLI surface.
- `packages/plugins-workflows/src/imednet_workflows/`: higher-level workflows built on the SDK.
- `packages/providers-airflow/src/apache_airflow_providers_imednet/`: Airflow hooks/operators.

For the architecture view, see `docs/architecture.rst`.

## Prerequisites
- [Make](https://www.gnu.org/software/make/) (optional, for building docs)

## Setup
**Hatch** and **uv** are the mandatory tools for repository environment setup.

Hatch uses `uv` as a backend installer. Ensure both are installed globally, or configure Hatch to use uv via `hatch config set installer uv`.

To initialize the default development workspace environment and install all package dependencies:

```bash
hatch env create
```

To interactively configure your `.env` credentials, endpoints, and test flags, run:

```bash
bash scripts/wizards/setup-imednet-env.sh
```

To run the unified quality and linter suite (Ruff, MyPy, etc.):

```bash
hatch run lint:all
```

To run the testing suite with strict coverage gates:

```bash
hatch run test
```

### Pre-commit hooks
To install the lightweight pre-commit hooks for automatic formatting, lint fixes, spell checking, and secret leak prevention:

```bash
pre-commit install
```

Our pre-commit configuration is optimized for developer velocity: hooks execute in <1.5s on staged files only (`ruff format`, `ruff --fix`, `typos`, `detect-secrets`, root sanitization). Slower, whole-codebase checks (`mypy`, `pytest`, `pylint-sim`, `vulture`, `pip-audit`) are reserved for pre-PR verification and CI.

## Issue reporting and triage
The repository uses a documented issue operating model for intake, prioritization,
and decomposition. Before opening or triaging an issue, review:

- `docs/issue_management.rst` for title conventions, label taxonomy, and hierarchy
- `docs/project_standards.rst` for engineering and verification standards
- `docs/triage_playbook.rst` for maintainer triage workflow

Issue titles should use the format `<type>(<area>): <concise outcome>`, for example
`bug(http): delegate URL path construction to httpx` or
`refactor(core): decouple async and sync client hierarchies`.

## Validation
Run before committing:

```bash
hatch run lint:all
hatch run test
hatch run docs
```
Coverage must stay ≥ 90%.

### Containerized Integration Tests

To run the containerized integration tests for the MongoDB and Neo4j sinks locally using ephemeral Docker containers, run the following single command:

```bash
docker compose up -d && pytest packages/plugins-sinks/tests/integration
```

This will automatically spin up the required database containers, run the verification tests, and tear the containers down afterwards.

## Incremental typing ratchet
Typing rigor is increased incrementally across workspace packages instead of one
strict-mode migration.

### Current baseline (captured 2026-05-20)
- `packages/core/src/imednet`: **0** mypy errors
- `packages/plugins-workflows/src/imednet_workflows`: **5** mypy errors
- `packages/providers-airflow/src/apache_airflow_providers_imednet`: **0** mypy errors

### Ratchet rules
- Keep global mypy at non-strict while tightening module by module.
- `imednet.models.*` and `imednet.errors.*` are strict targets and must remain
  clean under `mypy --strict`.
- `ignore_missing_imports` must stay scoped only to third-party modules that do
  not currently ship complete typing information (`pandas`, `pythonjsonlogger`,
  `opentelemetry`, `airflow`).
- CI enforces the strict subset so previously clean strict modules cannot regress.

### Top `Any` / dynamic typing hotspots (tracked TODOs)
- TODO (#938): reduce `Any` leakage in dynamic endpoint interfaces used by core
  generic endpoint patterns.
- TODO (#873): tighten plugin loading and endpoint surface typing in workflow
  orchestration paths.
- TODO (#935): complete sync/async interface separation to remove ambiguous
  endpoint capability typing.

## Credential redaction requirement
- Never log, print, persist, or surface API credentials in plaintext.
- Keep `ApiKeyAuth`/error message masking intact and add or update unit tests when
  touching auth, transport, logging, or CLI error paths.
- HTTP transport intentionally suppresses `httpx`/`httpcore` request logs during
  SDK calls to prevent accidental header/query leakage.

## HTTP transport mocking
- Use `respx` for tests that exercise `Client` or `AsyncClient` HTTP behavior.
- Do not patch `Client._client.request`, `AsyncClient._client.request`, or executor
  `send` callables just to intercept outbound `httpx` traffic.
- Prefer strict routers such as
  `@respx.mock(assert_all_called=True, assert_all_mocked=True)` so unmocked requests
  fail fast and unused routes are caught.
- Validate request construction inside the route handler when needed (for example,
  query parameters, dynamic URLs, and retry behavior).

## Package metadata and versioning
- The package metadata lives in `packages/*/pyproject.toml`.
- Never manually edit package versions in `packages/*/pyproject.toml`.

## Release workflow
Releases are fully automated and driven by merged PR titles:

1. Ensure your PR title follows Conventional Commits. Supported prefixes are `feat:`, `fix:`,
   `chore:`, `docs:`, `ci:`, `test:`, `refactor:`, `perf:`, and `revert:`. The CI
   `Semantic PR Title` check enforces this.
2. Ensure your branch is up to date and all validation checks pass:
   ```bash
   hatch run lint:all
   hatch run test
   hatch run docs
   ```
3. Merge to `main` using **Squash and merge** so the PR title becomes the merged commit message.
4. The `Automated Release` workflow runs `release-please` in manifest mode on `main` pushes and
   opens/updates a Release PR with calculated semantic version and changelog updates for the package
   manifests under `packages/`.
5. Maintainers trigger publication by approving and merging the bot-created Release PR.
6. Do not run manual publish commands (`python -m build`, `twine upload`) from contributor branches.

### How releases work

The repository uses [`release-please`](https://github.com/googleapis/release-please) in **manifest
mode** (`release-please-config.json` + `.release-please-manifest.json`). Each workspace package
has an independent version and changelog:

| PyPI package | Workspace path | Config key |
|---|---|---|
| `imednet` | `packages/core/` | `packages/core` |
| `imednet-workflows` | `packages/plugins-workflows/` | `packages/plugins-workflows` |
| `apache-airflow-providers-imednet` | `packages/providers-airflow/` | `packages/providers-airflow` |
| `imednet-plugins-sinks` | `packages/plugins-sinks/` | `packages/plugins-sinks` |

**Tag format** — release-please creates package-specific Git tags when a Release PR is merged:

| Package | Tag format | Example |
|---|---|---|
| `imednet` | `imednet-v<version>` | `imednet-v0.7.0` |
| `imednet-workflows` | `imednet-workflows-v<version>` | `imednet-workflows-v0.5.3` |
| `apache-airflow-providers-imednet` | `apache-airflow-providers-imednet-v<version>` | `apache-airflow-providers-imednet-v0.5.2` |
| `imednet-plugins-sinks` | `imednet-plugins-sinks-v<version>` | `imednet-plugins-sinks-v0.2.0` |

**PyPI publishing** — the `Pipeline` workflow in `.github/workflows/main.yml` contains publish jobs
(`publish-core`, `publish-workflows`, `publish-providers`, `publish-sinks`).
Publishing is triggered either by package tags or by the release commit pushed to `main`
(`chore: release main`). Jobs use `skip-existing` so repeated runs are idempotent.

```
imednet-v* tag or release commit         → publish-core       → uploads imednet to PyPI
imednet-workflows-v* or release commit   → publish-workflows  → uploads imednet-workflows to PyPI
apache-airflow-*-v* or release commit    → publish-providers  → uploads apache-airflow-providers-imednet to PyPI
imednet-plugins-sinks-v* or release comm → publish-sinks      → uploads imednet-plugins-sinks to PyPI
```

Publishing uses PyPI Trusted Publishers (OIDC) via `pypa/gh-action-pypi-publish`. No API token
configuration is required when Trusted Publishers are configured on PyPI.

**Changelog** — each package has its own `CHANGELOG.md` (e.g., `packages/core/CHANGELOG.md`).
Release-please writes per-package changelogs automatically.

**Version pinning** — to pin a specific release of each package:

```bash
pip install "imednet==0.7.0"
pip install "imednet-workflows==0.5.3"
pip install "apache-airflow-providers-imednet==0.5.2"
```

Changelogs and release notes per package are available on
[GitHub Releases](https://github.com/fderuiter/imednet-python-sdk/releases).

Configuration requirements:
- Publishing requires PyPI Trusted Publishers (OIDC) configured for each package on PyPI, **or**
  a `PYPI_API_TOKEN` repository secret as a fallback. An interactive wizard is available to guide this multi-package setup:
  ```bash
  bash scripts/wizards/setup-pypi-oidc.sh
  ```
- Live CI testing and smoke test workflows require the `live-testing` GitHub environment and credentials:
  ```bash
  bash scripts/wizards/setup-ci-live-testing.sh
  ```
- Configure branch protection on `main` to require pull request reviews and required status checks,
  including `Semantic PR Title`.

## Conventions
- DRY + SOLID. Line length 100.
- Use Conventional Commit prefixes in PR titles (`feat:`, `fix:`, `chore:`, `docs:`, `ci:`,
  `test:`, `refactor:`, `perf:`, `revert:`).
- Add tests, docs, and examples for any public change.

## Docstring standards

All modules, classes, functions, and methods (regardless of visibility—public or private) MUST be documented using **Google-style docstrings** to comply with the project's strict documentation governance policy. The CI pipeline will fail if any code member is missing a docstring.

Key rules:

1. **Type hints belong in the function signature only.** Do not duplicate type information inside
   the docstring body.
2. **Google format sections** use four-space indentation and the exact keywords `Args:`, `Returns:`,
   `Raises:`, `Example:`, etc. Writing `Arguments:` or `Parameters:` instead of `Args:` will cause
   malformed output.
3. **Mypy compliance is a prerequisite for documentation.** `sphinx-autodoc-typehints` evaluates
   actual Python type annotations; broken or missing annotations will render incorrectly.
4. **Static `.rst` files are reserved for architectural overviews and tutorials only.**
   Do not create manually maintained `.rst` files for individual modules, classes, or CLI commands.
   API reference documentation is generated automatically by `sphinx-apidoc` into `docs/api/`
   (excluded from version control via `.gitignore`).

### Docstring example

```python
def fetch_records(study_key: str, page: int = 0) -> list[Record]:
    """Retrieve a page of records for the given study.

    Args:
        study_key: Unique identifier for the study.
        page: Zero-based page index.

    Returns:
        A list of Record objects for the requested page.

    Raises:
        ApiError: If the server returns a non-2xx status code.
    """
```

### Pre-merge documentation checklist

Before opening a pull request that adds or modifies public APIs:

1. Run `mypy packages/core/src/imednet` and confirm zero errors.
2. Run `mypy packages/plugins-workflows/src/imednet_workflows`.
3. Run `mypy packages/providers-airflow/src/apache_airflow_providers_imednet`.
4. Run `hatch run docs` locally. This command regenerates `docs/api/` via `sphinx-apidoc` and then
   compiles the HTML with `-W --keep-going`, treating every Sphinx warning as an error.
5. Open `docs/_build/html/index.html` and verify that type hints appear in the parameter
   descriptions with no raw reStructuredText syntax leaking into the rendered page.


## Pull requests
1. Fork the repository and create a feature branch.
2. Include validation output in the PR description.
3. Keep PRs scoped; one change per PR.
4. Please follow the [Code of Conduct](https://github.com/fderuiter/imednet-python-sdk/blob/main/CODE_OF_CONDUCT.md).

## Dependency-Aware CI Pipeline
Our CI pipeline is optimized to run quality gates and test suites only for packages affected by your changes.
- A change to a "leaf" package (e.g., the Airflow provider) will trigger tests exclusively for that package.
- A change to the core `imednet` package will trigger the full validation matrix for all dependent packages in the monorepo.
- Ensure your tests and linters pass locally before submitting a PR.
- **Unified Static Analysis:** We use a centralized, orchestrated framework using Hatch. To execute Ruff, MyPy, and all security audits, simply run:
  `hatch run lint:all`
- **Adding or Updating Tools:** To add or update a linting or security tool, do **not** add it to `pre-commit-config.yaml` as an external hook or install it manually. Instead:
  1. Add or update the dependency in the `[tool.hatch.envs.lint]` block in the root `pyproject.toml`.
  2. Define or update the command in the `[tool.hatch.envs.lint.scripts]` section.
  3. Ensure your new script is included in the `all` command sequence so it executes via the unified interface and CI.
