---
name: research
description: Investigate a question against high-trust primary sources and capture findings in a cited Markdown file in the repo. Use when the user wants a topic researched, docs or API facts gathered, or deep reading delegated to a background agent.
---

Dispatch a background subagent (`invoke_subagent` on Antigravity, `Agent` on Claude Code, `spawn_agent` on Codex) so exploration does not stall the main thread.

1. Investigate against **primary sources** (official documentation, source files in `packages/`, specs, first-party APIs or `gh`)—never unverified secondary summaries. Trace every claim back to its source.
2. Write findings cleanly to a single cited Markdown file under `docs/agents/` (excluded from Sphinx builds via `docs/conf.py` `agents/**`) or `.scratch/` (never repo root, so `scripts/check_root_files.py` passes).
3. Link or summarize the verified report path cleanly when complete.
