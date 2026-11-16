# Cross-Harness Agent Skills & Governance Design (`imednet-toolkit`)

**Date:** 2026-11-16
**Target PR Branch:** `remove_matts_skills` (synced to `origin/main` `4c856c34`)
**Research Provenance:** [skills-synergy-research.md](../../agents/skills-synergy-research.md)

---

## 1. Intent & Why

On `origin/main` (`4c856c34`), `.agents/skills/` contains **52** skills side-by-side (`15` unmodified `obra/superpowers` directories + `37` `mattpocock/skills` directories), creating severe context load, conflicting trigger descriptions, contradictory workflow rules (e.g., TDD refactoring and seam approval), TypeScript/Husky instructions that violate Python ADRs, leaked Claude-specific tool calls across **14+** skills, broken Windows shell behavior, and drift from `imednet-toolkit`'s live GitHub labels, CI plan, and PR workflow.

This design turns `.agents/skills/`, `AGENTS.md`, `docs/agents/*`, and thin repo tooling helpers into **one unified, harness-agnostic, Python-tailored engineering system** (`52 -> 31` skills) that operates cleanly across **OpenAI Codex / ChatGPT**, **Claude Code**, and **Google Antigravity**.

---

## 2. Goals, Non-Goals & Anti-Goals

### Goals
- Eliminate irrelevant TypeScript, prose-writing, personal-learning, and Claude-locked skills plus shallow pass-through wrappers (**16 deleted** outright).
- Merge colliding Superpowers and Matt workflows (**5 twins merged** + `ask-matt` replaced by `workflow-guide`) into single deep modules (`31` total skills) combining both suites' best practices.
- Tailor all retained skills to `imednet-toolkit` (`Python >=3.11`, `uv`, `hatch`, `pytest`, strict `respx`, `mypy`, `ruff`, `typos`, Sphinx `generate_adr_index()`, `>=90%` coverage, Conventional Commits, Squash & merge, `release-please`, Windows + Linux).
- Enforce harness-agnostic action interfaces backed by per-harness adapters for ChatGPT/Codex, Claude Code, and Antigravity, with `100%` `disable-model-invocation` ↔ `agents/openai.yaml` parity.
- Seal repo/CI/governance seams (`docs/agents/*`, `scripts/ci_plan.py`, `.gitignore`, `scripts/check_root_files.py`, and Windows-safe helper scripts).

### Non-Goals
- Refactoring Python runtime packages (`packages/core`, `packages/plugins-workflows`, `packages/providers-airflow`, `packages/plugins-sinks`).
- Fixing unrelated stale `.rst` references in `docs/reference/*.rst` or `CONTRIBUTING.md` (`typer`, `black`, `isort`, `hatch.lock` — deferred to a dedicated follow-up docs PR).
- Making skills standalone ZIP bundles for external web/desktop chats lacking repository access (`ChatGPT`/`Claude.ai` outside git checkouts).
- Modifying files outside this repository (such as broken global NTFS junctions in `~/.gemini/config/skills/` or global plugins).

### Anti-Goals
- Tracked git symlinks (`.claude/skills -> ../.agents/skills`, `CLAUDE.md -> AGENTS.md`), which silently check out as plain text files on Windows where `git config core.symlinks` is `false`.
- Editing `SKILL.md` bodies with harness-specific tool names (`Skill tool`, `TodoWrite`, `write_to_file`) instead of keeping dialect translation inside adapters.
- Retaining duplicate model-invocable triggers for the same user intent.

---

## 3. Hard Constraints

