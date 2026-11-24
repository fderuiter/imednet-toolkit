# Contributing

## Project scope
The SDK targets Python 3.11–3.13 (development and documentation workspace checkouts require Python 3.11.5+) and includes:

- Core SDK package under `packages/core/src/imednet/`
- Workflow orchestration package under `packages/plugins-workflows/src/imednet_workflows/`
- Airflow provider package under `packages/providers-airflow/src/apache_airflow_providers_imednet/`
- Database sinks plugin package under `packages/plugins-sinks/src/imednet_sinks/`
- Terminal user interface plugin package under `packages/plugins-tui/src/imednet_tui/`
- Docs, tests, examples, and tooling in their directories

`AGENTS.md` is the authoritative contributor contract for architecture boundaries,
quality gates, and release expectations.

## Workspace package boundaries
- `packages/core/src/imednet/`: SDK transport, endpoints, models, CLI surface.
- `packages/plugins-workflows/src/imednet_workflows/`: higher-level workflows built on the SDK.
- `packages/providers-airflow/src/apache_airflow_providers_imednet/`: Airflow hooks, operators, and sensors.
- `packages/plugins-sinks/src/imednet_sinks/`: database export sinks (MongoDB, Neo4j, Snowflake).
- `packages/plugins-tui/src/imednet_tui/`: Textual terminal user interface and CLI plugin.

For the architecture view, see `docs/explanation/architecture.rst`.

