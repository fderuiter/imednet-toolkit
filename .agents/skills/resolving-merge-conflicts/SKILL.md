---
name: resolving-merge-conflicts
description: "Use when resolving an in-progress git merge or rebase conflict by tracing primary intent across both branches."
---

1. **Inspect conflict state:** Review conflicting files, branch history (`git log --merge`), and original PRs/issues (`gh pr view` / `gh issue view`).
2. **Trace primary intent:** Read commits on both sides of each conflict to understand *why* both edits happened.
3. **Resolve hunk by hunk:** Preserve both intents where compatible; where mutually exclusive, keep the behavior required by the target integration and explicitly note the trade-off. Resolve cleanly—never run `--abort` silently.
4. **Verify gates (`imednet-toolkit`):** Run `git diff --check`, `uvx pre-commit run --all-files`, `hatch run verify`, and `hatch run docs` (noting any environment blockers explicitly).
5. **Complete:** Stage resolved files cleanly and finalize the merge or `git rebase --continue`.
