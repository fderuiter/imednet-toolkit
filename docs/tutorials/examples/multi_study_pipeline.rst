Multi-Study Pipeline
====================

Prerequisites
-------------

* ``imednet`` package installed
* Access to an iMednet environment

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=<your-api-key>
   export IMEDNET_SECURITY_KEY=<your-security-key>
   # Optional filters and concurrency tuning:
   # export IMEDNET_WHITELIST="PROT-01,PROT-02"
   # export IMEDNET_BLACKLIST="PROT-INACTIVE"
   # export IMEDNET_MAX_WORKERS=8

Description
-----------

Demonstrates using :class:`~imednet.orchestration.MultiStudyOrchestrator` to
run a data extraction pipeline concurrently across all active iMednet studies,
with per-study fault isolation and structured telemetry context.

Each study runs in its own :func:`~imednet.core.context.study_context` thread
so failures in one study do not affect others. The orchestrator collects
results in a normalized :data:`~imednet.orchestration.OrchestratorResult`
dict keyed by study key.

For the recommended Airflow production pattern (TaskFlow discovery feeding
mapped provider operators with retries/timeouts/concurrency guardrails), see
:doc:`/how-to/airflow` and
``examples/airflow/airflow_multi_study_pipeline.py``.

.. literalinclude:: /../examples/workflows/multi_study_pipeline.py
   :language: python
