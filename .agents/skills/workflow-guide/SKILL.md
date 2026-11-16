---
name: workflow-guide
description: Unified workflow router explaining which agent skill fits your situation across Codex, Claude Code, and Antigravity.
disable-model-invocation: true
---

# `imednet-toolkit` Workflow Guide

One map across our unified **31-skill** suite (`obra/superpowers` discipline + `mattpocock/skills` domain/GitHub engineering + cross-harness adapters). Every session starts through `using-superpowers`.

---

## Standard Flow: Idea to PR

1. **Design Gate (`brainstorming`):** Interactive design exploration before touching code; pairs with `grilling` (multi-round decision-tree frontier) and `domain-modeling` (`CONTEXT.md` + `docs/adr/` + `generate_adr_index()`). Writes approved single-PR design docs to `docs/superpowers/specs/`.
2. **Optional Validation Detours:**
   - Need primary-source reading first? -> `research` (writes cited notes in `docs/agents/` or `.scratch/`).
   - Need to test state logic or UI behavior first? -> `prototype` (throwaway code in `.scratch/` or `prototype/<name>` branch).
   - Blocked on knowledge in someone else's head? -> `/to-questionnaire` (saves to `.scratch/questionnaires/`).
3. **Choose Planning Horizon:**
   - **Single feature / PR (default):** `writing-plans` -> bite-sized Python TDD plan in `docs/superpowers/plans/`.
   - **Multi-PR epic / backlog:** `/to-spec` (GitHub spec issue aligned with `.github/ISSUE_TEMPLATE/`) -> `/to-tickets` (vertical tracer-bullet GitHub issues with native blocking edges).
   - **Huge multi-session fog of war:** `/wayfinder` (maps `type:epic` decision tickets before collapsing into `/to-spec`).
4. **Isolated Workspace:** `using-git-worktrees` (`uv sync --locked --extra dev --extra docs`, `.venv` / `scripts/env.ps1`, baseline `pytest`).
5. **Implement:**
   - Parallel multi-task / strict review per task -> `subagent-driven-development` (uses `Agent` on Claude, `spawn_agent` on Codex, `invoke_subagent`/`swarm` on Antigravity).
   - Single-session inline execution -> `executing-plans`.
   - Independent parallel bug/test streams -> `dispatching-parallel-agents`.
   - Every slice follows `test-driven-development` (RED-GREEN-REFACTOR, public seams, `pytest`, strict `respx`, `fake_data`, `>=90%` coverage).
6. **Review, Verify & Ship:**
   - `requesting-code-review` (per-task alignment OR parallel two-axis Standards + Spec diff review) + `receiving-code-review`.
   - `verification-before-completion` (`uvx pre-commit run --all-files`, `hatch run verify`, `hatch run docs`, `uvx hatch run quality:all packages`).
   - `finishing-a-development-branch` (Conventional Commit PR title, `.github/PULL_REQUEST_TEMPLATE.md`, Squash & merge, `release-please`).

---

## On-Ramps & Specialized Diagnostics

- **Incoming GitHub issues** -> `/triage` (live labels from `docs/agents/triage-labels.md`, `READY` agent briefs, GitHub-native closed `wontfix` memory).
- **Bug, flake, or regression** -> `systematic-debugging` (redacted secrets, tight red loop, minimized repro, 3–5 ranked falsifiable hypotheses, root-cause tracing, 3-fix stop).
- **Architecture deepening scan** -> `/improve-codebase-architecture` + `codebase-design` (deep modules vs `docs/explanation/architecture.rst` boundaries & `import_rules.py`).
- **Session forensics or retrospective** -> `diagnosing-superpowers` (`path:line` transcript analysis across Antigravity/Claude/Codex + optional retrospective improvements).
- **Mid-conflict merge/rebase** -> `resolving-merge-conflicts`.

---

## Session, Clarification & Human-in-the-Loop Utilities

- **Explain that last reply simply** -> `/wait-what`.
- **Switch harness / directory / colleague cleanly** -> `/handoff` (writes portable `.scratch/handoffs/` or OS temp summary).
- **Manual secrets / dashboard setup steps** -> `wizard` (`scripts/wizards/*.sh` or `.ps1`).
- **Author or tune skills / `AGENTS.md`** -> `writing-skills` (context load ladder, `openai.yaml` parity, behavioral testing).
- **Local Claude Code bridge setup** -> run `python scripts/setup_agent_harnesses.py` once per checkout.

---

## Phase Transitions (Context Hygiene)

Between major phases (e.g., design -> implementation -> review), choose top-to-bottom:
1. **Continue in session** when context holds sufficient room and direct reasoning is load-bearing.
2. **Dispatch a subagent** for self-contained verification, code review, or primary-source reads.
3. **`/handoff`** only when changing harnesses, worktrees, or collaborators.
4. **Compact / clear** when switching to unrelated work.
