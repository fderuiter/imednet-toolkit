# Triage Labels & Live GitHub Vocabulary

**Precedence rule:** This guide is the **authoritative label reference** for automated skills and agents in `imednet-toolkit` (`fderuiter/imednet-toolkit`), verified against live GitHub labels. Until `docs/reference/{issue_management,triage_playbook}.rst` and `.github/ISSUE_TEMPLATE/*.yml` are synchronized in a follow-up docs PR, **this file overrides any non-existent legacy label strings** listed there.

## Canonical Triage Role Mapping

| Triage role (in skills) | Live GitHub label(s) | Meaning |
| --- | --- | --- |
| `needs-triage` | `status:triage` | Maintainer needs to evaluate this issue |
| `needs-info` | `question` | Waiting on reporter for clarification (do not apply non-existent `status:needs-info`) |
| `ready-for-agent` | `ready-for-agent`, `status:ready` | Fully specified with brief, ready for agent execution |
| `ready-for-human` | `status:needs-design` (design/judgment) or `status:ready` + `help wanted` (without `ready-for-agent`) | Requires human decision or manual work |
| `wontfix` | `wontfix` (or `duplicate` / `invalid`) | Will not be actioned; rationale captured in closing comment |

## Category & Epic Mapping

- **Bug / Defect:** `type:bug`
- **Feature / Enhancement:** `type:feature` or `type:enhancement`
- **Refactor / Tech Debt:** `type:refactor` or `type:tech-debt`
- **Docs / CI / Tests / Security:** `type:docs`, `type:ci`, `type:test`, `type:security`
- **Multi-PR Epics & Research (`wayfinder` / `to-spec`):** `type:epic`, `type:research` (with `status:needs-design`, `status:blocked`, `status:ready`, `status:in-progress`, `status:review`, `status:done`)

## Legacy `.rst` / `.github/ISSUE_TEMPLATE/` Translation Table

When other repo docs cite legacy spellings absent on GitHub, translate them as follows:

| Documented legacy spelling | Live GitHub label to apply |
| --- | --- |
| `status:needs-info` / `needs-info` | `question` |
| `needs-triage` | `status:triage` |
| `ready-for-human` | `status:needs-design` or `help wanted` (omit `ready-for-agent`) |
| `type:maintenance` | `type:tech-debt` |
| `area:workflows-plugin` | `area:workflows` |
| `area:airflow-provider` | `area:airflow` |
| `area:contributing` | `area:github` or `area:docs` |
| `risk:medium`, `risk:high` | Omit nonexistent label; state risk and rationale in issue body and `priority:*` |
| `wayfinder:map`, `wayfinder:*` | `type:epic`, `type:research` paired with `status:*` |

## Supported Dimensions

- **Area:** `area:core`, `area:http`, `area:models`, `area:auth`, `area:pagination`, `area:cli`, `area:workflows`, `area:airflow`, `area:docs`, `area:tests`, `area:release`, `area:github`, `area:ci`, `area:testing`, `area:streamlit`, `area:mapping`, `area:records`, `area:standards`, `area:dashboard`, `area:review-workbench`, `area:terminology`, `area:governance`, `area:performance`.
- **Priority & Size:** `priority:p0`, `priority:p1`, `priority:p2`, `priority:p3`; `size:xs`, `size:s`, `size:m`, `size:l`, `size:xl`; `risk:low`.
