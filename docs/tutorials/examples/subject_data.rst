Subject Data Aggregation
========================

Prerequisites
-------------

* ``imednet`` and ``imednet-workflows`` packages installed

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=<your-api-key>
   export IMEDNET_SECURITY_KEY=<your-security-key>
   export IMEDNET_STUDY_KEY=<your-study-key>
   export IMEDNET_SUBJECT_KEY=<your-subject-key>
   # Optional offline simulation mode:
   # export IMEDNET_SIMULATION_MODE=true

Description
-----------

Collect a single Subject's metadata, Visits, Intervals, Records, and Queries in
one call using ``SubjectDataWorkflow.get_all_subject_data``.

See :doc:`/how-to/workflows/subject_data` for consolidated Subject views.

.. literalinclude:: /../examples/workflows/subject_data.py
   :language: python
