Snowflake Export
================

This guide covers the warehouse-native Snowflake export path introduced as the first
implementation of the staged warehouse export framework.

For the DuckDB / local-analytical path see :doc:`/how-to/duckdb_integration`.
For general architecture context see :doc:`/explanation/architecture`.

Overview
--------

The Snowflake export path uses a staged-loading and merge approach:

1. **Stage** – Study records are mapped with top-level ``Record`` metadata columns
   (``study_key``, ``record_id``, ``subject_id``, ``subject_key``, ``form_id``,
   ``form_key``, ``visit_id``, ``record_status``, timestamps, etc.) and a JSON-encoded
   ``record_data`` column after clinical PHI masking and enrichment. Encoding
   ``record_data`` as JSON preserves heterogeneous Form schemas within the same batch
   without Arrow type collisions.
2. **PUT** – Each Parquet file is uploaded to a Snowflake *internal* stage via
   ``PUT file://... AUTO_COMPRESS = FALSE OVERWRITE = TRUE``.
3. **Load / Merge** – When ``idempotent=True`` (default), Snowflake bulk-loads the
   staged Parquet file into a temporary staging table via
   ``COPY INTO ... FILE_FORMAT = (TYPE = PARQUET) MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE FORCE = TRUE``
   and executes ``MERGE INTO`` keyed on ``(study_key, record_id)``. When
   ``idempotent=False``, Snowflake loads directly into the target table via
   ``COPY INTO ... FORCE = TRUE``.

This is intentionally different from :func:`~imednet.integrations.export_to_sql`, which
uses SQLAlchemy ``DataFrame.to_sql()`` and is appropriate for transactional SQL targets
(SQLite, PostgreSQL, etc.).  Do **not** mix the two paths.

.. important::

   **Staging constraint — local staging only.**
   The first version of this framework supports **local intermediate staging** only.
   Parquet files are written to the local filesystem and then uploaded to a Snowflake
   *internal* stage via ``PUT``.  Object-storage staging (S3, GCS, Azure Blob) is
   **not** yet supported.  If your Snowflake account restricts ``PUT`` to external
   stages only, you will need to wait for a future release.

Installation
------------

.. code-block:: bash

   pip install 'imednet[snowflake]'

This installs ``snowflake-connector-python`` (≥ 3.0) and ``pyarrow`` (included
transitively).

Quickstart — ``export_to_snowflake``
-------------------------------------

The :func:`~imednet_sinks.warehouse.export_to_snowflake` convenience function handles
batching, staging, and cleanup automatically:

.. testcode::

   import os
   from imednet import ImednetSDK
   from imednet_sinks.warehouse import export_to_snowflake, SnowflakeSinkConfig

   sdk = ImednetSDK(api_key=os.environ["IMEDNET_API_KEY"])

   snowflake_cfg = SnowflakeSinkConfig(
       study_key="MY_STUDY",
       account="myorg-myaccount",
       user="loader",
       password=os.environ["SNOWFLAKE_PASSWORD"],  # never hardcode
       database="IMEDNET_DB",
       schema="PUBLIC",
       warehouse="COMPUTE_WH",
       stage="MY_STAGE",
       table="RECORDS",
   )

   rows_loaded = export_to_snowflake(sdk, "MY_STUDY", config=snowflake_cfg)
   print(f"Loaded {rows_loaded} rows")

Advanced usage — ``SnowflakeExportSink`` directly
--------------------------------------------------

Use :class:`~imednet_sinks.warehouse.SnowflakeExportSink` directly when you need
per-batch control, custom ``batch_id`` keys, or integration with a workflow engine:

.. testcode::

   import os
   from imednet import ImednetSDK
   from imednet.integrations.sink_base import iter_batches
   from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

   sdk = ImednetSDK(api_key=os.environ["IMEDNET_API_KEY"])
   records = list(sdk.records.list(study_key="MY_STUDY"))

   snowflake_cfg_advanced = SnowflakeSinkConfig(
       study_key="MY_STUDY",
       account="myorg-myaccount",
       user="loader",
       password=os.environ["SNOWFLAKE_PASSWORD"],
       database="IMEDNET_DB",
       schema="PUBLIC",
       warehouse="COMPUTE_WH",
       stage="MY_STAGE",
       table="RECORDS",
       stage_prefix="imednet/MY_STUDY",
       batch_size=1000,
       max_retries=5,
       idempotent=True,
   )

   with SnowflakeExportSink(config=snowflake_cfg_advanced) as sink:
       for i, batch in enumerate(iter_batches(records, snowflake_cfg_advanced.batch_size)):
           rows = sink.write_batch(batch, batch_id=f"MY_STUDY/records/{i}")
           print(f"  batch {i}: {rows} rows")

