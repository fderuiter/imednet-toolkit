# Discover Session History Across Harnesses

Resolve the session your human partner named using read-only bounded filesystem inspection and the tools available in the active runtime. Verify every path and field schema on disk rather than assuming a format from memory.

## Common Harness Session Roots (Verify on Disk)

- **Google Antigravity:** `<appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl` (compact) and `transcript_full.jsonl` (untruncated). Subagent logs live under `<appDataDir>/brain/<subagent-conversation-id>/`. Check Windows (`%APPDATA%` / `~/.gemini/antigravity/brain/`) or workspace environment metadata (`Conversation ID`).
- **Claude Code:** `~/.claude/projects/<encoded-project-path>/<session-id>.jsonl` (plus nested subagent transcripts alongside or under session directories).
- **OpenAI Codex:** `${CODEX_HOME:-~/.codex}/sessions/` (rollout JSONL logs grouped by date/session id) or local Desktop state stores.

## Verification Rules

1. Confirm identity using session ID, working directory, timestamps, and quoted first prompt—never recency alone.
2. Measure line sizes before reading (`references/context-safety.md`); read compact files first and pull truncated lines selectively.
3. Expand home shorthands and environment variables to verified absolute paths before passing them to analyst subagents (a subagent's own current conversation ID differs from the target session).
4. Establish actor semantics (`human` vs hook/system injection vs tool result vs parent subagent prompt) directly from observed record keys.
5. Record all verified sources, rejected candidate paths, and field semantics in `case.md` so parallel analysts do not re-run discovery.
