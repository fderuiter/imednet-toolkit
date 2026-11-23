Airflow Integration
===================

Airflow support is provided via a standalone provider package.

Installation
------------

Install the provider package:

The current 1.x workflows, sinks and Airflow provider require core
``imednet>=1.0.0,<2.0.0``. Install matching 1.x packages together; the former
pre-1.0 dependency range excluded the released core and prevented ordinary
pip installation. Package versions remain managed by release-please.

.. code-block:: bash

   pip install "apache-airflow>=3.2.0,<4.0.0" apache-airflow-providers-imednet
   # Optional destination, lineage, and cloud extras:
   pip install "apache-airflow-providers-imednet[export]"
   pip install "apache-airflow-providers-imednet[duckdb]"
   pip install "apache-airflow-providers-imednet[sinks]"
   pip install "apache-airflow-providers-imednet[snowflake,mongodb,neo4j]"
   pip install "apache-airflow-providers-imednet[openlineage,amazon]"
   pip install "apache-airflow-providers-imednet[all]"

Production Reference DAG
------------------------

Use ``examples/airflow/airflow_multi_study_pipeline.py`` as the recommended production
pattern for dynamic task mapping deployments. It demonstrates lightweight
TaskFlow discovery feeding mapped provider operators while keeping execution
logic inside mapped tasks.

.. literalinclude:: /../examples/airflow/airflow_multi_study_pipeline.py
   :language: python

Operational safeguards highlighted in the reference DAG:

- Discovery only enumerates work items and returns JSON-serializable values.
- Retries and ``execution_timeout`` are configured for both discovery and
  mapped export execution.
- ``max_active_runs`` and ``max_active_tasks`` constrain DAG-level concurrency.
- ``pool`` is set on the mapped operator for shared-resource throttling.

Additional single-purpose DAGs are available in ``examples/airflow/`` for
focused operator/sensor usage patterns.

Connections
-----------

Create an Airflow connection ``imednet_default`` (connection type ``imednet``,
registered via ``apache_airflow_providers_imednet:get_provider_info``) or override
``imednet_conn_id``.
Provide ``api_key`` and ``security_key`` via the login/password fields (which the
Airflow UI relabels as **API Key** and **Security Key** via
``get_ui_field_behaviour()``), via custom connection form widgets
(``extra__imednet__*``), or in the ``extra`` JSON. ``base_url``,
password-masked ``oidc_token``, ``timeout``, ``strict_mode``, and
``simulation_mode`` are exposed directly as typed connection form fields. Base URL
resolution honors an explicit ``base_url`` first, then resolves ``Connection.host``
(with optional ``scheme`` and ``port``) while stripping ``/api/v1/edc``,
``/api/v1``, and ``/api`` suffixes before falling back to
``imednet.config.load_config`` environment defaults.
``ImednetHook.test_connection()`` validates credentials against the ``Study``
endpoint and redacts sensitive tokens on error.

