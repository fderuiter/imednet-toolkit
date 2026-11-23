Issue Management
================

This document defines the repository issue operating model for reporters,
contributors, and maintainers. It aligns issue intake with the engineering
constraints in ``AGENTS.md`` and the contributor workflow in ``CONTRIBUTING.md``.

Goals
-----

- Keep issue titles predictable and searchable.
- Separate classification from titles by using structured metadata and labels.
- Make epics and work items decomposable without title hacks such as ``[Step 1]``.
- Ensure every issue is actionable before implementation begins.

Issue hierarchy
---------------

The repository uses three levels of work tracking:

Initiatives
   Optional portfolio-level trackers for multi-epic programs. Use these sparingly
   and only when several epics must be coordinated together.

Epics
   Multi-PR efforts with a clear objective, scope, non-goals, and linked child
   issues.

Work items
   Single implementable outcomes such as a bug fix, feature, refactor, docs
   improvement, or test task.

Use GitHub sub-issues or explicit parent/child links in the issue body instead of
prefixes such as ``[Step 2]``.

Title convention
----------------

Issue titles should follow this format:

.. code-block:: text

   <type>(<area>): <concise outcome>

Examples:

- ``bug(http): delegate URL path construction to httpx``
- ``refactor(core): decouple async and sync client hierarchies``
- ``docs(governance): document issue triage workflow``
- ``epic(core): architecture modernization and thread-safety``

Avoid the following title patterns:

- ``[Step 1] ...``
- ``[P1] ...``
- ``[EPIC] ...``
- ``Initiative: ...``
- numbered defect titles such as ``4 Brittle URL Path Construction``

Priority, sequence, and hierarchy belong in labels, linked issues, and the issue
body rather than in the title.

Label taxonomy
--------------

Apply labels across consistent dimensions. Each ready work item should have one
label from the ``type:``, ``area:``, and ``priority:`` groups at minimum.

Type labels
^^^^^^^^^^^

- ``type:bug``
- ``type:feature``
- ``type:enhancement``
- ``type:refactor``
- ``type:tech-debt``
- ``type:docs``
- ``type:test``
- ``type:ci``
- ``type:security``
- ``type:epic``
- ``type:research``

Priority labels
^^^^^^^^^^^^^^^

- ``priority:p0``
- ``priority:p1``
- ``priority:p2``
- ``priority:p3``

Area labels
^^^^^^^^^^^

- ``area:core``
- ``area:models``
- ``area:http``
- ``area:auth``
- ``area:pagination``
- ``area:cli``
- ``area:workflows``
- ``area:airflow``
- ``area:docs``
- ``area:tests``
- ``area:testing``
- ``area:ci``
- ``area:release``
- ``area:github``
- ``area:mapping``
- ``area:records``
- ``area:standards``
- ``area:terminology``
- ``area:governance``
- ``area:performance``
- ``area:dashboard``
- ``area:review-workbench``
- ``area:streamlit``

Status and triage labels
^^^^^^^^^^^^^^^^^^^^^^^^

- ``status:triage``
- ``status:needs-design``
- ``status:ready``
- ``status:in-progress``
- ``status:blocked``
- ``status:review``
- ``status:done``
- ``question`` (waiting on reporter or maintainer clarification)
- ``ready-for-agent`` (fully specified brief ready for automated execution, paired with ``status:ready``)
- ``help wanted``
- ``wontfix`` / ``duplicate`` / ``invalid``

Size labels
^^^^^^^^^^^

- ``size:xs``
- ``size:s``
- ``size:m``
- ``size:l``
- ``size:xl``

Risk labels
^^^^^^^^^^^

- ``risk:low`` (only ``risk:low`` exists on GitHub; document medium or high risk explicitly in the issue body alongside ``priority:*`` rather than applying non-existent risk labels)

Required issue content
----------------------

Every implementation issue should capture the following:

- problem statement
- expected outcome
- constraints or non-goals
- acceptance criteria
- test impact
- docs impact
- affected package
- API surface impact
- compatibility impact
- verification requirements

The issue templates in ``.github/ISSUE_TEMPLATE/`` enforce this structure for new
issues.

Definition of Ready
-------------------

An issue is ready when it has:

- a normalized title
- one ``type:*`` label
- one ``area:*`` label
- one ``priority:*`` label
- a clear problem statement
- explicit acceptance criteria
- dependency notes when relevant
- documented test and docs impact

Definition of Done
------------------

An issue is done when:

- the linked PR is merged
- tests were added or updated when required
- docs and examples were updated when applicable
- status labels reflect the merged state
- the parent epic or initiative was updated when the work was part of a larger effort

Epic requirements
-----------------

Each epic should include:

- objective
- scope
- non-goals
- acceptance criteria
- linked child issues
- sequencing or dependency notes

An epic should track delivery, not duplicate full design documents.

Implementation issue requirements
---------------------------------

Each work item should answer:

1. What is wrong or missing today?
2. What outcome should exist when the issue is complete?
3. What constraints must be preserved?
4. How will reviewers know it is complete?

When a work item is derived from a larger effort, link the parent epic and avoid
duplicating milestone or sequencing information in the title.

PR linkage
----------

Pull requests should reference the issue they implement and use a Conventional
Commit title such as ``fix:``, ``feat:``, or ``refactor:``. Issue titles and PR
titles do not need to be identical, but they should describe the same outcome.

Migration guidance for existing issues
--------------------------------------

When retrofitting legacy issues:

1. Rename the issue to match the normalized title format.
2. Replace title prefixes with labels and body metadata.
3. Add parent or child links for epics and decomposed work.
4. Add missing acceptance criteria, dependency notes, and verification expectations.
5. Mark duplicates or superseded work explicitly instead of leaving overlapping issues ambiguous.
