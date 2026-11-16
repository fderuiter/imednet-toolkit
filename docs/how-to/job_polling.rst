Job Polling Mechanism
=====================

:class:`~imednet.utils.job_poller.JobPoller` waits for background jobs to
finish. It repeatedly checks the job status until a terminal state is reached or
a timeout occurs.

.. mermaid:: /diagrams/job_polling_2.mmd

Using the SDK
-------------

``ImednetSDK`` provides a convenience method ``poll_job`` that blocks until the
job finishes. ``AsyncImednetSDK`` exposes ``async_poll_job`` for the same
behavior in asynchronous code.

.. testcode::

   from imednet import ImednetSDK, AsyncImednetSDK

   # synchronous
   sdk = ImednetSDK()
   status = sdk.poll_job("STUDY", "BATCH", interval=2, timeout=60)

   # asynchronous
   async def wait_async() -> None:
       async with AsyncImednetSDK() as sdk:
           status = await sdk.async_poll_job("STUDY", "BATCH", interval=2, timeout=60)

Typed endpoint interface
------------------------

``sdk.jobs.poll(study_key, batch_id, on_progress=callback)`` and
``sdk.jobs.poll_many(study_key, batch_ids)`` use the core pollers. Await these
methods on the async SDK. SDK convenience methods accept the same optional
callback. A callback accepts either one ``JobStatusEvent`` or the legacy
``(batch_id, status, elapsed)`` arguments. Callback failures propagate once;
batch polling collects them in ``JobPollSummary.failures`` by default.

Recognized successful terminal aliases are COMPLETED, COMPLETE and SUCCESS;
FAILED, CANCELLED and TIMEOUT raise ``JobFailedError``. Missing states remain
nonterminal, unknown states fail validation, and local timeout raises
``JobTimeoutError``. Task cancellation does not cancel a server-side Job.
See :doc:`/adr/0006-endpoint-composition-and-job-polling` for compatibility and
evidence limits. Workflow poller imports and MultiStudyOrchestrator remain available.