For warehouse, graph, document, DuckDB, and transactional SQL exports, configure a
separate Airflow connection and pass ``destination_conn_id`` on
``ImednetExportOperator``. ``ImednetHook.resolve_destination_connection()``
resolves destination credentials (Snowflake ``account``/``user``/``password``
and coordinates, Neo4j ``uri``/``auth``/``database``, MongoDB ``uri`` and
database settings, DuckDB ``db_path`` and ``table_name``, or SQLAlchemy
connection URIs including SQLite ``sqlite:////abs/path.db``,
``sqlite:///rel/path.db``, and ``sqlite:///:memory:``) at task execution time
rather than embedding secrets in DAG definitions.

To prevent credential leakage in Airflow rendered task metadata and XComs,
templated fields (``study_key``, ``output_path``, ``export_kwargs``,
``modified_since``, ``record_filter``, ``record_data_filter``, and
``stream_name``) reject embedded URI userinfo, secrets in query strings,
single-delimiter credential assignments (such as ``api_key=leak``), and bearer
JWTs while permitting clinical ``Variable`` filter expressions (for example
``TOKEN==1`` or ``NOTES=="bearer of card"``) and explicit ``export_kwargs={"auth": "none"}``.
Secret option keys in ``export_kwargs`` (such as ``password``, ``api_key``,
``security_key``, ``token``, or non-``"none"`` ``auth``) remain blocked so
destination credentials are resolved via ``destination_conn_id``.

For task-mapping discovery, health checks, and async trigger execution,
``ImednetHook`` provides explicit helper methods (falling back to
``Connection.get_connection_from_secrets`` when ``SUPERVISOR_COMMS`` is absent
so ``test_connection()`` and ``dag.test()`` resolve database-stored
``Connection`` entries directly while propagating
``AirflowSecretsBackendAccessDenied``):

- ``get_conn()`` returns the live :class:`~imednet.ImednetSDK` adhering to the
  standard Airflow BaseHook contract.
- ``get_sdk_client()`` is a direct alias for ``get_conn()`` for use inside task
  execution context.
- ``get_async_conn()`` returns an :class:`~imednet.AsyncImednetSDK` resolved via
  ``BaseHook.aget_connection()`` (falling back to ``asyncio.to_thread()``) for
  deferrable triggers.
- ``get_base_url()`` returns the normalized non-secret API base URL for lineage
  namespaces and diagnostics.
- ``test_connection()`` validates connection credentials from the Airflow UI or
  CLI and scrubs sensitive tokens and URI credentials from error messages.
- ``list_studies_metadata()`` returns JSON-serializable primitive dictionaries
  (with sensitive keys redacted) for safe mapped task expansion.
- ``list_study_keys()`` returns primitive study keys only.
- ``describe_connection()`` returns redacted connection metadata and never exposes
  raw credentials.
- ``resolve_destination_connection()`` resolves runtime destination connection
  settings for ``snowflake``, ``neo4j``, ``mongodb``, ``duckdb``, and ``sql``
  (including ``sql_by_form`` and ``long_sql``) targets.

Operators and Sensors
---------------------
The Airflow integration organizes hooks, operators, and sensors in dedicated
subpackages for clarity.

``ImednetExportOperator`` is a unified execution engine that seamlessly writes
records to any supported destination (CSV, Parquet, DuckDB, SQL / ``sql_by_form``
/ ``long_sql``, Snowflake, Neo4j, MongoDB, etc.)
using the core SDK's universal sink interface. Common operational parameters
(``batch_size``, ``max_retries``, ``idempotent``, ``mask_phi``,
``quality_gate_enabled`` — ``False`` by default and deterministically forwarded
to tabular and sink targets — and ``min_schema_readiness_score``) work
consistently across plugin sinks and tabular exports without leaking sink-level
options into Pandas or JSON writer kwargs. PHI masking is enabled by default
(``mask_phi=True``) across all tabular and sink destinations, replacing
registered ``Subject`` identifiers and committed ``StudyConfiguration``
``phiFields`` with ``***MASKED***``; authorized enclave workflows can explicitly
opt out by passing ``mask_phi=False`` (or ``export_kwargs={"mask_phi": False}``)
without mutating ``global_sensitivity_registry``. Destination-specific non-secret
options—such as DuckDB ``table_name`` (or ``table``, defaulting to ``"records"``),
Snowflake staging settings (``stage_prefix``,
``local_staging_dir``, ``manifest_path``), and Neo4j ``database`` names—can be
supplied via ``export_kwargs`` or connection ``extra`` metadata.
``ImednetJobSensor(study_key=..., batch_id=..., imednet_conn_id=...)`` renders
templated ``study_key``, ``batch_id``, and ``imednet_conn_id`` fields and polls
an asynchronous iMednet ``Job`` until it reaches a terminal state (see
:ref:`rest_resource_jobs`). On completion, both synchronous (``deferrable=False``)
and deferrable (``deferrable=True``) runs emit an identical, compact ``Job``
summary XCom with raw ``results`` stripped and ``Subject`` PHI masked via
:func:`~imednet.utils.security.mask_clinical_phi`. Terminal ``resultUrl``
payloads are verified so batch-level or per-``Record`` failures scrub ``Subject``
PHI and raise a redacted ``AirflowException``. When ``soft_fail=True``, polling
timeouts are converted to ``AirflowSkipException`` in both modes, whereas
terminal ``FAILED`` ``Job`` states and ``failedRecords > 0`` batches still mark
the ``DagRun`` as ``FAILED``.
All operators use ``ImednetHook`` to obtain an :class:`~imednet.ImednetSDK` instance
from an Airflow connection. The production reference DAG above shows the recommended
dynamic-mapping pattern: keep static settings (for example ``destination``,
``imednet_conn_id``, and ``destination_conn_id``) in ``.partial(...)`` and map
only non-secret runtime fields (``study_key``, ``output_path``, ``export_kwargs``).

.. testcode::

   from datetime import datetime
   from typing import Any
   from airflow.sdk import DAG, task
   from apache_airflow_providers_imednet import ImednetExportOperator, ImednetHook

   with DAG(
       dag_id="imednet_dynamic_export",
       start_date=datetime(2024, 1, 1),
       schedule=None,
       catchup=False,
   ):

       @task
       def export_targets() -> list[dict[str, Any]]:
           hook = ImednetHook("imednet_default")
           return [
               {
                   "study_key": study_key,
                   "output_path": f"/tmp/{study_key}.csv",
                   "export_kwargs": {},
               }
               for study_key in hook.list_study_keys()
           ]

       ImednetExportOperator.partial(
           task_id="export_records",
           destination="csv",
           batch_size=1000,
           max_retries=5,
           idempotent=True,
           quality_gate_enabled=True,
           min_schema_readiness_score=95.0,
           imednet_conn_id="imednet_default",
       ).expand_kwargs(export_targets())

For warehouse or database sinks, bind ``destination_conn_id`` so credentials are
resolved only inside ``execute()``:

.. code-block:: python

   from apache_airflow_providers_imednet import ImednetExportOperator

   ImednetExportOperator(
       task_id="export_snowflake",
       study_key="MY_STUDY",
       destination="snowflake",
       imednet_conn_id="imednet_default",
       destination_conn_id="snowflake_warehouse",
       batch_size=1000,
       max_retries=5,
       idempotent=True,
       quality_gate_enabled=True,
       min_schema_readiness_score=95.0,
       export_kwargs={
           "table": "CLINICAL_RECORDS",
           "stage_prefix": "imednet/MY_STUDY",
           "manifest_path": "/tmp/imednet_manifest.jsonl",
       },
   )

Incremental Checkpoints with ``AirflowStateProvider`` and ``ExtractionStateLedger``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``ImednetExportOperator`` forwards all eight streaming checkpoint and ``Record``
filter parameters directly to :func:`~imednet.spi.sink_base.run_streaming_export`
and tabular export helpers while maintaining ``mask_phi=True`` by default:

- ``incremental``: Enable transactional high-water mark reads and commits.
- ``stream_name``: Stream key used to isolate checkpoints per destination or ``Form``.
- ``state_provider``: Explicit checkpoint provider such as
  :class:`~imednet_workflows.state_ledger.AirflowStateProvider` (persisting JSON
  watermarks via ``airflow.sdk.Variable`` under
  ``imednet_state:<study_key>:<stream_name>``, preserving per-``Study`` state
  across dynamic task mapping order changes and rolling ``RUNNING`` checkpoints
  back to ``FAILED`` on ``BaseException`` interrupts such as ``AirflowTaskTimeout``)
  or :class:`~imednet_workflows.ExtractionStateLedger`
  (:class:`~imednet_workflows.state_ledger.FileStateProvider`). When omitted on an
  ``incremental=True`` run, ``ImednetExportOperator`` defaults to the ``airflow``
  provider entry point automatically unless ``ledger_path`` is specified.
- ``ledger_path``: Shared filesystem path used when persisting checkpoints with
  ``ExtractionStateLedger`` instead of Airflow ``Variable`` storage.
- ``modified_since``: Initial UTC lower-bound watermark when no prior checkpoint exists.
- ``record_filter``: ``Record`` metadata filter dictionary or expression string.
- ``record_data_filter``: ``Variable`` payload filter expression (``recordDataFilter``).
- ``include_deleted``: Include voided (``deleted=True``) ``Record`` items directly
  in destination batches instead of reconciling deletes separately.

.. testcode::

   from apache_airflow_providers_imednet import ImednetExportOperator
   from imednet_workflows.state_ledger import AirflowStateProvider, ExtractionStateLedger

   airflow_state = AirflowStateProvider()
   shared_file_ledger = ExtractionStateLedger("/tmp/airflow_shared_ledger.json")

   incremental_export_task = ImednetExportOperator(
       task_id="incremental_export_snowflake",
       study_key="MY_STUDY",
       destination="snowflake",
       imednet_conn_id="imednet_default",
       destination_conn_id="snowflake_warehouse",
       mask_phi=True,
       incremental=True,
       stream_name="snowflake_vital_signs",
       ledger_path=None,
       state_provider=airflow_state,
       modified_since="2026-01-01T00:00:00Z",
       record_filter={"form_key": "F_VS"},
       record_data_filter="V_WEIGHT>0",
       include_deleted=False,
       export_kwargs={"table": "VITAL_SIGNS_RECORDS"},
   )

Deferrable Job Polling with ``ImednetJobTrigger``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``ImednetJobSensor`` supports Airflow 3 deferrable execution via ``deferrable=True``
(defaulting to ``airflow.configuration.conf.getboolean("operators", "default_deferrable", fallback=False)``
when ``deferrable=None``). In deferrable mode, ``execute()`` validates rendered
``study_key``, ``batch_id``, and ``imednet_conn_id`` identifiers and yields the
worker slot to :class:`~apache_airflow_providers_imednet.ImednetJobTrigger`,
which polls iMednet asynchronously inside the Airflow triggerer using
:meth:`~apache_airflow_providers_imednet.hooks.ImednetHook.get_async_conn` and
:class:`~imednet.utils.job_poller.AsyncJobPoller` (including terminal
``resultUrl`` verification for per-``Record`` failures).

``ImednetJobTrigger.serialize()`` persists only non-secret identifiers
(``study_key``, ``batch_id``, ``imednet_conn_id``, ``poke_interval``, and
``timeout``) to the Airflow metadata database. ``ImednetJobTrigger.run()``
excludes ``results`` and applies :func:`~imednet.utils.security.mask_clinical_phi`
before emitting a ``TriggerEvent`` so ``execute_complete()`` writes the exact
same PHI-free ``Job`` summary XCom as synchronous ``poke()``. Polling timeouts
emit ``{"status": "timeout"}`` (raising ``AirflowSensorTimeout``, which
``soft_fail=True`` skips via ``AirflowSkipException``), while terminal ``Job``
or ``Record`` failures emit ``{"status": "error"}`` with ``Subject`` PHI and
secrets scrubbed so broken ``Job`` batches fail the ``DagRun`` even under
``soft_fail=True``.

.. testcode::

   from apache_airflow_providers_imednet import ImednetJobSensor

   wait_for_batch = ImednetJobSensor(
       task_id="wait_for_batch",
       study_key="MY_STUDY",
       batch_id="BATCH_001",
       imednet_conn_id="imednet_default",
       poke_interval=30,
       timeout=3600,
       deferrable=True,
   )

URL Path Segment Validation Across Operators, Sensors, and Triggers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Airflow tasks frequently populate ``study_key`` and ``batch_id`` dynamically from DAG
parameters, mapped upstream XComs, or external REST payloads covering ``Study``,
``Subject``, ``Site``, ``Form``, ``Record``, ``Visit``, ``Interval``, ``Variable``,
``Query``, ``Data Dictionary``, and ``Job`` pipelines. Every provider entry point
validates dynamic URL segments through :func:`~imednet.utils.validate_path_segment`
(before formatting endpoints via :func:`~imednet.utils.build_safe_path`) and raises
:class:`~imednet.errors.PathTraversalValidationError` (``status_code=400``) before
opening connections or issuing HTTP requests:

- ``ImednetExportOperator.execute()`` validates templated ``study_key`` immediately after
  checking templated attributes for embedded credentials.
- ``ImednetJobSensor.poke()`` and ``ImednetJobSensor.execute()`` validate both
  ``study_key`` and ``batch_id`` prior to synchronous job polling or deferrable handoff.
- :class:`~apache_airflow_providers_imednet.triggers.ImednetJobTrigger` validates
  ``study_key`` and ``batch_id`` in both ``__init__()`` and ``run()`` alongside
  control-character and secret-token guards.

.. testcode::

   from typing import Any, cast
   from apache_airflow_providers_imednet import (
       ImednetExportOperator,
       ImednetJobSensor,
       ImednetJobTrigger,
   )
   from imednet.errors import PathTraversalValidationError
   from imednet.utils import build_safe_path, validate_path_segment

   assert validate_path_segment("MY_STUDY", name="study_key") == "MY_STUDY"
   assert (
       build_safe_path("studies", "MY_STUDY", "jobs", "BATCH_001")
       == "studies/MY_STUDY/jobs/BATCH_001"
   )

   bad_export_op = ImednetExportOperator(
       task_id="reject_traversal_export",
       study_key="../escape",
       destination="csv",
   )
   op_rejected = False
   try:
       ImednetExportOperator.execute(bad_export_op, cast(Any, {}))
   except PathTraversalValidationError as op_path_err:
       op_rejected = True
       assert op_path_err.status_code == 400
   assert op_rejected

   bad_job_sensor = ImednetJobSensor(
       task_id="reject_traversal_sensor",
       study_key="MY_STUDY",
       batch_id="%2e%2e%2fsecret",
       deferrable=False,
   )
   try:
       bad_job_sensor.poke(cast(Any, {}))
   except PathTraversalValidationError as sensor_path_err:
       assert sensor_path_err.status_code == 400

   try:
       ImednetJobTrigger(study_key="MY_STUDY", batch_id="../escape")
   except PathTraversalValidationError as trigger_path_err:
       assert trigger_path_err.status_code == 400

OpenLineage Dataset Facets
~~~~~~~~~~~~~~~~~~~~~~~~~~

When ``apache-airflow-providers-common-compat`` or ``apache-airflow-providers-openlineage``
is installed (available via ``apache-airflow-providers-imednet[openlineage]``),
``ImednetExportOperator`` implements ``get_openlineage_facets_on_start()`` and
``get_openlineage_facets_on_complete()`` lazily:

- **Input dataset**: ``namespace`` normalized from ``ImednetHook.get_base_url()``
  (for example ``https://edc.prod.imednetapi.com``) and ``name``
  ``study.<study_key>.records``.
- **Output dataset**: sanitized destination coordinates with userinfo, passwords,
  and query strings stripped across ``snowflake`` (``snowflake://<account>`` /
  ``<database>.<schema>.<table>``), ``duckdb`` (``duckdb`` /
  ``<db_path>.<table_name>``), ``sql`` / ``sql_by_form`` / ``long_sql``
  (``postgres://host:port`` or ``sqlite``), ``mongodb`` (``mongodb://<host>`` / ``<database>.<collection>``),
  ``neo4j`` (``neo4j://<host>`` / ``<database>.<label>``), object storage URIs
  (``s3://``, ``gs://``, ``abfss://``), and local ``file`` paths.

Testing with Airflow
--------------------

The unit tests stub out the Airflow dependencies. When the ``airflow`` package
is installed, ``tests/integration/test_airflow_dag.py`` runs a small DAG with
``ImednetExportOperator`` and ``ImednetJobSensor``. It is
skipped automatically if Airflow is missing.

Procedural export helpers use ``imednet.spi.export_helpers``, an explicit
module namespace for CSV, JSON, Excel, Parquet and SQL helpers. The legacy
``imednet.spi.export`` binding remains available for compatibility; provider
delegation does not depend on whether that name resolves to a dispatcher or
an imported module.
