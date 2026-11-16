---
name: diagnosing-superpowers
description: Use when an agent session went wrong (repeated work, ignored plans, stumbles, high cost/time, skill misfire) OR when conducting a session retrospective to improve AGENTS.md, skills, or automated checks across Antigravity, Claude Code, and Codex
---

# Diagnosing Agent Sessions & Retrospectives

Investigate agent session transcripts on disk using strict primary-source citations (`path:line`) before reporting causes or proposing workflow improvements.

**Core principle:** Every finding cites `path:line`. Every count or timing comes from observed transcript records or command output—never from memory.

---

## Choose Operating Mode During Intake

1. **Transcript Forensics (default):** User wants to know why a session stalled, wasted tokens, skipped skills, or needs an upstream bug report bundle. Report verified evidence and stay read-only—never propose unrequested skill edits in Forensics mode unless switched to Retrospective mode.
2. **Retrospective / Environment Improvement (from user asking for a retrospective / systemic improvements):** After tracing evidence in the session transcripts with `path:line` citations, surface severity-ranked improvements to the project's agent setup (`AGENTS.md`, `tests/`, automated checks, review rules, or `.agents/skills/`) following `writing-skills` (`references/agent-doc-principles.md`).

---

## Workflow

Track progress using your harness task-tracking equivalent. Steps 5–7 run only on their stated condition:

1. **Intake:** Ask focused questions until you can state target session(s), turn range (if known), expected behavior, observed behavior, observable metric (wall-clock, tokens, repeats, missed trigger), and whether a bug-report export or repo retrospective is wanted. (An already-scoped request is itself the problem statement.)
2. **Locate (`references/session-discovery.md`):** Resolve target transcript and subagent files across Antigravity, Claude Code, or Codex to verified absolute paths. Confirm identity with prompt/timestamp. Write case record to `~/.superpowers/diagnosing-superpowers/<session-id>/case.md` (`templates/case.md`) and share path.
3. **Triage:** Read the target region yourself (`references/context-safety.md` — single JSONL lines can exceed `1 MB`). Dispatch parallel read-only analyst subagents across dimensions (`prompts/analyst-common.md` + `prompts/{skill-timeline,plan-adherence,repeated-work,stumbles,quality-evidence,request-conflicts,cost-and-time}.md`). Drop findings lacking `path:line`.
4. **Report (`templates/report.md`):** Present evidence-backed findings in order.
   - *If in Retrospective Mode:* Follow the evidence report with prioritized improvement candidates across **Navigation**, **Automated checks** (`ruff`/`pytest`/`pre-commit`), **Review standards**, **`AGENTS.md` size/no-ops**, **Tool economy**, and **Information access**. Wait for approval before editing repo files.
5. **Upstream Issues (on condition / request):** Search existing issues per `references/github-issues.md`; obtain explicit approval before posting (`templates/issue.md`).
6. **Bundle Export (only when explicitly requested):** Build redacted archive using `templates/bundle-README.md` + `prompts/scrub.md` / `prompts/scrub-audit.md` until CLEAN, requiring user review before sharing.
7. **Cross-Session Pattern Search (only when asked):** Match confirmed signatures across historical logs (`prompts/similar-session.md`).

---

## Hard Rules

- **Context safety (`references/context-safety.md`):** Measure transcript line lengths and chunk bounded ranges before reading raw JSONL files.
- **Read-only on logs:** Never modify, move, or delete session history files.
- **Distinguish actors:** Separate human messages from system reminders, hook outputs, tool results, and parent-agent dispatches.
- **Approval gates:** Never post issues/comments or archive scrubbed bundles without user sign-off.
