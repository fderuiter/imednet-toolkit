Airflow Export Operator DAG
===========================

Prerequisites
-------------

* ``apache-airflow-providers-imednet`` and ``imednet[export]`` installed in an
  Apache Airflow 3.2+ environment

Environment variables
---------------------

Configure the ``imednet_default`` connection in Apache Airflow (or set
``AIRFLOW_CONN_IMEDNET_DEFAULT``):

.. code-block:: bash

   export AIRFLOW_CONN_IMEDNET_DEFAULT="https://<api-key>:<security-key>@your.imednet.com"

Description
-----------

Define an Apache Airflow DAG using ``ImednetExportOperator`` to export Study
Records to CSV via an Airflow connection.

See :doc:`/how-to/airflow` for connection setup and export destination options.

.. literalinclude:: /../examples/airflow/imednet_export_operator_dag.py
   :language: python
