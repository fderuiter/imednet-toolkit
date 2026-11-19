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
     - Preserves the hierarchy ``Study → Subject → Visit → Record`` as nodes and
       relationships, with ``record_data`` attached to ``Record`` nodes
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

PHI protection and masking controls
-----------------------------------

All destination sinks, partitioned Parquet exports, flat and hierarchical JSON
exports, and DataFrame converters mask Protected Health Information (PHI) at
mapper ingress **by default** (``mask_phi=True``):

* **Default sensitive keys**: Registered clinical identifiers—including
  ``patientName`` / ``patient_name``, ``patientInitials``, ``DOB`` /
  ``date_of_birth`` / ``birth_date``, ``SSN``, ``phone``, ``email``, and
  ``address``—are replaced with ``***MASKED***`` before column label mapping,
  hierarchy construction, or sink batch serialization.
* **Relational identifiers preserved**: Join and graph keys (``study_key``,
  ``subject_key``, and ``record_id``) are explicitly exempted from masking so
  relational joins, Hive partitions, and Neo4j graph edges remain intact.
* **Study-specific rules**: Study-level ``StudyConfiguration(phiFields=[...])``
  rules normalize camelCase, PascalCase, uppercase acronyms, and ``snake_case``
  field names automatically across both ``document`` and ``tabular`` mapper
  modes. Additional global identifiers can also be registered via
  ``global_sensitivity_registry`` (exposed on ``imednet.spi.utils`` for
  extension plugins).

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
identifiers (for example, an approved EDC migration), pass ``mask_phi=False``
on the sink configuration, mapper, or export helper. Opting out applies only to
that operation and never mutates ``global_sensitivity_registry``:

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

Runnable examples
-----------------

* :doc:`/tutorials/examples/mongodb_export`
* :doc:`/tutorials/examples/neo4j_export`
* :doc:`/tutorials/examples/snowflake_export`
