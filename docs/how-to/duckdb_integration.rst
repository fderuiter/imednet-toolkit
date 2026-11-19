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
   export_to_duckdb(sdk, "MY_STUDY", "./clinical.duckdb", "study_records")

   conn = duckdb.connect("./clinical.duckdb", read_only=True)
   print(conn.execute("SELECT COUNT(*) FROM study_records").fetchone())

`export_to_duckdb_by_form`
--------------------------

Use :func:`imednet.integrations.export_to_duckdb_by_form` when you want one table per
form key:

.. testcode::

   import duckdb
   from imednet import ImednetSDK
   from imednet.integrations import export_to_duckdb_by_form

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   export_to_duckdb_by_form(sdk, "MY_STUDY", "./clinical_by_form.duckdb")

   conn = duckdb.connect("./clinical_by_form.duckdb", read_only=True)
   print(conn.execute("SHOW TABLES").fetchall())

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

   conn = duckdb.connect()
   rows = conn.execute(
       """
       SELECT study_key, form_key, COUNT(*)
       FROM read_parquet('./lake/**/*.parquet', hive_partitioning = true, union_by_name = true)
       GROUP BY 1, 2
       ORDER BY 1, 2
       """
   ).fetchall()
   print(rows)

You can also generate the query string via
:func:`imednet.integrations.hive_parquet_query`:

.. testcode::

   import duckdb
   from imednet.integrations import hive_parquet_query

   conn = duckdb.connect()
   print(conn.execute(hive_parquet_query("./lake")).fetchdf())

`DuckDBIngestionWorkflow`
-------------------------

For incremental bronze/silver audit-revision ingestion, use
:class:`imednet_workflows.DuckDBIngestionWorkflow`. ``bronze_revisions`` stores all 19
:class:`~imednet.models.record_revisions.RecordRevision` fields keyed by
``(study_key, record_revision_id)`` so repeated ``mode="append"`` loads deduplicate
matching revisions, while ``silver_current_state`` projects the latest audit state per
``record_id``. Pre-upgrade persistent ``bronze_revisions`` tables with valid
``(study_key, record_revision_id)`` keys are migrated in place automatically while
preserving existing rows (and incompatible legacy tables can be rebuilt with
``mode="replace"``).

.. code-block:: python

   import duckdb
   from imednet import ImednetSDK
   from imednet_workflows import DuckDBIngestionWorkflow

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   workflow = DuckDBIngestionWorkflow(sdk, "./centralized.duckdb")

   # Incremental bronze load (deduplicated by study_key and record_revision_id)
   inserted = workflow.ingest_revisions(
       "MY_STUDY",
       start_date="2026-01-01",
       end_date="2026-01-31",
       mode="append",
   )
   print(f"Processed {inserted} revision rows")

   # Build silver current-state projection per record_id
   workflow.build_silver_view("MY_STUDY")

   conn = duckdb.connect("./centralized.duckdb", read_only=True)
   print(
       conn.execute(
           """
           SELECT
               record_revision_id,
               record_id,
               subject_key,
               form_key,
               record_status,
               record_revision,
               "user",
               reason_for_change,
               date_created
           FROM silver_current_state
           ORDER BY record_id
           LIMIT 10
           """
       ).fetchall()
   )

The two key methods are
:meth:`imednet_workflows.duckdb_centralizer.DuckDBIngestionWorkflow.ingest_revisions`
for incremental loads and
:meth:`imednet_workflows.duckdb_centralizer.DuckDBIngestionWorkflow.build_silver_view`
for current-state materialization.

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

The DuckDB path behind :func:`imednet.integrations.export_to_duckdb` and
:func:`imednet.integrations.export_to_duckdb_by_form` registers a DataFrame directly
in DuckDB before a ``CREATE TABLE AS SELECT`` operation. Compared to row-by-row insert
patterns often used with SQLAlchemy targets, this is typically much lower overhead for
large exports.

As an illustrative estimate (not a guaranteed benchmark), teams often observe whole-table
loads finishing in seconds to minutes where row-wise insert flows can take materially
longer for the same dataset shape.

Hive-partitioned Parquet via :func:`imednet.integrations.export_to_hive_parquet` also
avoids SQLite's 2000-column table ceiling by storing each form independently and allowing
federated reads through DuckDB.

Out of scope
------------

This guide does not cover MotherDuck/cloud DuckDB or multi-process orchestration
patterns.
