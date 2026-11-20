Live (End-to-End) Test Plan
===========================

These tests execute against a real iMednet environment. They are skipped by default
and require ``IMEDNET_RUN_E2E=1`` with valid credentials (see
:doc:`/reference/configuration`). Each item below should be covered by a dedicated test case
so failures are easy to diagnose.

Endpoints
---------

For every endpoint the ``list`` and ``get`` operations should be verified:

- ``StudiesEndpoint``
- ``SitesEndpoint``
- ``SubjectsEndpoint``
- ``RecordsEndpoint``
- ``IntervalsEndpoint``
- ``VisitsEndpoint``
- ``VariablesEndpoint``
- ``FormsEndpoint``
- ``QueriesEndpoint``
- ``RecordRevisionsEndpoint``
- ``UsersEndpoint``
- ``JobsEndpoint``
- ``CodingsEndpoint``
- ``JobsEndpoint.get`` should also be validated independently using a known batch ID.
- ``RecordsEndpoint.create`` should be exercised to confirm record submission and that
  ``JobsEndpoint.get`` retrieves the resulting job status.

Every synchronous test should have an asynchronous counterpart using
``AsyncImednetSDK``. For example, ``AsyncImednetSDK.studies.list()`` mirrors
``ImednetSDK.studies.list()`` and should verify the same behaviors.

SDK Utilities
-------------

Common convenience methods on :class:`ImednetSDK` also deserve coverage:

- ``get_studies``
- ``get_records``
- ``get_sites``
- ``get_subjects``
- ``get_forms``
- ``get_intervals``
- ``get_variables``
- ``get_visits``
- ``get_codings``
- ``get_queries``
- ``get_record_revisions``
- ``get_users``
- ``get_job``
- ``poll_job``

Workflow Helpers
----------------

The workflow utilities in ``imednet_workflows`` should be tested using small,
non-destructive calls. Each bullet corresponds to a separate live test.

- ``get_study_structure`` and ``async_get_study_structure``
- ``RegisterSubjectsWorkflow.register_subjects``
- ``DataExtractionWorkflow.extract_records_by_criteria``
- ``DataExtractionWorkflow.extract_audit_trail``
- ``SubjectDataWorkflow.get_all_subject_data``
- ``QueryManagementWorkflow.get_open_queries``
- ``QueryManagementWorkflow.get_unresolved_queries``
- ``QueryManagementWorkflow.get_queries_for_subject``
- ``QueryManagementWorkflow.get_queries_by_site``
- ``QueryManagementWorkflow.get_query_state_counts``
- ``QueryManagementWorkflow.sync_queries_to_triage``
- ``RecordMapper.dataframe``
- ``RecordUpdateWorkflow.create_or_update_records``
- ``RecordUpdateWorkflow.register_subject``
- ``RecordUpdateWorkflow.update_scheduled_record``
- ``RecordUpdateWorkflow.create_new_record``

CLI Commands
------------

Live tests should execute the CLI via ``typer.testing.CliRunner``.
Run each command with minimal arguments and verify a successful exit code:

- ``imednet studies list``
- ``imednet sites list <STUDY_KEY>``
- ``imednet subjects list <STUDY_KEY>``
- ``imednet records list <STUDY_KEY>``
- ``imednet jobs status <STUDY_KEY> <BATCH_ID>``
- ``imednet jobs wait <STUDY_KEY> <BATCH_ID>``
- ``imednet export parquet <STUDY_KEY> tmp.parquet``
- ``imednet export csv <STUDY_KEY> tmp.csv``
- ``imednet export excel <STUDY_KEY> tmp.xlsx``
- ``imednet export json <STUDY_KEY> tmp.json``
- ``imednet export sql <STUDY_KEY> TABLE sqlite:///test.db``
- ``imednet workflows extract-records <STUDY_KEY>``

Integrations
------------

The integration helpers require validation using temporary files or buckets:

- ``export_to_csv``
- ``export_to_excel``
- ``export_to_json``
- ``export_to_parquet``
- ``export_to_sql``
- ``ImednetJobSensor`` and ``ImednetExportOperator`` from ``apache_airflow_providers_imednet``
- ``ImednetHook`` for connection retrieval
