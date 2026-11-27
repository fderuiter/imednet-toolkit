===================================================
Audit Trails, 21 CFR Part 11 & Compliance Workflows
===================================================

Regulatory Compliance Auditors and Data Engineers use ``imednet`` and
``imednet-workflows`` to retrieve immutable ``RecordRevision`` histories,
verify monotonic 21 CFR Part 11 audit chains, enforce secure-by-default PHI
``Variable`` masking on exports, track configuration provenance, and inspect
WCAG 2.1 Level AA conformance disclosures.

.. seealso::
   * :doc:`duckdb_integration` — Ingest ``RecordRevision`` audit logs into ``bronze_revisions`` and materialize deduplicated ``record_revisions``, ``silver_current_state``, and SCD Type 2 ``silver_records_history`` views in DuckDB.
   * :doc:`export_destinations` — Enforce default ``mask_phi=True`` redaction and explicit ``StudyConfiguration`` PHI policies across CSV, Parquet, SQL, and Snowflake sinks.
   * :doc:`/reference/governance` — Track Fernet-encrypted, parent-chained SHA-256 ``Study`` configuration snapshots with ``ConfigVersionStore``.
   * :doc:`/reference/VPAT` — Review Voluntary Product Accessibility Template (WCAG 2.1 Level AA) conformance statements.

Inspecting ``RecordRevision`` History
-------------------------------------

Every historical mutation or sign-off on a clinical ``Record`` is exposed via
:class:`~imednet.endpoints.record_revisions.RecordRevisionsEndpoint`
(``sdk.record_revisions``) and
:class:`~imednet.endpoints.record_revisions.AsyncRecordRevisionsEndpoint`
(``async_sdk.record_revisions``), backed by the ``recordRevisions`` EDC REST
resource and the 19-attribute :class:`~imednet.models.record_revisions.RecordRevision`
model:

* **Identity & Scope**: ``recordRevisionId``, ``studyKey``, ``recordId``, ``recordOid``, ``subjectId``, ``subjectOid``, ``subjectKey``, ``siteId``, ``formKey``, ``intervalId``
* **Version & Lifecycle**: ``recordRevision`` (monotonic ``>= 1``), ``dataRevision`` (monotonic ``0 <= dataRevision <= recordRevision``), ``recordStatus``, ``deleted``
* **Attribution & Timestamps**: ``role``, ``user``, ``reasonForChange``, ``dateCreated``, ``lastUpdated``

Per the upstream `Mednet Record Revisions API <https://portal.prod.imednetapi.com/docs/record-revisions>`_,
``/recordRevisions`` returns only these 19 audit metadata fields and omits
clinical ``recordData`` ``Variable`` payloads (current clinical ``Variable``
values are served on ``/records`` via ``sdk.records``).

CLI Inspection
~~~~~~~~~~~~~~

Query ``RecordRevision`` records from the terminal in concise summary tables,
versioned JSON envelopes, or PHI-masked CSV/JSON file exports using
``imednet record-revisions list`` and the ``imednet workflows`` subcommands:

.. code-block:: bash

   # Stream all RecordRevision items for a Study as versioned JSON
   imednet record-revisions list STUDY --format json --all

   # Bound by UTC date window and export a PHI-masked CSV file
   imednet record-revisions list STUDY --start-date 2026-01-01 --end-date 2026-01-31 -o ./revisions.csv

   # Filter server-side by Record ID and dual dateCreated bounds
   imednet record-revisions list STUDY -f "recordId==601" -f "dateCreated>=2026-01-01" -f "dateCreated<=2026-01-31" --sort "recordRevision,asc"

   # Extract a verified audit slice or ingest revisions into ./audit_lakehouse.duckdb via workflows CLI
   imednet workflows extract-audit-trail STUDY --start-date 2026-01-01 --end-date 2026-01-31 --validate-chain -o ./audit_trail.json
   imednet workflows ingest-revisions STUDY --mode replace --validate-chain

``RecordRevisionsEndpoint.FILTER_ATTRIBUTES`` supports server-side filtering on
all 18 revision-level attributes (``recordRevisionId``, ``recordId``,
``recordOid``, ``recordRevision``, ``dataRevision``, ``recordStatus``,
``subjectId``, ``subjectOid``, ``subjectKey``, ``siteId``, ``formKey``,
``intervalId``, ``role``, ``user``, ``reasonForChange``, ``deleted``,
``dateCreated``, and ``lastUpdated``). When both lower and upper bounds are
supplied on ``dateCreated`` or ``lastUpdated`` (via ``start_date``/``end_date``,
tuple lists, or ``filter="..."``), the SDK dispatches at most one upstream bound
per timestamp attribute and evaluates secondary bounds locally.

Verifying Monotonic 21 CFR Part 11 Chains
-----------------------------------------

