Live Test Charter
=================

This document is the authoritative contract for all live test code in this
repository. It defines the purpose of the opt-in ``tests/live`` suite,
the mutation policy, environment prerequisites, discovery rules,
pass/skip/fail semantics, observability expectations, and a maintainer
runbook for interpreting results.

Every implementation issue that touches live testing should be
evaluated against this charter.

----

1. Purpose
----------

Offline checks and opt-in live validation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scheduled credential-dependent smoke testing and its Record-posting helper have
been retired. Expired or unavailable test credentials must not drive unattended
API requests or daily failure issues. The retired helper is also no longer a
pre-step of the manual live workflow.

Default CI retains offline unit, integration, Postman-contract, drift-regression,
and installed-package checks. These checks use synthetic or mocked responses;
they do not need live iMednet credentials. A test named "smoke" that only imports
SDK modules is an offline check and remains part of the suite.

The ``Live Integration Tests`` workflow remains available through manual
``workflow_dispatch`` with ``approved=true`` and the protected ``live-testing``
environment. It has no schedule or push/pull-request trigger. Its pytest suite
sets ``IMEDNET_RUN_E2E=1``; write scenarios still separately require
``IMEDNET_ALLOW_MUTATION=1``. Do not enable a live run to validate this retirement.

``tests/live`` pytest suite
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The live pytest suite provides **regression coverage of representative user
journeys** across every supported API surface.  It is not intended to cover
every permutation of every endpoint; it is intended to prove that each major
resource type and workflow can be reached and returns well-formed data.

A healthy live suite run means:

* every endpoint that is accessible in the configured environment returned
  expected model types,
* mutation scenarios ran when ``IMEDNET_ALLOW_MUTATION=1`` and produced
  completed jobs,
* no test failed due to a defect in the SDK; skipped tests have explicit,
  documented reasons.

----

2. Scope
--------

The live suite covers:

* all ``list`` and ``get`` operations for every endpoint,
* record creation and job polling (when mutation is enabled),
* CLI commands via ``typer.testing.CliRunner``,
* export helpers (CSV, Excel, JSON, Parquet, SQL),
* Airflow hook and operator integration (when Airflow is installed),
* workflow utilities (extract, register, update) when mutation is enabled.

Breadth of coverage is preferred over depth.  Each resource type needs at
least one live test.  Deep parametric tests for edge cases belong in the unit
suite.

----

3. Mutation policy
------------------

.. list-table::
   :header-rows: 1
   :widths: 35 15 50

   * - Coverage area
     - Read-only
     - Mutating (requires ``IMEDNET_ALLOW_MUTATION=1``)
   * - ``studies.list`` / ``studies.get``
     - ✓
     -
   * - ``sites.list`` / ``sites.get``
     - ✓
     -
   * - ``subjects.list`` / ``subjects.get``
     - ✓
     -
   * - ``records.list`` / ``records.get``
     - ✓
     -
   * - ``forms.list`` / ``forms.get``
     - ✓
     -
   * - ``intervals.list`` / ``intervals.get``
     - ✓
     -
   * - ``visits.list`` / ``visits.get``
     - ✓
     -
   * - ``variables.list``
     - ✓
     -
   * - ``queries.list``
     - ✓
     -
   * - ``record_revisions.list``
     - ✓
     -
   * - ``users.list``
     - ✓
     -
   * - ``codings.list``
     - ✓
     -
   * - ``jobs.get``
     - ✓
     -
   * - ``records.create``
     -
     - ✓
   * - ``poll_job``
     -
     - ✓
   * - ``RecordUpdateWorkflow`` (all methods)
     -
     - ✓
   * - ``RegisterSubjectsWorkflow.register_subjects``
     -
     - ✓

**Rules:**

* Read-only tests must always pass when credentials are valid and the
  environment contains at least one study.
* Mutating tests skip automatically when ``IMEDNET_ALLOW_MUTATION`` is not
  set to ``1``.  Missing the variable is never a failure.
