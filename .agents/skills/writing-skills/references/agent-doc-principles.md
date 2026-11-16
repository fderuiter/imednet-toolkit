# Agent Document & Instruction Principles (`AGENTS.md`, Docs & Skills)

Use this reference alongside `SKILL.md` when designing or trimming `AGENTS.md`, context pointers, pointed-at reference docs, or deciding between model-invoked and user-invoked skills.

## Context Load vs. Cognitive Load

Every file or pointer spends one of two budgets:
- **Context load:** Always-loaded tokens sitting in every turn (`AGENTS.md`, model-invoked skill `description` strings). Antigravity caps rule files at `24,000` bytes each (`20k` token aggregate); Codex caps `AGENTS.md` at `32 KiB`. Keep always-loaded context lean and surgical.
- **Cognitive load:** Human mental index of what manual commands exist and when to type them. When manual skills proliferate, group discoverability behind a single manual router (`workflow-guide`) rather than keeping `N` separate shortcuts in human memory.

## Context Pointers & Three-Tier Progressive Disclosure

A context pointer (`AGENTS.md` line or skill `description`) names external material and encodes the exact **branch** that triggers loading it:
- Front-load distinguishing trigger words.
- One branch per distinct condition—collapse synonyms.
- Place content at its lowest effective rung on the information hierarchy:
  1. **In-file step** (ordered workflow actions always executed).
  2. **In-file reference** (rules consulted across runs of that skill).
  3. **Disclosed reference** (`references/*.md`, `docs/*.md` loaded only when its branch triggers).

## Single Source of Truth vs. Environment Caching

- Keep each rule in **one** authoritative location. Restating rules in both `AGENTS.md` and `SKILL.md` causes drift.
- Treat executable configs and repo tooling (`pyproject.toml`, `.pre-commit-config.yaml`, `scripts/*`, `--help`) as live truth; document only unwritten conventions, governance precedence, and non-obvious gotchas.
- Prune **no-ops** (instructions the model already obeys by default) and stale **sediment** ruthlessly.

## Prompt Shaping & Leading Words

- Anchor recurring concepts with existing compact **leading words** (`tracer bullet`, `seam`, `red loop`, `horizon`) rather than multi-sentence explanations.
- For output shaping (structure, brevity, tone), state **positive recipes** ("present Standards and Spec side by side") rather than negative bans (*don't think of an elephant*), reserving hard prohibitions and rationalization tables strictly for discipline rules violated under time/sunk-cost pressure (`references/bulletproofing.md`).

## Model-Invoked vs. Manual-Only Mechanics

- **Model-invocable (default):** Omit `disable-model-invocation` in `SKILL.md` AND keep `policy.allow_implicit_invocation: true` (or omitted `false`) in `agents/openai.yaml`. Describe `"Use when..."` branches concisely so agents trigger cleanly without summary-spoiling the workflow.
- **User-invoked only:** Set `disable-model-invocation: true` in `SKILL.md` **AND** `policy.allow_implicit_invocation: false` in `agents/openai.yaml`. Write a short human-facing summary description; keep shared knowledge out of manual skills in plain external reference files (`docs/agents/*`) so automated skills can still reach them.
