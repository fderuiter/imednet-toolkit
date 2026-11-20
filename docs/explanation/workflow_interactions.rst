Workflow Interactions
=====================

The helpers in :mod:`imednet_workflows` combine multiple endpoint calls to
automate common tasks. The diagrams below outline the main steps in each workflow.

Data Extraction
---------------

The data extraction process involves fetching records and applying mapping logic.
For detailed API information, see :class:`~imednet_workflows.data_extraction.DataExtractionWorkflow`
and the underlying :class:`~imednet_workflows.record_mapper.RecordMapper` utility used to transform raw data.

.. mermaid:: /diagrams/workflow_interactions_8.mmd

Record Update
-------------

.. mermaid:: /diagrams/workflow_interactions_9.mmd

Subject Data
------------

.. mermaid:: /diagrams/workflow_interactions_10.mmd

Query Management
----------------

.. mermaid:: /diagrams/workflow_interactions_11.mmd

Background Sync Worker
----------------------

:class:`~imednet_workflows.sync_worker.SyncWorker` runs incremental record
synchronisation in a background thread so that the main application thread
(e.g. a CLI or TUI event loop) never blocks on API calls.

.. mermaid:: /diagrams/workflow_interactions_12.mmd

**Concurrency and epoch-fencing model**

SQLite is opened in WAL mode (``PRAGMA journal_mode=WAL``) so multiple reader
connections can query ``record_cache`` while a single writer commits changes.
:class:`~imednet_workflows.cached_loader.CachedRecordsLoader` executes all
remote SDK HTTP calls (delta fetches, active-ID projections, and missing-ID
backfills) completely outside :class:`filelock.FileLock` so network latency
never blocks readers or concurrent writers.

Write transactions acquire :class:`filelock.FileLock` only for brief SQLite
commits:

* Each ``_upsert_records`` batch increments the per-study ``sync_epoch`` in
  ``study_sync_state`` and stamps updated rows with ``upserted_epoch`` (storing
  ``date_modified=None`` as SQL ``NULL``).
* During reconciliation, ``sync_records`` captures the pre-reconcile
  ``sync_epoch`` before fetching upstream active IDs, backfills any missing
  active ``Record`` payloads, and fences ``reconcile_cache`` deletions to
  ``upserted_epoch <= max_epoch`` so concurrent :class:`~imednet_workflows.record_mapper.RecordMapper`,
  :class:`~imednet_workflows.schema_profiler.SchemaProfiler`, or
  :class:`~imednet_workflows.sync_worker.SyncWorker` writes are never pruned by
  a stale snapshot.
* ``study_sync_state`` advances ``high_water_mark`` monotonically only after
  delta, backfill, and reconciliation steps succeed, preserving the previous
  watermark whenever a step fails.

**Worker lifecycle and health states**

:class:`~imednet_workflows.sync_worker.SyncWorker` leaves ``loader.lock.timeout``
unchanged and tracks health across
:class:`~imednet_workflows.sync_worker.SyncWorkerState` values (``IDLE``,
``RUNNING``, ``DEGRADED``, ``STOPPED``, ``FAILED``), exposing
``consecutive_failures``, ``last_error``, ``on_error`` callbacks, and
``max_consecutive_failures`` to stop automatically during persistent outages.

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
