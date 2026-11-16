# Project Agent Skills

Installed from [obra/superpowers](https://github.com/obra/superpowers) and [mattpocock/skills](https://github.com/mattpocock/skills) for use with AI coding agents (Google Antigravity, Claude Code, Codex, Gemini CLI, OpenCode, etc.).

All skills are registered in `.agents/skills.json` and discovered under `.agents/skills/<skill-name>/`. Antigravity users may also install the global `Superpowers` plugin for session-start hooks while keeping `.agents/skills/` as the repository-level source of truth across all harnesses.

---

## Superpowers Methodology (`obra/superpowers`)

End-to-end software development workflow built around composable, auto-triggering skills. See `AGENTS.md` for skill precedence and `imednet-toolkit` project overrides (`uv sync`, `.worktrees`, `docs/superpowers/`, and CI verification gates).

### Bootstrap & Meta

- **`using-superpowers`**: Session bootstrap establishing skill discovery and invocation discipline (includes platform tool mappings such as `references/antigravity-tools.md`).
- **`writing-skills`**: TDD-style discipline for authoring, testing, and bulletproofing new or updated skills.
- **`diagnosing-superpowers`**: Investigate session transcripts when a Superpowers workflow misfires or stalls.

### Design, Planning & Execution

- **`brainstorming`**: Interactive design refinement before writing code; produces approved specs under `docs/superpowers/specs/`.
- **`using-git-worktrees`**: Set up an isolated workspace (native worktree tools or `.worktrees/`) before executing feature work.
- **`writing-plans`**: Break approved specs into bite-sized, unambiguous TDD implementation tasks under `docs/superpowers/plans/`.
- **`subagent-driven-development`**: Execute plans by dispatching a fresh implementer subagent per task, followed by spec-compliance and code-quality reviews.
- **`executing-plans`**: Inline task-by-task plan execution in the current session with a final whole-branch review.
- **`dispatching-parallel-agents`**: Coordinate concurrent subagents across 2+ independent tasks without shared state.

### Quality, Debugging & Completion

- **`test-driven-development`**: Strict RED-GREEN-REFACTOR discipline with testing anti-patterns guidance.
- **`systematic-debugging`**: 4-phase root-cause investigation before attempting fixes (root-cause tracing, defense-in-depth, condition-based waiting).
- **`requesting-code-review`**: Dispatch a fresh code-reviewer subagent between tasks or before merging.
- **`receiving-code-review`**: Technical evaluation and verification of review feedback before implementing changes.
- **`verification-before-completion`**: Require fresh command output (`uvx pre-commit run --all-files`, `hatch run verify`, `hatch run docs`) before any completion claim.
- **`finishing-a-development-branch`**: Verify tests and guide merge/PR/cleanup decisions once implementation is complete.

---

## Engineering & Productivity Skills (`mattpocock/skills`)

### Skill Router

When unsure which `mattpocock/skills` entry to reach for, use:

- **`/ask-matt`**: Explains the full workflow topology and directs you to the right skill or on-ramp.

### Engineering Skills — User-Invoked

- **`/grill-with-docs`**: Grilling session that builds your domain model in `CONTEXT.md` and ADRs.
- **`/implement`**: Build work described by spec/tickets test-first and review before committing.
- **`/improve-codebase-architecture`**: Scan codebase for deepening opportunities.
- **`/setup-matt-pocock-skills`**: Configure this repo for engineering skills (issue tracker, triage labels, domain docs).
- **`/to-spec`**: Turn conversation into a spec and publish to the issue tracker.
- **`/to-tickets`**: Break specs/plans into tracer-bullet tickets with blocking edges.
- **`/triage`**: Move issues through a state machine of triage roles.
- **`/wayfinder`**: Chart and resolve large, foggy efforts as decision tickets.

### Engineering Skills — Model-Invoked / Automated

- **`code-review`**: Two-axis review (Standards + Spec) of diffs before committing.
- **`codebase-design`**: Deep module and clean seam design discipline.
- **`diagnosing-bugs`**: Disciplined root-cause isolation and regression testing.
- **`domain-modeling`**: Build and maintain project domain model and ADRs.
- **`prototype`**: Build throwaway prototypes to answer design questions.
- **`research`**: Investigate questions against primary sources via background agent.
- **`resolving-merge-conflicts`**: Intent-traced git conflict resolution.
- **`tdd`**: Test-driven development with red-green-refactor loop.
- **`wizard`**: Interactive wizard script for human-only operational steps.

### Productivity Skills

- **`/grill-me`**: Relentless interview to resolve design decisions (stateless).
- **`/handoff`**: Compact conversation into a handoff document for another session/agent.
- **`/teach`**: Stateful multi-session learning workspace.
- **`/to-questionnaire`**: Turn decisions into questionnaires for external stakeholders.
- **`/wait-what`**: Clarify and re-pitch misunderstood agent responses.
- **`grilling`**: Reusable interview primitive across decision trees.
- **`writing-for-agents`**: Discipline for authoring agent-readable documents and skills.

### In-Progress & Misc Skills

- **`claude-handoff`**: Context compaction for Claude Code.
- **`implement-spec`**: Multi-agent spec execution across ticket task graphs.
- **`loop-me`**: Feedback loop driver.
- **`retro`**: Analyze session transcripts to extract systemic improvements.
- **`setup-ts-deep-modules`**: Deep module boundary enforcement for TypeScript.
- **`writing-beats`**: Assemble raw material into a narrative journey of beats.
- **`writing-fragments`**: Mine raw writing fragments into a single quarry file.
- **`writing-shape`**: Shape raw material into an article paragraph by paragraph.
- **`git-guardrails-claude-code`**: Safety guardrails blocking destructive git commands.
- **`migrate-to-shoehorn`**: Migration tool to shoehorn architecture.
- **`scaffold-exercises`**: Scaffolding coding exercises.
- **`setup-pre-commit`**: Configure pre-commit hooks.
