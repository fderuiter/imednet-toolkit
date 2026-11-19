Neo4j Export Example
====================

Export Study Records into a Neo4j property graph preserving the
``Study → Subject → Visit → Record`` hierarchy.

Prerequisites
-------------

* ``imednet[neo4j]`` installed:

  .. code-block:: bash

     pip install 'imednet[neo4j]'

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=...
   export IMEDNET_SECURITY_KEY=...      # if required by your environment
   export NEO4J_URI=bolt://localhost:7687
   export NEO4J_USER=neo4j
   export NEO4J_PASSWORD=...            # never hardcode
   export NEO4J_DATABASE=neo4j
   export IMEDNET_STUDY_KEY=MY_STUDY

.. literalinclude:: /../examples/neo4j_export.py
   :language: python