* Mutation tests are responsible for their own cleanup.  No cleanup helpers
  are required, but tests must not assume data they create persists across
  runs.

----

4. Environment prerequisites
----------------------------

Required variables
~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 50 20

   * - Variable
     - Description
     - Required
   * - ``IMEDNET_API_KEY``
     - API key for authentication.
     - Always
   * - ``IMEDNET_SECURITY_KEY``
     - Security key for authentication.
     - Always
   * - ``IMEDNET_RUN_E2E``
     - Must be ``1`` to enable any live test.
     - Always
   * - ``IMEDNET_BASE_URL``
     - Override the default API base URL.
     - Optional
   * - ``IMEDNET_STUDY_KEY``
     - Pin a specific study instead of auto-discovery.
     - Optional
   * - ``IMEDNET_FORM_KEY``
     - Pin a specific form instead of auto-discovery.
     - Optional
   * - ``IMEDNET_BATCH_ID``
     - Supply a known batch ID for job-polling tests.
     - Optional
   * - ``IMEDNET_ALLOW_MUTATION``
     - Set to ``1`` to enable write-path tests.
     - Optional

Minimum environment state
~~~~~~~~~~~~~~~~~~~~~~~~~

The configured study must contain:

* at least one form with ``subject_record_report=true`` and ``disabled=false``
  for mutation tests,
* at least one site for site-listing tests; an active site is required for
  registration scenarios,
* at least one subject for subject-listing tests; an active subject is
  required for write scenarios,
* at least one non-disabled interval for interval-listing tests; a non-disabled
  interval is required for scheduled-visit scenarios.

Tests that require these entities **skip** when they are absent.  Absence is
treated as environment drift, not a code defect.  A failed API call
(network error, ``5xx``, ``4xx``) is always a test failure.

Acceptable status values
~~~~~~~~~~~~~~~~~~~~~~~~

The following table documents which statuses are treated as eligible.

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - Entity
     - Eligible for reads
     - Eligible for writes
   * - Study
     - any (first returned)
     - same
   * - Form
     - ``subject_record_report=true``, ``disabled=false``
     - same
   * - Site
     - any (first returned)
     - ``site_enrollment_status`` equals ``active`` or ``enrollment_open``
       (case-insensitive)
   * - Subject
     - any (first returned)
     - ``subject_status`` equals ``active``, ``registered``, ``baseline``, or
       ``enrolled`` (case-insensitive)
   * - Interval
     - ``disabled=false``
     - same

Status checks are **case-insensitive equality checks**, not substring matches.
This prevents false rejections when server environments use values like
``"Active"`` or ``"ACTIVE"``, while avoiding false positives from statuses
like ``"Inactive"`` or ``"Deactivated"``.

----

5. Discovery rules
------------------

Discovery is the process by which live tests locate the identifiers they need.
The rules are applied in the following priority order:

1. **Explicit environment variable** — if ``IMEDNET_STUDY_KEY`` (or
   ``IMEDNET_FORM_KEY``) is set, it is used without validation against the live
   environment.  The operator is responsible for correctness.
2. **Auto-discovery** — the discovery module queries the API and selects the
   first entity that satisfies the eligibility criteria in the table above.
3. **Skip** — if auto-discovery finds no eligible entity, the test skips with a
   message that names the missing entity.

Discovery **never fails a test** on its own.  A test fails only when the API
returns an error during discovery or when an assertion on the returned data
fails.

Capability-based selection means the eligibility criteria are evaluated on
observable attributes (``disabled``, ``subject_record_report``, status fields)
rather than on hard-coded status strings.  The ``imednet.discovery`` module
implements this contract.

----

6. Pass / skip / fail semantics
--------------------------------

