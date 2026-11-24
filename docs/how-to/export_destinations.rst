Export Destinations
===================

This guide explains when to use each export destination family and how to install
only the optional dependencies you need.

For sink internals and data-flow architecture, see :doc:`/explanation/architecture`.
For full CLI syntax, see :doc:`/how-to/cli`.

Installation
------------

.. include:: /_includes/install_extensions.rst

Destination decision matrix
---------------------------

.. list-table::
   :header-rows: 1
   :widths: 18 26 28 28

   * - Destination
     - Best fit
     - Data-shape characteristics
     - Operational constraints
   * - MongoDB
     - Application-facing document reads, schema-preserving archival
     - Keeps nested ``record_data`` as document fields and stores ``Record`` metadata
       envelope fields (``Study``/``Form``/``Visit``/``Subject`` aligned to
       ``naming_convention``) or emits flat CDISC SDTM domain documents when
       ``as_sdtm=True``. Encodes pure ``date`` scalars as ISO-8601 strings, preserves
       ``datetime`` values natively, and encodes ``Decimal`` values as ``bson.Decimal128``.
     - Requires ``pymongo`` and a MongoDB URI. Idempotent upserts key ``_id`` on
       ``<study_key>/<record_id>`` (falling back to ``<STUDYID>/<DOMAIN>/<USUBJID>/<*SEQ>``,
       with ``*SEQ`` defaulting to ``1``, when ``as_sdtm=True`` and ``record_id`` is absent).
       Graph traversal and relationship analytics are weaker than a native graph store.
       See :class:`~imednet_sinks.document.MongoDbExportSink` and :func:`~imednet_sinks.document.export_to_mongodb`.
   * - Neo4j
     - Relationship-heavy exploration and lineage queries
     - Preserves the hierarchy ``Study → Subject → Visit → Record`` for scheduled
       ``Visit`` items and normalizes non-positive ``visit_id <= 0`` markers to ``None``
       so unscheduled ``Record`` nodes link directly via
       ``(:Subject)-[:HAS_RECORD]->(:Record)``, with ``record_data`` attached to
       ``Record`` nodes (or flat CDISC SDTM domain properties when ``as_sdtm=True``)
     - Requires ``neo4j`` driver and Bolt/Neo4j endpoint credentials.
       Idempotent mode uses ``MERGE`` keyed on ``(record_id, study_key)`` (aligned to
       ``naming_convention``) or ``(STUDYID, DOMAIN, USUBJID, *SEQ)`` when
       ``as_sdtm=True`` (``(STUDYID, DOMAIN, USUBJID)`` for domains without ``*SEQ``
       such as ``DM``).
       See :class:`~imednet_sinks.graph.Neo4jExportSink` and :func:`~imednet_sinks.graph.export_to_neo4j`.
   * - Snowflake
     - Warehouse-native analytics and bulk loading at scale
     - Writes Parquet batch files, uploads to an internal stage, then issues
       ``COPY INTO`` for table loading
     - Requires ``snowflake-connector-python`` + ``pyarrow`` and stage privileges
       for ``PUT`` / ``COPY INTO``.
       See :class:`~imednet_sinks.warehouse.SnowflakeExportSink` and :func:`~imednet_sinks.warehouse.export_to_snowflake`.

SDK examples
------------

.. note::

   Every sink configuration (``MongoDbSinkConfig``, ``Neo4jSinkConfig``, and
   ``SnowflakeSinkConfig``) enables PHI masking by default (``mask_phi=True``),
   resolves committed ``StudyConfiguration`` governance rules automatically via
   ``ConfigVersionStore`` (failing closed to ``global_sensitivity_registry``
   redaction when no configuration history or encryption key is present),
   supports pre-write ``Record`` readiness filtering via
   ``quality_gate_enabled=True`` and ``min_schema_readiness_score``, and shares
   CDISC SDTM transformation and standards readiness controls (``as_sdtm``,
   ``sdtm_domain``, ``standards_profile``, ``min_standards_score``, and
   ``naming_convention``; see :doc:`/how-to/record_mapping` and
   :doc:`/how-to/cli`).

MongoDB (document export):

.. testcode::

   from imednet import ImednetSDK
   from imednet_sinks import MongoDbSinkConfig, export_to_mongodb

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   mongodb_cfg = MongoDbSinkConfig(
       study_key="MY_STUDY",
       uri="mongodb://localhost:27017",
       database="imednet",
       collection="records",
       batch_size=500,
       idempotent=True,
   )
   rows = export_to_mongodb(sdk, "MY_STUDY", config=mongodb_cfg)
   print(rows)

