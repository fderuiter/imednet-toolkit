Airflow Deferrable Job Sensor DAG
=================================

Prerequisites
-------------

* ``apache-airflow-providers-imednet`` installed in an Apache Airflow 3.2+
  environment with a running triggerer service

Environment variables
---------------------

Configure the ``imednet_default`` connection in Apache Airflow (or set
``AIRFLOW_CONN_IMEDNET_DEFAULT``):

.. code-block:: bash

   export AIRFLOW_CONN_IMEDNET_DEFAULT="https://<api-key>:<security-key>@your.imednet.com"

Description
-----------

Monitor an asynchronous iMednet Job in Apache Airflow using
``ImednetJobSensor`` with ``deferrable=True``, releasing the Airflow slot to
``ImednetJobTrigger`` until the Job reaches a terminal state.

See :doc:`/how-to/airflow` for deferrable sensor configuration.

.. literalinclude:: /../examples/airflow/imednet_job_sensor_dag.py
   :language: python