1. **Discovery & Budgets:**
   - Canonical skills live under `.agents/skills/<name>/SKILL.md`, registered in `.agents/skills.json`.
   - `AGENTS.md` stays concise (`<10 KB`; strictly below Codex's 32 KiB limit and Antigravity's 24,000-byte per-file / 20k-token total rules budget).
   - Every retained skill must satisfy the [Agent Skills Spec](https://agentskills.io/specification) (`name` matches directory, concise `description`), with dual invocation gating (`disable-model-invocation: true` in `SKILL.md` paired 1:1 with `policy.allow_implicit_invocation: false` in `agents/openai.yaml`).
2. **Pre-Commit & CI Constraints:**
   - `scripts/check_root_files.py` rejects unapproved files on disk at repo root.
   - `.pre-commit-config.yaml` runs `trailing-whitespace`, `end-of-file-fixer` (LF endings), `check-yaml`, `check-json`, `typos` (`typos.toml`), and `ruff` / `ruff-format` across `.agents/` and `scripts/` (`pyproject.toml` lints all `.py` files outside `sandbox/` under strict `D` Google docstrings, `S` Bandit, `PL`, `SLF`, `I`).
   - `docs/conf.py` already excludes `agents/**` and `superpowers/**`. `scripts/build_docs.py` auto-generates `docs/adr/index.rst` from `docs/adr/\d{4}-.+\.md` files (each requiring `# <Title>`).
3. **Windows & Linux Parity:**
   - Shell scripts calling sibling scripts use `"${BASH:-bash}"` (`writing-skills` rule), avoid `/tmp/` hardcodes where `.scratch/` or OS temp lookup applies, and Python tooling notes `scripts/env.ps1` + `.venv` on Windows (`where.exe bash` may hit WSL bash before Git Bash).

---

## 4. Approaches Considered & Decisions Made

### Q1 — Cross-Harness Scope & Discovery Bridge
- *Considered:* (A) Repo coding agents only with canonical `.agents/skills/` + `AGENTS.md` + local bridge script for Claude; (B) Tracked `.claude/skills` copies + `CLAUDE.md` in git; (C) Repo + self-contained web/desktop `.zip` bundles.
- *Picked:* **Option A**. Avoids duplicate tracked copies and broken Windows git symlinks while giving Codex and Antigravity native discovery and giving Claude Code a single-command local setup script (`scripts/setup_agent_harnesses.py`) creating gitignored `.claude/skills` (Windows junction / POSIX symlink) + `CLAUDE.md` (`@AGENTS.md`).

### Q2 — Ownership & Unification of `obra/superpowers` vs `mattpocock/skills`
- *Considered:* (A) Unify & tailor in `.agents/skills/` (prune alien/shallow skills, merge colliding pairs, tailor to Python/CI/3 harnesses); (B) Keep 15 upstream Superpowers `SKILL.md` verbatim + non-overlapping Matt skills; (C) Externalize Superpowers to plugins.
- *Picked:* **Option A**. Eliminates TS/Husky/npm mismatches, tool leaks, and conflicting rules while keeping Priority 1 repo precedence deterministic over global plugins.

### Q3 — Ideation, Spec & Planning Topology
- *Considered:* (A) Dual-horizon split; (B) Repo-markdown only; (C) GitHub-issue only.
- *Picked:* **Option A**. `brainstorming` is the single upfront design `<HARD-GATE>` (using `grilling`'s decision-tree frontier + `domain-modeling` inline updates to `CONTEXT.md`/`docs/adr/` when appropriate). Immediate/single-PR builds output `docs/superpowers/specs/` + `writing-plans` -> `docs/superpowers/plans/`; multi-PR epics output via `to-spec` / `to-tickets` (and `wayfinder` for foggy multi-session exploration) aligned to `.github/ISSUE_TEMPLATE/` and real GitHub labels.

### Q4 — TDD & Refactoring Discipline
- *Considered:* (A) Unified `test-driven-development` (RED-GREEN-REFACTOR inside every cycle + Matt's seam/anti-tautology/vertical-slice rules + Python/`pytest`/`respx` examples); (B) Matt's strict seam pre-approval gate + no refactor in loop; (C) Superpowers-only without seam/anti-tautology guidance.
- *Picked:* **Option A**. Keeps the codebase clean inside every red-green cycle while enforcing behavior/seam tests and only pausing to confirm seams when the public boundary is genuinely ambiguous.

### Q5 — Execution & Parallelism Model
- *Considered:* (A) Retire `implement` & `implement-spec`, standardize on Superpowers execution skills (`executing-plans`, `subagent-driven-development`, `dispatching-parallel-agents`) adapted to Claude `Agent`, Codex `spawn_agent`, and Antigravity `invoke_subagent` / `swarm`; (B) Retain `implement-spec` alongside SDD.
- *Picked:* **Option A**. Eliminates overlapping execution entrypoints while mapping isolated parallel streams cleanly per harness.

### Q6 — Borderline Utilities & Router
- *Considered:* (A) Drop personal/life-loop skills (`teach`, `loop-me`), keep practical engineering helpers (`to-questionnaire` redirected to `.scratch/`, `wait-what`, `handoff`, `wizard`, `prototype`, `research`, `resolving-merge-conflicts`, `improve-codebase-architecture`, `codebase-design`, `domain-modeling`, `triage`), replace `ask-matt` with unified manual router **`workflow-guide`**; (B) Strict engineering core only; (C) Keep `teach`/`loop-me` redirected to `.scratch/`.
- *Picked:* **Option A**.

### Q7 — Infrastructure & Governance Scope in This PR
- *Considered:* (A) Focused PR (`AGENTS.md`, `.agents/`, `docs/agents/*`, `.gitignore`, `scripts/ci_plan.py` + tests, `scripts/check_root_files.py`, `scripts/setup_agent_harnesses.py` + tests; defer stale `docs/reference/*.rst` to follow-up); (B) Bundle `docs/reference/*.rst` fixes here; (C) Strictly skills/docs only without `scripts/`.
- *Picked:* **Option A**.

---

## 5. Detailed Architecture & Component Design

### 5.1 Harness Adapter Seam (Candidate 1)
- **Root Instruction Contract ([AGENTS.md](../../../AGENTS.md)):** Keep concise (`<10 KB`), strictly authoritative:
  1. Core doc pointers (`docs/reference/agent_rules.rst`, `docs/how-to/verification.rst`, `docs/explanation/architecture.rst`).
  2. Unified skill bootstrap (`using-superpowers`), dual-horizon planning rule (`docs/superpowers/` vs GitHub epics in `docs/agents/*`), and Antigravity/Codex/Claude runtime notes (including `workflow-guide` router).
  3. Repo-mandatory overrides: `uv sync --locked --extra dev --extra docs`, `scripts/env.ps1` + `.venv` on Windows (avoiding WSL bash / WindowsApps stub), `.worktrees/` fallback vs native worktrees, `.scratch/` for transient outputs (never repo root), Conventional Commits (`Semantic PR Title`), Squash & merge, `release-please`, and mandatory local CI gates (`uvx pre-commit run --all-files`, `hatch run verify`, `hatch run docs`).
- **Action-Only Skill Interface:** Rewrite all **14+** `"Call the Skill tool"` / `"Use Skill"` / `TodoWrite` / `write_to_file` / `claude --bg` references inside active `SKILL.md` files to harness-neutral action phrases (`"Activate the `<name>` skill"`, `"dispatch a subagent"`, `"track task state"`).
- **Per-Harness Adapter References (`using-superpowers/references/`):**
  - Update [antigravity-tools.md](../../../.agents/skills/using-superpowers/references/antigravity-tools.md): replace retired `write_to_file` / `replace_file_content` / `ArtifactMetadata.ArtifactType` with current Antigravity tools (`read_file`, `write_file`, `edit_file`, `ArtifactMetadata: {Summary, UserFacing, RequestFeedback}`, `ask_question`, `invoke_subagent` with `Workspace: "inherit"|"branch"|"share"`, `swarm` with `workspace: "share"` for multi-author collaboration, `schedule`), plus how Antigravity built-ins (`automation`, `generative_ui`, `swarm`, `plugin`, `ui-plugin-navigation`) compose cleanly with repo skills without duplication.
  - Refine `claude-code-tools.md` & `codex-tools.md`: remove outdated/self-referential assumptions, document `.claude/skills` / `CLAUDE.md` local bridge for Claude Code and `agents/openai.yaml` + Codex App/Sandbox detached-HEAD handling for ChatGPT/Codex.
- **Claude Local Bridge (`scripts/setup_agent_harnesses.py`):**
  - Cross-platform CLI creating `.claude/skills -> ../.agents/skills` (NTFS directory junction via `cmd /c mklink /J` or Python/Win32 on Windows without elevation; `os.symlink` on POSIX) and `.gitignore`d `CLAUDE.md` containing `@AGENTS.md` (with `--check`, `--dry-run`, and `--remove` modes, zero external dependencies).
  - Update `.gitignore` to ignore `.claude/` and `CLAUDE.md` (not `.out-of-scope/`, since `triage` uses GitHub-native `wontfix` memory per Builder Check #2).
  - Add `"CLAUDE.md"` to `approved_files` in `scripts/check_root_files.py` so locally generated ignored `CLAUDE.md` never triggers root-file sanitization failures on pre-commit, while keeping non-approved root files rejected.

### 5.2 Complete 52 → 31 Skill Portfolio Disposition

#### Deleted Outright (16 skills)
- **Incompatible / Wrong Stack / Obsolete (10):** `claude-handoff`, `git-guardrails-claude-code`, `migrate-to-shoehorn`, `scaffold-exercises`, `setup-matt-pocock-skills`, `setup-pre-commit`, `setup-ts-deep-modules`, `writing-beats`, `writing-fragments`, `writing-shape`.
- **Personal / Non-Repo Workspaces (2):** `loop-me`, `teach`.
- **Shallow Wrappers & Superseded Entrypoints (4):** `grill-me`, `grill-with-docs`, `implement`, `implement-spec`.

#### Merged Twins (5 deleted after folding best capabilities into survivor)
1. **`tdd` → `test-driven-development`:** Preserves Superpowers RED-GREEN-REFACTOR loop + iron-law test-first discipline; folds in Matt's public-seam targeting, anti-tautology (`independent oracle`), implementation-coupling rules, and vertical slicing; replaces JS/TS snippets with `pytest`, strict `respx`, `imednet.testing.fake_data`, `>=90%` coverage; updates `writing-good-tests.md`. (Pause for seam confirmation only when interface is genuinely unsettled.)
2. **`diagnosing-bugs` → `systematic-debugging`:** Preserves 4-phase root-cause investigation, root-cause tracing, defense-in-depth, condition-based waiting, and 3-fix architectural stop; folds in secret redaction (`<REDACTED>`), aggressive Phase 1 tight red feedback loop + minimization, 3–5 ranked falsifiable hypotheses checkpoint, `[DEBUG-xxxx]` cleanup hygiene, and cross-OS HITL loop guidance; converts TS/JS/npm examples (`condition-based-waiting-example.*`, `find-polluter`) to Python/`pytest`/`asyncio`.
3. **`code-review` → `requesting-code-review`:** Combines per-task/pre-merge plan alignment review with Matt's **two-axis parallel Standards + Spec** architecture (embedding the 12 Fowler smell heuristics with `docs/reference/agent_rules.rst` & `docs/explanation/architecture.rst` overrides); fixes `/tmp/` hardcoding to `.scratch/` or OS temp; replaces `Subagent` / tool assumptions with action phrasing.
4. **`writing-for-agents` → `writing-skills`:** Reconciles positive target prompting with discipline bulletproofing (positive target + hard guardrail only where needed), incorporates context load vs cognitive load, 3-tier progressive disclosure, single source of truth vs environment caching, and `SKILL-MECHANICS` invocation/router principles (`references/agent-doc-principles.md`); enforces `agentskills.io` + `openai.yaml` + repo Ruff/`typos`/CI rules.
5. **`retro` → `diagnosing-superpowers`:** Keeps `diagnosing-superpowers` read-only transcript forensics (`path:line` citation discipline) as the default mode, and adds an explicit **Retrospective / Improvement Proposal** mode (activated on request / "retrospective") allowing evidence-backed proposals across `AGENTS.md`, skills, automated checks, and tool economy; drops JS/Claude-only path leaks (`references/session-discovery.md` updated to document Claude Code, Codex, and Antigravity transcripts!).

#### Replaced Router (1:1)
- **`ask-matt` → `workflow-guide`** (`disable-model-invocation: true`, `allow_implicit_invocation: false`): Concise human-facing map of the 31 unified skills across the whole flow (Ideation/Architecture -> Planning/Issue Graph -> Execution/TDD/Debug -> Review/Verification/PR Finish -> Utilities).

#### Retained & Tailored Core Skills (25 skills, bringing total to 31)
- `using-superpowers`: Action-only bootstrap referencing `references/{antigravity,claude-code,codex}-tools.md` + Antigravity built-in boundaries.
- `brainstorming`: Upfront design `<HARD-GATE>`, optional `grilling` frontier / `domain-modeling` / Antigravity `ask_question`, saves to `docs/superpowers/specs/`; visual companion notes `scripts/` + Windows Git Bash note.
- `grilling`: Action-neutral decision-tree interview primitive; notes Antigravity `ask_question` vs markdown Q-round formats cleanly; keeps `disable-model-invocation: false` + `openai.yaml` `allow_implicit_invocation: true` so other skills can call it.
- `domain-modeling`: `CONTEXT.md` + `docs/adr/` + mandatory `hatch run docs` / `generate_adr_index()` regeneration & H1 `# <Title>` check.
- `using-git-worktrees`: `uv sync --locked --extra dev --extra docs`, `scripts/env.ps1` / `.venv`, `pytest` baseline, linked worktree & harness native worktree support.
- `writing-plans`: Python `pytest` & `ruff`/`mypy` step templates, Conventional Commit commit examples, `docs/superpowers/plans/`, handoff to SDD or inline execution.
- `to-spec` (`DMI`): Aligned with `.github/ISSUE_TEMPLATE/` sections (`Problem`, `Outcome`, `Constraints / Non-goals`, `Acceptance criteria`, `Test / Docs / API impact`, `Verification`), `docs/reference/issue_management.rst` `<type>(<area>): <outcome>` titles, and live GitHub labels mapped via canonical `docs/agents/triage-labels.md` (`ready-for-agent`, `status:ready`, `type:*`, `area:*`, explicitly overriding stale `.rst`/template label spellings).
- `to-tickets` (`DMI`): Multi-PR tracer-bullet ticket decomposition + `expand-contract` exception aligned with live GitHub labels (`docs/agents/triage-labels.md` precedence) & `<type>(<area>): ...` naming; local fallback under `.scratch/<feature>/issues/`.
- `wayfinder` (`DMI`): Multi-session foggy GitHub epic mapping using real GH labels (`type:epic`, `type:research`, `status:needs-design`, `status:blocked`, `status:ready`) instead of non-existent `wayfinder:*` labels.
- `triage` (`DMI`): Uses real GH labels mapped canonically in `docs/agents/triage-labels.md` (overriding legacy `.rst` / template label strings), uses GitHub-native `wontfix` memory (`gh issue list --label wontfix --state closed` search + structured rejection rationale directly in GitHub closing comments; retire `.out-of-scope/` and delete `triage/OUT-OF-SCOPE.md`), uses `gh` JSON conventions, and uses action-only sub-skill references.
- `subagent-driven-development`: Cross-harness dispatch guidance (`Agent` / `spawn_agent` / `invoke_subagent` / `swarm`), fix Windows script execution (`${BASH:-bash}`), include `hatch run verify` / `docs` quality gates in final review.
- `executing-plans`: Fix `scripts/task-start` line 26 and `scripts/task-done` line 28 to invoke `"${BASH:-bash}"` consistently; Windows Git Bash note.
- `dispatching-parallel-agents`: Cross-harness action phrasing; notes `pytest` scoped parallel troubleshooting cleanly.
- `receiving-code-review`: Tailored `gh pr` commands + Python `pytest` re-verification; `openai.yaml` added.
- `verification-before-completion`: Names exact canonical gates (`uvx pre-commit run --all-files`, `hatch run verify`, `hatch run docs`, `uvx hatch run quality:all packages`) + Windows `scripts/env.ps1`.
- `finishing-a-development-branch`: Reorders PR path as primary norm (Conventional Commits for `Semantic PR Title`, `.github/PULL_REQUEST_TEMPLATE.md`, Squash & merge, `release-please` note, `uvx pre-commit run --all-files`, `hatch run verify`, `hatch run docs`), warns that `main` enforces PR checks for local merge option, handles Codex/worktree environments cleanly.
- `improve-codebase-architecture` (`DMI`): Explores `packages/` + `tests/unit/architecture/import_rules.py` + `CONTEXT.md`/`docs/adr/`, drops hardcoded `Call the Skill tool`, keeps OS temp HTML report (noting Antigravity artifact/browser distinction).
- `codebase-design`: Replaces TypeScript examples with idiomatic Python (`Protocol`, dataclass, dependency injection) and explicitly harmonizes glossary terms (`module`/`interface`/`seam`) with `docs/explanation/architecture.rst`.
- `research`: Background subagent research saved under `docs/agents/` (Sphinx-excluded) or `.scratch/`, cited from primary sources.
- `prototype`: Throwaway prototype in `.scratch/` or `prototype/<name>` branch using Python/`uv` conventions.
- `resolving-merge-conflicts`: Keeps intent-based resolution, notes `git diff --check` + `pytest` + `scripts/build_docs.py` check.
- `wizard`: Cross-OS interactive operator setup (supports PowerShell `.ps1` alongside bash `.sh` matching `scripts/wizards/`).
- `handoff` (`DMI`): Harness-agnostic handoff saved to OS temp / `.scratch/handoffs/` recommending cross-harness skill names (not `"Skill tool"`).
- `wait-what` (`DMI`): Clean clarification skill referencing `CONTEXT.md`.
- `to-questionnaire` (`DMI`): Writes to `.scratch/questionnaires/to-questionnaire-<slug>.md` (never repo root).

#### OpenAI Metadata Parity
- **Every one of the 31 skills** carries `agents/openai.yaml` with `interface.display_name`, `interface.short_description`, and exact `policy.allow_implicit_invocation: false` parity wherever `disable-model-invocation: true` is set (`policy.allow_implicit_invocation: true` or omitted where model-invoked).

### 5.3 Governance, Scripts & CI Contract Updates (Candidate 4)
1. **`docs/agents/triage-labels.md` (Canonical Precedence Over Legacy Docs):**
   - Explicitly declare that until the follow-up `.rst` docs cleanup PR lands, `docs/agents/triage-labels.md` takes **precedence** over mismatched legacy label strings in `docs/reference/{issue_management,triage_playbook}.rst` and `.github/ISSUE_TEMPLATE/*.yml` (recording the exact remapping: `status:needs-info` → `question`, `type:maintenance` → `type:tech-debt`, `area:workflows-plugin` → `area:workflows`, `area:airflow-provider` → `area:airflow`, `risk:medium/high` → omit nonexistent risk labels or note in priority/body).
   - Sync triage role mapping table to live GitHub labels:
     - Role `needs-triage` → `status:triage`
     - Role `needs-info` → `question` (+ triage comment noting reporter input needed; optionally pair `status:blocked` if waiting)
     - Role `ready-for-agent` → `ready-for-agent` (paired with `status:ready`)
     - Role `ready-for-human` → `status:needs-design` (for design/judgment) or `status:ready` + `help wanted` (without `ready-for-agent`)
     - Role `wontfix` → `wontfix` (or `duplicate` / `invalid`)
     - Category roles → `type:bug`, `type:enhancement`, `type:feature`, `type:refactor`, `type:docs`, `type:ci`, `type:test`, `type:tech-debt`, `type:security`, `type:epic`, `type:research`.
2. **`docs/agents/issue-tracker.md`:** Align creation commands with `<type>(<area>): <concise outcome>` titles and `.github/ISSUE_TEMPLATE/` sections; update `Wayfinding operations` to use real labels (`type:epic`, `type:research`, `status:needs-design`, `status:blocked`, `status:ready`) instead of non-existent `wayfinder:*` labels; document GitHub-native `wontfix` closed-issue search and structured closing comments replacing `.out-of-scope/`.
3. **`docs/agents/domain.md`:** Document `scripts/build_docs.py` `generate_adr_index()` requirement (`docs/adr/\d{4}-.+\.md` starting with `# <Title>`, regenerating `docs/adr/index.rst` via `hatch run docs`) and remove stale `src/` mock layout bias where `packages/` applies.
4. **`scripts/ci_plan.py` & `tests/unit/test_ci_plan.py`:** Extract pure helpers (`_is_non_runtime_path` and `plan_ci_impact`) so paths starting with `.agents/` (e.g., `.agents/skills/*/agents/openai.yaml`, shell/js helpers) do **not** trigger full 4-package Python build/test matrices (`{"test": [], "build": []}` when only `docs/`, `examples/`, `.agents/`, or `*.md` change). Cover `.agents/` paths, non-`.md` `.agents/` scripts/yaml, mixed changes (`scripts/*.py` fallback vs `packages/core/...`), and `.md` root changes in `tests/unit/test_ci_plan.py`.
5. **`scripts/check_root_files.py`, `.gitignore` & `scripts/setup_agent_harnesses.py`:**
   - Ignore `.claude/` and `CLAUDE.md` in `.gitignore` (`docs/reference` `.rst` label fixes remain deferred to follow-up PR; `.out-of-scope/` is retired).
   - Allow `"CLAUDE.md"` in `scripts/check_root_files.py` `approved_files` (with regression tests in `tests/unit/test_root_file_worktrees.py`).
   - Add `scripts/setup_agent_harnesses.py` (with Google docstrings, Ruff clean, unit tests in `tests/unit/test_setup_agent_harnesses.py` covering dry-run/idempotency/symlink/junction creation & `@AGENTS.md` shim).
6. **Skills Contract & Behavioral Verification (`tests/unit/test_agent_skills_contract.py` + Targeted Subagent Scenarios):**
   - **Architect call (CI Scope):** Include mandatory deterministic contract tests in `tests/unit/test_agent_skills_contract.py` scoped **strictly** to structural invariants across `.agents/skills/*/SKILL.md` and `agents/openai.yaml` (`name == dir`, non-empty `description`, exact `disable-model-invocation: true` ↔ `allow_implicit_invocation: false` parity, `.agents/skills.json` registration check, and banning legacy leaking tokens like `Skill tool`, `TodoWrite`, `write_to_file`, `needs-triage`, `poetry install` inside `SKILL.md` frontmatter/bodies — while **excluding** prose docs like `.agents/skills/README.md` or `references/*` tool tables so unrelated Python PRs in CI `quality` never break on documentation edits).
   - **Targeted behavioral subagent verification (Builder Check #1):** Run fresh baseline/post-edit subagent behavioral scenarios on the **6 high-risk merged/discipline skills** (`using-superpowers`, `test-driven-development`, `systematic-debugging`, `requesting-code-review`, `finishing-a-development-branch`, `workflow-guide`) to verify clean trigger discrimination, Python gate adherence, and zero cross-harness leaks, rather than 31 sequential redundant subagent loops.

---

## 6. Builder-Check Resolutions & Explicit Architect Calls

- **Skill verification model (Partner Choice #1):** Targeted behavioral subagent scenarios on the 6 high-risk merged/discipline skills + mandatory deterministic `tests/unit/test_agent_skills_contract.py` + full `pre-commit`/`hatch run verify`/`hatch run docs`.
- **Triage rejected-request memory (Partner Choice #2):** GitHub-native `wontfix` closed-issue search & structured closing comments (`triage/OUT-OF-SCOPE.md` deleted; `.out-of-scope/` not created or gitignored).
- **Documentation label drift precedence (Partner Choice #3):** `docs/reference/{issue_management,triage_playbook}.rst` & `.github/ISSUE_TEMPLATE/*.yml` remain frozen for the follow-up docs PR; `AGENTS.md`, `docs/agents/triage-labels.md`, `triage`, `to-spec`, and `to-tickets` explicitly declare `docs/agents/triage-labels.md` canonical for live GitHub label mapping.
- **Contract test CI scope (Architect Call):** Restrict `tests/unit/test_agent_skills_contract.py` to structural frontmatter / `openai.yaml` parity and `SKILL.md` body token checks so CI `quality` enforces invariants deterministically without coupling Python PRs to prose docs.

## 7. Left to the Builder / Implementation Plan

- Exact copy refactoring inside individual `SKILL.md` sections and reference docs, preserving all §5 constraints with LF endings, zero trailing spaces, `typos` clean, and Ruff-clean Python helpers.
- Exact task sequencing and TDD assertions in `docs/superpowers/plans/2026-11-16-cross-harness-skills-synergy.md`.
