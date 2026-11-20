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
     - Keeps nested ``record_data`` as document fields and stores record metadata
       envelope fields (study/form/visit/subject)
     - Requires ``pymongo`` and a MongoDB URI. Graph traversal and relationship
       analytics are weaker than a native graph store.
       See :class:`~imednet_sinks.document.MongoDbExportSink` and :func:`~imednet_sinks.document.export_to_mongodb`.
   * - Neo4j
     - Relationship-heavy exploration and lineage queries
     - Preserves the hierarchy ``Study → Subject → Visit → Record`` for scheduled
       visits and links unscheduled ``Record`` nodes directly via
       ``(:Subject)-[:HAS_RECORD]->(:Record)``, with ``record_data`` attached to
       ``Record`` nodes
     - Requires ``neo4j`` driver and Bolt/Neo4j endpoint credentials.
       Idempotent mode uses ``MERGE`` for safe re-runs.
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
   redaction when no configuration history or encryption key is present), and
   supports pre-write ``Record`` readiness filtering via
   ``quality_gate_enabled=True`` and ``min_schema_readiness_score``.

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

Warehouse, document, graph, and tabular exports share eight streaming checkpoint
and filter options on :class:`~imednet.spi.sink_base.SinkConfig`,
:func:`~imednet.spi.sink_base.run_streaming_export`, tabular helpers
(:func:`~imednet.integrations.export_to_parquet`,
:func:`~imednet.integrations.export_to_csv`,
:func:`~imednet.integrations.export_to_duckdb`,
:func:`~imednet.integrations.export_to_duckdb_by_form`,
:func:`~imednet.integrations.export_to_hive_parquet`), and
``ImednetExportOperator``:

* ``incremental`` (``bool``) – Enable transactional high-water mark reads and commits.
* ``stream_name`` (``str | None``) – Logical stream key in the ledger (for example ``"mongodb_records"`` or ``"vitals_parquet"``).
* ``ledger_path`` (``str | Path | None``) – Path for file-backed checkpoint state via :class:`~imednet_workflows.ExtractionStateLedger` (:class:`~imednet_workflows.state_ledger.FileStateProvider`).
* ``state_provider`` (``StateProviderProtocol | None``) – Explicit provider instance such as ``ExtractionStateLedger(...)``, :class:`~imednet_workflows.state_ledger.AirflowStateProvider`, or :func:`~imednet.spi.sink_base.get_state_provider`.
* ``modified_since`` (``datetime | str | None``) – Initial UTC lower-bound watermark used when the stream has no prior checkpoint.
* ``record_filter`` (``dict[str, Any] | str | None``) – ``Record`` metadata filter pushed down to ``RecordsEndpoint``.
* ``record_data_filter`` (``str | None``) – ``Variable`` payload filter expression (``recordDataFilter``).
* ``include_deleted`` (``bool``) – Include voided (``deleted=True``) ``Record`` items directly in batches instead of reconciling deletes separately.

State providers registered under ``imednet.state_providers`` (``default``, ``file``, and
``airflow``) commit the updated watermark only after every batch and ``sink.flush()``
complete cleanly. Below is a resumable ``run_streaming_export`` example using
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
* **Pre-write quality-gate filtering**: Setting ``quality_gate_enabled=True``
  (and optional ``min_schema_readiness_score``) on ``SnowflakeSinkConfig``,
  ``MongoDbSinkConfig``, ``Neo4jSinkConfig``, or tabular exports runs
  ``apply_quality_gate`` before batch writes to drop low-readiness ``Record``
  items consistently across warehouse, document, and graph targets.
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
