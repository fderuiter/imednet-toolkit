# Agent Skills Synergy & Project-Tailoring Research (`imednet-toolkit`)

Primary-source research evaluating how `obra/superpowers`, `mattpocock/skills`, and Google Antigravity built-in/plugin customizations currently interact inside `imednet-toolkit`, where they conflict or duplicate effort, and what project constraints govern an integrated, tailored setup.

---

## 1. Current Repository & Environment State

### 1.1 Branch & Worktree State
- **Current worktree (`remove_matts_skills`):** Originally at `72e89579`, now **fast-forwarded to `origin/main` (`4c856c34`)** (`git merge --ff-only origin/main`). Now matches `origin/main` with **52** skills under `.agents/skills/`, updated `AGENTS.md`, `.worktrees`/`.superpowers` in `.gitignore`, and `"superpowers/**"` in `docs/conf.py`. Vendor-specific paths `.claude/`, `.codex/`, `CLAUDE.md`, and `GEMINI.md` are all absent from the repository root.
- **`origin/main` (`4c856c34`) & primary clone (`D:\Users\Fred\Documents\GitHub\imednet-toolkit`):** Merged `PR #1768` (`bd13af53`, *chore: configure obra/superpowers skills and project workflow*) and `PR #1769` (`4c856c34`, empty-diff commit vs `bd13af53` that intended to also land `docs/adr/0008-circuit-breaker-health-domains.md`).
  - `bd13af53` modified 84 files (+10,969 / -23 lines): added **15** `obra/superpowers` skill directories (80 files) into `.agents/skills/`, expanded `AGENTS.md` with `## Superpowers Methodology`, updated `.agents/skills/README.md`, added `.worktrees` and `.superpowers` to `.gitignore`, and added `"superpowers/**"` to `exclude_patterns` in `docs/conf.py`.
  - Byte-for-byte comparison against `~/.gemini/config/plugins/superpowers/skills/` shows **79 of 80** Superpowers files in `origin/main:.agents/skills/` are verbatim upstream copies; only `writing-skills/references/graphviz-conventions.dot` differs by a trailing `\n` added for pre-commit `end-of-file-fixer`.
  - Note: In `D:\Users\Fred\Documents\GitHub\imednet-toolkit`, `docs/adr/0008-circuit-breaker-health-domains.md` and the `docs/adr/index.rst` update remain uncommitted in the working tree because `4c856c34` landed with an empty diff.

### 1.2 Installed Customization Layers & Discovery Precedence
Per `agy-customizations/SKILL.md` and `agy-customizations/docs/*.md`, Antigravity resolves customizations in strict priority order (highest wins on name collision):

1. **Workspace Project** (walking CWD up to `.git` root): `AGENTS.md`, `GEMINI.md`, `.agents/rules/*.md`, `.agents/skills/<name>/SKILL.md`
2. **Workspace Declared Configs**: `.agents/skills.json`, `.agents/plugins.json` (one-level scan)
3. **Global Discovery**: `~/.gemini/config/skills/` and `~/.gemini/config/plugins/<name>/skills/`
4. **Built-in Customizations**: Mounted explicitly by Antigravity config (`~/.gemini/antigravity/builtin/skills/`)
5. **Global Declared Configs**: Explicitly configured global JSON manifests

**Current active footprint across layers:**
- **Repo `.agents/skills/` (`origin/main`):** **52** skills (**15** `obra/superpowers` + **37** `mattpocock/skills`). All 37 Matt skills include `agents/openai.yaml` (Codex invocation policy); none of the 15 Superpowers skills do. **22** Matt skills set `disable-model-invocation: true` (excluded from model-facing `<skills>` list; user slash-command only), leaving **30** model-invocable repo skills (`5,364` description chars total).
- **Global Plugins (`~/.gemini/config/plugins/` & `config.json`):**
  - `superpowers` (`v7.0.2`, Jesse Vincent): ships the same 15 skills plus `plugin.json`, `gemini-extension.json`, `GEMINI.md`, `AGENTS.md`, `hooks/`, `scripts/`, `tests/`. On `origin/main`, Priority 1 repo `.agents/skills/` shadows Priority 3 global plugin skills.
  - `excel-cli` (`v2.0.8`): enabled by discovery, adds `excel-cli`.
