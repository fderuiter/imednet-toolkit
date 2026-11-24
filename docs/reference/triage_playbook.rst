Triage Playbook
===============

This playbook is for maintainers who intake, rewrite, prioritize, and decompose
issues.

Triage objectives
-----------------

- turn vague requests into actionable work items
- classify issues consistently
- identify duplicates and overlaps early
- connect execution issues to epics instead of embedding hierarchy in titles

New issue workflow
------------------

For each new issue:

1. Confirm the title follows ``<type>(<area>): <concise outcome>``. Rewrite it if needed.
2. Confirm the issue body includes a problem statement, outcome, acceptance criteria,
   and test/docs impact.
3. Apply labels across the required dimensions: type, area, priority, and status (starting at ``status:triage`` for unreviewed intake).
4. Add size labels (and ``risk:low`` where applicable; record elevated risk in the issue body and ``priority:*``) when they improve planning clarity.
5. Link the issue to an epic or initiative when the work is part of a larger stream.

If the issue lacks enough information to schedule work, apply ``question`` and
request the missing details. Use ``status:needs-design`` when architectural
judgment is required, ``ready-for-agent`` with ``status:ready`` when fully
specified for automated execution, or ``status:ready`` with ``help wanted``
(without ``ready-for-agent``) when manual human work is needed.

Required maintainer checks
--------------------------

Before an issue moves to ``status:ready``, confirm:

- the title is normalized
- scope is clear
- acceptance criteria are testable
- dependencies or sequencing are documented
- affected package and compatibility notes are present
- docs impact is explicit, even if the answer is ``none``

How to rewrite weak issues
--------------------------

When an issue is vague, rewrite the body into these sections:

- Problem
- Desired outcome
- Constraints or non-goals
- Acceptance criteria
- Test impact
- Docs impact

Move urgency and execution order out of the title and into labels or body notes.

Prioritization rubric
---------------------

Use the following guidance when assigning priority:

``priority:p0``
   Active production breakage, security impact, or a blocking regression.

``priority:p1``
   Important correctness or maintainability work that should land in the next
   planned delivery window.

``priority:p2``
   Valuable work that improves reliability, usability, or architecture but is not
   currently blocking other delivery.

``priority:p3``
   Nice-to-have work, backlog grooming, or longer-range improvements.

Capture elevated risk (such as changes affecting public API compatibility, credential
handling, concurrency, or critical transport logic) directly in the issue body and
reflect urgency with ``priority:*``; ``risk:low`` is the only ``risk:*`` label defined
on GitHub.

Epic and sub-issue management
-----------------------------

Use epics for multi-PR efforts that need coordinated delivery. Each epic should
track:

- objective
- scope and non-goals
- acceptance criteria
- linked child issues
- sequencing notes

Child issues should describe one implementable outcome each. Avoid titles such as
``[Step 1]`` or ``[Step 4]`` because sequencing may change as work evolves.

Duplicates and overlap
----------------------

When two issues describe the same work:

1. keep the clearer or more complete issue open
2. link the duplicate to the surviving issue
3. close the duplicate with an explanation

When two issues overlap but are not duplicates, clarify the split in both issue
bodies and link them explicitly.

Blocked and stale issues
------------------------

- Use ``status:blocked`` when an issue cannot proceed because of an upstream dependency.
- Use ``question`` when the reporter or maintainer must provide more detail before work can proceed.
- Close non-actionable issues with ``wontfix``, ``duplicate``, or ``invalid`` and record the rationale in the closing comment.
- Revisit blocked or ``question`` issues during backlog review instead of leaving them unlabeled.

Definition of done for triage
-----------------------------

Triage is complete when the issue is either:

- ready for implementation
- waiting on information
- marked blocked with a concrete dependency
- closed as duplicate, invalid, or superseded

Triage should reduce ambiguity, not just add labels.
