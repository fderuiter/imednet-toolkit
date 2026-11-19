MongoDB Export Example
======================

Export Study Records to MongoDB while preserving nested ``record_data`` payloads.

Prerequisites
-------------

* ``imednet[mongodb]`` installed:

  .. code-block:: bash

     pip install 'imednet[mongodb]'

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=...
   export IMEDNET_SECURITY_KEY=...      # if required by your environment
   export MONGODB_URI=mongodb://localhost:27017
   export MONGODB_DATABASE=imednet
   export MONGODB_COLLECTION=records
   export IMEDNET_STUDY_KEY=MY_STUDY

.. literalinclude:: /../examples/mongodb_export.py
   :language: python
