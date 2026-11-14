# AGENTS.md — Contributor Guide

**Note:** This file contains strict instructions for AI agents and human contributors working in this directory and its subdirectories.

1. **Stack & Rules**: You MUST follow the dependency stack rules and immutable constraints (credential handling, testing boundaries, PR guidelines) defined in `docs/reference/agent_rules.rst`.
2. **Verification**: You MUST execute the full CI quality gate locally before proposing a solution, as detailed in `docs/how-to/verification.rst`. Contributors must execute `uvx pre-commit run --all-files` before opening pull requests.
3. **Architecture**: You MUST adhere to the separation of concerns defined in `docs/explanation/architecture.rst`.

## Agent skills

### Issue tracker

GitHub issues (`fderuiter/imednet-toolkit`). See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical triage roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context (`CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.