- **Global Skills (`~/.gemini/config/skills/`):** Contains **40 NTFS directory junctions** pointing to `D:\Users\Fred\Documents\GitHub\agy-skills\skills\...` (including customized names like `ask-fred`, `setup-agy-skills`, `setup-python-deep-modules`, `git-guardrails`). That target repository **does not exist on disk**, so all 40 junctions are broken and inactive.
- **Antigravity Built-in Skills (`~/.gemini/antigravity/builtin/skills/`):** **15** directories on disk; **10** are mounted in `<skills>` (`agy-customizations`, `antigravity-guide`, `automation`, `generative_ui`, `migrate-agent-settings`, `migrate-workflows`, `swarm`, `plugin`, `swarm_monitor`, `ui-plugin-navigation`) and **5** are unmounted/role-scoped (`mcp-app-development`, `permissioned-github`, `run-workflow`, `swarm_narrator`, `swarm_pane`).

---

## 2. Primary-Source Runtime & Portability Findings

### 2.1 Bootstrap, Hooks & Tool-Name Mismatches in Antigravity
1. **Superpowers `SessionStart` hook does not run in Antigravity:**
   - Per `agy-customizations/docs/plugins.md` (lines 61–70) and `docs/hooks.md`, native Antigravity plugins (directories with `plugin.json`) load `hooks.json` **only from the plugin root** using Antigravity's `{ "<hook-name>": { "SessionStart": [{ "type": "command", ... }] } }` schema and `{"injectSteps": [{"systemMessage": {"systemMessage": "..."}}]}` stdout contract.
   - `~/.gemini/config/plugins/superpowers/hooks/hooks.json` sits under `hooks/` (not root) and uses Claude Code / Cursor / Copilot schemas (`${CLAUDE_PLUGIN_ROOT}`, `hookSpecificOutput.additionalContext`).
   - Superpowers' own `tests/antigravity/test-plugin-manifest.sh` and `docs/porting-to-a-new-harness.md` confirm Antigravity deliberately relies on **progressive disclosure via `using-superpowers`'s description** (`"Use when starting any conversation..."`) rather than a `SessionStart` hook or `rules/`.
2. **Stale tool mappings in `using-superpowers/references/antigravity-tools.md`:**
   - Upstream `antigravity-tools.md` maps subagent dispatch to `invoke_subagent` (`self` / `research`), which matches Antigravity, **but** maps task tracking to `write_to_file`, `replace_file_content`, `multi_replace_file_content`, and `ArtifactMetadata.ArtifactType: "task"`.
   - Current Antigravity tools are `write_file`, `edit_file`, `ArtifactMetadata: {Summary, UserFacing, RequestFeedback}` (no `ArtifactType`), `ask_question`, `swarm` (`post_message`, `read_board`, `swarm_wait`), `schedule`, and `read_file` (to activate skills).
   - Meanwhile, the plugin's `GEMINI.md` points to `references/gemini-tools.md` (Gemini CLI tools like `activate_skill`, `write_todos`, `run_shell_command`), which differ from Antigravity's tool names.
3. **Context-window rule budget (`agy-customizations/docs/rules.md`):**
   - Each rule file (`AGENTS.md`) is capped at **24,000 bytes** (post `@[label](path)` expansion) and shares a **20,000-token** aggregate rules budget (`defaultRulesBudget`), separate from the skill/MCP budget. Over-budget rules degrade to path pointers.
