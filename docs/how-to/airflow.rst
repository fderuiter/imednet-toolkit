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
In the Apache Airflow 3 UI, the ``imednet`` connection form hides ``host``,
``schema``, ``port``, and ``extra`` via ``get_provider_info()`` and
``ImednetHook.get_ui_field_behaviour()``. When configuring an ``imednet``
connection in the UI, supply ``api_key`` and ``security_key`` in the ``login``
and ``password`` fields (relabeled as **API Key** and **Security Key**) and use
the typed connection form widgets (``extra__imednet__*``) for ``base_url``,
password-masked ``oidc_token``, ``timeout``, ``strict_mode``, and
``simulation_mode``.
To pass ``host`` (with optional ``schema`` and ``port``) or raw ``extra`` JSON
payloads directly, create the connection via the Airflow CLI
(``airflow connections add``), an ``AIRFLOW_CONN_*`` URI or JSON environment
variable, or a ``generic`` connection type. Base URL resolution honors an
explicit ``base_url`` first, then resolves ``Connection.host`` (with optional
``schema`` and ``port``) while stripping ``/api/v1/edc``, ``/api/v1``, and
``/api`` suffixes before falling back to ``imednet.config.load_config``
environment defaults.
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
``stream_name``) reject raw secret keys (such as ``password``, ``auth``,
``api_key``, ``security_key``, ``token``, or ``conn_str`` containing
credentials) as well as URIs with embedded userinfo.

For task-mapping discovery, health checks, and async trigger execution,
``ImednetHook`` provides explicit helper methods:

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
The Airflow integration organizes hooks, operators, sensors, and triggers in
dedicated subpackages under the canonical ``apache_airflow_providers_imednet``
distribution namespace (shipped with ``py.typed`` markers) while registering a
runtime ``sys.meta_path`` alias for ``airflow.providers.imednet.*`` so Airflow 3
provider discovery resolves ``airflow.providers.imednet.hooks``,
``airflow.providers.imednet.operators``, ``airflow.providers.imednet.sensors``,
and ``airflow.providers.imednet.triggers`` without duplicating wheel packages on
disk. Import directly from ``apache_airflow_providers_imednet`` in DAGs validated
with ``mypy --strict`` because static type checkers inspect installed filesystem
packages rather than dynamic runtime ``airflow.providers.imednet.*`` aliases.

``ImednetExportOperator`` is a unified execution engine that seamlessly writes
``Record`` items to any supported destination (CSV, Parquet, DuckDB, SQL / ``sql_by_form``
/ ``long_sql``, Snowflake, Neo4j, MongoDB, etc.)
using the core SDK's universal sink interface. Common operational parameters
(``batch_size``, ``max_retries``, ``idempotent``, ``mask_phi``,
``quality_gate_enabled``, and ``min_schema_readiness_score``) work consistently
across plugin sinks and tabular exports without leaking sink-level options into
Pandas or JSON writer kwargs. PHI masking is enabled by default
(``mask_phi=True``) across all tabular and sink destinations, replacing
registered ``Subject`` identifiers and committed ``StudyConfiguration``
``phiFields`` with ``***MASKED***``; authorized enclave workflows can explicitly
opt out by passing ``mask_phi=False`` (or ``export_kwargs={"mask_phi": False}``)
without mutating ``global_sensitivity_registry``. Destination-specific non-secret
options—such as DuckDB ``table_name`` (or ``table``, defaulting to ``"records"``),
Snowflake staging settings (``stage_prefix``,
``local_staging_dir``, ``manifest_path``), and Neo4j ``database`` names—can be
supplied via ``export_kwargs`` or via ``extra`` JSON on connections configured
through the Airflow CLI, ``AIRFLOW_CONN_*`` URIs, ``generic`` connections, or
destination-specific connection types (because the ``imednet`` UI form hides
``host`` and ``extra``).
``ImednetJobSensor(study_key=..., batch_id=...)`` polls an asynchronous iMednet
job identified by ``study_key`` and ``batch_id`` until it reaches a terminal
state (see :ref:`rest_resource_jobs`), fetching and validating any terminal
``resultUrl`` payload so batch-level or per-``Record`` failures raise a redacted
``AirflowException``.
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

