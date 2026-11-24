Data Dictionary UAT Synthesis and Execution
===========================================

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

Combine Study ``DataDictionary`` Variable bounds, codelists, and conditional
business logic rules with ``UATWorkflow`` and ``UATExecutionEngine`` to
synthesize boundary, out-of-bounds, missing-required, and rule-triggering
Records and verify resulting Study Queries.

See :doc:`/how-to/workflows/uat_workflow` and :doc:`/how-to/data_dictionary`
for Data Dictionary validation details.

.. literalinclude:: /../examples/workflow_uat.py
   :language: python
