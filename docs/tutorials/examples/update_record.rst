Create Record and Poll Job
==========================

Prerequisites
-------------

* ``imednet`` and ``imednet-workflows`` packages installed

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=<your-api-key>
   export IMEDNET_SECURITY_KEY=<your-security-key>
   export IMEDNET_STUDY_KEY=<your-study-key>
   export IMEDNET_FORM_KEY=<your-form-key>
   export IMEDNET_SUBJECT_KEY=<your-subject-key>
   # Optional offline simulation mode:
   # export IMEDNET_SIMULATION_MODE=true

Description
-----------

Create an unscheduled Form Record for an existing Subject via
``RecordUpdateWorkflow.create_new_record`` and poll the resulting asynchronous
Job batch identifier with ``JobPoller`` until terminal completion.

See :doc:`/how-to/workflows/record_update` for scheduled Visit Record updates
and Data Dictionary validation.

.. literalinclude:: /../examples/workflows/update_record.py
   :language: python
