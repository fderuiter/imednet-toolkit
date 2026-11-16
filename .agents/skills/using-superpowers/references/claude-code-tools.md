# Claude Code Tool Mapping

Claude Code acts as the reference action vocabulary (`Skill`, `Agent`, todos, `AskUserQuestion`), with repo-specific bridge rules for `imednet-toolkit`.

| Action skills request | Claude Code equivalent |
| --- | --- |
| Activate a skill | `Skill` tool or `Read` on `.agents/skills/<name>/SKILL.md` |
| Dispatch a subagent | `Agent` tool (use worktree isolation for parallel writers) |
| Task tracking | Built-in task / todo tools |

## Local Repo Discovery Bridge (`.claude/skills` & `CLAUDE.md`)

`imednet-toolkit` stores canonical skills in `.agents/skills/` and instructions in `AGENTS.md`, and sets `git config core.symlinks` safely for Windows compatibility (tracked git symlinks are forbidden). Before running Claude Code locally in this repo, run once per checkout:

```bash
python scripts/setup_agent_harnesses.py
```

This creates gitignored local bridges:
- `.claude/skills` -> `.agents/skills` (Windows NTFS junction or POSIX symlink)
- `CLAUDE.md` -> `@AGENTS.md` (gitignored and whitelisted in `scripts/check_root_files.py`)

Verify at any time with `python scripts/setup_agent_harnesses.py --check`.

## Cheaper Orchestration for `subagent-driven-development`

Claude Code supports nested subagents (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`). When your partner asks to conserve top-tier orchestration tokens on a whole plan, dispatch one mid-tier orchestrator subagent pointing at `docs/superpowers/plans/<plan>.md` to run `subagent-driven-development` end to end, requiring its final summary to reproduce the `"Rulings I made"` list verbatim. Do not add a nested orchestrator for single-task edits.
