Governance — Trust, Traceability & Audit Trail
===============================================

The governance module provides core capabilities required for regulated
clinical data environments:

1. :ref:`config-version-control` — immutable, AES-256/Fernet-encrypted,
   parent-chained SHA-256 ``Study`` configuration history with RBAC enforcement,
   structural diff, and read-only rollback.

This capability is implemented in ``imednet-workflows`` via the ``ConfigVersionStore`` class.

.. _config-version-control:

Configuration Version Control
------------------------------

``imednet_workflows.config_version_control`` provides ``ConfigVersionStore``,
a thread-safe SQLite-backed ledger for :class:`~imednet.models.study_config.StudyConfiguration`
versions.

.. rubric:: Key properties

* **AES-256 / Fernet payload encryption** — serialized ``StudyConfiguration``
  JSON payloads are encrypted at rest before SQLite persistence using
  ``cryptography.fernet.Fernet``. Key material is resolved from
  ``IMEDNET_ENCRYPTION_KEY`` (falling back to ``IMEDNET_SECURITY_KEY``): a
  32-byte URL-safe base64 Fernet key is used directly, while passphrases are
  hashed with SHA-256 into a 32-byte key. Missing or mismatched keys fail
  closed with ``ValueError``.
* **Dynamic host and Study path scoping** — the base SQLite path defaults to
  ``IMEDNET_CONFIG_DB_PATH`` or ``~/.imednet/config_versions.sqlite3``. At
  runtime, ``db_path`` isolates ledger files under
  ``<base_dir>/<sanitized_host>/[<study_key>/]<filename>`` derived from the
  active ``base_url_context`` / ``IMEDNET_BASE_URL`` (default ``localhost``)
  and optional ``study_context`` (validated via ``validate_partition_key`` to
  prevent path traversal).
* **Parent-chained SHA-256 commit hashes & SQLite immutability** — each payload
  computes ``content_hash = sha256(config_json)``. Root commits use
  ``content_hash`` as ``commit_id``, while child commits chain cryptographically
  via ``sha256(f"{parent_commit_id}:{content_hash}")`` (with ``Study``-key
  disambiguation if an identical chain appears in another ``Study``) and update
  per-``Study`` HEAD pointers in ``config_heads``. SQLite ``BEFORE UPDATE`` and
  ``BEFORE DELETE`` triggers block in-place mutation or deletion of
  ``config_commits`` rows, and ``get_history`` /
  :meth:`~imednet_workflows.config_version_control.ConfigVersionStore.rollback_config`
  re-verify both ``content_hash`` and the parent-chained ``commit_id`` after
  decryption. Committing an unchanged configuration relative to HEAD raises
  ``ValueError``.
* **Role-Based Access Control (RBAC)** — ``commit_config(..., sdk=sdk)``
  requires an authenticated ``ImednetSDK`` instance with ``sdk.auth`` configured
  (raising ``PermissionError`` otherwise). ``sdk.auth.get_user_roles()`` must
  grant ``"admin"`` or ``"manager"`` (rejecting ``"reviewer"`` and ``"viewer"``
  roles with ``PermissionError``). When legacy ``ApiKeyAuth`` is used without
  OIDC role claims, ``ConfigVersionStore`` validates server-side ``Study``
  access via ``sdk.get_sites(study_key, limit=1)`` before recording
  ``author="legacy-api-key"``.
* **Rollback safety** — :meth:`~imednet_workflows.config_version_control.ConfigVersionStore.rollback_config`
  is non-destructive; it decrypts, verifies, and returns the historical
  :class:`~imednet.models.study_config.StudyConfiguration` without modifying
  existing ledger rows.

.. rubric:: Quick-start

.. testcode::

    import os
    import tempfile
    from pathlib import Path

    from imednet import ImednetSDK
    from imednet.models.study_config import MappingRule, StudyConfiguration
    from imednet.simulation import SimulationTransport
    from imednet_workflows import ConfigVersionStore

    prev_enc_key = os.environ.get("IMEDNET_ENCRYPTION_KEY")
    os.environ["IMEDNET_ENCRYPTION_KEY"] = "governance-demo-key"  # pragma: allowlist secret
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = Path(tmpdir) / "config_versions.sqlite3"
            store = ConfigVersionStore(db_path=db_file)
            transport = SimulationTransport()

            with ImednetSDK(
                api_key="sim-api-key",  # pragma: allowlist secret
                security_key="sim-security-key",  # pragma: allowlist secret
                transport=transport,
            ) as sdk:
                v1_config = StudyConfiguration.model_validate(
                    {
                        "studyKey": "STUDY",
                        "mappings": [
                            MappingRule.model_validate(
                                {
                                    "domain": "AE",
                                    "targetField": "AETERM",
                                    "sourceFormKey": "AE_FORM",
                                    "sourceVariableName": "ae_term",
                                }
                            )
                        ],
                    }
                )
                v1_id = store.commit_config(
                    study_key="STUDY",
                    config=v1_config,
                    desc="Initial AE Record mapping configuration",
                    sdk=sdk,
                )

                v2_config = StudyConfiguration.model_validate(
                    {
                        "studyKey": "STUDY",
                        "phiFields": ["custom_note"],
                        "mappings": v1_config.mappings,
                    }
                )
                v2_id = store.commit_config(
                    study_key="STUDY",
                    config=v2_config,
                    desc="Add custom PHI Variable declaration",
                    sdk=sdk,
                )

                history = store.get_history("STUDY")
                assert len(history) == 2
                assert history[0]["commit_id"] == v1_id
                assert history[1]["commit_id"] == v2_id
                assert history[1]["parent_commit_id"] == v1_id

                diff = store.diff_configs(v1_id, v2_id)
                assert diff["added"] == {"phiFields[0]": "custom_note"}

                restored = store.rollback_config("STUDY", v1_id)
                assert restored.phi_fields == []
    finally:
        if prev_enc_key is None:
            os.environ.pop("IMEDNET_ENCRYPTION_KEY", None)
        else:
            os.environ["IMEDNET_ENCRYPTION_KEY"] = prev_enc_key

.. rubric:: API reference

.. autoclass:: imednet_workflows.config_version_control.ConfigVersionStore
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