Neo4j (graph export):

.. testcode::

   from imednet import ImednetSDK
   from imednet_sinks import Neo4jSinkConfig, export_to_neo4j

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   neo4j_cfg = Neo4jSinkConfig(
       study_key="MY_STUDY",
       uri="bolt://localhost:7687",
       auth=("neo4j", "..."),
       database="neo4j",
       batch_size=500,
       idempotent=True,
       quality_gate_enabled=True,
       min_schema_readiness_score=90.0,
   )
   rows = export_to_neo4j(sdk, "MY_STUDY", config=neo4j_cfg)
   print(rows)

Snowflake (warehouse-native export):

.. testcode::

   from imednet import ImednetSDK
   from imednet_sinks import SnowflakeSinkConfig, export_to_snowflake

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   snowflake_cfg = SnowflakeSinkConfig(
       study_key="MY_STUDY",
       account="myorg-myaccount",
       user="loader",
       password="...",
       database="IMEDNET_DB",
       schema="PUBLIC",
       warehouse="COMPUTE_WH",
       stage="MY_STAGE",
       table="RECORDS",
   )
   rows = export_to_snowflake(sdk, "MY_STUDY", config=snowflake_cfg)
   print(rows)

Incremental streaming checkpoints
---------------------------------

Warehouse, document, graph, and all 10 tabular exporters in :mod:`imednet.integrations`
(:func:`~imednet.integrations.export_to_parquet`,
:func:`~imednet.integrations.export_to_csv`,
:func:`~imednet.integrations.export_to_excel`,
:func:`~imednet.integrations.export_to_json`,
:func:`~imednet.integrations.export_to_sql`,
:func:`~imednet.integrations.export_to_sql_by_form`,
:func:`~imednet.integrations.export_to_long_sql`,
:func:`~imednet.integrations.export_to_duckdb`,
:func:`~imednet.integrations.export_to_duckdb_by_form`, and
:func:`~imednet.integrations.export_to_hive_parquet`) share eight streaming checkpoint
and filter options on :class:`~imednet.spi.sink_base.SinkConfig`,
:func:`~imednet.spi.sink_base.run_streaming_export`, and
``ImednetExportOperator``:

* ``incremental`` (``bool``) – Enable transactional high-water mark reads and commits.
* ``stream_name`` (``str | None``) – Logical stream key in the ledger (for example ``"mongodb_records"`` or ``"vitals_parquet"``).
* ``ledger_path`` (``str | Path | None``) – Path for file-backed checkpoint state via :class:`~imednet_workflows.ExtractionStateLedger` (:class:`~imednet_workflows.state_ledger.FileStateProvider`).
* ``state_provider`` (``StateProviderProtocol | None``) – Explicit provider instance such as ``ExtractionStateLedger(...)``, :class:`~imednet_workflows.state_ledger.AirflowStateProvider`, or :func:`~imednet.spi.sink_base.get_state_provider`.
* ``modified_since`` (``datetime | str | None``) – Initial UTC lower-bound watermark used when the stream has no prior checkpoint.
* ``record_filter`` (``dict[str, Any] | str | None``) – ``Record`` metadata filter pushed down to ``RecordsEndpoint``.
* ``record_data_filter`` (``str | None``) – ``Variable`` payload filter expression (``recordDataFilter``).
* ``include_deleted`` (``bool``) – Include voided (``deleted=True``) ``Record`` items directly in batches instead of reconciling deletes separately.

When ``incremental=True`` (or ``modified_since`` / ``state_provider`` is supplied),
``idempotent=True``, and ``include_deleted=False``, all 10 tabular helpers perform
keyed ``recordId`` (``recordId``, ``record_id``, or ``RECORD_ID``) upserts and prune
newly voided (``deleted=True``) ``Record`` items without duplicating history:

* **CSV and Parquet** (:func:`~imednet.integrations.export_to_csv`,
  :func:`~imednet.integrations.export_to_parquet`): stage incoming batches and voided
  IDs via ``reconcile_deleted``, deduplicate deltas by canonical ``recordId``, unify
  columns or Arrow schemas while streaming non-superseded rows to a ``.tmp`` file, and
  atomically replace the destination file on ``flush()``.