## Prerequisites
- Python 3.11.5 or newer for workspace checkouts (published packages support Python 3.11+); recreate existing Python 3.10 virtual environments.
- [Make](https://www.gnu.org/software/make/) (optional, for building docs)

## Setup
**uv** and **Hatch** are the mandatory tools for repository environment setup.

Hatch uses `uv` as a backend installer. Ensure `uv` is installed (`hatch` can also be invoked via `uvx hatch`).

To initialize the locked development workspace `.venv` with all five packages, member extras, and default `dev` and `docs` dependency groups:

```bash
uv sync --locked --all-packages --all-extras
```

You can also initialize the Hatch environment directly with:

```bash
hatch env create
```

To interactively configure your `.env` credentials, endpoints, and test flags, run:

```bash
bash scripts/wizards/setup-imednet-env.sh
```

To run the canonical static and coverage verification gate across all five packages:

```bash
hatch run verify
```

To run the unified quality and security scanner suite (Ruff, MyPy, Vulture, Semgrep, Pylint, etc.):

```bash
hatch run lint:all
```

To run the testing suite:

```bash
hatch run test
```

### Pre-commit hooks
To install the lightweight pre-commit hooks for automatic formatting, lint fixes, non-mutating spell checking, and secret leak prevention:

```bash
pre-commit install
```

Our pre-commit configuration is optimized for developer velocity: hooks execute in <1.5s on staged files only (`ruff-format`, `ruff-check --fix`, non-mutating `typos --force-exclude`, `detect-secrets`, root sanitization). Slower, whole-codebase checks (`mypy`, `pytest`, `pylint-sim`, `vulture`, `pip-audit`) are reserved for pre-PR verification and CI.

## Issue reporting and triage
The repository uses a documented issue operating model for intake, prioritization,
and decomposition. Before opening or triaging an issue, review:

- `docs/reference/issue_management.rst` for title conventions, label taxonomy, and hierarchy
- `docs/reference/project_standards.rst` for engineering and verification standards
- `docs/reference/triage_playbook.rst` for maintainer triage workflow

Issue titles should use the format `<type>(<area>): <concise outcome>`, for example
`bug(http): delegate URL path construction to httpx` or
`refactor(core): decouple async and sync client hierarchies`.

## Validation
Run before committing:

```bash
uvx pre-commit run --all-files
hatch run verify
uvx hatch run quality:all
hatch run docs
```
Coverage must stay ≥ 90% across all five workspace packages (`imednet`, `imednet_workflows`, `apache_airflow_providers_imednet`, `imednet_sinks`, and `imednet_tui`).

### Containerized Integration Tests

To run the containerized integration tests for the MongoDB and Neo4j sinks locally using ephemeral Docker containers, run the following single command:

```bash
(docker compose up -d && trap 'docker compose down -v' EXIT && sleep 15 && IMEDNET_TEST_CONTAINERS=1 uv run pytest -o addopts="" packages/plugins-sinks/tests/integration/test_containerized_sinks.py -v)
```

This spins up the required database containers, waits for service initialization, enables the `IMEDNET_TEST_CONTAINERS=1` gate for `packages/plugins-sinks/tests/integration/test_containerized_sinks.py`, and tears the containers and volumes down afterwards (even if a test fails).

## Strict typing enforcement
Strict static typing (`strict = true` in `[tool.mypy]`) is enforced across all five workspace packages, examples, and endpoint typing contracts.

### Current baseline
- `packages/core/src/imednet`: **0** mypy errors
- `packages/plugins-workflows/src/imednet_workflows`: **0** mypy errors
- `packages/providers-airflow/src/apache_airflow_providers_imednet`: **0** mypy errors
- `packages/plugins-sinks/src/imednet_sinks`: **0** mypy errors
- `packages/plugins-tui/src/imednet_tui`: **0** mypy errors
- `examples`: **0** mypy errors
- `tests/typing/endpoint_contracts.py`: **0** mypy errors

### Typing rules
- Keep `[tool.mypy]` `strict = true` across all five workspace packages, examples, and endpoint contracts (`uv run mypy packages examples tests/typing/endpoint_contracts.py`).
- `imednet.models.*` and `imednet.errors.*` additionally retain explicit strict overrides and generated `.pyi` stub parity (`uv run python -m imednet.models.engine --check`).
- `ignore_missing_imports` must stay scoped only to third-party modules that do not ship complete typing information, and `disallow_subclassing_any = false` is scoped solely to `apache_airflow_providers_imednet.*` for untyped Airflow base classes.
- CI enforces strict `mypy` across all five packages so typing regressions cannot land.

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
   uvx pre-commit run --all-files
   hatch run verify
   uvx hatch run quality:all
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
| `imednet-tui` | `packages/plugins-tui/` | `packages/plugins-tui` |

**Tag format** — release-please creates package-specific Git tags when a Release PR is merged:

| Package | Tag format | Example |
|---|---|---|
| `imednet` | `imednet-v<version>` | `imednet-v1.1.0` |
| `imednet-workflows` | `imednet-workflows-v<version>` | `imednet-workflows-v1.1.0` |
| `apache-airflow-providers-imednet` | `apache-airflow-providers-imednet-v<version>` | `apache-airflow-providers-imednet-v1.1.0` |
| `imednet-plugins-sinks` | `imednet-plugins-sinks-v<version>` | `imednet-plugins-sinks-v1.0.1` |
| `imednet-tui` | `imednet-tui-v<version>` | `imednet-tui-v1.0.0` |

**PyPI publishing** — the `Release` workflow in `.github/workflows/release.yml` contains publish jobs
(`publish`).
Publishing is triggered either by package tags or by the release commit pushed to `main`
(`chore: release main`). Jobs use `skip-existing` so repeated runs are idempotent.

```
imednet-v* tag or release commit                  → publish (core)              → uploads imednet to PyPI
imednet-workflows-v* tag or release commit        → publish (plugins-workflows) → uploads imednet-workflows to PyPI
apache-airflow-*-v* tag or release commit         → publish (providers-airflow) → uploads apache-airflow-providers-imednet to PyPI
imednet-plugins-sinks-v* tag or release commit    → publish (plugins-sinks)     → uploads imednet-plugins-sinks to PyPI
imednet-tui-v* tag or release commit              → publish (plugins-tui)       → uploads imednet-tui to PyPI
```

Publishing uses PyPI Trusted Publishers (OIDC) via `pypa/gh-action-pypi-publish`. No API token
configuration is required when Trusted Publishers are configured on PyPI.

**Changelog** — each package has its own `CHANGELOG.md` (e.g., `packages/core/CHANGELOG.md`).
Release-please writes per-package changelogs automatically.

**Version pinning** — to pin a specific release of each package:

```bash
pip install "imednet==1.1.0"
pip install "imednet-workflows==1.1.0"
pip install "apache-airflow-providers-imednet==1.1.0"
pip install "imednet-plugins-sinks==1.0.1"
pip install "imednet-tui==1.0.0"
```

Changelogs and release notes per package are available on
[GitHub Releases](https://github.com/fderuiter/imednet-toolkit/releases).

Configuration requirements:
- Automated Release PR creation, lockfile commits, and tag-triggered workflows in
  `.github/workflows/release-please.yml` require a repository-scoped GitHub App (granted
  `Contents`, `Issues`, and `Pull requests` read/write permissions on `imednet-toolkit`) with
  `RELEASE_PLEASE_APP_ID` and `RELEASE_PLEASE_PRIVATE_KEY` configured as repository secrets so
  `pull_request` CI checks and package tag releases start automatically:
  ```bash
  bash scripts/wizards/setup-release-please-app.sh
  ```
- Publishing requires PyPI Trusted Publishers (OIDC) configured for each package on PyPI, **or**
  a `PYPI_API_TOKEN` repository secret as a fallback. An interactive wizard is available to guide this multi-package setup:
  ```bash
  bash scripts/wizards/setup-pypi-oidc.sh
  ```
- Live CI testing is an explicitly approved, manual workflow. It requires the `live-testing`
  GitHub environment and valid credentials; scheduled smoke testing has been retired:
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
   API reference documentation is generated automatically by `scripts/build_docs.py` (`sphinx-apidoc`
   plus curated core reference helpers) into `docs/reference/api/`, which is tracked in version
   control and staged atomically before replacing existing files.

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

1. Run strict `mypy` across all five workspace packages, examples, and endpoint contracts (`uv run mypy packages examples tests/typing/endpoint_contracts.py` or individually):
   - `mypy packages/core/src/imednet`
   - `mypy packages/plugins-workflows/src/imednet_workflows`
   - `mypy packages/providers-airflow/src/apache_airflow_providers_imednet`
   - `mypy packages/plugins-sinks/src/imednet_sinks`
   - `mypy packages/plugins-tui/src/imednet_tui`
   - `mypy examples`
   - `mypy tests/typing/endpoint_contracts.py`
2. Run `hatch run docs` (or `uv run python scripts/build_docs.py`) locally. This command stages and
   updates `docs/reference/api/` via `sphinx-apidoc` and compiles the HTML with `-W --keep-going`,
   treating every Sphinx warning as an error.
3. Commit any intentional `docs/reference/api/` updates when adding or renaming public modules.
4. Open `docs/_build/html/index.html` and verify that type hints appear in the parameter
   descriptions with no raw reStructuredText syntax leaking into the rendered page.


## Pull requests
1. Fork the repository and create a feature branch.
2. Include validation output in the PR description.
3. Keep PRs scoped; one change per PR.
4. Please follow the [Code of Conduct](https://github.com/fderuiter/imednet-toolkit/blob/main/CODE_OF_CONDUCT.md).

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
