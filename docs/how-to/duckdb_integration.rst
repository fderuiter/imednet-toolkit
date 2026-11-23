DuckDB Integration
==================

This guide covers the DuckDB and Hive-partitioned Parquet export paths for analytical
workloads.

For destination-selection guidance across MongoDB, Neo4j, and Snowflake, see
:doc:`export_destinations`.

For record-to-DataFrame mapping internals, see :doc:`record_mapping`.
For workflow interaction patterns, see :doc:`/explanation/workflow_interactions`.

Installation
------------

.. code-block:: bash

   # Minimal DuckDB-only install
   pip install 'imednet[duckdb]'

   # Full analytical stack (includes pandas, pyarrow, sqlalchemy, openpyxl, duckdb)
   pip install 'imednet[export]'

`export_to_duckdb` quickstart
-----------------------------

Use :func:`imednet.integrations.export_to_duckdb` to export one study into one DuckDB
table and query it immediately:

.. testcode::

   import duckdb
   from imednet import ImednetSDK
   from imednet.integrations import export_to_duckdb

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   export_to_duckdb(sdk, "STUDY", "./clinical.duckdb", "study_records")

   conn = duckdb.connect("./clinical.duckdb", read_only=True)
   count_row = conn.execute("SELECT COUNT(*) FROM study_records").fetchone()
   assert count_row is not None and count_row[0] > 0
   print(count_row)
   conn.close()

`export_to_duckdb_by_form`
--------------------------

Use :func:`imednet.integrations.export_to_duckdb_by_form` when you want one table per
form key:

.. testcode::

   import duckdb
   from imednet import ImednetSDK
   from imednet.integrations import export_to_duckdb_by_form

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   export_to_duckdb_by_form(sdk, "STUDY", "./clinical_by_form.duckdb")

   conn = duckdb.connect("./clinical_by_form.duckdb", read_only=True)
   tables = conn.execute("SHOW TABLES").fetchall()
   assert len(tables) > 0
   print(tables)
   conn.close()

Hive-partitioned Parquet for multi-study workloads
---------------------------------------------------

Use :func:`imednet.integrations.export_to_hive_parquet` when many studies are being
extracted concurrently and you want each study/form stored independently on disk. In
that scenario, partitioned Parquet avoids a single shared database file lock boundary
while preserving direct queryability from DuckDB.

Example layout:

.. code-block:: text

   lake/
   ├── study_key=STUDY_A/
   │   ├── form_key=DEMOGRAPHICS/
   │   │   └── records.parquet
   │   └── form_key=LABS/
   │       └── records.parquet
   └── study_key=STUDY_B/
       └── form_key=DEMOGRAPHICS/
           └── records.parquet

DuckDB query with Hive partition discovery and schema union:

.. testcode::

   import duckdb
   from imednet import ImednetSDK
   from imednet.integrations import export_to_hive_parquet

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   export_to_hive_parquet(sdk, "STUDY", "./lake")

   conn = duckdb.connect()
   hive_rows = conn.execute(
       """
       SELECT study_key, form_key, COUNT(*)
       FROM read_parquet('./lake/**/*.parquet', hive_partitioning = true, union_by_name = true)
       GROUP BY 1, 2
       ORDER BY 1, 2
       """
   ).fetchall()
   assert len(hive_rows) > 0
   print(hive_rows)
   conn.close()

You can also generate the query string via
:func:`imednet.integrations.hive_parquet_query`:

.. testcode::

   import duckdb
   from imednet.integrations import hive_parquet_query

   conn = duckdb.connect()
   hive_df = conn.execute(hive_parquet_query("./lake")).fetchdf()
   assert not hive_df.empty
   print(hive_df)
   conn.close()

`DuckDBIngestionWorkflow`
-------------------------

For incremental bronze/silver ``Record`` revision ingestion, use
:class:`imednet_workflows.DuckDBIngestionWorkflow` (also registered under the
``imednet.workflows`` plugin entry point ``duckdb_centralizer`` via
``sdk.workflows.duckdb_centralizer``).

.. testcode::

   import duckdb
   from imednet import ImednetSDK
   from imednet_workflows import DuckDBIngestionWorkflow

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   workflow = DuckDBIngestionWorkflow(sdk, "./centralized.duckdb")

   # Incremental bronze load
   inserted = workflow.ingest_revisions(
       "STUDY",
       start_date="2026-01-01",
       end_date="2026-01-31",
       mode="append",
   )
   assert inserted > 0
   print(f"Inserted {inserted} revision rows")

   # Build silver current-state projection
   workflow.build_silver_view("STUDY")

   conn = duckdb.connect("./centralized.duckdb")
   silver_rows = conn.execute(
       'SELECT record_id, form_key, record_revision, "user", reason_for_change, date_created '
       "FROM silver_current_state ORDER BY record_id LIMIT 10"
   ).fetchall()
   assert len(silver_rows) > 0
   print(silver_rows)
   conn.close()

The two key methods are
:meth:`imednet_workflows.duckdb_centralizer.DuckDBIngestionWorkflow.ingest_revisions`
for incremental loads and
:meth:`imednet_workflows.duckdb_centralizer.DuckDBIngestionWorkflow.build_silver_view`
for current-state materialization.