* **Excel and JSON** (:func:`~imednet.integrations.export_to_excel`,
  :func:`~imednet.integrations.export_to_json`): deduplicate delta rows by ``recordId``,
  prune superseded and ``deleted=True`` ``Record`` entries across Excel workbooks, flat
  JSON arrays, and hierarchical ``Subject → Visit → Form`` JSON archives
  (``hierarchical=True``), and atomically rewrite the target file.
* **SQL and DuckDB** (:func:`~imednet.integrations.export_to_sql`,
  :func:`~imednet.integrations.export_to_sql_by_form`,
  :func:`~imednet.integrations.export_to_long_sql`,
  :func:`~imednet.integrations.export_to_duckdb`,
  :func:`~imednet.integrations.export_to_duckdb_by_form`): delete superseded
  ``recordId`` / ``record_id`` rows before appending updated batches (chunked
  ``DELETE FROM ... WHERE ... IN (...)`` on SQLAlchemy targets; ``DELETE FROM`` plus
  ``INSERT INTO ... BY NAME`` in DuckDB) and delete newly voided ``deleted=True``
  ``Record`` IDs via ``reconcile_deleted``.
* **Hive Parquet** (:func:`~imednet.integrations.export_to_hive_parquet`): write new
  per-``Form`` partition Parquet files under ``study_key=<study>/form_key=<form>`` and
  rewrite pre-existing partition Parquet files to prune superseded and ``deleted=True``
  ``recordId`` rows before committing per-``Form`` checkpoints.

State providers registered under ``imednet.state_providers`` (``default``, ``file``, and
``airflow``) commit the updated watermark only after every batch, delete reconciliation,
and ``sink.flush()`` complete cleanly. When ``quality_gate_enabled=True`` and
:func:`~imednet.spi.sink_base.apply_quality_gate` drops low-readiness ``Record`` items,
both :func:`~imednet.spi.sink_base.run_streaming_export` and multi-``Form`` tabular
streaming cap the committed ``max_ts`` watermark at ``min_rejected_ts`` (selecting the
highest exported ``Record`` timestamp ``<= min_rejected_ts``, or holding back watermark
advancement when no exported timestamp precedes ``min_rejected_ts`` or any rejected
``Record`` lacks a timestamp) so remediated ``Record`` items are re-evaluated on
subsequent incremental runs. Below is a resumable ``run_streaming_export`` example using
``ExtractionStateLedger`` with ``mask_phi=True`` preserved by default:

.. testcode::

   import tempfile
   from pathlib import Path
   from imednet import ImednetSDK
   from imednet.spi.sink_base import run_streaming_export
   from imednet_sinks import MongoDbExportSink, MongoDbSinkConfig
   from imednet_workflows import ExtractionStateLedger

   with tempfile.TemporaryDirectory() as tmp_dir:
       ledger_file = Path(tmp_dir) / "export_state.json"
       ledger = ExtractionStateLedger(str(ledger_file))
       incremental_cfg = MongoDbSinkConfig(
           study_key="MY_STUDY",
           uri="mongodb://localhost:27017",
           database="imednet",
           collection="records",
           mask_phi=True,
           incremental=True,
           stream_name="mongodb_records",
           ledger_path=ledger_file,
           state_provider=ledger,
           modified_since="2026-01-01T00:00:00Z",
           record_filter={"form_key": "F_VS"},
           record_data_filter="V_WEIGHT>0",
           include_deleted=False,
       )
       written_count = run_streaming_export(
           ImednetSDK(simulation_mode=True),
           "STUDY",
           lambda: MongoDbExportSink(incremental_cfg),
           incremental_cfg,
       )
       assert written_count >= 0

PHI protection and masking controls
-----------------------------------

All destination sinks, partitioned Parquet exports, flat and hierarchical JSON
exports, and DataFrame converters mask Protected Health Information (PHI) at
mapper ingress **by default** (``mask_phi=True``):

* **Default sensitive keys**: Registered clinical and CDISC identifiers—including
  ``patientName`` / ``patient_name``, ``patientInitials``, ``SUBJINIT``,
  ``DOB`` / ``date_of_birth`` / ``birth_date``, ``BRTHDTC``, ``BRTHDAT``,
  ``BRTHYR``, ``SSN``, ``phone``, ``email``, and ``address``—are replaced with
  ``***MASKED***`` before column label mapping (``use_labels_as_columns=True``),
  hierarchy construction, or sink batch serialization. On CDISC SDTM exports
  (``sdtm_dataframe``, ``iter_sdtm_dataframes``, and ``as_sdtm=True``), masked
  ``BRTHDTC`` values are redacted to four-digit ISO 8601 years (``YYYY``) or
  ``None`` rather than ``***MASKED***`` text.