4. **Cross-harness portability (`agentskills.io` & `AGENTS.md` AAIF standard):**
   - Keeping canonical skills in `.agents/skills/<name>/SKILL.md` with root `AGENTS.md` is portable across Antigravity, Codex, Gemini CLI, Copilot CLI, and Claude Code (via `.agents/skills` or `.claude` bridge).
   - However, Claude Code-specific skills (`claude-handoff`, `git-guardrails-claude-code`), Codex-only `agents/openai.yaml` assumptions, and `Skill` tool phrasing (in Matt's skills) assume a specific harness unless abstracted cleanly.

### 2.2 Windows Host vs Ubuntu CI Execution Hazards
- **CI runs on `ubuntu-latest`** (`.github/workflows/main.yml`), whereas local development runs on **Windows** (`powershell` default shell).
- **Verified host shell realities:**
  - `uv`, `hatch`, `python`, and `py` are **not on the default Windows agent `PATH`** unless `scripts/env.ps1` / `.venv` is resolved (`scripts/env.ps1` also sets `$env:PYTHONUTF8 = "1"` and `[Console]::OutputEncoding = UTF8` per ADR-0003). The primary clone (`D:\Users\Fred\Documents\GitHub\imednet-toolkit`) has `.venv`, whereas this worktree does not.
  - `git config --get core.symlinks` is **`false`** on this Windows host, so tracking git symlinks (such as `.claude/skills -> ../.agents/skills` or `CLAUDE.md -> AGENTS.md`) would check out plain text files on Windows clones without Developer Mode + symlink config.
  - `where.exe bash` finds `C:\Windows\System32\bash.exe` (WSL, which has `python3` at **3.10.12** — below `pyproject.toml`'s `>=3.11` floor!) **before** Git Bash (`C:\Program Files\Git\bin\bash.exe`).
  - `executing-plans/scripts/task-start` (line 26) and `task-done` (line 28) invoke `"$sdd/task-brief"` and `"$sdd/sdd-workspace"` directly without `${BASH:-bash}`, violating `writing-skills/SKILL.md` line 161 (`Permission denied` when executable bits are stripped on Windows/zip unpacks).
  - `systematic-debugging/find-polluter.sh` hardcodes `npm test`; `requesting-code-review/code-reviewer.md` hardcodes `/tmp/review-[SHA]`; `improve-codebase-architecture` uses `$TMPDIR`/`%TEMP%` + external Tailwind/Mermaid CDNs (blocked inside Antigravity inline iframes per `generative_ui/SKILL.md`).

### 2.3 Cross-Harness Matrix: ChatGPT/Codex, Claude, and Antigravity
A comprehensive automated scan of all **52** skills on `origin/main` (`4c856c34`) plus official documentation for OpenAI Codex/ChatGPT, Anthropic Claude (Code & Desktop/Web), and Google Antigravity establishes the exact compatibility matrix:

| Capability | OpenAI Codex / ChatGPT | Claude Code / Claude Apps | Google Antigravity |
|---|---|---|---|
| **Repo instruction file** | `AGENTS.md` (32 KiB limit) | `CLAUDE.md` native; `AGENTS.md` only as fallback when no `CLAUDE.md` exists (`check_root_files.py` blocks `CLAUDE.md`) | `AGENTS.md` / `GEMINI.md` (24,000 bytes/file, 20k-token total rules budget) |
| **Repo skill discovery path** | `.agents/skills/` (walks CWD to repo root) & `.codex/skills/` | `.claude/skills/` & `~/.claude/skills/` in official docs (`.agents/skills/` still requested upstream; requires bridge/plugin/symlink) | `.agents/skills/` + `.agents/skills.json` (Priority 1/2 workspace precedence) |
| **Manual-only invocation gating** | `agents/openai.yaml` (`policy.allow_implicit_invocation: false`) | `disable-model-invocation: true` in `SKILL.md` frontmatter | `disable-model-invocation: true` in `SKILL.md` frontmatter |
| **Outside-repo Chat/Desktop apps** | ChatGPT / Codex web/app skills (`SKILL.md` bundle) | `.zip` upload (`<skill-name>/SKILL.md` root folder; no `AGENTS.md`/`docs/` repo context) | N/A (IDE/CLI workspace agent) |
| **Current repo parity & leaks** | **37/37** Matt skills have `agents/openai.yaml` (`22` manual, exact 22/22 parity with `disable-model-invocation: true`); **0/15** Superpowers skills have `openai.yaml` | **14** skills still name `"Skill tool"` verbatim; **8+** name `CLAUDE.md`, `.claude/`, `TodoWrite`, or `claude --bg` | `antigravity-tools.md` names obsolete tools (`write_to_file`, `replace_file_content`, `ArtifactType`); built-in platform skills not integrated |

- **Repo script & metadata CI scan across 52 skills:** **42** skills carry non-`.md` files (**37** `agents/openai.yaml` + **5** Superpowers non-`.md` skills, with **9** skills total carrying scripts/templates alongside or beyond `openai.yaml`: `brainstorming`, `diagnosing-bugs`, `executing-plans`, `git-guardrails-claude-code`, `setup-ts-deep-modules`, `subagent-driven-development`, `systematic-debugging`, `wizard`, `writing-skills`), triggering `scripts/ci_plan.py`'s full package build/test matrix whenever edited; `pyproject.toml` Ruff enforces `D`/`S`/`PL` rules on any repo `.py` outside `sandbox/`.

---

## 3. Comprehensive Skill Overlap, Conflict & Relevance Analysis

### 3.1 Summary Classification of All 67 Skills (`52` Repo + `15` Built-in)

| Classification | Count | Skills |
|---|---|---|
| **1. Incompatible / Irrelevant to `imednet-toolkit` (Drop or Replace)** | 11 | `migrate-to-shoehorn` (TS), `setup-ts-deep-modules` (TS `dependency-cruiser`; repo uses Python `tests/unit/architecture/import_rules.py`), `setup-pre-commit` (Husky/Prettier; explicitly rejected in ADR-0002), `scaffold-exercises` (`ai-hero-cli`), `claude-handoff` (`claude --bg` only), `git-guardrails-claude-code` (`.claude/settings.json` only), `setup-matt-pocock-skills` (setup wizard already applied, with stale defaults), `writing-beats`, `writing-fragments`, `writing-shape` (long-form prose drafting), plus global `excel-cli` (personal plugin outside repo) |
| **2. Overlapping Workflow Clusters (Must Unify Precedence or Merge)** | 27 | See 5 collision clusters in §3.2 below |
| **3. Complementary Repo-Specific / Cross-Cutting Utilities (Keep & Tailor)** | 14 | `domain-modeling`, `triage` (w/ `AGENT-BRIEF.md` & `OUT-OF-SCOPE.md`), `wait-what`, `resolving-merge-conflicts`, `wizard`, `handoff`, `to-questionnaire` (fix CWD root issue), `using-superpowers`, `using-git-worktrees`, `verification-before-completion`, `finishing-a-development-branch`, `receiving-code-review`, `writing-skills` / `writing-for-agents` (reconcile authoring guidance) |
| **4. Antigravity Platform Built-ins (Adapt Repo Skills to Leverage, Never Duplicate)** | 15 | `agy-customizations`, `antigravity-guide`, `automation`, `generative_ui`, `migrate-agent-settings`, `migrate-workflows`, `swarm` (+ `swarm_monitor`, `swarm_narrator`, `swarm_pane`), `plugin`, `ui-plugin-navigation`, `mcp-app-development`, `permissioned-github`, `run-workflow` |

### 3.2 The Five Core Collision Clusters

1. **Ideation, Interviewing & Design Specs:**
   - *Skills:* `superpowers:brainstorming` vs `mattpocock:grill-me`, `grill-with-docs`, `grilling`, `to-spec`, `wayfinder`.
   - *Friction:* `brainstorming` demands 1 question at a time, offers a Node/WebSocket visual companion, enforces a `<HARD-GATE>` before any code, and saves specs to `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`. `grilling`/`grill-with-docs` asks round-based frontier batches with recommended answers and writes lazily to `CONTEXT.md` and `docs/adr/NNNN-*.md`. `to-spec` synthesizes a conversation into a GitHub issue with `ready-for-agent`. `wayfinder` builds a GitHub `wayfinder:map` issue tree of decision tickets (`research`/`prototype`/`grilling`/`task`).
   - *Synergy Opportunity:* Distinguish **Ideation/Design Gate (`brainstorming`)**, **Deep Stress-Test Primitive (`grilling` / `grill-with-docs` updating `CONTEXT.md` & `docs/adr/`)**, **Multi-Session Epic Fog (`wayfinder`)**, and **Tracker Publishing (`to-spec` aligned with `.github/ISSUE_TEMPLATE/` and `docs/reference/issue_management.rst`)**.

2. **Planning & Task Decomposition:**
   - *Skills:* `superpowers:writing-plans` vs `mattpocock:to-tickets`.
   - *Friction:* `writing-plans` produces a step-by-step TDD markdown plan under `docs/superpowers/plans/` tailored for single-branch execution (`subagent-driven-development` / `executing-plans`). `to-tickets` produces vertical tracer-bullet GitHub issues with native blocking edges (`.github` / `gh api`), explicitly forbidding file paths/code snippets because tickets age across sessions.
   - *Synergy Opportunity:* Keep both as distinct output targets by horizon — **local execution plans** (`writing-plans` for immediate single-feature/PR builds) vs **backlog issue decomposition** (`to-tickets` for multi-PR epics like `#1752`).

3. **Implementation, Multi-Agent Execution & Swarms:**
   - *Skills:* `superpowers:subagent-driven-development`, `executing-plans`, `dispatching-parallel-agents` vs `mattpocock:implement`, `implement-spec` vs Antigravity built-in `swarm` + `run-workflow`.
   - *Friction:*
     - `subagent-driven-development` **forbids parallel implementers** (sequential tasks, per-task spec+quality review, bash scripts under `.superpowers/sdd/`).
     - `implement-spec` **maximizes parallel implementers** in separate git worktrees with a merger subagent.
     - `implement` is a 15-line bare prompt calling `/tdd` and `/code-review` without `verification-before-completion`, `uvx pre-commit`, `hatch run verify`, or `hatch run docs`.
     - Antigravity `swarm` provides native shared-board coordination with `workspace: "share"`/`"branch"` and `swarm_monitor`, while `dispatching-parallel-agents` is read/fix-oriented for independent failures.
   - *Synergy Opportunity:* Retire redundant `implement` / `implement-spec` in favor of Superpowers (`subagent-driven-development` / `executing-plans` / `dispatching-parallel-agents`) plus Antigravity `swarm` when true multi-author parallelism (`workspace: "share"`) is warranted, and fix Windows bash invocation & `antigravity-tools.md`.

4. **TDD, Debugging & Architecture Modeling:**
   - *Skills:* `superpowers:test-driven-development` vs `mattpocock:tdd`; `superpowers:systematic-debugging` vs `mattpocock:diagnosing-bugs`; `mattpocock:codebase-design` + `improve-codebase-architecture` vs `docs/explanation/architecture.rst` + `tests/unit/architecture/import_rules.py`.
   - *Friction:*
     - Both `tdd` skills trigger on the exact same keywords ("red-green-refactor", "test-first", "build features or fix bugs"). **Contradiction:** `mattpocock:tdd` states *"Refactoring is not part of the loop"* and *"Test only at pre-agreed seams... confirm them with the user"*, whereas `superpowers:test-driven-development` mandates RED-GREEN-REFACTOR in every cycle and testing immediately. Both use TypeScript/Jest examples instead of `pytest`, `respx` strict routers, `imednet.testing.fake_data`, and `>=90%` coverage (`docs/reference/agent_rules.rst`).
     - Both `systematic-debugging` and `diagnosing-bugs` trigger on bugs/failures/slowness. `systematic-debugging` is broader (root-cause tracing, defense-in-depth, condition-based waiting, 3-fix architectural stop); `diagnosing-bugs` adds a strong ranked-hypothesis checkpoint, `[DEBUG-xxxx]` tag cleanup, and HITL loop template.
     - `codebase-design` forbids terms like "service" and "component" that `docs/explanation/architecture.rst` uses, while `improve-codebase-architecture` uses CDN Tailwind/Mermaid HTML in `$TMPDIR` and ignores `tests/unit/architecture/import_rules.py`.

5. **Code Review, Finishing, Diagnostics & Meta-Authoring:**
   - *Skills:* `superpowers:requesting-code-review` vs `mattpocock:code-review`; `superpowers:finishing-a-development-branch`; `superpowers:diagnosing-superpowers` vs `mattpocock:retro`; `superpowers:writing-skills` vs `mattpocock:writing-for-agents`.
   - *Friction:*
     - `requesting-code-review` checks plan alignment, code quality, tests, production readiness; `code-review` enforces a strict **two-axis split** (Standards + Fowler smell baseline vs Spec) using parallel subagents.
     - `finishing-a-development-branch` offers local `git merge` into `main` and generic PR creation without Conventional Commit titles (`Semantic PR Title` CI check), **Squash and merge**, `release-please` rules, `.github/PULL_REQUEST_TEMPLATE.md`, or `hatch run verify` / `hatch run docs`.
     - `diagnosing-superpowers` is strictly read-only evidence reporting on transcripts; `retro` proposes concrete `AGENTS.md`/lint/standard improvements from transcripts.
     - `writing-skills` vs `writing-for-agents` disagree directly on `description` rules (`"Use when..."` triggers only vs `"state what the material is + trigger branches"`) and negative prohibitions (discipline bulletproofing vs positive prompting).

---

## 4. `imednet-toolkit` Governance, Tooling & CI Constraints

Primary-source audit of `.github/*`, `docs/*`, `scripts/*`, `pyproject.toml`, `.pre-commit-config.yaml`, `typos.toml`, and live `gh` metadata surfaced critical project-specific constraints:

1. **GitHub Labels & Templates Mismatch (`docs/agents/triage-labels.md` vs GitHub reality):**
   - `gh label list` shows **73** live labels: `type:*` (`bug`, `feature`, `refactor`, `docs`, `tech-debt`, `security`, `ci`, `test`, `epic`, `enhancement`, `research`), `area:*` (`core`, `http`, `models`, `auth`, `pagination`, `cli`, `workflows`, `airflow`, `docs`, `tests`, `release`, `github`, `ci`, `testing`, `streamlit`, `mapping`, `records`, `standards`, `dashboard`, `review-workbench`, `terminology`, `governance`, `performance`), `priority:p0..p3`, `status:*` (`triage`, `ready`, `blocked`, `in-progress`, `needs-design`, `review`, `done`), `size:xs..xl`, `risk:low` (missing `risk:medium`/`high`), `track:*`, plus legacy `ready-for-agent` and `wontfix`.
   - `docs/agents/triage-labels.md` still lists uncustomized Matt defaults (`needs-triage`, `needs-info`, `ready-for-human`) that **do not exist** on GitHub, and `wayfinder`'s `wayfinder:map` / `wayfinder:<type>` labels **do not exist** on GitHub.
   - `docs/reference/issue_management.rst` and `.github/ISSUE_TEMPLATE/*.yml` prescribe `<type>(<area>): <concise outcome>` titles and structured sections (Problem, Outcome, Constraints/Non-goals, Acceptance criteria, Test/Docs impact, API surface & Compatibility impact, Verification), yet `to-spec` and `to-tickets` use completely different markdown templates and drop those fields.
   - Note: `triage` uses `gh ... | jq`, which conflicts with Antigravity's `permissioned-github` skill (unmounted in this session) if that skill is ever activated.
2. **Pre-Commit Root-Directory Sanitization (`scripts/check_root_files.py`):**
   - Runs on **every commit** via `.pre-commit-config.yaml`. Fails on any unapproved root file (only 23 filenames + `.coverage.*` allowed; `AGENTS.md`, `CONTEXT.md`, `CONTEXT-MAP.md` are allowed, **`CLAUDE.md`, `GEMINI.md`, `NOTES.md`, `MISSION.md`, `RESOURCES.md`, `to-questionnaire-*.md` are NOT**).
   - Skills that write into CWD/repo root (`teach`, `loop-me`, `to-questionnaire`) will break commits unless redirected to ignored or approved subdirectories (e.g., `.scratch/`, `.superpowers/`, or OS temp).
3. **CI Path-Filter Cost (`scripts/ci_plan.py`):**
   - CI skips full `build` / `test` / `fuzzing` / `validate-extras` matrices **only** when every changed file starts with `docs/`, `examples/`, or ends with `.md`.
   - Any edit to non-`.md` files inside `.agents/` (e.g., `.yaml`, `.json`, `.sh`, `.js`, `.cjs`, extensionless scripts) triggers `fallback()` and runs the full 4-package × 3-Python-version CI matrix.
4. **Pre-Commit & Ruff Scope (`.pre-commit-config.yaml` & `pyproject.toml`):**
   - `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-json`, `check-added-large-files`, and `typos` (configured by `typos.toml`) scan **all** `.agents/` files in CI `pre-commit`!
   - `ruff` and `ruff-format` scan the whole repo except `^sandbox/`. Any `.py` helper added under `.agents/` would be subject to strict `D` (Google docstrings), `S` (Bandit), `PL`, `SLF`, `I` rules because `.agents/` has no `per-file-ignores` in `pyproject.toml`.
   - `detect-secrets` in `.pre-commit-config.yaml` is scoped to `^packages/.*/src/`.
5. **Sphinx Documentation Rules (`scripts/build_docs.py`, `scripts/validate_docs.py`, `docs/conf.py`):**
   - `docs/conf.py` on `origin/main` excludes `agents/**` and `superpowers/**`.
   - `scripts/build_docs.py` **auto-generates** `docs/adr/index.rst` from `docs/adr/^\d{4}-.+\.md$` (each ADR must start with `# <Title>`). `domain-modeling` (`ADR-FORMAT.md`) does not mention regenerating `docs/adr/index.rst` or avoiding unexcluded `docs/` paths.
   - `docs/reference/` and `CONTRIBUTING.md` have stale references (`project_standards.rst` cites `hatch.lock`, `typer[all]`, `black`, `isort`, `hatch run lint`; `ADR-0001` cites Typer; `live_tests.rst`/`live_test_charter.rst` cite `typer.testing.CliRunner`; `CONTRIBUTING.md` links `docs/issue_management.rst` instead of `docs/reference/issue_management.rst`; `README.md`/`config.yml` link `imednet-python-sdk`; `agent_rules.rst` cites `responses` and Render).
6. **Monorepo Architecture Guard (`tests/unit/architecture/import_rules.py`):**
   - Already enforces Python layering across `core`, `plugins-workflows`, `plugins-sinks`, and `providers-airflow` (`core` cannot import CLI or extensions; extensions must import `PUBLIC_CORE` or `PUBLIC_SYMBOLS`; `workflows` cannot import `providers-airflow`).
   - Also `hatch run verify` (`verify-static` + `verify-tests` + `docs`) + `hatch run quality:all packages` (10 lint/security/duplication checks) + `scripts/validate_package_consumption.py` already cover everything `setup-ts-deep-modules` and `setup-pre-commit` were meant to solve in TypeScript.

---

## 5. Best-Practice Principles for Consolidation & Tailoring

Cross-referencing `agentskills.io`, `writing-skills/references/discovery.md`, `bulletproofing.md`, `writing-for-agents/SKILL.md`, `SKILL-MECHANICS.md`, `anthropic-best-practices.md`, and `agy-customizations/docs/*`:

1. **Minimize always-loaded context:** Keep root `AGENTS.md` short, crisp, and strictly authoritative (single source of truth for rules, overrides, and pointers; no duplication of lookup tables). Keep model-invocable skills focused (`<500` lines, `<20KB`) and mark purely manual commands `disable-model-invocation: true` with a single router skill if needed.
2. **One unambiguous skill per intent (eliminate trigger collisions):** Never keep two model-invocable skills with overlapping `"Use when..."` descriptions for the same trigger (`tdd` vs `test-driven-development`, `diagnosing-bugs` vs `systematic-debugging`, `code-review` vs `requesting-code-review`). Either consolidate/merge the best ideas into one skill or clearly differentiate triggers (and set `disable-model-invocation: true` or remove redundant ones).
3. **Keep upstream vs custom maintainability clean:** Where upstream `obra/superpowers` is installed globally as a plugin (auto-updated in `~/.gemini/config/plugins/superpowers`), copying unmodified upstream files into `.agents/skills/` freezes updates unless deliberately maintained, OR intentionally tailoring/overriding via Priority 1 `.agents/skills/` / thin project-adapter skills / `AGENTS.md` prevents divergence drift.
4. **Tailor examples, checks, and scripts to Python + Windows/Linux:** Replace TypeScript/Jest/npm assumptions in active skills with `pytest`, `respx`, `uv sync --locked --extra dev --extra docs`, `hatch run verify`, `hatch run quality:all packages`, `hatch run docs`, `uvx pre-commit run --all-files`, `scripts/env.ps1`, and safe Git Bash / Python script invocations.
5. **Align issue/spec workflows with actual repo governance:** Synchronize `docs/agents/triage-labels.md`, `docs/agents/issue-tracker.md`, `to-spec`, `to-tickets`, `triage`, and `domain-modeling` with the real GitHub labels, `.github/ISSUE_TEMPLATE/*.yml` schemas, Conventional Commits (`Semantic PR Title`), Squash & merge, `release-please`, and `generate_adr_index()`.

---

## 6. Deep-Module Architecture Diagnosis (`codebase-design` Lens)

Applying `/improve-codebase-architecture` and `/codebase-design` (**module**, **interface**, **depth**, **seam**, **adapter**, **leverage**, **locality**, **deletion test**) to our agentic ecosystem identifies four structural deepening candidates:

1. **Cross-Harness Adapter Seam (Top Recommendation):** Harness dialects (`"Call the Skill tool"`, `TodoWrite`, `write_to_file`, `${CLAUDE_PLUGIN_ROOT}`, hardcoded `npm`/`/tmp/`, Windows symlink limits) currently leak through **14+** skill bodies while Claude lacks a repo discovery adapter. Deepening one harness adapter layer (`AGENTS.md` + normalized `using-superpowers/references/{antigravity,claude,codex}-tools.md` + optional `.claude`/script adapter + dual `disable-model-invocation` / `allow_implicit_invocation` sync) lets every `SKILL.md` stay pure action vocabulary (**leverage** across 3 harnesses; **locality** for dialect fixes).
2. **Shallow Wrapper Collapse & Deletion Test:** Deleting 10 incompatible skills plus shallow 8–19L pass-through wrappers (`grill-me`, `grill-with-docs`, `implement`, `claude-handoff`) cuts active repo skill count significantly, lowers always-loaded context load, and removes contradicting TS/Husky rules.
3. **Colliding Workflow Seam Unification:** Merging competing twins (`test-driven-development` + `tdd`; `systematic-debugging` + `diagnosing-bugs`; `requesting-code-review` + `code-review`; `subagent-driven-development`/`executing-plans` + `implement-spec`; `brainstorming` + `grilling`; `diagnosing-superpowers` + `retro`; `writing-skills` + `writing-for-agents`) behind one unambiguous trigger and deep Python-tailored implementation per intent eliminates trigger nondeterminism.
4. **Governance & CI Seam Alignment:** Syncing `docs/agents/{triage-labels,issue-tracker,domain}.md` with live GitHub labels & `.github/ISSUE_TEMPLATE/`, aligning `finishing-a-development-branch` with Conventional Commits / Squash & Merge / `release-please` / `verify`, relocating transient root files into `.scratch/`, and exempting `.agents/` skill metadata/scripts from `scripts/ci_plan.py`'s full-package matrix closes governance and CI leaks.

---

## Primary Sources Cited
- Repo governance & CI: `AGENTS.md` (`72e89579` & `origin/main` `4c856c34`), `docs/reference/{agent_rules,project_standards,issue_management,triage_playbook,contributing,live_test_charter,live_tests,verification_reports,security_exceptions}.rst`, `docs/how-to/verification.rst`, `docs/explanation/architecture.rst`, `CONTRIBUTING.md`, `README.md`, `CONTEXT.md`, `docs/adr/{0001..0007}.md`, `D:\Users\Fred\Documents\GitHub\imednet-toolkit\docs\adr\0008-circuit-breaker-health-domains.md`, `docs/conf.py`, `pyproject.toml`, `.pre-commit-config.yaml`, `typos.toml`, `.gitignore`, `.github/{workflows/main.yml,workflows/live-tests.yml,workflows/release-please.yml,PULL_REQUEST_TEMPLATE.md,ISSUE_TEMPLATE/*,CODEOWNERS,SECURITY.md}`, `scripts/{ci_plan,check_root_files,build_docs,validate_docs,validate_showcases,typecheck_docs,validate_package_consumption,aggregate_verification}.py`, `scripts/env.ps1`, `scripts/wizards/*.sh`, `tests/unit/architecture/{test_architecture,import_rules,test_import_rules}.py`, `tests/typing/endpoint_contracts.py`, `tests/conftest.py`.
- GitHub tracker state: `gh label list`, `gh issue view 1767`, `gh pr view 1768`, `gh pr view 1769`, `gh issue list --state open`.
- Repo & plugin skills: `.agents/skills.json`, `.agents/skills/README.md`, all 37 `mattpocock/skills` in `.agents/skills/*/SKILL.md`, all 15 `obra/superpowers` skills in `origin/main:.agents/skills/` and `C:\Users\FrederickdeRuiter\.gemini\config\plugins\superpowers\` (`plugin.json`, `AGENTS.md`, `GEMINI.md`, `hooks/*`, `docs/testing.md`, `skills/*`).
- Antigravity platform docs & config: `C:\Users\FrederickdeRuiter\.gemini\antigravity\builtin\skills\agy-customizations\{SKILL.md,docs/*.md}`, `antigravity_guide/*`, `native_swarm/SKILL.md`, `automation/SKILL.md`, `generative_ui/SKILL.md`, `plugin/SKILL.md`, `migrate-agent-settings/SKILL.md`, `migrate-workflows/SKILL.md`, `permissioned-github/SKILL.md`, `run-workflow/SKILL.md`, `C:\Users\FrederickdeRuiter\.gemini\config\config.json`, `C:\Users\FrederickdeRuiter\.gemini\config\skills\*`.
- External specifications: [Agent Skills Specification (`agentskills.io`)](https://agentskills.io/specification) & [AGENTS.md Open Standard](https://agents.md/).
