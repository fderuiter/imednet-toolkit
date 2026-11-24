MongoDB Export Example
======================

Export ``Study`` ``Record`` items to MongoDB while preserving nested ``record_data``
envelopes (or flat CDISC SDTM domain documents when ``as_sdtm=True``), encoding pure
``date`` values as ISO-8601 strings and ``Decimal`` values as ``bson.Decimal128``, and
keying ``_id`` on ``<study_key>/<record_id>`` (or ``<STUDYID>/<DOMAIN>/<USUBJID>/<*SEQ>``
fallback when ``as_sdtm=True`` and ``record_id`` is absent).

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
