## Description
A clear and concise description of what this PR changes and why.

## Related Issue
Closes # <!-- issue number — required -->

## Type of Change

- [ ] Bug fix
- [ ] New feature / capability
- [ ] Refactor (internal restructure, no behavior change)
- [ ] Tech debt / code quality
- [ ] Documentation update
- [ ] CI / tooling change
- [ ] Security fix

## Surface Impact

- [ ] No public API change
- [ ] Public SDK API changed (method signature, return type, model field)
- [ ] CLI interface changed (command name, flag, help text, exit code)
- [ ] Environment variable or configuration key added/changed/removed
- [ ] Breaking change — migration note added to PR description

## How Has This Been Tested?
Describe the tests added or updated. If tests were intentionally skipped, explain why.

## Quality Gate Checklist

### Formatting & linting
- [ ] `hatch run ruff format --check .`
- [ ] `hatch run ruff check .`
- [ ] Similarity gates pass locally (`hatch run lint:pylint-sim packages/`)

### Type checking (run for each affected package)
- [ ] `hatch run mypy packages/core/src/imednet`
- [ ] `hatch run mypy packages/plugins-workflows/src/imednet_workflows` _(if workflows changed)_
- [ ] `hatch run mypy packages/providers-airflow/src/apache_airflow_providers_imednet` _(if Airflow changed)_
- [ ] `hatch run mypy packages/plugins-sinks/src/imednet_sinks` _(if sinks changed)_
- [ ] `hatch run mypy packages/plugins-tui/src/imednet_tui` _(if TUI changed)_
- [ ] `hatch run mypy examples tests/typing/endpoint_contracts.py`

### Tests
- [ ] `hatch run pytest -q --cov=imednet --cov=imednet_workflows --cov=apache_airflow_providers_imednet --cov=imednet_sinks --cov=imednet_tui --cov-fail-under=90`
- [ ] All new and existing tests pass
- [ ] Coverage remains >= 90%

### Docs
- [ ] `hatch run docs` runs with zero warnings _(required when public API or CLI surface changed)_
- [ ] Docstrings added or updated for changed public symbols
- [ ] `docs/` pages updated if user-visible behavior changed

### Process
- [ ] PR title follows [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `refactor:`, `docs:`, etc.)
- [ ] This PR is linked to an issue above
- [ ] New modules align with package boundaries described in `AGENTS.md`
- [ ] No secrets, tokens, or credentials added to source code
- [ ] No duplicate code implementations bypassing core shared utilities were introduced
