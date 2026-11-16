---
name: writing-skills
description: Use when creating or editing skills, modifying AGENTS.md or agent-facing docs, tuning skill triggers and context load, or testing skill behavior before deployment
---

# Writing Skills & Agent Instructions

Authoring skills and agent-facing documentation applies **TDD and progressive disclosure** together: watch an agent fail or misroute first, place guidance at the lowest effective tier of context load, match the prompt form to the failure mode, and verify both structural contracts and behavior across Codex, Claude Code, and Antigravity.

**Prerequisites:** Apply `test-driven-development` discipline for RED-GREEN verification, and read [references/agent-doc-principles.md](references/agent-doc-principles.md) before modifying `AGENTS.md`, adding manual vs model-invocable skills, or splitting documents.

---

## Core Architecture & Context Budgets

1. **Choose the right home before writing a skill:**
   - Repo-wide invariants & pointers -> `AGENTS.md` (`<10 KB`, strictly authoritative).
   - Reference truth synced with repo governance -> `docs/agents/*.md`.
   - Reusable task workflow across sessions/branches -> `.agents/skills/<name>/SKILL.md`.
   - Mechanical checks (frontmatter parity, file layout, lint) -> deterministic tests (`pytest`, `ruff`, `typos`), never prose.
2. **Progressive disclosure hierarchy:** Keep `SKILL.md` focused (`<500` lines); push heavy domain references, templates, or scripts into `references/` and `scripts/`, loaded only when their branch triggers.
3. **Dual-harness manual vs automatic parity (`imednet-toolkit` contract):**
   - **Canonical path:** `.agents/skills/<name>/SKILL.md` (names lowercase `[a-z0-9-]`, `<=64` chars, matching directory name) + `.agents/skills/<name>/agents/openai.yaml` (`interface.display_name`, `interface.short_description`).
   - **Manual-only skill:** Set `disable-model-invocation: true` in `SKILL.md` **AND** `policy.allow_implicit_invocation: false` in `agents/openai.yaml`. Strip trigger lists down to a crisp human-facing summary.
   - **Model-invocable skill:** Keep `description` starting with `"Use when..."` encoding distinct trigger conditions without summarizing the workflow steps (avoid trigger collisions across sibling skills), and match `allow_implicit_invocation: true` in `openai.yaml`.
4. **Cross-harness action language & OS portability:**
   - Express cross-skill and platform actions neutrally ("activate `<skill>`", "dispatch a subagent", "ask user", "track tasks"), keeping vendor-specific tool mappings confined strictly to `using-superpowers/references/*-tools.md`.
   - Invoke bundled scripts via explicit interpreter (`"${BASH:-bash}" scripts/tool.sh`, `python`, `node`), use Python examples matching `imednet-toolkit` (`pytest`, `respx`), and route transient outputs to `.scratch/` or OS temp—never repository root (`scripts/check_root_files.py`).

---

## Match Form to Failure Mode

See [references/agent-doc-principles.md](references/agent-doc-principles.md) and [references/bulletproofing.md](references/bulletproofing.md):

| Baseline failure | Right form | Wrong form |
| --- | --- | --- |
| Skips or bends a discipline rule under pressure | Clear Iron Law + rationalization table + red-flags list (`references/bulletproofing.md`) | Soft suggestions ("prefer...", "consider...") |
| Follows steps, produces wrong output shape | Positive recipe defining exact structure & sequence | Negative bans ("don't restate", "never narrate") |
| Omits a required output item | Required named slot/field in template | Narrative reminder outside template |
| Conditional behavior | Observable predicate ("when spec exists, verify against it") | Broad rule + exception prose clauses |

---

## TDD Cycle for Skill Creation & Edits (RED-GREEN-REFACTOR)

For official authoring patterns, trigger design, graphviz conventions, persuasion psychology, and subagent pressure testing, read only the references your change needs:
- Frontmatter & discovery optimization -> [references/discovery.md](references/discovery.md)
- Anthropic best practices -> [references/anthropic-best-practices.md](references/anthropic-best-practices.md)
- Bulletproofing & persuasion -> [references/bulletproofing.md](references/bulletproofing.md), [references/persuasion-principles.md](references/persuasion-principles.md)
- Flowcharts -> [references/flowcharts.md](references/flowcharts.md) (`render-graphs.js` / `references/graphviz-conventions.dot`)
- Subagent test harnesses -> [references/testing-skills-with-subagents.md](references/testing-skills-with-subagents.md)

### 1. RED — Baseline Before Editing
Run a pressure, application, or routing scenario in a fresh subagent **without** the new/updated skill guidance and capture verbatim baseline failure modes or trigger misses. For wording sensitivity on shaping prompts, run 5+ fresh-context micro-test reps against a no-guidance control and read matches manually.

### 2. GREEN — Minimal Targeted Skill Edit
Write concise instructions addressing the observed failures using positive structure or targeted bulletproofing, update `agents/openai.yaml`, add/update contract tests in `tests/unit/test_agent_skills_contract.py` when roster or invariants change, and verify the fresh subagent now complies cleanly.

### 3. REFACTOR — Close Loopholes & Verify Repo Gates
- Hunt new rationalizations or ambiguous trigger branches, tighten wording, and re-run subagent verification on discipline changes.
- Verify structural contract & hygiene checks before committing:
  - `pytest tests/unit/test_agent_skills_contract.py -v`
  - `uvx pre-commit run --all-files` (trailing whitespace, LF endings, valid YAML/JSON, clean `typos`, `check_root_files.py`, clean `ruff` if Python helpers changed)
  - `python scripts/setup_agent_harnesses.py --check` (if Claude bridge active)