Under 21 CFR Part 11, full historical revisions for any ``Record`` (grouped by
``studyKey`` and ``recordId`` or ``recordOid``) must start at
``record_revision == 1``, maintain contiguous ``record_revision`` counters
(incrementing by ``+1`` without gaps), maintain monotonically non-decreasing
``data_revision`` counters (``0 <= data_revision <= record_revision``), preserve
chronological ``date_created`` timestamps (rejecting backdated revisions), and
treat ``deleted=True`` tombstones as irreversible (never reverting to
``deleted=False``). When an investigator verifies or signs a ``Record`` without
altering clinical ``Variable`` values, ``record_revision`` increments while
``data_revision`` remains unchanged.

Use :meth:`RecordRevision.validate_chain <imednet.models.record_revisions.RecordRevision.validate_chain>`,
``validate_record_revision_chain``, or ``is_valid_record_revision_chain`` in
strict mode (``allow_partial=False``, default) for complete ``Record`` histories,
or pass ``allow_partial=True`` when validating date-bounded or actor-filtered
slices that may start above revision ``1`` or omit intermediate revisions:

.. testcode::

   from imednet.models.record_revisions import (
       RecordRevision,
       is_valid_record_revision_chain,
   )

   revisions = [
       RecordRevision.model_validate(
           {
               "recordRevisionId": 9001,
               "studyKey": "STUDY",
               "recordId": 601,
               "recordOid": "REC-601",
               "recordRevision": 1,
               "dataRevision": 1,
               "recordStatus": "Complete",
               "subjectId": 501,
               "subjectOid": "SUBJ-501",
               "subjectKey": "SUB1",
               "siteId": 201,
               "formKey": "F_VS",
               "intervalId": 401,
               "role": "Clinical Research Coordinator",
               "user": "crc_site201",
               "reasonForChange": "Initial Vital Signs Record capture",
               "deleted": False,
               "dateCreated": "2026-11-15 18:10:00",
               "lastUpdated": "2026-11-15 18:10:00",
           }
       ),
       RecordRevision.model_validate(
           {
               "recordRevisionId": 9002,
               "studyKey": "STUDY",
               "recordId": 601,
               "recordOid": "REC-601",
               "recordRevision": 2,
               "dataRevision": 1,
               "recordStatus": "Verified",
               "subjectId": 501,
               "subjectOid": "SUBJ-501",
               "subjectKey": "SUB1",
               "siteId": 201,
               "formKey": "F_VS",
               "intervalId": 401,
               "role": "Principal Investigator",
               "user": "pi_site201",
               "reasonForChange": "21 CFR Part 11 investigator verification sign-off",
               "deleted": False,
               "dateCreated": "2026-11-15 18:35:45",
               "lastUpdated": "2026-11-15 18:35:45",
           }
       ),
   ]

   verified_chain = RecordRevision.validate_chain(revisions)
   strict_slice_ok = is_valid_record_revision_chain(revisions[1:])
   partial_slice_ok = is_valid_record_revision_chain(revisions[1:], allow_partial=True)
   reversed_ok = is_valid_record_revision_chain(list(reversed(revisions)))
   assert len(verified_chain) == 2 and not strict_slice_ok and partial_slice_ok and not reversed_ok
   print(f"Verified revisions: {len(verified_chain)}, Out-of-order valid: {reversed_ok}")

Date-Bounded Audit Trail Extraction
-----------------------------------

For regulatory inspection scopes spanning specific UTC date bounds, ``User``
accounts, ``Site`` identifiers, or ``Form`` keys, use
:meth:`DataExtractionWorkflow.extract_audit_trail <imednet_workflows.data_extraction.DataExtractionWorkflow.extract_audit_trail>`
alongside synchronous or asynchronous SDK endpoints. Pass ``allow_partial=True``
when validating date- or actor-filtered slices so monotonic ordering,
non-decreasing ``data_revision``, chronological ``date_created``, and
irreversible deletion invariants are enforced without requiring revision ``1``
or intermediate revisions authored outside the filter window:

.. testcode::

   import asyncio
   from imednet import AsyncImednetSDK, ImednetSDK
   from imednet.models.record_revisions import RecordRevision
   from imednet.simulation import reset_default_simulation_state
   from imednet_workflows.data_extraction import DataExtractionWorkflow


   def extract_verified_audit_trail(study_key: str) -> list[RecordRevision]:
       with ImednetSDK(simulation_mode=True) as sdk:
           workflow = DataExtractionWorkflow(sdk)
           revisions = workflow.extract_audit_trail(
               study_key=study_key,
               start_date="2026-01-01",
               end_date="2026-01-31",
               user="sim_user",
               sort="recordRevision,asc",
           )
           return RecordRevision.validate_chain(revisions, allow_partial=True)


   async def stream_form_revisions_async(study_key: str) -> list[RecordRevision]:
       async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
           revisions = [
               rev
               async for rev in async_sdk.record_revisions.list(
                   study_key,
                   formKey="F_VS",
                   sort="recordRevision,asc",
               )
           ]
           return RecordRevision.validate_chain(revisions)


   reset_default_simulation_state()
   audit_trail = extract_verified_audit_trail("STUDY")
   form_revisions = asyncio.run(stream_form_revisions_async("STUDY"))
   assert len(audit_trail) == 3
   assert all(rev.user == "sim_user" for rev in audit_trail)
   assert len(form_revisions) == 1
   assert form_revisions[0].form_key == "F_VS"

