Extract Records by Criteria
===========================

Prerequisites
-------------

* ``imednet`` and ``imednet-workflows`` packages installed

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=<your-api-key>
   export IMEDNET_SECURITY_KEY=<your-security-key>
   export IMEDNET_STUDY_KEY=<your-study-key>
   # Optional offline simulation mode:
   # export IMEDNET_SIMULATION_MODE=true

Description
-----------

Filter Study Records across correlated Subject and Visit criteria—such as
enrolled Subjects within a ``Baseline`` Interval—using
``DataExtractionWorkflow.extract_records_by_criteria``.

See :doc:`/how-to/workflows/data_extraction` for multi-entity Record filtering
patterns.

.. literalinclude:: /../examples/workflows/extract_records.py
   :language: python
