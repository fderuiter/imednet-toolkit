Snowflake Export Example
========================

Export Study Records to Snowflake using staged Parquet and ``MERGE INTO`` / ``COPY INTO``.

Prerequisites
-------------

* ``imednet[snowflake]`` installed:

  .. code-block:: bash

     pip install 'imednet[snowflake]'

* Access to a Snowflake account with an internal stage and sufficient privileges to
  execute ``PUT`` and ``COPY INTO``.

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=...
   export IMEDNET_SECURITY_KEY=...      # if required by your environment
   export SNOWFLAKE_ACCOUNT=myorg-myaccount
   export SNOWFLAKE_USER=loader
   export SNOWFLAKE_PASSWORD=...        # never hardcode
   export SNOWFLAKE_DATABASE=IMEDNET_DB
   export SNOWFLAKE_SCHEMA=PUBLIC
   export SNOWFLAKE_WAREHOUSE=COMPUTE_WH
   export SNOWFLAKE_STAGE=MY_STAGE
   export SNOWFLAKE_TABLE=RECORDS
   export IMEDNET_STUDY_KEY=MY_STUDY

Description
-----------

Study Records are fetched from the iMednet API, serialised to Parquet in a local staging
directory, uploaded to the Snowflake internal stage via ``PUT``, and merged into the
destination table via ``COPY INTO`` and ``MERGE INTO``.  See :doc:`/how-to/snowflake_export`
for a full explanation of the framework, idempotency, and cleanup behaviour.

.. literalinclude:: /../examples/snowflake_export.py
   :language: python
