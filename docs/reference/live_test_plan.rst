Live (End-to-End) Test Plan
===========================

These tests execute against a real iMednet environment. They are skipped by default
and require ``IMEDNET_RUN_E2E=1`` with valid credentials (see
:doc:`/reference/configuration`). Each item below should be covered by a dedicated test case
so failures are easy to diagnose.

Endpoints
---------

Live endpoint tests reflect each EDC resource's supported operations:

**List-and-get endpoints** (verify both ``list`` and ``get``):

- ``StudiesEndpoint`` (``studies.list()`` and ``studies.get(None, study_key)``)
- ``SitesEndpoint`` (``sites.list`` and ``sites.get``)
- ``SubjectsEndpoint`` (``subjects.list`` and ``subjects.get``)
- ``RecordsEndpoint`` (``records.list`` and ``records.get``)
- ``FormsEndpoint`` (``forms.list`` and ``forms.get``)
- ``IntervalsEndpoint`` (``intervals.list`` and ``intervals.get``)
- ``VariablesEndpoint`` (``variables.list`` and ``variables.get``)
- ``RecordRevisionsEndpoint`` (``record_revisions.list`` and ``record_revisions.get``)

**List-only endpoints** (verify ``list`` and confirm no ``get`` method is exposed):

- ``VisitsEndpoint`` (``visits.list``)
- ``QueriesEndpoint`` (``queries.list``)
- ``UsersEndpoint`` (``users.list``)
- ``CodingsEndpoint`` (``codings.list``)

**Job status and Record submission endpoints**:

- ``JobsEndpoint`` does not expose ``list``; validate ``JobsEndpoint.get(study_key, batch_id)`` independently using a known batch ID (``generated_batch_id``), alongside ``JobsEndpoint.poll`` / ``poll_many`` helpers.
- ``RecordsEndpoint.create`` (when ``IMEDNET_ALLOW_MUTATION=1``) exercises ``register``, ``scheduled``, and ``new`` Record payloads and confirms ``JobsEndpoint.get`` retrieves the resulting ``Job`` status.

Study-scoped synchronous tests have asynchronous counterparts on
``AsyncImednetSDK`` (while ``AsyncStudiesEndpoint`` also provides ``list`` and
``get``). Synchronous ``list(...)`` methods return an ``Iterator`` whereas
``AsyncImednetSDK`` ``list(...)`` methods return an ``AsyncIterator`` consumed with
``async for``, and ``await async_sdk.<endpoint>.get(...)`` awaits single-entity
lookups on list-and-get endpoints.

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

Live tests execute the ``argparse``-based CLI entrypoint (``imednet.cli.app``) via the
in-process ``CliRunner`` helper in ``tests/live/test_cli_live.py``, capturing ``stdout``,
``stderr``, and ``SystemExit`` codes. Run each command and verify a zero exit code:

- ``imednet studies list``
- ``imednet sites list <STUDY_KEY>``
- ``imednet subjects list <STUDY_KEY>``
- ``imednet records list <STUDY_KEY>``
- ``imednet jobs status <STUDY_KEY> <BATCH_ID>``
- ``imednet jobs wait <STUDY_KEY> <BATCH_ID> --interval 1 --timeout 60``
- ``imednet export parquet <STUDY_KEY> data.parquet``
- ``imednet export csv <STUDY_KEY> data.csv``
- ``imednet export excel <STUDY_KEY> data.xlsx``
- ``imednet export json <STUDY_KEY> data.json``
- ``imednet export sql <STUDY_KEY> table sqlite:///test.db --single-table``
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
