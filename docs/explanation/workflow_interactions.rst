Workflow Interactions
=====================

The helpers in :mod:`imednet_workflows` combine multiple endpoint calls to
automate common tasks. The diagrams below outline the main steps in each workflow.

Data Extraction
---------------

:class:`~imednet_workflows.data_extraction.DataExtractionWorkflow` coordinates
cross-endpoint filtering across ``SubjectsEndpoint``, ``VisitsEndpoint``,
``RecordsEndpoint``, and ``RecordRevisionsEndpoint`` so callers can extract
targeted ``Record`` cohorts or ``21 CFR Part 11`` audit trails without manually
joining identifiers across paginated responses. Pair extracted ``Record`` lists
with :class:`~imednet_workflows.record_mapper.RecordMapper` (``sdk.workflows.record_mapper``)
to project ``Variable`` columns into analytical DataFrames.

.. mermaid:: /diagrams/workflow_interactions_8.mmd

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows.data_extraction import DataExtractionWorkflow

   ext_sdk = ImednetSDK(simulation_mode=True)
   extractor = DataExtractionWorkflow(cast(Any, ext_sdk))
   matched_records = extractor.extract_records_by_criteria(
       study_key="STUDY",
       record_filter={"form_key": "F_VS"},
       subject_filter={"site_name": "SITE"},
   )
   assert len(matched_records) >= 1

Record Update
-------------

:class:`~imednet_workflows.record_update.RecordUpdateWorkflow` validates ``Form``
and ``Variable`` schemas once against :class:`~imednet.validation.cache.SchemaValidator`
before submitting chunked ``Record`` payloads through ``RecordsEndpoint`` and
polling asynchronous ``Job`` batches via ``JobsEndpoint``. Use
``create_or_update_records_batch`` (or ``async_create_or_update_records_batch``)
when submitting multi-chunk updates and inspecting :class:`~imednet.utils.batch_coordinator.BatchSubmissionSummary`.

.. mermaid:: /diagrams/workflow_interactions_9.mmd

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet.simulation import reset_default_simulation_state
   from imednet_workflows.record_update import RecordUpdateWorkflow

   upd_sdk = ImednetSDK(simulation_mode=True)
   updater = RecordUpdateWorkflow(cast(Any, upd_sdk))
   update_summary = updater.create_or_update_records_batch(
       study_key="STUDY",
       records_data=[
           {
               "formKey": "F_VS",
               "siteName": "SITE",
               "subjectKey": "SUB1",
               "intervalName": "Baseline",
               "data": {"V_WEIGHT": 73.0},
           }
       ],
       wait_for_completion=True,
       poll_interval=1,
   )
   assert update_summary.all_successful is True
   reset_default_simulation_state()

Subject Data
------------

:class:`~imednet_workflows.subject_data.SubjectDataWorkflow` aggregates a single
``Subject`` dossier across ``SubjectsEndpoint``, ``VisitsEndpoint``,
``RecordsEndpoint`` (excluding voided ``deleted=True`` items unless
``include_deleted=True``), and ``QueriesEndpoint`` into one typed
:class:`~imednet_workflows.subject_data.SubjectComprehensiveData` container.

.. mermaid:: /diagrams/workflow_interactions_10.mmd

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows.subject_data import SubjectDataWorkflow

   subj_sdk = ImednetSDK(simulation_mode=True)
   subject_workflow = SubjectDataWorkflow(cast(Any, subj_sdk))
   dossier = subject_workflow.get_all_subject_data("STUDY", "SUB1")
   assert dossier.subject_details is not None

Query Management
----------------

:class:`~imednet_workflows.query_management.QueryManagementWorkflow` wraps
``QueriesEndpoint`` and ``SubjectsEndpoint`` to filter open ``Query`` items,
resolve ``Site``-scoped ``Query`` queues via enrolled ``Subject`` keys, and
compute case-insensitive :class:`~imednet_workflows.query_management.QueryStateCounts`
summaries across active and closed states.

.. mermaid:: /diagrams/workflow_interactions_11.mmd

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows.query_management import QueryManagementWorkflow

   qry_sdk = ImednetSDK(simulation_mode=True)
   query_workflow = QueryManagementWorkflow(cast(Any, qry_sdk))
   open_queries = query_workflow.get_open_queries("STUDY")
   state_counts = query_workflow.get_query_state_counts("STUDY")
   assert len(open_queries) == state_counts["Open"]

