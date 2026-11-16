# Google Antigravity Tool Mapping

Skills use harness-neutral action verbs. On Google Antigravity (IDE & `agy` CLI), map those actions to the live tools below:

| Action skills request | Antigravity tool equivalent |
| --- | --- |
| Activate a skill | `read_file` on `.agents/skills/<name>/SKILL.md` |
| Read / write / edit files | `read_file`, `write_file`, `edit_file` |
| Structured user questions / rounds | `ask_question` (`(Recommended)` option first, 1 question or focused round at a time) |
| Dispatch an isolated subagent | `invoke_subagent` (`TypeName: "self"` for editing/command tasks, `"research"` for read-only exploration; `Workspace: "inherit" \| "branch" \| "share"`) |
| Coordinate parallel multi-author agents | Antigravity built-in `swarm` (`workspace: "share"` or `"branch"`) paired with `dispatching-parallel-agents` |
| Task tracking ("track tasks", "plan checklist") | Plan / task **artifact** (`write_file` / `edit_file` inside the session `<appDataDir>/brain/<conversation-id>/` with `ArtifactMetadata: {Summary, UserFacing, RequestFeedback}`). Do **not** confuse with `manage_task`, which manages background shell processes. |
| Recurring timers / schedules | Built-in `schedule` (or Antigravity `automation` skill) |
| Interactive visual reports / UI | Built-in `generative_ui` (self-contained HTML without external CDN links when embedded inline) |

## Built-In Platform Skills Composition

Antigravity ships platform built-ins (`agy-customizations`, `automation`, `generative_ui`, `swarm`, `plugin`, `ui-plugin-navigation`). Repo skills under `.agents/skills/` intentionally complement rather than duplicate them:

- Prefer `scripts/env.ps1` via PowerShell (`pwsh`) or direct `.venv` tools on Windows; remember WSL `bash.exe` often precedes Git Bash (`C:\Program Files\Git\bin\bash.exe`) on `PATH`.
- Keep all repo-committed artifacts inside approved directories (`docs/superpowers/`, `docs/agents/`, `.scratch/`) so `scripts/check_root_files.py` never fails on commit.
