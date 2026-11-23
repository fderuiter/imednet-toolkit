Package Changelogs
==================

Each distribution in the ``imednet-toolkit`` workspace ships independently under
Semantic Versioning managed by Release Please. Review the per-package changelogs
below alongside :doc:`/reference/compatibility` before upgrading ``1.x``
distributions.

.. list-table:: Workspace package changelogs
   :header-rows: 1
   :widths: 35 30 35

   * - Distribution
     - Workspace path
     - Release notes
   * - ``imednet``
     - ``packages/core``
     - :doc:`core`
   * - ``imednet-workflows``
     - ``packages/plugins-workflows``
     - :doc:`plugins-workflows`
   * - ``imednet-plugins-sinks``
     - ``packages/plugins-sinks``
     - :doc:`plugins-sinks`
   * - ``apache-airflow-providers-imednet``
     - ``packages/providers-airflow``
     - :doc:`providers-airflow`
   * - ``imednet-tui``
     - ``packages/plugins-tui``
     - :doc:`plugins-tui`

.. toctree::
   :maxdepth: 2

   core
   plugins-workflows
   plugins-sinks
   providers-airflow
   plugins-tui
