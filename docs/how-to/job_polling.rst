Job Polling Mechanism
=====================

:class:`~imednet.utils.job_poller.JobPoller` and
:class:`~imednet.utils.job_poller.AsyncJobPoller` wait for background ``Job``
executions to reach a terminal state or raise on timeout.

.. mermaid:: /diagrams/job_polling_2.mmd

Single-Job Polling
------------------

``ImednetSDK`` provides a convenience method ``poll_job`` that blocks until a
single ``Job`` finishes. ``AsyncImednetSDK`` exposes ``async_poll_job`` for
asynchronous callers:

.. testcode::

   from imednet import AsyncImednetSDK, ImednetSDK
   from imednet.simulation import reset_default_simulation_state

   # Synchronous single-Job polling
   sdk = ImednetSDK(simulation_mode=True)
   job = sdk.records.create(
       "STUDY",
       [
           {
               "formKey": "F_VS",
               "siteName": "SITE",
               "subjectKey": "SUBJ-001",
               "intervalName": "Baseline",
               "data": {"V_WEIGHT": 70.5},
           }
       ],
   )
   assert job.batch_id
   status = sdk.poll_job("STUDY", job.batch_id, interval=0.05, timeout=10)

   # Asynchronous single-Job polling
   async def wait_async() -> None:
       async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
           assert job.batch_id
           await async_sdk.async_poll_job("STUDY", job.batch_id, interval=0.05, timeout=10)

   reset_default_simulation_state()

Chunked Batch Submission and Multi-Job Polling
----------------------------------------------

Large ``Record`` writes should be split into bounded chunks so a single payload
never exceeds API request limits and schema validation runs once across the full
submission:

* **Endpoint chunking**: ``sdk.records.create_batch(study_key, records_data, *, chunk_size=100, schema=None)``
  (and ``await async_sdk.records.create_batch(...)``) validates ``Record`` items
  once and submits bounded POST chunks via :class:`~imednet.utils.batch_coordinator.BatchCoordinator`
  or :class:`~imednet.utils.batch_coordinator.AsyncBatchCoordinator`, returning a
  :class:`~imednet.utils.batch_coordinator.BatchSubmissionSummary`.
* **SDK convenience orchestration**: ``sdk.create_records_batch(..., wait_for_completion=True)``
  (and ``await async_sdk.async_create_records_batch(...)``) combines ``BatchCoordinator``
  chunk submission with ``sdk.poll_jobs`` / ``async_sdk.async_poll_jobs`` so every
  submitted ``batch_id`` is polled to completion and attached to ``summary.poll_summary``.
* **Direct coordinator control**: Instantiate ``BatchCoordinator(chunk_size=...)`` or
  ``AsyncBatchCoordinator(chunk_size=...)`` directly when wrapping custom chunk
  submission callables while preserving single-pass validation and
  ``BatchSubmissionSummary`` reporting (``jobs``, ``batch_ids``, ``chunk_sizes``,
  ``total_records``, ``skipped_count``, ``last_job``, and ``all_successful``).
* **Concurrent multi-Job polling**: ``sdk.poll_jobs(study_key, batch_ids, *, interval=5.0, timeout=300.0, fail_fast=False)``
  and ``sdk.jobs.poll_many(...)`` poll multiple ``Job`` IDs concurrently, resolve
  terminal ``resultUrl`` payloads when available, and return a
  :class:`~imednet.utils.job_poller.JobPollSummary` containing ``results`` and
  ``failures`` dictionaries.

.. testcode::

   from imednet import AsyncImednetSDK, ImednetSDK
   from imednet.simulation import reset_default_simulation_state
   from imednet.utils.batch_coordinator import AsyncBatchCoordinator, BatchCoordinator

   batch_sdk = ImednetSDK(simulation_mode=True)
   records_payload = [
       {
           "formKey": "F_VS",
           "siteName": "SITE",
           "subjectKey": "SUBJ-001",
           "intervalName": "Baseline",
           "data": {"V_WEIGHT": 70.5},
       },
       {
           "formKey": "F_VS",
           "siteName": "SITE",
           "subjectKey": "SUBJ-001",
           "intervalName": "Baseline",
           "data": {"V_WEIGHT": 71.2},
       },
   ]

   # 1. Endpoint-level create_batch + explicit poll_jobs
   endpoint_summary = batch_sdk.records.create_batch("STUDY", records_payload, chunk_size=1)
   poll_summary = batch_sdk.poll_jobs("STUDY", endpoint_summary.batch_ids, interval=0.05, timeout=10.0)
   print(endpoint_summary.chunk_count, poll_summary.all_successful)

   # 2. One-step convenience batch submission with polling
   convenience_summary = batch_sdk.create_records_batch(
       "STUDY",
       records_payload,
       chunk_size=1,
       wait_for_completion=True,
       interval=0.05,
       timeout=10.0,
   )
   print(convenience_summary.total_records, convenience_summary.all_successful)

   # 3. Direct BatchCoordinator and AsyncBatchCoordinator usage
   coordinator = BatchCoordinator(chunk_size=1)
   coord_summary = coordinator.submit(
       "STUDY",
       records_payload,
       lambda chunk: batch_sdk.records.create("STUDY", chunk),
   )
   print(len(coord_summary.batch_ids))

   async def submit_async_batches() -> None:
       async with AsyncImednetSDK(simulation_mode=True) as async_batch_sdk:
           async_coord = AsyncBatchCoordinator(chunk_size=1)
           summary = await async_coord.submit(
               "STUDY",
               records_payload,
               lambda chunk: async_batch_sdk.records.create("STUDY", chunk),
           )
           await async_batch_sdk.async_poll_jobs("STUDY", summary.batch_ids, interval=0.05)

   reset_default_simulation_state()