Manifest output
---------------

Pass ``manifest_path`` to record every loaded batch in a JSON-lines file for audit
purposes or replay:

.. testcode::

   snowflake_cfg_manifest = SnowflakeSinkConfig(
       study_key="MY_STUDY",
       account="myorg",
       user="user",
       database="db",
       schema="public",
       warehouse="wh",
       stage="stg",
       table="tbl",
       manifest_path="/tmp/imednet_manifest.jsonl",
   )

Each entry contains:

.. code-block:: json

   {
       "batch_id":   "MY_STUDY/records/0",
       "stage_path": "@MY_STAGE/imednet/MY_STUDY_records_0.parquet",
       "row_count":  500,
       "loaded_at":  "2026-01-15T12:00:00+00:00"
   }

Idempotency
-----------

When ``SnowflakeSinkConfig.idempotent`` is ``True`` (default), each batch upload runs
``PUT ... AUTO_COMPRESS = FALSE OVERWRITE = TRUE`` so refreshed Parquet files replace stale
staged artifacts, bulk-loads the batch into a session-scoped temporary staging table via
``COPY INTO ... FORCE = TRUE``, and executes ``MERGE INTO`` keyed on
``(study_key, record_id)``. Scheduled re-runs update modified records in place without
inserting duplicate rows. Set ``idempotent = False`` (``--force-reload`` in the CLI) to
bypass staging-table merges and append directly into the target table via
``COPY INTO ... FORCE = TRUE``.

Error handling and retries
--------------------------

Every ``write_batch`` call retries up to ``SinkConfig.max_retries`` times (default 3)
with exponential back-off (``retry_backoff * 2 ** attempt``, default base 1 s).  After
all retries are exhausted, :class:`~imednet.errors.ExportBatchError` is raised with the
failing ``batch_id``.  Connection failures during ``__init__`` raise
:class:`~imednet.errors.ExportConfigurationError`.

Stage lifecycle and cleanup
---------------------------

* **Temporary staging directory** – When ``local_staging_dir`` is not set, a
  :class:`tempfile.TemporaryDirectory` is created automatically and removed when the
  context manager exits, regardless of success or failure.
* **Persistent staging directory** – When ``local_staging_dir`` is set, the directory
  is **not** cleaned up automatically; this lets you inspect or re-upload the Parquet
  files.
* **Snowflake stage cleanup** – The SDK does **not** remove files from the Snowflake
  internal stage after loading.  Use ``REMOVE @MY_STAGE/imednet/`` from a Snowflake
  session to purge them when they are no longer needed.

CLI reference
-------------

.. code-block:: bash

   imednet export snowflake --help

Minimal example:

.. code-block:: bash

   imednet export snowflake MY_STUDY \
       myorg-myaccount loader "$SNOWFLAKE_PASSWORD" \
       IMEDNET_DB PUBLIC COMPUTE_WH MY_STAGE RECORDS

With options:

.. code-block:: bash

   imednet export snowflake MY_STUDY \
       myorg-myaccount loader "$SNOWFLAKE_PASSWORD" \
       IMEDNET_DB PUBLIC COMPUTE_WH MY_STAGE RECORDS \
       --stage-prefix imednet/prod \
       --batch-size 1000 \
       --manifest-path /tmp/manifest.jsonl \
       --force-reload

Configuration reference
-----------------------

.. autoclass:: imednet_sinks.warehouse.SnowflakeSinkConfig
   :members:
   :undoc-members:

.. autofunction:: imednet_sinks.warehouse.export_to_snowflake

.. autoclass:: imednet_sinks.warehouse.SnowflakeExportSink
   :members:

Out of scope
------------

* Object-storage (S3/GCS/Azure) staging — planned for a future release.
* Snowpark / Python connector Pandas integration — not used; Parquet staging is the
  preferred path for throughput.
* Row-wise insert via SQLAlchemy — use :func:`~imednet.integrations.export_to_sql`
  for transactional SQL targets instead.
