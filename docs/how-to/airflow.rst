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

Production Reference DAG
------------------------

Use ``examples/airflow/multi_study_pipeline.py`` as the recommended production
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

Create an Airflow connection ``imednet_default`` or override ``imednet_conn_id``.
Provide ``api_key`` and ``security_key`` via the login/password fields or in the
``extra`` JSON. ``base_url`` may be added in ``extra`` for a non-standard
environment. The hook merges these settings with values from
``imednet.config.load_config`` so environment variables still apply.

For warehouse, graph, document, and transactional SQL exports, configure a
separate Airflow connection and pass ``destination_conn_id`` on
``ImednetExportOperator``. ``ImednetHook.resolve_destination_connection()``
resolves destination credentials (Snowflake ``account``/``user``/``password``
and coordinates, Neo4j ``uri``/``auth``/``database``, MongoDB ``uri`` and
database settings, or SQLAlchemy connection URIs) at task execution time rather
than embedding secrets in DAG definitions.

To prevent credential leakage in Airflow rendered task metadata and XComs,
templated fields (``export_kwargs`` and ``output_path``) reject raw secret keys
(such as ``password``, ``auth``, ``api_key``, ``security_key``, ``token``, or
``conn_str`` containing credentials) as well as URIs with embedded userinfo.

For task-mapping discovery steps, ``ImednetHook`` provides explicit helper methods:

- ``get_conn()`` returns the live :class:`~imednet.ImednetSDK` adhering to the
  standard Airflow BaseHook contract.
- ``get_sdk_client()`` is a direct alias for ``get_conn()`` for use inside task
  execution context.
- ``list_studies_metadata()`` returns JSON-serializable primitive dictionaries
  (with sensitive keys redacted) for safe mapped task expansion.
- ``list_study_keys()`` returns primitive study keys only.
- ``describe_connection()`` returns redacted connection metadata and never exposes
  raw credentials.
- ``resolve_destination_connection()`` resolves runtime destination connection
  settings for ``snowflake``, ``neo4j``, ``mongodb``, and ``sql`` targets.

Operators and Sensors
---------------------
The Airflow integration organizes hooks, operators, and sensors in dedicated
subpackages for clarity.

``ImednetExportOperator`` is a unified execution engine that seamlessly writes
records to any supported destination (CSV, Parquet, Snowflake, Neo4j, etc.)
using the core SDK's universal sink interface. Common operational parameters
(``batch_size``, ``max_retries``, ``idempotent``, ``quality_gate_enabled``, and
``min_schema_readiness_score``) work consistently across plugin sinks and
tabular exports without leaking sink-level options into Pandas or JSON writer
kwargs. Destination-specific non-secret options—such as Snowflake staging
settings (``stage_prefix``, ``local_staging_dir``, ``manifest_path``) and Neo4j
``database`` names—can be supplied via ``export_kwargs`` or connection ``extra``
metadata. ``ImednetJobSensor`` waits for an export job to complete.
All operators use ``ImednetHook`` to obtain an :class:`~imednet.ImednetSDK` instance
from an Airflow connection. The production reference DAG above shows the recommended
dynamic-mapping pattern: keep static settings (for example ``destination``,
``imednet_conn_id``, and ``destination_conn_id``) in ``.partial(...)`` and map
only non-secret runtime fields (``study_key``, ``output_path``, ``export_kwargs``).

.. testcode::

   from typing import Any
   from airflow.sdk import task
   from apache_airflow_providers_imednet import ImednetExportOperator, ImednetHook

   @task
   def export_targets() -> list[dict[str, Any]]:
       hook = ImednetHook("imednet_default")
       return [
           {
               "study_key": study_key,
               "output_path": f"/tmp/{study_key}.csv",
               "export_kwargs": {"index": False},
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

Incremental Checkpointing in Airflow 3
--------------------------------------

``ImednetExportOperator(incremental=True)`` and ``imednet-workflows`` standardize
on the file-backed :class:`~imednet_workflows.ExtractionStateLedger`
(``FileStateProvider``) rather than querying or mutating Airflow's internal XCom
metadata tables across DAG runs. In Airflow 3 deployments, choose one of three
supported patterns to carry Study high-water marks between runs:

1. **Operator outputs and TaskFlow return values**: pass lightweight ISO-8601
   watermarks explicitly between tasks within a DAG run and supply them via
   ``modified_since`` on ``ImednetExportOperator``.
2. **Airflow 3 Variables** (``airflow.sdk.Variable``): persist compact per-Study
   watermarks in Airflow's variable store and pass ``Variable.get(...)`` into
   ``modified_since`` at task execution time.
3. **Shared filesystem or Airflow ObjectStorage**
   (``airflow.sdk.ObjectStoragePath``): point ``ledger_path`` (or
   ``IMEDNET_WORKFLOW_LEDGER_PATH``) at a shared volume, or stage the JSON ledger
   to and from object storage around ``ImednetExportOperator(incremental=True)``:

.. code-block:: python

   from pathlib import Path
   from airflow.sdk import ObjectStoragePath, Variable, task
   from apache_airflow_providers_imednet import ImednetExportOperator

   # Option A: Airflow 3 Variable + explicit modified_since watermark
   watermark = Variable.get("imednet_my_study_watermark", default=None)
   ImednetExportOperator(
       task_id="export_incremental_variable",
       study_key="MY_STUDY",
       destination="parquet",
       output_path="/tmp/my_study_incremental.parquet",
       modified_since=watermark,
       imednet_conn_id="imednet_default",
   )

   # Option B: Stage ExtractionStateLedger via Airflow ObjectStorage
   @task
   def stage_ledger_down(remote_uri: str, local_ledger: str) -> str:
       remote = ObjectStoragePath(remote_uri)
       target = Path(local_ledger)
       if remote.exists():
           target.parent.mkdir(parents=True, exist_ok=True)
           target.write_bytes(remote.read_bytes())
       return str(target)

   @task
   def stage_ledger_up(local_ledger: str, remote_uri: str) -> str:
       source = Path(local_ledger)
       if source.exists():
           ObjectStoragePath(remote_uri).write_bytes(source.read_bytes())
       return remote_uri

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
