Synthetic UAT Record Generation
===============================

Prerequisites
-------------

* ``imednet`` and ``imednet-workflows`` packages installed

Environment variables
---------------------

No live credentials are required; this script synthesizes deterministic Form
Record payloads offline from an in-memory ``UATSpecification``.

Description
-----------

Configure ``UATSpecification``, ``UATSubjectSpec``, ``UATFormSpec``, and
``UATVariableSpec`` strategies (such as boundary testing and valid choice
sampling) and generate deterministic Subject registration and Visit Record sets
with ``SyntheticRecordGenerator``.

See :doc:`/how-to/workflows/uat_workflow` for end-to-end UAT preparation.

.. literalinclude:: /../examples/uat_generate_records.py
   :language: python
