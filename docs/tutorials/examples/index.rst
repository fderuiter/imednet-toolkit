Examples
========

Runnable scripts covering core SDK endpoints, clinical workflows, UAT
synthesis, Apache Airflow orchestration, and external export sinks.

Quick start
-----------

.. toctree::
   :maxdepth: 1

   quick_start
   async_quick_start

Basic endpoints
---------------

.. toctree::
   :maxdepth: 1

   get_studies
   get_sites
   get_subjects
   get_records
   get_records_async
   get_record_revisions
   get_codings
   get_users
   get_variables
   get_visits
   get_study_structure
   post_register_subjects

Clinical workflows & UAT
------------------------

.. toctree::
   :maxdepth: 1

   data_dictionary
   extract_records
   extract_audit_trail
   subject_data
   query_management
   queries_by_site
   record_mapping
   update_record
   uat_generate_records
   workflow_uat
   workflow_bulk_submission
   multi_study_pipeline

Apache Airflow
--------------

.. toctree::
   :maxdepth: 1

   airflow_export_operator_dag
   airflow_job_sensor_dag
   airflow_multi_study_pipeline

Export sinks & advanced
-----------------------

.. toctree::
   :maxdepth: 1

   custom_retry
   export_long_sql
   mongodb_export
   neo4j_export
   query_partitioned_lakehouse
   snowflake_export
   build_form_payload