Incremental checkpoints for DuckDB and Hive Parquet helpers
-----------------------------------------------------------

:func:`imednet.integrations.export_to_duckdb`,
:func:`imednet.integrations.export_to_duckdb_by_form`, and
:func:`imednet.integrations.export_to_hive_parquet` accept all eight streaming
checkpoint options—``incremental``, ``stream_name``, ``ledger_path``,
``state_provider``, ``modified_since``, ``record_filter``, ``record_data_filter``,
and ``include_deleted``—along with ``max_buffered_records`` and
``quality_gate_enabled`` while keeping ``mask_phi=True`` as the default PHI guard.

Multi-``Form`` exports (:func:`~imednet.integrations.export_to_duckdb_by_form` and
:func:`~imednet.integrations.export_to_hive_parquet`) stream ``Study`` ``Record``
items in a single pass through :class:`~imednet.spi.sink_base.BoundedFormBufferRouter`,
maintaining isolated per-``Form`` watermarks (``duckdb:<form_key>`` and
``hive_parquet:<form_key>``, or ``<stream_name>:<form_key>``) and evicting the largest
buffered ``Form`` whenever total buffered ``Record`` items reach
``max_buffered_records`` (default ``max(batch_size, 5000)``). When ``idempotent=True``
and ``include_deleted=False``, incremental runs upsert modified ``recordId`` rows and
prune voided (``deleted=True``) ``Record`` items (via ``DELETE FROM`` plus
``INSERT INTO ... BY NAME`` in DuckDB and partition Parquet pruning prior to checkpoint
commit in Hive Parquet), while capping ``max_ts`` at ``min_rejected_ts`` if
``quality_gate_enabled=True`` drops low-readiness ``Record`` items.

Use :class:`~imednet_workflows.ExtractionStateLedger`
(:class:`~imednet_workflows.state_ledger.FileStateProvider`) for local analytical
pipelines or :class:`~imednet_workflows.state_ledger.AirflowStateProvider` when
orchestrating DuckDB refreshes from Apache Airflow:

.. testcode::

   import tempfile
   from pathlib import Path
   from imednet import ImednetSDK
   from imednet.integrations import export_to_duckdb
   from imednet_workflows import ExtractionStateLedger

   with tempfile.TemporaryDirectory() as duck_tmp_dir:
       duck_db_path = Path(duck_tmp_dir) / "incremental.duckdb"
       duck_ledger_path = Path(duck_tmp_dir) / "duckdb_state.json"
       duck_ledger = ExtractionStateLedger(str(duck_ledger_path))

       export_to_duckdb(
           ImednetSDK(simulation_mode=True),
           "STUDY",
           str(duck_db_path),
           "vital_signs_records",
           mask_phi=True,
           incremental=True,
           stream_name="duckdb_vital_signs",
           ledger_path=duck_ledger_path,
           state_provider=duck_ledger,
           modified_since="2026-01-01T00:00:00Z",
           record_filter={"form_key": "F_VS"},
           record_data_filter="V_WEIGHT>0",
           include_deleted=False,
       )

CLI `duckdb` subcommand reference
---------------------------------

Inspect the full command and option reference:

.. code-block:: bash

   imednet export duckdb --help

Annotated examples:

.. code-block:: bash

   # Export only selected variables to a single DuckDB table
   imednet export duckdb MY_STUDY study_records ./clinical.duckdb --vars AGE,SEX,WEIGHT

   # Export only selected forms (comma-separated numeric form IDs)
   imednet export duckdb MY_STUDY study_records ./clinical.duckdb --forms 10,20,30

   # Use variable labels as column names instead of variable names
   imednet export duckdb MY_STUDY study_records ./clinical.duckdb --use-labels

Performance notes
-----------------

``TabularDuckDBSink`` behind :func:`imednet.integrations.export_to_duckdb` and
:func:`imednet.integrations.export_to_duckdb_by_form` streams bounded ``batch_size``
DataFrame chunks rather than materializing an entire ``Study`` in memory. The initial
batch initializes (or replaces on full idempotent runs) the target table, unseen
``Variable`` columns in later batches are added via ``ALTER TABLE ... ADD COLUMN``, and
subsequent and incremental batches register each chunk DataFrame for vectorized
``INSERT INTO ... BY NAME`` writes instead of row-by-row SQL inserts.

For multi-``Form`` workloads, :func:`imednet.integrations.export_to_duckdb_by_form` and
:func:`imednet.integrations.export_to_hive_parquet` route ``Record`` streams in a single
API pass via :class:`~imednet.spi.sink_base.BoundedFormBufferRouter`. Each ``Form``
flushes as soon as its active or voided buffer reaches ``batch_size`` (or ``chunk_size``),
and if total buffered ``Record`` items across all ``Form`` buffers hit
``max_buffered_records`` (default ``max(batch_size, 5000)``), the router evicts and
flushes the largest ``Form`` buffer first to enforce bounded worker memory.

Hive-partitioned Parquet via :func:`imednet.integrations.export_to_hive_parquet` also
avoids SQLite's 2000-column table ceiling by storing each ``Form`` independently and
allowing federated reads through DuckDB.

Out of scope
------------

This guide does not cover MotherDuck/cloud DuckDB or multi-process orchestration
patterns.
