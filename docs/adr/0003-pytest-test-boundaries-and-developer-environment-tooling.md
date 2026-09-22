# Pytest Test Boundaries and Developer Environment Tooling

## Context and Decision

During local test execution and CI maintenance, several environment and testing boundaries caused developer friction:
1. **Pytest Test Boundaries**: Executing `pytest` without explicit `--ignore` flags collected `tests/browser/` (requiring headless Playwright browser binaries) and risked discovering `tests/live/` (requiring live credentials and network access).
2. **Windows Shell & Console Environment**: On Windows, tools installed into user scripts directories (e.g. `%APPDATA%\Python\Python3xx\Scripts\uv.exe`) were absent from default PowerShell sessions. Furthermore, default console codepages (`cp1252`) triggered `charmap` codec `UnicodeDecodeError` / `UnicodeEncodeError` when tools output emojis or Unicode strings.
3. **Conventional Commit Semantics for Security Work**: Issue titles prefixed with `security:` failed the `Semantic PR Title` CI check, as standard Conventional Commits use `fix:` or `chore:`.

We decided to:
1. **Enforce Root Pytest Ignores**: Configure `[tool.pytest.ini_options]` in `pyproject.toml` with `addopts = "-ra --ignore=tests/live --ignore=tests/browser"` and `testpaths = ["tests", "packages/plugins-sinks/tests"]`. This ensures any bare `pytest` or `uv run pytest` execution runs only the hermetic unit test suite by default. Dedicated browser and live test jobs specify their target paths explicitly.
2. **Standardize Windows Environment Helpers**: Provide `scripts/env.ps1` to configure UTF-8 session encoding (`$env:PYTHONUTF8 = "1"`), auto-detect `uv.exe` across user directories and prepend it to `PATH`, and activate `.venv`. The interactive Bash setup wizard (`scripts/wizards/setup-imednet-env.sh`) complements this by configuring `.env` and offering workspace synchronization.
3. **Standardize Security Fixes under `fix:`**: Document that security patches and CVE remediations must use the `fix:` prefix (e.g. `fix(deps):` or `fix(security):`) to comply with Conventional Commits and ensure automated patch release generation via `release-please`.
4. **Group Dependabot Updates**: Configure `.github/dependabot.yml` with group rules to consolidate automated dependency updates into atomic PRs.

## Consequences

- Bare `pytest` commands run fast and safely across all environments without accidental browser or live test execution.
- Windows developers and automation tools have a reliable environment activator script (`scripts/env.ps1`) and setup wizard (`scripts/wizards/setup-imednet-env.sh`) that eliminate PATH and encoding errors.
- PR titles and releases adhere to consistent SemVer and Conventional Commit standards.