Inspecting ``JobStatus`` Record Counts and Partial Failures
-----------------------------------------------------------

In the iMednet REST API, a batch ``Job`` can report a top-level ``state`` of
``COMPLETED`` even when individual ``Record`` items inside the batch failed
validation or rule checks. Aligned with
:doc:`/adr/0006-endpoint-composition-and-job-polling`,
:class:`~imednet.models.jobs.JobStatus` exposes normalized record-level counters
and partial-failure predicates:

* ``status.total_records``: Total ``Record`` entries processed in the batch
  (derived from ``results`` entries or ``totalRecords``).
* ``status.successful_records``: Count of ``Record`` entries that succeeded
  (``PASS``, ``SUCCESS``, or ``COMPLETED`` entries or ``successfulRecords``).
* ``status.failed_records``: Count of ``Record`` entries rejected with ``FAIL``,
  ``FAILED``, ``ERROR``, per-entry ``error``/``errors`` details, or ``failedRecords``.
* ``status.result_error``: Top-level error string attached when fetching or
  evaluating the batch ``resultUrl`` payload (``resultError``).
* ``status.is_failed`` / ``status.is_successful``: ``status.is_failed`` evaluates
  to ``True`` not only when ``state`` is ``FAILED``, ``CANCELLED``, or ``TIMEOUT``,
  but also whenever a terminal ``COMPLETED`` ``Job`` has ``failed_records > 0`` or a
  non-empty ``result_error``. Conversely, ``status.is_successful`` is ``True`` only
  when ``failed_records == 0`` and ``result_error`` is empty.

.. testcode::

   from imednet.models.jobs import JobStatus

   partial_failure = JobStatus.model_validate(
       {
           "batchId": "BATCH-PARTIAL-01",
           "state": "COMPLETED",
           "totalRecords": 3,
           "successfulRecords": 2,
           "failedRecords": 1,
           "resultError": "1 Record rejected by Range Check",
       }
   )
   assert partial_failure.total_records == 3
   assert partial_failure.successful_records == 2
   assert partial_failure.failed_records == 1
   assert partial_failure.result_error == "1 Record rejected by Range Check"
   assert partial_failure.is_failed is True
   assert partial_failure.is_successful is False

Typed Endpoint Interface and Terminal Semantics
-----------------------------------------------

``sdk.jobs.poll(study_key, batch_id, on_progress=callback)`` and
``sdk.jobs.poll_many(study_key, batch_ids)`` use the core pollers. Await these
methods on ``AsyncImednetSDK``. SDK convenience methods accept the same optional
callback, which accepts either a single ``JobStatusEvent`` or the legacy
``(batch_id, status, elapsed)`` positional arguments. Callback exceptions
propagate immediately during single-Job polling; batch polling collects per-Job
failures in ``JobPollSummary.failures`` by default, while ``fail_fast=True``
wakes or cancels sibling pollers and raises the triggering ``JobFailedError``
or ``JobTimeoutError`` directly.

Recognized successful terminal state aliases are ``COMPLETED``, ``COMPLETE``, and
``SUCCESS``; ``FAILED``, ``CANCELLED``, and ``TIMEOUT`` (as well as terminal
partial failures where ``JobStatus.is_failed`` is ``True``) raise
``JobFailedError`` in single-Job polling and populate ``JobPollSummary.failures``
in ``poll_many``. Missing states remain non-terminal, unknown states fail
validation, and local timeout raises ``JobTimeoutError``. Local task cancellation
does not cancel a server-side ``Job``. See
:doc:`/adr/0006-endpoint-composition-and-job-polling` for full contract details.
