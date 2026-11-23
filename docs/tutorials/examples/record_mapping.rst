Record Mapping to CSV
=====================

Prerequisites
-------------

* ``imednet`` and ``imednet-workflows`` packages installed (or ``imednet[export]``)

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_API_KEY=<your-api-key>
   export IMEDNET_SECURITY_KEY=<your-security-key>
   export IMEDNET_STUDY_KEY=<your-study-key>
   # Optional output path override or offline simulation mode:
   # export IMEDNET_OUTPUT_PATH=./record_mapping_output/records.csv
   # export IMEDNET_SIMULATION_MODE=true

Description
-----------

Pivot Study Records and Variable values into a tabular ``pandas.DataFrame`` with
``RecordMapper.dataframe`` and write the resulting dataset to CSV.

See :doc:`/how-to/record_mapping` for wide and long tabular export options.

.. literalinclude:: /../examples/workflows/record_mapping.py
   :language: python
