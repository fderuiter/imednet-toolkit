Bulk Record Creation Workflow
=============================

Prerequisites
-------------

* ``imednet`` and ``imednet-workflows`` packages installed

Environment variables
---------------------

.. code-block:: bash

   # Falls back automatically to simulation mode when credentials are unset:
   export IMEDNET_STUDY_KEY=STUDY

Description
-----------

Register Subjects at a Site and submit dependent Study Form Records in ordered
batches using ``BulkRecordSubmissionWorkflow``, polling asynchronous Job
completion before dispatching follow-up Visit Records.

See :doc:`/how-to/workflows/bulk_submission` for batch sizing and registration
polling configuration.

.. literalinclude:: /../examples/workflow_bulk_submission.py
   :language: python