Background Sync Worker
----------------------

:class:`~imednet_workflows.sync_worker.SyncWorker` runs incremental record
synchronisation in a background thread so that the main application thread
(e.g. a CLI or TUI event loop) never blocks on API calls.

.. mermaid:: /diagrams/workflow_interactions_12.mmd

**Concurrency and epoch-fencing model**

SQLite is opened in WAL mode (``PRAGMA journal_mode=WAL``) so multiple reader
connections can query ``record_cache`` while a single writer commits changes.
Both ``record_cache`` and ``study_sync_state`` are partitioned by normalized
``(base_url, study_key)`` keys (migrating pre-existing unpartitioned caches in
place) so training and production SDK instances can safely share a cache
directory without cross-origin contamination.
:class:`~imednet_workflows.cached_loader.CachedRecordsLoader` executes all
remote SDK HTTP calls (active-only initial loads, incremental delta fetches,
lightweight metadata projections, and missing or stale record backfills)
completely outside :class:`filelock.FileLock` so network latency never blocks
readers or concurrent writers.

Write transactions acquire :class:`filelock.FileLock` once after remote calls
finish for a single atomic SQLite commit:

* Active delta and backfill batches increment the per-origin, per-study
  ``sync_epoch`` in ``study_sync_state`` and stamp updated rows with
  ``upserted_epoch`` (storing ``date_modified=None`` as SQL ``NULL``), while
  deleting any newly voided ``Record`` IDs without persisting ``deleted=True``
  payloads.
* During reconciliation, ``sync_records`` captures the pre-reconcile
  ``sync_epoch`` before fetching upstream active metadata projections,
  backfills missing or newly completed ``Record`` payloads, and fences
  ``reconcile_cache`` deletions to ``upserted_epoch <= max_epoch`` so concurrent
  :class:`~imednet_workflows.record_mapper.RecordMapper`,
  :class:`~imednet_workflows.schema_profiler.SchemaProfiler`, or
  :class:`~imednet_workflows.sync_worker.SyncWorker` writes are never pruned by
  a stale snapshot.
* ``study_sync_state`` advances ``high_water_mark`` monotonically in the same
  atomic commit only after delta, backfill, and reconciliation succeed, rolling
  back partial changes and preserving the previous watermark whenever a step
  fails.

**Worker lifecycle and health states**

:class:`~imednet_workflows.sync_worker.SyncWorker` leaves ``loader.lock.timeout``
unchanged and tracks health across
:class:`~imednet_workflows.sync_worker.SyncWorkerState` values (``IDLE``,
``RUNNING``, ``DEGRADED``, ``STOPPED``, ``FAILED``), exposing
``consecutive_failures``, ``last_error``, ``on_error`` callbacks, and
``max_consecutive_failures`` to stop automatically during persistent outages.
Permanent client API errors (HTTP ``400``, ``401``, ``403``, ``404``) transition
the worker immediately into terminal ``FAILED`` state without retrying.

Usage example
~~~~~~~~~~~~~

.. testcode::

   from imednet import ImednetSDK
   from imednet_workflows.cached_loader import CachedRecordsLoader
   from imednet_workflows.sync_worker import (
       SyncWorker,
       SyncWorkerConfig,
       SyncWorkerState,
   )
   import threading

   sdk = ImednetSDK(api_key="...", security_key="...")
   from typing import Any, cast
   loader = CachedRecordsLoader(cast(Any, sdk))
   worker = SyncWorker(
       loader,
       config=SyncWorkerConfig(
           study_key="PROT-01",
           interval_seconds=900,
           max_consecutive_failures=5,
       ),
       on_error=lambda exc, failures: None,
   )

   # Start the worker in a daemon thread so it stops when the main process exits.
   t = threading.Thread(target=worker.run_forever, daemon=True)
   t.start()

   # Your application reads from the cache and inspects worker health without blocking.
   records = loader.get_cached_records("PROT-01")
   is_healthy = worker.state in (SyncWorkerState.IDLE, SyncWorkerState.RUNNING)

   worker.stop()
   t.join()
