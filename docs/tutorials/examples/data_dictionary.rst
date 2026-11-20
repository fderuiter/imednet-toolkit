Data Dictionary & Rule Validation
=================================

Prerequisites
-------------

* ``imednet`` and ``imednet-workflows`` packages installed

Environment variables
---------------------

No live EDC credentials are required; this showcase runs offline using temporary
CSV/ODM fixtures and ``SimulationTransport``.

Description
-----------

Load a Study Data Dictionary from 4-file CSV exports, CDISC ODM 1.3 XML, REDCap CSV,
and the simulated REST API, compute structural diffs across revisions, evaluate
conditional XML edit checks via ``LogicTreeEvaluator``, and enforce
``schema=data_dictionary`` before submitting Records.

See :doc:`/how-to/data_dictionary` and :doc:`/explanation/schema_validation` for full
CLI and Python API reference recipes.

.. literalinclude:: /../examples/data_dictionary.py
   :language: python