``tests/live`` pytest suite
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 35 15 50

   * - Condition
     - Outcome
     - Meaning
   * - ``IMEDNET_RUN_E2E`` not ``1`` or credentials absent
     - **Skip** (whole suite)
     - Live tests are not enabled.  This is the expected default in CI
       without live secrets.
   * - Discovery finds no eligible entity for this test
     - **Skip** (single test)
     - The environment does not have the data this test needs.  Not a code
       defect.
   * - ``IMEDNET_ALLOW_MUTATION`` not set and test requires mutation
     - **Skip** (single test)
     - Mutation is not permitted in this run.  Not a defect.
   * - Optional dependency (``openpyxl``, ``pyarrow``, etc.) not installed
     - **Skip** (single test)
     - The optional integration is not available in this environment.
   * - API call returns a successful response and assertions pass
     - **Pass**
     - The SDK behaves correctly against the live server.
   * - API call returns a ``4xx`` or ``5xx`` response
     - **Fail**
     - The server rejected a valid request or returned an error.
   * - Network error, timeout, or connection refused
     - **Fail**
     - Infrastructure problem; the harness is misconfigured or the
       environment is down.
   * - Assertion on returned data fails
     - **Fail**
     - A code defect or schema mismatch was detected.

A **green** run means every non-skipped test passed.  A run with only
expected skips (no eligible data, mutation disabled, optional deps absent)
is also considered green.

A run that is **entirely skipped** because ``IMEDNET_RUN_E2E`` is not set is
not a signal about environment health; it simply means the run did not
execute.

A run with **any failure** indicates a real problem: a code defect, an API
change, or an infrastructure issue.

----

7. Observability expectations
------------------------------

At startup, ``tests/live/conftest.py`` prints the following context:

* whether ``IMEDNET_RUN_E2E`` is set,
* whether ``IMEDNET_ALLOW_MUTATION`` is set,
* the value of ``IMEDNET_STUDY_KEY`` (if set, or ``auto-discover`` otherwise),
* the resolved base URL.

This output is always visible, even when tests are subsequently skipped.  It
lets operators verify that the right environment was targeted before reading
individual test outcomes.

When a test is skipped due to missing data, the skip message must:

* name the specific entity that was not found (e.g. ``"No active sites for
  study ABC123"``),
* not mention credentials or other unrelated context.

When a test fails, the failure message must:

* include the HTTP status code and response body when an API error occurred,
* include the assertion that failed and the actual value returned.

Warnings emitted during a live run should be suppressed or redirected to a
log file unless they originate from the SDK itself.

----

8. Maintainer runbook
---------------------

Interpreting live pytest results
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

All tests skipped (``IMEDNET_RUN_E2E`` absent)
   Not a live run.  Expected in standard CI.

Tests skipped with data-absence messages
   The environment exists but lacks the required entities.  This is
   environment drift, not a code defect.  Populate the study or check that
   ``IMEDNET_STUDY_KEY`` points to a prepared study.

Mutation tests skipped
   ``IMEDNET_ALLOW_MUTATION=1`` was not set.  Intentional.

Tests failed with ``4xx`` or ``5xx`` errors
   The server rejected a request.  Check:

   1. Whether the API surface changed (compare SDK models against API docs).
   2. Whether the study key is valid and accessible.
   3. Whether a required environment entity changed status or was deleted.

Tests failed with network errors
   The host is unreachable.  Check ``IMEDNET_BASE_URL`` and network access.

Tests failed with assertion errors
   A code defect.  The SDK returned data that did not match the expected
   model or value.  Open a bug issue.

Determining the cause of a "no eligible data" skip
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For an explicitly requested live run, inspect the startup context and the
per-test skip reason. Pin ``IMEDNET_STUDY_KEY`` and ``IMEDNET_FORM_KEY`` when
you need a specific study and form. Use the EDC portal or deliberately invoked
read-only SDK/CLI queries to inspect the missing entity; do not create records
merely to diagnose a skip. Mutation coverage requires a prepared test study and
separate mutation opt-in.

Keeping the charter current
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Update this document when:

* a new endpoint or resource type is added,
* the mutation policy changes (e.g., cleanup is added),
* new environment variables are introduced,
* skip or fail semantics are intentionally changed.

The charter is the single source of truth for what a live run means.  Code
changes that alter live test behavior without updating this document should
be rejected in review.
