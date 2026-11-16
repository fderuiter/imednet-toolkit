# AGENTS.md — Contributor Guide

**Note:** Authoritative instructions for AI agents and human contributors working across **OpenAI Codex / ChatGPT**, **Claude Code**, and **Google Antigravity** in `imednet-toolkit`.

1. **Stack & Rules:** Follow dependencies, credential redaction, testing boundaries, and PR rules in `docs/reference/agent_rules.rst`.
2. **Verification:** Run local CI gates per `docs/how-to/verification.rst`: `uvx pre-commit run --all-files`, `hatch run verify` (`>=90%` coverage), `hatch run docs`, and `uvx hatch run quality:all packages` before opening PRs.
3. **Architecture:** Adhere to package layering in `docs/explanation/architecture.rst` and `tests/unit/architecture/import_rules.py`.

## Cross-Harness Agent Skills Workflow

All canonical skills live under `.agents/skills/` (`.agents/skills/README.md`, registered in `.agents/skills.json`) using harness-agnostic action vocabulary and 1:1 `disable-model-invocation` ↔ `agents/openai.yaml` (`policy.allow_implicit_invocation`) parity.

- **Bootstrap:** Start by checking `.agents/skills/using-superpowers/SKILL.md` and your harness adapter (`using-superpowers/references/{codex,claude-code,antigravity}-tools.md`). For an interactive workflow map, see `/workflow-guide`.
- **Claude Code local setup:** Run `python scripts/setup_agent_harnesses.py` once per checkout to create gitignored `.claude/skills` and `@AGENTS.md` `CLAUDE.md` bridges (never commit symlinks since `core.symlinks` may be `false` on Windows).
- **Single-PR Flow:** `brainstorming` (+ `grilling` / `domain-modeling`) → `using-git-worktrees` → `writing-plans` → `subagent-driven-development` or `executing-plans` (+ `test-driven-development`) → `requesting-code-review` + `receiving-code-review` → `verification-before-completion` → `finishing-a-development-branch`.
- **Multi-PR Epics & Triage:** Use `to-spec`, `to-tickets`, `wayfinder`, and `triage` backed by `docs/agents/{issue-tracker,triage-labels}.md`.
- **Diagnostics & Utilities:** `systematic-debugging`, `improve-codebase-architecture` + `codebase-design`, `diagnosing-superpowers`, `writing-skills`, `research`, `prototype`, `resolving-merge-conflicts`, `wizard`, `handoff`, `wait-what`, `to-questionnaire`.

## Project Overrides & Governance Precedence

- **Environment Setup:** Use `uv sync --locked --extra dev --extra docs` (never `poetry`, unpinned `pip`, or unlisted deps). On Windows PowerShell, use `pwsh` with `scripts/env.ps1` / `.venv`, and prefer Git Bash (`C:\Program Files\Git\bin\bash.exe`) over WSL `bash.exe`.
- **Workspace & Root Hygiene:** Use native harness worktrees or `.worktrees/`; save ephemeral artifacts, questionnaires, and handoffs under `.scratch/` or OS temp—never repo root (`scripts/check_root_files.py`).
- **Specs & Domain Model:** Store single-PR specs/plans in `docs/superpowers/{specs,plans}/` (excluded in `docs/conf.py`). Maintain `CONTEXT.md` + `docs/adr/` via `domain-modeling` and regenerate `docs/adr/index.rst` via `hatch run docs` (`docs/agents/domain.md`).
- **Triage Label Precedence:** `docs/agents/triage-labels.md` is authoritative for live GitHub labels and overrides legacy label spellings in frozen `docs/reference/{issue_management,triage_playbook}.rst` and `.github/ISSUE_TEMPLATE/*.yml`.
- **PRs & Merging:** PR titles must satisfy Conventional Commits (`Semantic PR Title` CI check) and integrate via **Squash and merge** (`release-please`).
