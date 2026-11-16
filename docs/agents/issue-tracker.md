# Issue Tracker: GitHub (`fderuiter/imednet-toolkit`)

Issues and multi-PR epic specs for this repo live as GitHub issues (`fderuiter/imednet-toolkit`). Use the `gh` CLI (`gh issue`, `gh pr`, `gh api`) for all operations, translating every label through [triage-labels.md](triage-labels.md) (which overrides legacy label names in frozen `.rst` docs and `.github/ISSUE_TEMPLATE/*.yml`).

## Issue Conventions & Required Structure

- **Titles:** Follow Conventional Issue titles from `docs/reference/issue_management.rst`: `<type>(<area>): <concise outcome>` (e.g., `feat(workflows): normalize codelist values`).
- **Bodies:** Align issue bodies with `.github/ISSUE_TEMPLATE/` sections (`Problem`, `Outcome`, `Constraints / Non-goals`, `Acceptance criteria`, `Test / Docs impact`, `API surface & Compatibility impact`, `Verification`).
- **Labels:** Apply at least one `type:*`, one `area:*`, one `priority:*`, and one `status:*` label from [triage-labels.md](triage-labels.md). Never pass unverified label names.

## Core Commands

- **Create an issue:** `gh issue create --title "<type>(<area>): <outcome>" --label "..." --body-file <file>` (prefer `--body-file` or stdin heredoc over inline multiline shell escaping on Windows).
- **Read an issue:** `gh issue view <number> --comments --json number,title,body,labels,comments`.
- **List open issues:** `gh issue list --state open --json number,title,body,labels,comments`.
- **Search previously rejected requests (`wontfix` memory):** `gh issue list --state closed --label wontfix --json number,title,body,labels,comments` (or `--search "<concept> label:wontfix"`). Do not create local `.out-of-scope/` files; capture rejection rationale directly inside the GitHub closing comment.
- **Comment / Label / Close:** `gh issue comment <number> --body-file <file>`, `gh issue edit <number> --add-label "..." --remove-label "..."`, `gh issue close <number> --comment "..."`.

## Pull Requests as a Triage Surface

**PRs as a request surface: no.** Unless the maintainer asks to triage an explicit `#<number>` PR, triage focuses on issues. When inspecting an explicit PR, use `gh pr view <number> --comments` and `gh pr diff <number>`. Note GitHub shares numeric IDs across issues and PRs (`gh pr view` vs `gh issue view`).

## Wayfinding & Epic Graph Operations

Used by `wayfinder` and `to-tickets` for multi-PR epics:

- **Epic / Map:** A single parent issue labeled `type:epic` and `status:needs-design` holding Objectives, Decisions-so-far, and remaining Fog-of-war open questions.
- **Child Tickets:** Linked as GitHub sub-issues (`gh api` sub-issues endpoint) or listed in a task list with `Part of #<map>` in the child body. Use live category/status labels (`type:research`, `type:feature`, `status:needs-design`, `status:ready`, `status:blocked`)—never nonexistent `wayfinder:*` labels.
- **Blocking Edges:** Use GitHub native issue dependencies (`gh api --method POST repos/{owner}/{repo}/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>` where `<blocker-db-id>` comes from `gh api repos/{owner}/{repo}/issues/<n> --jq .id`, falling back to `Blocked by: #<n>` in the child body if disabled).
- **Frontier Query:** Open children with no assignee and zero open blockers (`issue_dependencies_summary.blocked_by == 0`).
- **Claim & Resolve:** Assign `@me` (`gh issue edit <n> --add-assignee @me`); close resolved children with a structured summary comment (`gh issue close <n> --comment "..."`) and append a link + gist to the parent epic.
