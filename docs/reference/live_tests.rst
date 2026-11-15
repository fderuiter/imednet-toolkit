Live Test Overview
==================

This document summarizes the end-to-end tests located in ``tests/live``. These tests
execute against a real iMednet environment and are skipped unless
``IMEDNET_RUN_E2E=1`` is set along with valid credentials (see
:doc:`/reference/configuration`). Each test verifies that the SDK behaves correctly when
interacting with a running server.

Scheduled smoke tests have been retired. The GitHub Actions live workflow runs
only through manual dispatch with ``approved=true`` in the ``live-testing``
environment. It no longer posts smoke Records before pytest. Mutating scenarios
remain separately gated by ``IMEDNET_ALLOW_MUTATION=1``. Default offline CI and
synthetic Postman contract checks do not require these credentials.

The manual workflow uses a read-only GitHub token and publishes outcome counts
in the Actions job summary. Test failures, missing results, and report aggregation
failures all fail the job; it does not post pull-request comments.

The suite can auto-discover required IDs when environment variables are unset.
See :mod:`imednet.discovery` for details on locating study, form, site, subject,
and interval identifiers at runtime.

See :doc:`test_skip_conditions` for a summary of all variables and optional
dependencies that control skipping.

CLI Tests (``test_cli_live.py``)
--------------------------------
These checks invoke the CLI using ``typer.testing.CliRunner`` and expect a zero exit code:

- ``imednet studies list`` — Lists available studies.
- ``imednet sites list <STUDY_KEY>`` — Lists sites for the chosen study.
- ``imednet subjects list <STUDY_KEY>`` — Lists subjects for the study.
- ``imednet records list <STUDY_KEY>`` — Lists records for the study.
- ``imednet jobs status <STUDY_KEY> <BATCH_ID>`` — Retrieves the status of a known job.
- ``imednet jobs wait <STUDY_KEY> <BATCH_ID>`` — Waits until the specified job completes.
- ``imednet export parquet <STUDY_KEY> file.parquet`` — Exports records to Parquet.
- ``imednet export csv <STUDY_KEY> file.csv`` — Exports records to CSV.
- ``imednet export excel <STUDY_KEY> file.xlsx`` — Exports records to Excel.
- ``imednet export json <STUDY_KEY> file.json`` — Exports records to JSON.
- ``imednet export sql <STUDY_KEY> TABLE sqlite:///file.db`` — Exports records to a SQL database.
- ``imednet workflows extract-records <STUDY_KEY>`` — Runs the workflow to export records.

Each command should complete successfully and create the expected output file where applicable.

Endpoint Tests
--------------

### Synchronous (``test_endpoints_sync_live.py``)
These tests verify that every REST endpoint works as expected using ``ImednetSDK``.
When creating records, the first available form in the study is used
automatically so ``IMEDNET_FORM_KEY`` does not need to be set:

- Listing endpoints (``list`` methods) return a list of model objects.
- Individual ``get`` calls return an object whose key matches the input.
- ``RecordsEndpoint.create`` submits a new record and ``JobsEndpoint.get`` retrieves the resulting job status.
- ``JobsEndpoint.get`` with a known batch ID returns the corresponding job.
- All endpoints tested include studies, sites, subjects, records, intervals, visits, variables, forms, queries, record revisions, users, jobs, and codings.

### Asynchronous (``test_endpoints_async_live.py``)
The asynchronous tests mirror the synchronous ones using ``AsyncImednetSDK``.
Every ``async_list`` and ``async_get`` call behaves the same as the synchronous
version, and an additional check creates a record and polls the resulting job.

Schema Validation (``test_schema_validator_live.py``)
-----------------------------------------------------
Uses ``SchemaValidator`` to ensure that invalid data raises ``ValidationError`` and that schemas are cached correctly:

- Submitting a payload with an unknown variable should raise ``ValidationError``.
- Submitting a payload with the wrong data type should also raise ``ValidationError``.

SDK Utility Helpers (``test_sdk_utilities_live.py``)
----------------------------------------------------
Confirms that convenience wrapper methods on ``ImednetSDK`` return lists or objects as expected. This includes ``get_studies``, ``get_records``, ``get_sites``, ``get_subjects``, ``get_forms``, ``get_intervals``, ``get_variables``, ``get_visits``, ``get_codings``, ``get_queries``, ``get_record_revisions``, ``get_users``, ``get_job``, and ``poll_job``.

Integration Helpers (``test_integrations_live.py``)
---------------------------------------------------
Verifies helper functions and Airflow operators:

- ``export_to_csv``, ``export_to_excel``, ``export_to_json``, ``export_to_parquet``, and ``export_to_sql`` create the requested file.
- ``ImednetHook`` returns a valid SDK connection.
- ``ImednetExportOperator`` executes successfully when given minimal parameters.
- ``ImednetJobSensor`` raises an exception when polling fails.
- When Airflow is installed the suite also runs ``test_airflow_dag.py`` to execute a simple DAG using these operators.

Workflow Tests (``test_workflows_live.py``)
-------------------------------------------
Exercises workflow utilities with non-destructive calls:

- ``get_study_structure`` and ``async_get_study_structure`` return the structure for the specified study.
- ``RegisterSubjectsWorkflow.register_subjects`` submits registration requests when mutations are allowed.
- ``DataExtractionWorkflow.extract_records_by_criteria`` and ``extract_audit_trail`` return lists of records or audit entries.
- ``SubjectDataWorkflow.get_all_subject_data`` fetches details for a single subject.
- Query management helpers return lists or dictionaries of queries and counts.
- ``RecordMapper.dataframe`` yields a DataFrame-like object.
- ``RecordUpdateWorkflow`` methods (``create_or_update_records``, ``register_subject``, ``update_scheduled_record``, ``create_new_record``) start a batch job and return a job object when mutations are enabled.

Fake Data Utilities
-------------------
The ``imednet.testing.fake_data`` module offers helper functions for generating realistic payloads using ``Faker``. These payloads match the REST API examples and can be parsed directly by the SDK models for offline testing.

``fake_forms_for_cache`` and ``fake_variables_for_cache`` create ``Form`` and ``Variable`` objects that can populate a ``SchemaCache``. Patch ``FormsEndpoint.list`` and ``VariablesEndpoint.list`` in your tests to return these lists before calling ``schema.refresh()``. Use ``fake_record`` with that cache to produce typed record payloads for validation.

Expected Results
----------------
All live tests should pass when run against a properly configured iMednet environment. Each test ensures that API calls succeed without raising exceptions and that any created files or returned objects match the requested parameters. Failures typically indicate connectivity issues or a mismatch between the SDK and server APIs.
