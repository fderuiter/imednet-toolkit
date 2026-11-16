# AGENTS.md — Contributor Guide

**Note:** This file contains strict instructions for AI agents and human contributors working in this directory and its subdirectories.

1. **Stack & Rules**: You MUST follow the dependency stack rules and immutable constraints (credential handling, testing boundaries, PR guidelines) defined in `docs/reference/agent_rules.rst`.
2. **Verification**: You MUST execute the full CI quality gate locally before proposing a solution, as detailed in `docs/how-to/verification.rst`. Contributors must execute `uvx pre-commit run --all-files` before opening pull requests.
3. **Architecture**: You MUST adhere to the separation of concerns defined in `docs/explanation/architecture.rst`.

## Superpowers Methodology

This repository includes the [obra/superpowers](https://github.com/obra/superpowers) skills library under `.agents/skills/` (alongside the existing `mattpocock/skills` suite; see `.agents/skills/README.md`).

- **Session Bootstrap**: Start conversations by checking `.agents/skills/using-superpowers/SKILL.md` (and `.agents/skills/using-superpowers/references/antigravity-tools.md` when running in Antigravity) and invoke any applicable skill before acting or writing code.
- **Standard Flow**: `brainstorming` (design/spec) -> `using-git-worktrees` (isolated branch/worktree) -> `writing-plans` -> `subagent-driven-development` or `executing-plans` + `test-driven-development` -> `requesting-code-review` -> `verification-before-completion` -> `finishing-a-development-branch`.
- **Skill Precedence** (coexisting suites in `.agents/skills/`):
  - Default to **Superpowers** (`brainstorming`, `writing-plans`, `subagent-driven-development` / `executing-plans`, `test-driven-development`, `systematic-debugging`, `requesting-code-review`, `receiving-code-review`, `verification-before-completion`, `finishing-a-development-branch`) for end-to-end feature and bugfix execution.
  - Use **`mattpocock/skills`** for GitHub-issue/graph workflows (`to-spec`, `to-tickets`, `triage`, `wayfinder` backed by `docs/agents/*`), domain/architecture modeling (`grill-with-docs`, `domain-modeling`, `improve-codebase-architecture`, `codebase-design`), and session utilities (`handoff`, `wait-what`, `teach`, `to-questionnaire`, `wizard`, `prototype`, `research`).
- **Project Overrides** (these take precedence over generic defaults in upstream skills):
  - **Environment Setup**: Use `uv sync --locked --extra dev --extra docs` instead of `poetry` or `pip`. Never introduce unlisted dependencies without explicit approval per `docs/reference/agent_rules.rst`.
  - **Worktrees**: Prefer harness-native worktree tools when available; otherwise use `.worktrees/` (ignored in `.gitignore`).
  - **Specs & Plans**: Save Superpowers design specs and implementation plans under `docs/superpowers/specs/` and `docs/superpowers/plans/` (excluded from Sphinx builds in `docs/conf.py`), or publish issues/tickets to GitHub per `docs/agents/issue-tracker.md`.
  - **Verification & Finishing**: `verification-before-completion` and `finishing-a-development-branch` MUST pass `uvx pre-commit run --all-files`, `hatch run verify` (or `verify-static` + `verify-tests` with >=90% coverage), and `hatch run docs` before claiming completion or opening a PR. PR titles must follow Conventional Commits and merge via Squash and merge.

## Agent skills

### Issue tracker

GitHub issues (`fderuiter/imednet-toolkit`). See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical triage roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context (`CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.