CDISC SDTM Mapping and ``Variable`` Naming Conventions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``ImednetExportOperator`` forwards all six CDISC SDTM transformation, standards
readiness, and ``Variable`` naming options across tabular exports (such as
``destination="csv"``, ``"parquet"``, ``"duckdb"``, and ``"sql"``) and plugin
sinks:

- ``as_sdtm``: Transform exported ``Record`` payloads into standardized CDISC SDTM
  domain rows via :func:`~imednet.spi.models.transform_records_to_sdtm_rows`
  (automatically enabled whenever ``domain`` or ``sdtm_domain`` is set).
- ``domain``: Convenience alias for ``sdtm_domain`` accepting a 2-character CDISC
  SDTM domain code (such as ``"AE"``, ``"DM"``, or ``"VS"``) when ``sdtm_domain``
  is not passed explicitly.
- ``sdtm_domain``: Explicit 2-character CDISC SDTM target domain code filtering
  and shaping ``Form`` and ``Record`` outputs.
- ``standards_profile``: Optional CDISC standards validation profile name (for
  example ``"drug"``, ``"device"``, or ``"general"``) evaluated against the
  ``Study`` ``Data Dictionary`` prior to writing.
- ``min_standards_score``: Optional minimum CDISC standards readiness score
  (``0.0``–``100.0``) enforced via
  :func:`~imednet.spi.sink_base.evaluate_standards_readiness` before export.
- ``naming_convention``: Optional ``Variable`` and metadata key naming convention
  (``"snake_case"``, ``"camelCase"``, or ``"sdtm"``) applied across tabular,
  document, graph, and warehouse mappers.

.. testcode::

   from apache_airflow_providers_imednet import ImednetExportOperator

   sdtm_vs_export = ImednetExportOperator(
       task_id="export_sdtm_vital_signs",
       study_key="MY_STUDY",
       destination="csv",
       output_path="/tmp/vs_domain.csv",
       imednet_conn_id="imednet_default",
       as_sdtm=True,
       domain=None,
       sdtm_domain="VS",
       standards_profile="drug",
       min_standards_score=90.0,
       naming_convention="sdtm",
   )

   sdtm_ae_alias_export = ImednetExportOperator(
       task_id="export_sdtm_adverse_events",
       study_key="MY_STUDY",
       destination="csv",
       output_path="/tmp/ae_domain.csv",
       imednet_conn_id="imednet_default",
       domain="AE",
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
when ``deferrable=None``). In deferrable mode, ``execute()`` validates path-safe
``study_key`` and ``batch_id`` segments and yields the worker slot to
:class:`~apache_airflow_providers_imednet.ImednetJobTrigger`, which polls
iMednet asynchronously inside the Airflow triggerer using
:meth:`~apache_airflow_providers_imednet.hooks.ImednetHook.get_async_conn` and
:class:`~imednet.utils.job_poller.AsyncJobPoller` (including terminal
``resultUrl`` verification for per-``Record`` failures).

``ImednetJobTrigger.serialize()`` persists only non-secret identifiers
(``study_key``, ``batch_id``, ``imednet_conn_id``, ``poke_interval``, and
``timeout``) to the Airflow metadata database and scrubs any error messages
before emitting a ``TriggerEvent``.

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

Procedural export helpers are exposed lazily on :mod:`imednet.spi` via module
``__getattr__`` (aliasing :mod:`imednet.integrations.export` without registering
``imednet.spi.export_helpers`` in ``sys.modules``). Custom Airflow tasks and
plugins should import ``export_helpers`` as an attribute from ``imednet.spi``
rather than importing ``imednet.spi.export_helpers`` as a submodule:

.. testcode::

   from imednet.spi import export_helpers

   assert callable(export_helpers.export_to_csv)

The ``apache_airflow_providers_imednet.export`` wrappers delegate directly to
``from imednet.spi import export_helpers`` for CSV, DuckDB, Parquet, Excel,
JSON, and SQL (``export_to_sql``, ``export_to_sql_by_form``,
``export_to_long_sql``) exports, while the legacy ``from imednet.spi import export``
attribute remains available for backward compatibility.
