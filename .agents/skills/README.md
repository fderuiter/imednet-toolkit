# Project Agent Skills (`imednet-toolkit`)

Unified, Python-tailored skill portfolio merging [obra/superpowers](https://github.com/obra/superpowers) workflow discipline and [mattpocock/skills](https://github.com/mattpocock/skills) domain/GitHub engineering into **31** harness-agnostic modules under `.agents/skills/` (registered in `.agents/skills.json`).

## Harness Compatibility

- **OpenAI Codex / ChatGPT:** Reads root `AGENTS.md` and `.agents/skills/` natively; `agents/openai.yaml` enforces 1:1 `allow_implicit_invocation` parity on every skill.
- **Google Antigravity:** Reads `AGENTS.md`, `.agents/skills.json`, and `.agents/skills/` natively; platform built-ins (`swarm`, `automation`, `generative_ui`, `plugin`) compose alongside `using-superpowers/references/antigravity-tools.md`.
- **Claude Code:** Run `python scripts/setup_agent_harnesses.py` once per checkout to link gitignored `.claude/skills -> ../.agents/skills` (NTFS junction on Windows, symlink on POSIX) and `CLAUDE.md -> @AGENTS.md`. Verify with `--check` or remove cleanly with `--remove`.

---

## Core Lifecycle Skills

### Bootstrap, Routing & Meta
- **`using-superpowers`**: Mandatory pre-action skill discovery + harness tool adapters (`references/{antigravity,claude-code,codex}-tools.md`).
- **`/workflow-guide`**: Manual router mapping every situation to the right skill across the 31-skill suite.
- **`writing-skills`**: TDD authoring, context-load hierarchy (`references/agent-doc-principles.md`), bulletproofing, and contract tests for skills & `AGENTS.md`.
- **`diagnosing-superpowers`**: Transcript forensics (`path:line` evidence across Antigravity/Claude/Codex logs) + opt-in session retrospectives.

### Design, Domain & Planning
- **`brainstorming`**: Upfront design `<HARD-GATE>` saving single-PR specs to `docs/superpowers/specs/`.
- **`grilling`**: Decision-tree frontier interview primitive (`ask_question` or structured Q-rounds).
- **`domain-modeling`**: Updates `CONTEXT.md` and `docs/adr/` (with `generate_adr_index()` via `hatch run docs`).
- **`writing-plans`**: Detailed Python TDD implementation plans under `docs/superpowers/plans/`.
- **`/to-spec`**: Synthesize multi-PR epic spec to GitHub aligned with `.github/ISSUE_TEMPLATE/` and `docs/agents/triage-labels.md`.
- **`/to-tickets`**: Break multi-PR specs into tracer-bullet GitHub issues with native blocking edges (or `.scratch/<slug>/issues/`).
- **`/wayfinder`**: Multi-session foggy GitHub epic mapping (`type:epic` + live `status:*` labels).
- **`/improve-codebase-architecture`** & **`codebase-design`**: Deep-module architecture scans aligned with `docs/explanation/architecture.rst` and `tests/unit/architecture/import_rules.py`.

### Workspaces, Execution, Testing & Debugging
- **`using-git-worktrees`**: Isolated branch/worktree setup with `uv sync --locked --extra dev --extra docs` and `pytest` baseline.
- **`subagent-driven-development`**: Per-task fresh implementer + two-stage spec & quality review.
- **`executing-plans`**: Single-session inline plan execution sharing `.superpowers/sdd/` ledgers.
- **`dispatching-parallel-agents`**: Concurrent dispatch for independent streams (`Agent` / `spawn_agent` / `invoke_subagent` / `swarm`).
- **`test-driven-development`**: RED-GREEN-REFACTOR Python TDD (`pytest`, strict `respx`, `fake_data`, `>=90%` coverage, interface seams, anti-tautology).
- **`systematic-debugging`**: Redacted secrets, tight red loop, repro minimization, 3–5 ranked falsifiable hypotheses, root-cause tracing, 3-fix stop.

### Review, Verification & Branch Closure
- **`requesting-code-review`**: Per-task plan review or parallel two-axis Standards (12 Fowler smells + repo overrides) & Spec diff review.
- **`receiving-code-review`**: Technical evaluation and evidence-backed pushback on feedback.
- **`verification-before-completion`**: Requires fresh output from `uvx pre-commit run --all-files`, `hatch run verify`, `hatch run docs`, and `uvx hatch run quality:all packages`.
- **`finishing-a-development-branch`**: Verification gate + Conventional Commit PR creation (`.github/PULL_REQUEST_TEMPLATE.md`, Squash & merge, `release-please`).

---

## Triage & Utilities

- **`/triage`**: Issue triage state machine aligned with live GitHub labels (`docs/agents/triage-labels.md`) and GitHub-native closed `wontfix` memory.
- **`research`**: Background subagent investigation cited against primary sources (`docs/agents/` or `.scratch/`).
- **`prototype`**: Fast throwaway state/UI experiments under `.scratch/` or `prototype/<name>`.
- **`resolving-merge-conflicts`**: Intent-traced conflict resolution with full CI verification.
- **`wizard`**: Cross-OS (`scripts/wizards/*.sh` or `.ps1`) human-only credential/ops setup.
- **`/handoff`**: Portable cross-harness session summary in OS temp / `.scratch/handoffs/`.
- **`/wait-what`**: Simplified plain-English re-explanation grounded in `CONTEXT.md`.
- **`/to-questionnaire`**: External stakeholder discovery questionnaires under `.scratch/questionnaires/`.