``extract_audit_trail`` automatically normalizes ISO-8601 ``start_date`` and
``end_date`` inputs to inclusive UTC bounds on ``dateCreated`` while preserving
additional equality or operator filters in ``user_filter`` and ``**filters``.

Analytical Lineage, PHI Controls & Governance
---------------------------------------------

Combine ``RecordRevision`` verification with downstream compliance controls
across the toolkit:

1. **DuckDB Lakehouse Lineage** (:doc:`duckdb_integration`): Load deduplicated,
   PHI-masked (``mask_phi=True`` by default, with Study-scoped ``mode="replace"``
   refreshes) ``(study_key, record_revision_id)`` audit metadata (with
   UTC-normalized timestamps) into the ``bronze_revisions`` table (with a
   ``record_revisions`` compatibility view) via
   ``DuckDBIngestionWorkflow.ingest_revisions``, and materialize per-Study
   (``"silver_current_state_{study_key}"``,
   ``"silver_records_history_{study_key}"``) and combined
   ``silver_current_state`` (selecting the latest revision per ``study_key``
   and ``COALESCE(CAST(record_id AS VARCHAR), NULLIF(record_oid, ''), CAST(record_revision_id AS VARCHAR))``
   ordered by ``record_revision``, ``data_revision``, ``date_created``, and
   ``record_revision_id``, excluding voided ``deleted=True`` records unless
   ``include_deleted=True``) and ``silver_records_history`` (an SCD Type 2
   temporal view exposing ``valid_from``, ``valid_to``, ``is_current``, and
   ``LAG()`` transition diff columns ``prev_record_status``,
   ``prev_data_revision``, ``prev_deleted``, ``data_changed``, and
   ``change_type``—emitting ``'created'`` for initial ``record_revision == 1``
   or unnumbered revisions and ``'window_baseline'`` with
   ``data_changed IS NULL`` when a date-bounded slice starts at
   ``record_revision > 1``) via ``DuckDBIngestionWorkflow.build_silver_view``.
   On the CLI, ``imednet workflows ingest-revisions`` persists to
   ``./audit_lakehouse.duckdb`` by default, rejects ``--db-path :memory:``
   without ``-o``/``--output``, validates merged ``bronze_revisions`` chains in
   a single fetch when ``--validate-chain`` is passed, and omits voided
   ``deleted=True`` rows from ``-o`` file exports unless ``--include-deleted``
   is specified.
2. **PHI Masking & Redaction** (:doc:`export_destinations`): All sink plugins,
   CLI revision exports, and ``DuckDBIngestionWorkflow`` default to
   ``mask_phi=True`` and honor global ``SensitivityRegistry`` rules plus
   ``StudyConfiguration`` PHI ``Variable`` declarations while preserving
   ``deleted=True`` tombstones and unconfigured actor attribution.
3. **Configuration Provenance** (:doc:`/reference/governance`): Record
   Fernet-encrypted, parent-chained SHA-256 digests, RBAC-verified actor
   attribution, and structural diffs for ``Study`` configuration updates with
   ``ConfigVersionStore``.
4. **Accessibility Conformance** (:doc:`/reference/VPAT`): Inspect WCAG 2.1
   Level AA verification criteria for the documentation portal, CLI, and TUI.

.. testcode::

   import tempfile
   from pathlib import Path

   import duckdb
   from imednet import ImednetSDK
   from imednet.simulation import SimulationTransport
   from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow

   with tempfile.TemporaryDirectory() as tmpdir:
       db_file = str(Path(tmpdir) / "audit_lakehouse.duckdb")
       with ImednetSDK(
           api_key="sim-api-key",  # pragma: allowlist secret
           security_key="sim-security-key",  # pragma: allowlist secret
           transport=SimulationTransport(),
       ) as sdk:
           with DuckDBIngestionWorkflow(sdk, db_path=db_file) as lakehouse:
               inserted = lakehouse.ingest_revisions("STUDY", mode="replace")
               lakehouse.build_silver_view("STUDY")

       conn = duckdb.connect(db_file)
       try:
           bronze_row = conn.execute(
               "SELECT COUNT(*) FROM bronze_revisions WHERE study_key = ?",
               ["STUDY"],
           ).fetchone()
           silver_row = conn.execute(
               "SELECT COUNT(*) FROM silver_current_state WHERE study_key = ?",
               ["STUDY"],
           ).fetchone()
           history_row = conn.execute(
               "SELECT COUNT(*) FROM silver_records_history "
               "WHERE study_key = ? AND valid_from IS NOT NULL "
               "AND is_current IS NOT NULL AND change_type IS NOT NULL",
               ["STUDY"],
           ).fetchone()
           compat_row = conn.execute(
               "SELECT COUNT(*) FROM record_revisions WHERE study_key = ?",
               ["STUDY"],
           ).fetchone()
           assert bronze_row is not None and bronze_row[0] == inserted
           assert compat_row is not None and compat_row[0] == bronze_row[0]
           assert history_row is not None and history_row[0] == bronze_row[0]
           assert silver_row is not None and silver_row[0] <= bronze_row[0]
       finally:
           conn.close()