* **Relational identifiers preserved**: Join, graph, and CDISC subject/study
  keys (``study_key``, ``subject_key``, ``record_id``, ``STUDYID``,
  ``USUBJID``, and ``SUBJID``) are explicitly exempted from masking so
  relational joins, MongoDB ``_id`` documents, Hive partitions, CDISC domain
  merges, and Neo4j ``MERGE`` graph edges remain intact.
* **Automatic Study governance resolution**: Exports automatically inspect
  committed ``StudyConfiguration`` rules from ``ConfigVersionStore`` (when
  ``imednet-workflows`` is installed) to enforce custom ``phiFields``,
  ``terminologyLookups``, and ``mappings`` across flat, hierarchical, and sink
  paths. Store errors, missing plugins, or missing encryption keys fail closed
  so ``global_sensitivity_registry`` PHI redaction still applies.
* **Pre-write quality-gate filtering**: Quality-gate filtering is opt-in
  (``quality_gate_enabled=False`` by default across all CSV, Parquet, Excel,
  JSON, DuckDB, SQL, warehouse, document, and graph exports). Setting
  ``quality_gate_enabled=True`` (and optional ``min_schema_readiness_score``)
  on ``SnowflakeSinkConfig``, ``MongoDbSinkConfig``, ``Neo4jSinkConfig``, or
  tabular exports runs :func:`~imednet.spi.sink_base.apply_quality_gate` before
  batch writes to drop empty drafts or invalid ``Record`` items while retaining
  sparse ``Record`` payloads that omit optional ``Variable`` fields across
  warehouse, document, graph, and tabular targets. On incremental runs,
  ``run_streaming_export`` and multi-``Form`` tabular streaming cap ``max_ts``
  at ``min_rejected_ts`` whenever ``apply_quality_gate`` drops ``Record`` items
  (or leave ``max_ts`` uncommitted when no exported timestamp is
  ``<= min_rejected_ts`` or a rejected ``Record`` has no timestamp) so dropped
  ``Record`` items are re-fetched after remediation.
* **Study-specific rules**: Study-level ``StudyConfiguration(phiFields=[...])``
  rules normalize camelCase, PascalCase, uppercase acronyms, and ``snake_case``
  field names automatically across both ``document`` and ``tabular`` mapper
  modes and replace matched values with ``***MASKED***``. Additional global
  identifiers can also be registered via ``global_sensitivity_registry``
  (exposed on ``imednet.spi.utils`` for extension plugins).

Customizing study PHI fields with ``CentralizedMapper``:

.. testcode::

   from imednet.integrations.enrichment import CentralizedMapper
   from imednet.models.study_config import StudyConfiguration

   study_cfg = StudyConfiguration(
       studyKey="MY_STUDY",
       version="1.0",
       reportingProfile="general",
       phiFields=["patientName", "DOB", "SSN", "medicalRecordNumber"],
   )
   mapper = CentralizedMapper(mode="tabular", study_config=study_cfg)

Opting out for authorized identifiable data transfers:

When protocol governance explicitly authorizes transferring unmasked clinical
identifiers (for example, an approved ``21 CFR Part 11`` enclave archive or EDC
migration), pass ``mask_phi=False`` on the sink configuration, mapper, Airflow
``ImednetExportOperator``, or export helper—or pass ``--no-mask-phi`` on
``imednet export`` CLI commands. Opting out applies only to that operation and
never mutates ``global_sensitivity_registry``:

.. testcode::

   from imednet import ImednetSDK
   from imednet_sinks import MongoDbSinkConfig, export_to_mongodb

   sdk = ImednetSDK(api_key="mock", security_key="mock")
   authorized_cfg = MongoDbSinkConfig(
       study_key="MY_STUDY",
       uri="mongodb://localhost:27017",
       database="imednet",
       collection="authorized_records",
       mask_phi=False,
   )
   export_to_mongodb(sdk, "MY_STUDY", config=authorized_cfg)

.. code-block:: bash

   imednet export json MY_STUDY /secure/enclave/study_archive.json --hierarchical --no-mask-phi
   imednet export neo4j MY_STUDY bolt://localhost:7687 neo4j "$NEO4J_PASSWORD" --quality-gate --no-mask-phi

Runnable examples
-----------------

* :doc:`/tutorials/examples/mongodb_export`
* :doc:`/tutorials/examples/neo4j_export`
* :doc:`/tutorials/examples/snowflake_export`
