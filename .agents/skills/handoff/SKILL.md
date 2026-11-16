---
name: handoff
description: Compact the current conversation into a handoff document for another agent session or harness to pick up.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

Write a concise, portable handoff document summarizing the current session so a fresh agent session (on OpenAI Codex, Claude Code, or Google Antigravity) can resume without re-discovering settled facts.

Save the document in the OS temp directory or `.scratch/handoffs/` (never repo root, so `scripts/check_root_files.py` never fails) and report its exact path.

Include:
1. Current branch/worktree state, commits made, and verification commands already run vs still needed.
2. Exact primary-source paths (specs, plans, ADRs, GitHub issues/PRs, modified modules) by pointer rather than copying large bodies.
3. A **Suggested Skills** section naming exact `.agents/skills/<name>` skills for the next session to activate.
4. Any human decisions or open questions pending.

Redact all credentials, API keys, tokens, or PII (`<REDACTED>`). If the user supplied arguments describing the next session focus, tailor the handoff accordingly.
