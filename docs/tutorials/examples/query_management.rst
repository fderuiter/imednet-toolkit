Query Management
================

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

Inspect open Queries across a Study and aggregate Query state counts using
``sdk.workflows.query_management.get_open_queries`` and
``get_query_state_counts``.

See :doc:`/how-to/workflows/query_management` and
:doc:`/how-to/workflows/triage_store` for full Query management guides.

.. literalinclude:: /../examples/workflows/query_management.py
   :language: python
