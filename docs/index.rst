.. note::
   This SDK is unofficial and not affiliated with the vendor. Use at your own risk.

Welcome to imednet's documentation!
===================================

Role-Based Pathways
-------------------

.. container:: role-cards-grid

   .. container:: role-card role-pathway-section

      .. rubric:: Clinical Data Managers
         :class: role-card-title

      Curated workflows for clinical study configuration, subject registration, form building, and data verification.

      * :doc:`how-to/register_subjects`
      * :doc:`how-to/form_builder`
      * :doc:`how-to/workflows/bulk_submission`
      * :doc:`how-to/verification`

   .. container:: role-card role-pathway-section

      .. rubric:: Integration Engineers
         :class: role-card-title

      Quick-start tutorials, CLI tooling, Airflow orchestration, and core Python SDK and REST API references.

      * :doc:`tutorials/quick_start`
      * :doc:`how-to/cli`
      * :doc:`how-to/airflow`
      * :doc:`reference/rest_api_reference`
      * :doc:`reference/api/core`

   .. container:: role-card role-pathway-section

      .. rubric:: Regulatory Compliance Auditors
         :class: role-card-title

      Accessibility conformance disclosures (VPAT / WCAG 2.1 AA), governance policies, standards, and architecture records.

      * :doc:`reference/VPAT`
      * :doc:`reference/governance`
      * :doc:`reference/project_standards`
      * :doc:`reference/security_exceptions`
      * :doc:`adr/index`

Documentation Overview
----------------------

This documentation is organized according to the Diátaxis framework into four main sections.

.. toctree::
   :maxdepth: 2
   :caption: Tutorials:

   tutorials/quick_start
   tutorials/async_quick_start
   tutorials/examples/index

.. toctree::
   :maxdepth: 2
   :caption: How-to Guides:

   how-to/cli
   how-to/logging_and_tracing
   how-to/pagination
   how-to/form_builder
   how-to/register_subjects
   how-to/caching
   how-to/retry_policy
   how-to/job_polling
   how-to/airflow
   how-to/workflows/uat_workflow
   how-to/workflows/bulk_submission
   how-to/export_destinations
   how-to/duckdb_integration
   how-to/snowflake_export
   how-to/orchestration
   how-to/record_mapping
   how-to/verification

.. toctree::
   :maxdepth: 2
   :caption: Explanation:

   explanation/architecture
   explanation/api_overview
   explanation/parsing
   explanation/schema_validation
   explanation/plugins
   explanation/workflow_interactions
   explanation/refactoring_patterns

.. toctree::
   :maxdepth: 2
   :caption: Reference:

   reference/rest_api_reference
   reference/configuration
   reference/agent_rules
   reference/refactoring_roadmaps
   reference/test_skip_conditions
   reference/live_test_charter
   reference/live_test_plan
   reference/live_tests
   reference/contributing
   reference/issue_management
   reference/project_standards
   reference/triage_playbook
   reference/governance
   reference/verification_reports
   reference/security_exceptions
   reference/VPAT
   adr/index
   reference/api/core
   reference/api/plugins-sinks
   reference/api/plugins-workflows
   reference/api/providers-airflow

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
