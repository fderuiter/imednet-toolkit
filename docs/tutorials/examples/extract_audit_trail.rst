Extract Audit Trail
===================

Prerequisites
-------------

* ``imednet`` and ``imednet-workflows`` packages installed

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=<your-api-key>
   export IMEDNET_SECURITY_KEY=<your-security-key>
   export IMEDNET_STUDY_KEY=<your-study-key>
   # Optional user filter or offline simulation mode:
   # export IMEDNET_AUDIT_USER=<username>
   # export IMEDNET_SIMULATION_MODE=true

Description
-----------

Retrieve Record revision history across a Study date range with
``DataExtractionWorkflow.extract_audit_trail``, optionally scoped to an audit
user filter. When ``IMEDNET_SIMULATION_MODE=true`` is set, the script executes
against deterministic in-memory Study fixtures without live credentials.

See :doc:`/how-to/workflows/data_extraction` for additional audit extraction
options.

.. literalinclude:: /../examples/workflows/extract_audit_trail.py
   :language: python
