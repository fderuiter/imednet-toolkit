Airflow Multi-Study Dynamic Mapping DAG
=======================================

Prerequisites
-------------

* ``apache-airflow-providers-imednet`` and ``imednet[export]`` installed in an
  Apache Airflow 3.2+ environment
* An ``imednet_exports`` Airflow pool configured to match your Study export
  concurrency limits

Environment variables
---------------------

.. code-block:: bash

   export AIRFLOW_CONN_IMEDNET_DEFAULT="https://<api-key>:<security-key>@your.imednet.com"
   export IMEDNET_EXPORT_ROOT="/tmp/imednet_exports"

Description
-----------

Production reference DAG pairing lightweight TaskFlow Study discovery via
``ImednetHook`` with dynamically mapped ``ImednetExportOperator`` instances,
bounded concurrency pools, retries, and execution timeouts.

See :doc:`/how-to/airflow` and :doc:`multi_study_pipeline` for comparison with
in-process SDK orchestration.

.. literalinclude:: /../examples/airflow/airflow_multi_study_pipeline.py
   :language: python
