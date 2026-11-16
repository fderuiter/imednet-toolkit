---
name: using-superpowers
description: Use when starting any conversation - establishes how to discover and activate skills before ANY response, clarifying question, or file edit across Codex, Claude Code, and Antigravity
---

<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific scoped task, skip this bootstrap skill unless instructed otherwise.
</SUBAGENT-STOP>

<EXTREMELY-IMPORTANT>
If there is even a 1% chance a skill applies to what you are doing, read and follow that skill BEFORE answering or editing files.
</EXTREMELY-IMPORTANT>

## The Core Discipline

**Activate relevant or requested skills BEFORE any response or action** — including clarifying questions, exploring the codebase, or checking files. If a skill turns out inapplicable after reading it, move on cleanly.

- **Before implementation or planning:** if design decisions are not yet settled and approved, activate `brainstorming` first.
- **When activating a skill:** read its `SKILL.md`, announce `"Using <skill> to <purpose>"`, and follow its steps in order (using your harness's task tracker if multi-step).

## Skill Priority & Cohesive Topology

When multiple skills apply, **process skills run first**, then domain/implementation skills, then verification/review skills:

1. **Upfront design / bug root cause:** `brainstorming` (features/changes), `systematic-debugging` (bugs/failures/slowness), `improve-codebase-architecture` (structural deepening), or `research` (primary-source lookup).
2. **Planning:** `writing-plans` (`docs/superpowers/plans/`) for single-PR changes, or `to-spec` / `to-tickets` / `wayfinder` for multi-PR GitHub epics.
3. **Execution:** `using-git-worktrees` -> `subagent-driven-development` / `executing-plans` / `dispatching-parallel-agents` + `test-driven-development` + `domain-modeling` / `codebase-design`.
4. **Closure:** `requesting-code-review` -> `receiving-code-review` -> `verification-before-completion` -> `finishing-a-development-branch`.
5. **Discovery:** When unsure which manual workflow fits, direct the user to `workflow-guide`.

## Anti-Rationalization Checks

| Rationalization | Reality |
| --- | --- |
| "This is just a simple question" | Questions are tasks. Check for skills first. |
| "I need more context or git checks first" | Skills govern HOW to explore and gather context. |
| "I remember how this skill works" | Skills evolve and are repo-tailored. Read the current `SKILL.md`. |
| "This skill looks like overkill" | Small tasks hide coupling and edge cases. Follow the skill. |
| "I'll run one quick command first" | Check skills BEFORE running commands or writing code. |

## Cross-Harness Tool Adapter Seam

Skills use harness-neutral action phrasing ("activate `<skill>`", "read file", "edit file", "dispatch subagent", "ask user", "track tasks"). Translate those actions using your active harness adapter reference:

- **Google Antigravity:** `references/antigravity-tools.md`
- **Claude Code:** `references/claude-code-tools.md`
- **OpenAI Codex / ChatGPT:** `references/codex-tools.md`
- **Other harnesses (Gemini CLI, Pi, Hermes, Muse):** check sibling `references/*-tools.md` if applicable.

## Precedence

Repository `AGENTS.md`, direct human requests, `docs/reference/agent_rules.rst`, `docs/how-to/verification.rst`, `docs/explanation/architecture.rst`, and `docs/agents/triage-labels.md` take precedence over generic defaults inside skills.
