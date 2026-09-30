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

**Concurrency model**

SQLite is opened in WAL mode (``PRAGMA journal_mode=WAL``) so multiple reader
connections can query the cache while a single writer holds the write-lock.
A :class:`filelock.FileLock` placed next to the database file serialises
concurrent *writers* without blocking readers at all.

Usage example
~~~~~~~~~~~~~

.. testcode::

   from imednet import ImednetSDK
   from imednet_workflows.cached_loader import CachedRecordsLoader
   from imednet_workflows.sync_worker import SyncWorker, SyncWorkerConfig
   import threading

   sdk = ImednetSDK(api_key="...", security_key="...")
   from typing import Any, cast
   loader = CachedRecordsLoader(cast(Any, sdk))
   worker = SyncWorker(
       loader,
       config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=900),
   )

   # Start the worker in a daemon thread so it stops when the main process exits.
   t = threading.Thread(target=worker.run_forever, daemon=True)
   t.start()

   # Your application reads from the cache without waiting for the API.
   records = loader.get_cached_records("PROT-01")

   worker.stop()
   t.join()
