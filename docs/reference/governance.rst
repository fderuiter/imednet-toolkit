Governance — Trust, Traceability & Audit Trail
===============================================

The governance module provides core capabilities required for regulated
clinical data environments:

1. :ref:`config-version-control` — immutable, SHA-256-signed study
   configuration history with diff and rollback.

This capability is implemented in ``imednet-workflows`` via the ``ConfigVersionStore`` class.

.. _config-version-control:

Configuration Version Control
------------------------------

``imednet_workflows.config_version_control`` provides ``ConfigVersionStore``,
a thread-safe SQLite-backed ledger for :class:`~imednet.models.study_config.StudyConfiguration`
versions.

.. rubric:: Key properties

* **Immutability** — commit rows are append-only; no history block can be
  edited or deleted in place.
* **Hash integrity** — each commit is identified by the SHA-256 digest of its
  serialised JSON body.  Attempting to commit an unchanged configuration raises
  ``ValueError``.
* **Rollback safety** — :meth:`~imednet_workflows.config_version_control.ConfigVersionStore.rollback_config`
  is non-destructive; it returns the historical
  :class:`~imednet.models.study_config.StudyConfiguration` without touching
  any existing rows.

.. rubric:: Quick-start

.. code-block:: python

    from imednet.models.study_config import StudyConfiguration, MappingRule
    from imednet_workflows import ConfigVersionStore

    store = ConfigVersionStore()          # default: ~/.imednet/config_versions.sqlite3

    config = StudyConfiguration(
        studyKey="MY_STUDY",
        mappings=[
            MappingRule(
                domain="AE",
                targetField="aeTerm",
                sourceFormKey="AE_FORM",
                sourceVariableName="ae_term",
            )
        ],
    )

    # Commit a version
    commit_id = store.commit_config(
        study_key="MY_STUDY",
        config=config,
        user="alice",
        desc="Initial mapping configuration",
    )
    print(commit_id)   # SHA-256 hex digest

    # Browse history
    for entry in store.get_history("MY_STUDY"):
        print(entry["version_tag"], entry["commit_id"][:12], entry["timestamp"])

    # Diff two versions (example)
    # diff = store.diff_configs(commit_id, other_commit_id)
    # print(diff["added"], diff["removed"], diff["changed"])

    # Rollback (read-only)
    old_config = store.rollback_config("MY_STUDY", commit_id)

.. rubric:: API reference

.. autoclass:: imednet_workflows.config_version_control.ConfigVersionStore
   :members:
   :undoc-members:
   :show-inheritance:
