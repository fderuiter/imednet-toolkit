---
name: requesting-code-review
description: Use when completing implementation tasks, reviewing work-in-progress changes or branches since a fixed point (main, commit, PR), or verifying changes match repo standards and spec before merging
---

# Requesting Code Review

Dispatch isolated reviewer subagents to catch architectural defects, standard violations, and spec drift before changes cascade. Reviewers receive crafted diff and requirement context—never your conversational thinking history.

**Core principle:** Review early, keep axes independent, act cleanly on Critical and Important findings.

---

## Choosing the Review Mode

1. **Per-task / step alignment (`subagent-driven-development` or focused change):**
   - Resolve `BASE_SHA` (`HEAD~1` or task base) and `HEAD_SHA` (`HEAD`).
   - Dispatch **one** read-only reviewer subagent using [code-reviewer.md](code-reviewer.md), checking plan alignment, Python quality (`docs/reference/agent_rules.rst`, `docs/explanation/architecture.rst`), test integrity, and production readiness.
2. **Branch, PR, or fixed-point review ("review since `<ref>`" / pre-PR gate):**
   - Pin `<fixed-point>` (`origin/main`, commit SHA, tag), verify `git rev-parse <fixed-point>` and non-empty `git diff <fixed-point>...HEAD` (and `git log <fixed-point>..HEAD --oneline`), and run **two parallel read-only subagents** (Standards Axis + Spec Axis) per [code-reviewer.md](code-reviewer.md) and **Two-Axis Evaluation** below so neither axis masks the other.

---

## Two-Axis Evaluation (Fixed-Point & Branch Reviews)

A change can follow clean conventions while implementing the wrong behavior, or implement the exact spec while violating architecture rules. Report the two headings separately without reranking across axes:

### Axis 1: Standards + Baseline Smells
Check against documented repo standards first (`AGENTS.md`, `docs/reference/agent_rules.rst`, `docs/explanation/architecture.rst`, `tests/unit/architecture/import_rules.py`, `CONTRIBUTING.md`) and skip issues static tooling (`ruff`, `mypy`, `typos`, `check_root_files.py`) already catches.

Additionally check the diff against the **12 Fowler Smell Heuristics** (documented repo standards always override; smells are labelled heuristics, never blind dogmas):
- **Mysterious Name**: symbol name obscures intent -> rename clearly.
- **Duplicated Code**: repeated logic shape across hunks/files -> extract shared helper.
- **Feature Envy**: method operates heavily on another object's internals -> move onto data owner.
- **Data Clumps**: recurring tuple/group of parameters -> introduce typed structure/model.
- **Primitive Obsession**: raw strings/dicts standing in for domain concepts (`CONTEXT.md`) -> type it.
- **Repeated Switches**: identical `if/elif`/`match` dispatch cascades -> use mapping/polymorphism.
- **Shotgun Surgery**: one logical change scatters edits across unrelated modules -> consolidate behind seam.
- **Divergent Change**: one module edits for unrelated reasons -> split along domain seams.
- **Speculative Generality**: unused hooks/abstractions/params -> delete and inline.
- **Message Chains**: deep `a.b().c().d()` traversal -> expose cohesive method on owner.
- **Middle Man**: pass-through wrapper adding no leverage -> call target directly.
- **Refused Bequest**: subclass ignoring/overriding parent contract -> replace with composition/Protocol.

### Axis 2: Spec Fidelity
Locate the originating spec in priority order:
1. Issue reference in commits/branch (`gh issue view <n> --comments` per `docs/agents/issue-tracker.md`).
2. User-provided plan or spec path (`docs/superpowers/plans/`, `docs/superpowers/specs/`).
3. Local feature spec under `docs/` or `.scratch/`.

Check strictly: (a) missing/partial spec requirements, (b) unrequested scope creep, (c) mismatches between implementation behavior and spec wording (quoting spec line per finding). If no spec exists, report `"No spec provided — skipped Spec axis"`.

---

## Acting on Findings

- Fix **Critical** issues immediately before proceeding.
- Fix **Important** issues (and verify tests) before moving to the next task or branch integration.
- Record **Minor** issues or push back explicitly with primary-source code/test evidence when a reviewer misreads a constraint.
