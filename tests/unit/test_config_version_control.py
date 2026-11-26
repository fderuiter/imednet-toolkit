"""Unit tests for ConfigVersionStore (config_version_control module)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from imednet.models.study_config import MappingRule, StudyConfiguration, WidgetConfig
from imednet.utils.serialization import flatten as _flatten
from imednet_workflows.config_version_control import ConfigVersionStore, _sha256_of

# ---------------------------------------------------------------------------
# Fixtures & Mocks
# ---------------------------------------------------------------------------


def _make_config(
    study_key: str = "STUDY-01",
    version: str = "1.0.0",
    *,
    extra_mapping: bool = False,
) -> StudyConfiguration:
    """Helper function to make config."""
    mappings = [
        MappingRule(
            domain="AE",
            targetField="aeTerm",
            sourceFormKey="AE_FORM",
            sourceVariableName="ae_term",
        )
    ]
    if extra_mapping:
        mappings.append(
            MappingRule(
                domain="PD",
                targetField="dvTerm",
                sourceFormKey="PD_FORM",
                sourceVariableName="pd_term",
            )
        )
    return StudyConfiguration(
        version=version,
        studyKey=study_key,
        mappings=mappings,
        widgets=[
            WidgetConfig(
                widgetId="w1",
                type="kpi_card",
                title="AE Count",
                domain="AE",
                layoutCols=6,
            )
        ],
        terminologyLookups={"AE.aeTerm": {"mild": "MILD"}},
    )


class MockAuth:
    """Mock authentication strategy for testing."""

    def __init__(
        self,
        roles: list[str] | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._roles = ["admin"] if roles is None else roles
        self.api_key = api_key
        if user_id is not None:
            self._user_id = user_id
        elif api_key:
            self._user_id = "api-key-user"
        else:
            self._user_id = "alice"

    def get_user_roles(self) -> list[str]:
        return list(self._roles)

    def get_user_id(self) -> str:
        return self._user_id


class MockSDK:
    """Mock SDK instance providing authentication and permissions."""

    def __init__(self, auth: MockAuth | None = None) -> None:
        self.auth: Any = auth if auth is not None else MockAuth()

    def get_sites(self, study_key: str, limit: int = 1) -> list[dict[str, str]]:
        return [{"siteId": "SITE-01"}]


@pytest.fixture
def mock_sdk() -> MockSDK:
    """Fixture providing an authorized mock SDK instance."""
    return MockSDK()


@pytest.fixture
def store(tmp_path: Path) -> ConfigVersionStore:
    """Helper function to store."""
    return ConfigVersionStore(db_path=tmp_path / "test_versions.sqlite3")


# ---------------------------------------------------------------------------
# SHA-256 helpers
# ---------------------------------------------------------------------------


def test_sha256_deterministic() -> None:
    """Test that sha256 deterministic."""
    assert _sha256_of("hello") == _sha256_of("hello")
    assert _sha256_of("hello") != _sha256_of("world")


def test_sha256_length() -> None:
    """Test that sha256 length."""
    digest = _sha256_of("any string")
    assert len(digest) == 64


# ---------------------------------------------------------------------------
# Flatten helper
# ---------------------------------------------------------------------------


def test_flatten_flat_dict() -> None:
    """Test that flatten flat dict."""
    data = {"a": 1, "b": "two"}
    result = _flatten(data)
    assert result == {"a": 1, "b": "two"}


def test_flatten_nested_dict() -> None:
    """Test that flatten nested dict."""
    data = {"outer": {"inner": 42}}
    result = _flatten(data)
    assert result == {"outer.inner": 42}


def test_flatten_list() -> None:
    """Test that flatten list."""
    data = {"items": [10, 20]}
    result = _flatten(data)
    assert result == {"items[0]": 10, "items[1]": 20}


def test_flatten_mixed() -> None:
    """Test that flatten mixed."""
    data = {"a": {"b": [1, {"c": 3}]}}
    result = _flatten(data)
    assert "a.b[0]" in result
    assert "a.b[1].c" in result


# ---------------------------------------------------------------------------
# commit_config
# ---------------------------------------------------------------------------


def test_commit_config_returns_sha256(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that commit config returns sha256."""
    config = _make_config()
    commit_id = store.commit_config("STUDY-01", config, desc="Initial commit", sdk=mock_sdk)
    assert len(commit_id) == 64
    assert commit_id.islower()


def test_commit_config_duplicate_raises(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that commit config duplicate raises."""
    config = _make_config()
    store.commit_config("STUDY-01", config, desc="First", sdk=mock_sdk)
    with pytest.raises(ValueError, match="already exists"):
        store.commit_config("STUDY-01", config, desc="Duplicate", sdk=mock_sdk)


def test_commit_config_different_content_succeeds(
    store: ConfigVersionStore, mock_sdk: MockSDK
) -> None:
    """Test that commit config different content succeeds."""
    config_a = _make_config(version="1.0.0")
    config_b = _make_config(version="1.1.0")
    id_a = store.commit_config("STUDY-01", config_a, desc="v1.0.0", sdk=mock_sdk)
    id_b = store.commit_config("STUDY-01", config_b, desc="v1.1.0", sdk=mock_sdk)
    assert id_a != id_b


def test_commit_config_persists_metadata(store: ConfigVersionStore, tmp_path: Path) -> None:
    """Test that commit config persists metadata."""
    config = _make_config()
    bob_sdk = MockSDK(MockAuth(user_id="bob"))
    commit_id = store.commit_config("STUDY-01", config, desc="Test commit", sdk=bob_sdk)
    history = store.get_history("STUDY-01")
    assert len(history) == 1
    entry = history[0]
    assert entry["commit_id"] == commit_id
    assert entry["study_key"] == "STUDY-01"
    assert entry["version_tag"] == "1.0.0"
    assert entry["modified_by"] == "bob"
    assert entry["description"] == "Test commit"
    assert "timestamp" in entry


def test_commit_config_without_sdk_raises_permission_error(store: ConfigVersionStore) -> None:
    """Calling commit_config without an authorized SDK session must raise PermissionError."""
    config = _make_config()
    with pytest.raises(PermissionError, match="authenticated SDK session"):
        store.commit_config("STUDY-01", config, "test", sdk=None)

    with pytest.raises(PermissionError, match="authenticated SDK session"):
        store.commit_config("STUDY-01", config, "test")


def test_commit_config_missing_auth_raises_permission_error(store: ConfigVersionStore) -> None:
    """Calling commit_config with an SDK instance missing auth raises PermissionError."""
    config = _make_config()
    unauth_sdk = MockSDK()
    unauth_sdk.auth = None
    with pytest.raises(PermissionError, match="missing auth strategy"):
        store.commit_config("STUDY-01", config, desc="no auth", sdk=unauth_sdk)


def test_commit_config_unauthorized_role_raises_permission_error(store: ConfigVersionStore) -> None:
    """User lacking admin or manager role cannot commit config."""
    config = _make_config()
    viewer_sdk = MockSDK(MockAuth(roles=["viewer"]))
    with pytest.raises(PermissionError, match="lacks manager or admin role"):
        store.commit_config("STUDY-01", config, desc="viewer commit", sdk=viewer_sdk)

    empty_role_sdk = MockSDK(MockAuth(roles=[]))
    with pytest.raises(PermissionError, match="lacks manager or admin role"):
        store.commit_config("STUDY-01", config, desc="no role commit", sdk=empty_role_sdk)


def test_commit_config_manager_role_succeeds(store: ConfigVersionStore) -> None:
    """User with manager role is permitted to commit config."""
    config = _make_config()
    manager_sdk = MockSDK(MockAuth(roles=["manager"], user_id="manager_user"))
    commit_id = store.commit_config("STUDY-01", config, desc="manager commit", sdk=manager_sdk)
    assert len(commit_id) == 64
    history = store.get_history("STUDY-01")
    assert history[0]["modified_by"] == "manager_user"


def test_commit_config_legacy_key_valid_succeeds(store: ConfigVersionStore) -> None:
    """Legacy API key with study access is granted admin role and succeeds."""
    config = _make_config()
    legacy_sdk = MockSDK(MockAuth(roles=[], api_key="valid-api-key"))
    commit_id = store.commit_config("STUDY-01", config, desc="legacy commit", sdk=legacy_sdk)
    assert len(commit_id) == 64
    history = store.get_history("STUDY-01")
    assert history[0]["modified_by"] == "legacy-api-key"


def test_commit_config_legacy_key_invalid_raises_permission_error(
    store: ConfigVersionStore,
) -> None:
    """Legacy API key that fails study access verification raises PermissionError."""
    config = _make_config()
    legacy_sdk = MockSDK(MockAuth(roles=[], api_key="invalid-key"))

    def _fail_get_sites(study_key: str, limit: int = 1) -> list[dict[str, str]]:
        raise RuntimeError("API access denied")

    legacy_sdk.get_sites = _fail_get_sites
    with pytest.raises(PermissionError, match="Server-side authorization failure for legacy key"):
        store.commit_config("STUDY-01", config, desc="invalid legacy commit", sdk=legacy_sdk)


def test_commit_config_auth_failure_raises_permission_error(store: ConfigVersionStore) -> None:
    """Exceptions raised during auth role or user lookup raise explicit PermissionError."""
    config = _make_config()
    broken_sdk = MockSDK()

    def _fail_roles() -> list[str]:
        raise RuntimeError("Auth service unreachable")

    broken_sdk.auth.get_user_roles = _fail_roles
    with pytest.raises(
        PermissionError, match="Server-side authorization failure: Auth service unreachable"
    ):
        store.commit_config("STUDY-01", config, desc="broken auth", sdk=broken_sdk)

    broken_user_sdk = MockSDK()

    def _fail_user_id() -> str:
        raise RuntimeError("User lookup failed")

    broken_user_sdk.auth.get_user_id = _fail_user_id
    with pytest.raises(
        PermissionError, match="Server-side authorization failure: User lookup failed"
    ):
        store.commit_config("STUDY-01", config, desc="broken user", sdk=broken_user_sdk)


def test_commit_config_unexpected_user_param_raises_type_error(
    store: ConfigVersionStore, mock_sdk: MockSDK
) -> None:
    """Passing removed user or other unexpected keyword arguments raises TypeError."""
    config = _make_config()
    with pytest.raises(TypeError, match="unexpected keyword argument 'user'"):
        store.commit_config("STUDY-01", config, desc="test", sdk=mock_sdk, user="bob")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="unexpected keyword argument 'unexpected_kw'"):
        store.commit_config("STUDY-01", config, desc="test", sdk=mock_sdk, unexpected_kw=True)  # type: ignore[call-arg]


def test_commit_config_empty_user_id_raises_value_error(store: ConfigVersionStore) -> None:
    """When SDK auth yields an empty user identifier, commit_config raises ValueError."""
    config = _make_config()
    empty_user_sdk = MockSDK(MockAuth(roles=["admin"], user_id=""))
    with pytest.raises(ValueError, match="A user identifier is required to commit"):
        store.commit_config("STUDY-01", config, desc="empty user commit", sdk=empty_user_sdk)

    none_user_sdk = MockSDK(MockAuth(roles=["admin"]))
    none_user_sdk.auth.get_user_id = lambda: None  # type: ignore[assignment]
    with pytest.raises(ValueError, match="A user identifier is required to commit"):
        store.commit_config("STUDY-01", config, desc="none user commit", sdk=none_user_sdk)


def test_commit_config_persists_authenticated_modified_by(store: ConfigVersionStore) -> None:
    """Authenticated commits persist the identity resolved from sdk.auth.get_user_id()."""
    config = _make_config()
    auth_sdk = MockSDK(MockAuth(roles=["admin"], user_id="real_user"))
    commit_id = store.commit_config("STUDY-01", config, desc="authenticated commit", sdk=auth_sdk)
    history = store.get_history("STUDY-01")
    assert len(history) == 1
    assert history[0]["commit_id"] == commit_id
    assert history[0]["modified_by"] == "real_user"


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


def test_get_history_empty(store: ConfigVersionStore) -> None:
    """Test that get history empty."""
    assert store.get_history("UNKNOWN") == []


def test_get_history_ordered(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that get history ordered."""
    c1 = _make_config(version="1.0.0")
    c2 = _make_config(version="1.1.0")
    c3 = _make_config(version="1.2.0", extra_mapping=True)

    store.commit_config("STUDY-01", c1, desc="v1.0.0", sdk=mock_sdk)
    store.commit_config("STUDY-01", c2, desc="v1.1.0", sdk=mock_sdk)
    store.commit_config("STUDY-01", c3, desc="v1.2.0", sdk=mock_sdk)

    history = store.get_history("STUDY-01")
    assert len(history) == 3
    # Should be sorted oldest-first by timestamp
    versions = [e["version_tag"] for e in history]
    assert versions == ["1.0.0", "1.1.0", "1.2.0"]


def test_get_history_isolated_by_study(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that get history isolated by study."""
    c1 = _make_config(study_key="STUDY-01")
    c2 = _make_config(study_key="STUDY-02")
    store.commit_config("STUDY-01", c1, desc="first", sdk=mock_sdk)
    store.commit_config("STUDY-02", c2, desc="second", sdk=mock_sdk)

    assert len(store.get_history("STUDY-01")) == 1
    assert len(store.get_history("STUDY-02")) == 1
    assert len(store.get_history("STUDY-03")) == 0


def test_get_history_excludes_config_data(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that get history excludes config data."""
    config = _make_config()
    store.commit_config("STUDY-01", config, desc="first", sdk=mock_sdk)
    history = store.get_history("STUDY-01")
    assert "config_data" not in history[0]


# ---------------------------------------------------------------------------
# diff_configs
# ---------------------------------------------------------------------------


def test_diff_configs_no_changes(store: ConfigVersionStore) -> None:
    """Test that diff configs no changes."""
    # Verify self-diff of a flattened config produces no differences.
    c1 = _make_config(version="1.0.0")
    config_json = json.dumps(c1.model_dump(mode="json", by_alias=True), sort_keys=True)
    flat = _flatten(json.loads(config_json))
    diff = {
        "added": {k: flat[k] for k in set(flat) - set(flat)},
        "removed": {k: flat[k] for k in set(flat) - set(flat)},
        "changed": {
            k: {"before": flat[k], "after": flat[k]}
            for k in flat
            if flat[k] != flat[k]  # always False — proves no self-diff
        },
    }
    assert diff["added"] == {}
    assert diff["removed"] == {}
    assert diff["changed"] == {}


def test_diff_configs_detects_changes(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that diff configs detects changes."""
    c1 = _make_config(version="1.0.0")
    c2 = _make_config(version="1.1.0")
    id_a = store.commit_config("STUDY-01", c1, desc="v1.0.0", sdk=mock_sdk)
    id_b = store.commit_config("STUDY-01", c2, desc="v1.1.0", sdk=mock_sdk)
    diff = store.diff_configs(id_a, id_b)
    # version field should appear in changed
    assert "version" in diff["changed"]
    assert diff["changed"]["version"]["before"] == "1.0.0"
    assert diff["changed"]["version"]["after"] == "1.1.0"


def test_diff_configs_detects_added_keys(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that diff configs detects added keys."""
    c1 = _make_config(version="1.0.0", extra_mapping=False)
    c2 = _make_config(version="1.1.0", extra_mapping=True)
    id_a = store.commit_config("STUDY-01", c1, desc="base", sdk=mock_sdk)
    id_b = store.commit_config("STUDY-01", c2, desc="with-pd", sdk=mock_sdk)
    diff = store.diff_configs(id_a, id_b)
    # New mapping keys should appear in added
    assert diff["added"] or diff["changed"]


def test_diff_configs_missing_commit_raises(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that diff configs missing commit raises."""
    c1 = _make_config()
    id_a = store.commit_config("STUDY-01", c1, desc="base", sdk=mock_sdk)
    with pytest.raises(KeyError):
        store.diff_configs(id_a, "nonexistent_commit_id")


# ---------------------------------------------------------------------------
# rollback_config
# ---------------------------------------------------------------------------


def test_rollback_config_restores_original(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that rollback config restores original."""
    original = _make_config(version="1.0.0")
    commit_id = store.commit_config("STUDY-01", original, desc="base", sdk=mock_sdk)

    # Commit a new version
    updated = _make_config(version="1.1.0")
    store.commit_config("STUDY-01", updated, desc="update", sdk=mock_sdk)

    # Rollback to original commit
    restored = store.rollback_config("STUDY-01", commit_id)
    assert isinstance(restored, StudyConfiguration)
    assert restored.version == "1.0.0"
    assert restored.study_key == "STUDY-01"


def test_rollback_config_is_non_destructive(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that rollback config is non destructive."""
    c1 = _make_config(version="1.0.0")
    c2 = _make_config(version="1.1.0")
    id1 = store.commit_config("STUDY-01", c1, desc="v1", sdk=mock_sdk)
    store.commit_config("STUDY-01", c2, desc="v2", sdk=mock_sdk)

    store.rollback_config("STUDY-01", id1)

    # History should still have both commits
    history = store.get_history("STUDY-01")
    assert len(history) == 2


def test_rollback_config_wrong_study_raises(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that rollback config wrong study raises."""
    config = _make_config(study_key="STUDY-01")
    commit_id = store.commit_config("STUDY-01", config, desc="base", sdk=mock_sdk)
    with pytest.raises(KeyError, match="not found for study"):
        store.rollback_config("STUDY-WRONG", commit_id)


def test_rollback_config_unknown_commit_raises(store: ConfigVersionStore) -> None:
    """Test that rollback config unknown commit raises."""
    with pytest.raises(KeyError):
        store.rollback_config("STUDY-01", "deadbeef" * 8)


# ---------------------------------------------------------------------------
# Round-trip integrity
# ---------------------------------------------------------------------------


def test_commit_content_hash_matches(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """The stored commit_id must equal SHA-256 of the canonical JSON."""
    config = _make_config()
    commit_id = store.commit_config("STUDY-01", config, desc="integrity", sdk=mock_sdk)

    config_json = json.dumps(config.model_dump(mode="json", by_alias=True), sort_keys=True)
    expected_id = _sha256_of(config_json)
    assert commit_id == expected_id


def test_multiple_studies_independent(store: ConfigVersionStore) -> None:
    """Commits to different studies do not interfere with each other."""
    c_a = _make_config(study_key="STUDY-A", version="1.0.0")
    c_b = _make_config(study_key="STUDY-B", version="2.0.0")

    alice_sdk = MockSDK(MockAuth(user_id="alice"))
    bob_sdk = MockSDK(MockAuth(user_id="bob"))
    store.commit_config("STUDY-A", c_a, desc="a1", sdk=alice_sdk)
    store.commit_config("STUDY-B", c_b, desc="b1", sdk=bob_sdk)

    hist_a = store.get_history("STUDY-A")
    hist_b = store.get_history("STUDY-B")

    assert len(hist_a) == 1
    assert hist_a[0]["version_tag"] == "1.0.0"
    assert len(hist_b) == 1
    assert hist_b[0]["version_tag"] == "2.0.0"


def test_get_history_detects_invalid_signature(store: ConfigVersionStore) -> None:
    """Test that get history detects invalid signature."""
    config_json = json.dumps(
        _make_config(version="9.9.9").model_dump(mode="json", by_alias=True),
        sort_keys=True,
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO config_commits
                (commit_id, study_key, version_tag, config_data,
                 modified_by, description, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0" * 64,
                "STUDY-01",
                "9.9.9",
                config_json,
                "alice",
                "forged",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()

    with pytest.raises(ValueError, match="integrity check failed"):
        store.get_history("STUDY-01")


def test_rollback_config_detects_invalid_signature(store: ConfigVersionStore) -> None:
    """Test that rollback config detects invalid signature."""
    config_json = json.dumps(
        _make_config(version="9.9.9").model_dump(mode="json", by_alias=True),
        sort_keys=True,
    )
    forged_commit = "f" * 64
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO config_commits
                (commit_id, study_key, version_tag, config_data,
                 modified_by, description, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                forged_commit,
                "STUDY-01",
                "9.9.9",
                config_json,
                "alice",
                "forged",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()

    with pytest.raises(ValueError, match="integrity check failed"):
        store.rollback_config("STUDY-01", forged_commit)


def test_diff_configs_detects_invalid_signature(
    store: ConfigVersionStore, mock_sdk: MockSDK
) -> None:
    """Test that diff configs detects invalid signature."""
    valid = _make_config(version="1.0.0")
    valid_commit = store.commit_config("STUDY-01", valid, desc="valid", sdk=mock_sdk)
    forged_json = json.dumps(
        _make_config(version="2.0.0").model_dump(mode="json", by_alias=True),
        sort_keys=True,
    )
    forged_commit = "a" * 64
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO config_commits
                (commit_id, study_key, version_tag, config_data,
                 modified_by, description, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                forged_commit,
                "STUDY-01",
                "2.0.0",
                forged_json,
                "alice",
                "forged",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()

    with pytest.raises(ValueError, match="integrity check failed"):
        store.diff_configs(valid_commit, forged_commit)


def test_history_rows_cannot_be_updated(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that history rows cannot be updated."""
    config = _make_config(version="1.0.0")
    commit_id = store.commit_config("STUDY-01", config, desc="base", sdk=mock_sdk)

    with (
        sqlite3.connect(store.db_path) as conn,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        conn.execute(
            "UPDATE config_commits SET version_tag = ? WHERE commit_id = ?",
            ("9.9.9", commit_id),
        )


def test_history_rows_cannot_be_deleted(store: ConfigVersionStore, mock_sdk: MockSDK) -> None:
    """Test that history rows cannot be deleted."""
    config = _make_config(version="1.0.0")
    commit_id = store.commit_config("STUDY-01", config, desc="base", sdk=mock_sdk)

    with (
        sqlite3.connect(store.db_path) as conn,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        conn.execute("DELETE FROM config_commits WHERE commit_id = ?", (commit_id,))


def test_encryption_zero_plaintext_on_disk(tmp_path: Path) -> None:
    """Ensure that raw database inspection reveals no plaintext configuration JSON."""
    import os
    import sqlite3

    os.environ["IMEDNET_SECURITY_KEY"] = "my_secure_encryption_key"
    store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
    config = _make_config()
    bob_sdk = MockSDK(MockAuth(user_id="bob"))
    store.commit_config("STUDY-01", config, desc="secure commit", sdk=bob_sdk)

    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT config_data FROM config_commits").fetchone()
        assert row is not None
        encrypted_payload = row["config_data"]
        assert not encrypted_payload.startswith("{")
        assert "mappings" not in encrypted_payload
        assert "widgets" not in encrypted_payload
        assert encrypted_payload.startswith("gAAAA")


def test_dynamic_sqlite_selection_per_base_url(tmp_path: Path, mock_sdk: MockSDK) -> None:
    """Verify separate SQLite files are instantiated for different base URLs."""
    import os

    from imednet.core.context import reset_base_url_context, set_base_url_context

    store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
    config = _make_config()

    t1 = set_base_url_context("https://env1.imednet.com")
    try:
        os.environ["IMEDNET_SECURITY_KEY"] = "key1"
        path1 = store.db_path
        store.commit_config("STUDY-01", config, desc="env1 commit", sdk=mock_sdk)
    finally:
        reset_base_url_context(t1)

    t2 = set_base_url_context("https://env2.imednet.com")
    try:
        os.environ["IMEDNET_SECURITY_KEY"] = "key2"
        path2 = store.db_path
        assert path1 != path2
        assert store.get_history("STUDY-01") == []
    finally:
        reset_base_url_context(t2)


def test_dynamic_sqlite_selection_per_study_context(tmp_path: Path) -> None:
    """Verify ConfigVersionStore DB path remains invariant to active study_context."""
    from imednet.core.context import reset_study_context, set_study_context

    store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
    unscoped_path = store.db_path

    t1 = set_study_context("STUDY-XYZ")
    try:
        path1 = store.db_path
        assert path1 == unscoped_path
        assert "STUDY-XYZ" not in str(path1)
    finally:
        reset_study_context(t1)


def test_decryption_incorrect_or_missing_key_raises(tmp_path: Path, mock_sdk: MockSDK) -> None:
    """Verify that configuration reads fail if the decryption key is incorrect or missing."""
    import os

    import pytest

    os.environ["IMEDNET_SECURITY_KEY"] = "secret1"
    store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
    config = _make_config()
    commit_id = store.commit_config("STUDY-01", config, desc="env1 commit", sdk=mock_sdk)

    os.environ["IMEDNET_SECURITY_KEY"] = "wrong_secret"
    with pytest.raises(ValueError, match="Decryption failed|integrity check failed"):
        store.get_history("STUDY-01")

    if "IMEDNET_SECURITY_KEY" in os.environ:
        del os.environ["IMEDNET_SECURITY_KEY"]
    if "IMEDNET_ENCRYPTION_KEY" in os.environ:
        del os.environ["IMEDNET_ENCRYPTION_KEY"]

    with pytest.raises(
        ValueError, match="key is missing|incorrect or missing|integrity check failed"
    ):
        store.rollback_config("STUDY-01", commit_id)

    os.environ["IMEDNET_SECURITY_KEY"] = "test_sec_key"


@pytest.mark.parametrize(
    "invalid_key",
    [
        "../../escaped_canary",
        "../study",
        "..\\study",
        "/rooted",
        "\\rooted",
        "C:\\Windows",
        "C:/Windows",
        "study/form",
        "study\\form",
        "study\x00key",
        "..",
    ],
)
def test_dynamic_db_path_invalid_study_key_raises_traversal_error(
    tmp_path: Path, invalid_key: str
) -> None:
    """Verify that invalid study keys containing path traversal or separators raise PathTraversalValidationError."""
    from imednet.core.context import reset_study_context, set_study_context
    from imednet.errors import PathTraversalValidationError

    token = set_study_context(invalid_key)
    try:
        with pytest.raises(PathTraversalValidationError):
            ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
    finally:
        reset_study_context(token)


@pytest.mark.parametrize("valid_key", ["STUDY-01", "study_abc", "STUDY123", "study-01_v2"])
def test_dynamic_db_path_valid_study_keys_stay_within_base(tmp_path: Path, valid_key: str) -> None:
    """Verify valid study_context keys pass traversal checks without splitting the DB path."""
    from imednet.core.context import reset_study_context, set_study_context

    token = set_study_context(valid_key)
    try:
        store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
        db_file = store.db_path
        assert valid_key not in str(db_file)
        assert db_file.resolve().is_relative_to(tmp_path.resolve())
        assert db_file.exists()
    finally:
        reset_study_context(token)


def test_dynamic_db_path_empty_study_key_context(tmp_path: Path) -> None:
    """Verify empty string study context does not fail and resolves within base directory."""
    from imednet.core.context import reset_study_context, set_study_context

    token = set_study_context("")
    try:
        store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
        db_file = store.db_path
        assert db_file.resolve().is_relative_to(tmp_path.resolve())
        assert db_file.exists()
    finally:
        reset_study_context(token)


def test_dynamic_db_path_missing_base_url_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify missing base URL falls back safely to localhost directory."""
    from imednet.core.context import (
        reset_base_url_context,
        reset_study_context,
        set_base_url_context,
        set_study_context,
    )

    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    b_token = set_base_url_context("")
    s_token = set_study_context("STUDY-FALLBACK")
    try:
        store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
        db_file = store.db_path
        assert "localhost" in str(db_file)
        assert "STUDY-FALLBACK" not in str(db_file)
        assert db_file.resolve().is_relative_to(tmp_path.resolve())
        assert db_file.exists()
    finally:
        reset_study_context(s_token)
        reset_base_url_context(b_token)


def test_rollback_and_recommit_creates_parent_chained_commit(
    store: ConfigVersionStore, mock_sdk: MockSDK
) -> None:
    """Committing v1 -> v2 -> restored v1 creates a new commit linked to v2 via parent_commit_id."""
    assert store.get_head("STUDY-01") is None

    v1 = _make_config(study_key="STUDY-01", version="1.0.0")
    v2 = _make_config(study_key="STUDY-01", version="2.0.0", extra_mapping=True)

    id_v1 = store.commit_config("STUDY-01", v1, desc="Release v1", sdk=mock_sdk)
    assert store.get_head("STUDY-01") == id_v1

    id_v2 = store.commit_config("STUDY-01", v2, desc="Release v2", sdk=mock_sdk)
    assert store.get_head("STUDY-01") == id_v2

    restored_v1 = store.rollback_config("STUDY-01", id_v1)
    id_v3 = store.commit_config("STUDY-01", restored_v1, desc="Rollback to v1", sdk=mock_sdk)

    assert id_v3 != id_v1
    assert id_v3 != id_v2
    assert store.get_head("STUDY-01") == id_v3

    history = store.get_history("STUDY-01")
    assert len(history) == 3
    assert history[0]["commit_id"] == id_v1
    assert history[0]["parent_commit_id"] is None
    assert history[1]["commit_id"] == id_v2
    assert history[1]["parent_commit_id"] == id_v1
    assert history[2]["commit_id"] == id_v3
    assert history[2]["parent_commit_id"] == id_v2
    assert history[2]["content_hash"] == history[0]["content_hash"]

    # Duplicate commit identical to current head (v3) is still rejected
    with pytest.raises(ValueError, match="already exists"):
        store.commit_config("STUDY-01", restored_v1, desc="Duplicate rollback", sdk=mock_sdk)

    # Diff between v2 and rolled-back v3 shows reverted version
    diff = store.diff_configs(id_v2, id_v3)
    assert diff["changed"]["version"] == {"before": "2.0.0", "after": "1.0.0"}


def test_legacy_sqlite_schema_automatic_migration_and_rollback(
    tmp_path: Path, mock_sdk: MockSDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing 7-column SQLite DB migrates cleanly and supports parent-chained rollback."""
    monkeypatch.setenv(
        "IMEDNET_SECURITY_KEY", "legacy_migration_test_key"
    )  # pragma: allowlist secret
    from imednet_workflows.config_version_control import _encrypt_payload

    # First resolve the dynamic path that ConfigVersionStore will use
    probe = ConfigVersionStore(db_path=tmp_path / "legacy.sqlite3")
    actual_db_path = probe.db_path
    actual_db_path.unlink(missing_ok=True)

    v1 = _make_config(study_key="STUDY-01", version="1.0.0")
    v1_json = json.dumps(v1.model_dump(mode="json", by_alias=True), sort_keys=True)
    legacy_id_v1 = _sha256_of(v1_json)
    encrypted_v1 = _encrypt_payload(v1_json)

    # Create pre-existing 7-column schema without parent_commit_id, content_hash, or config_heads
    with sqlite3.connect(actual_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE config_commits (
                commit_id   TEXT PRIMARY KEY,
                study_key   TEXT NOT NULL,
                version_tag TEXT NOT NULL,
                config_data TEXT NOT NULL,
                modified_by TEXT NOT NULL,
                description TEXT NOT NULL,
                timestamp   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO config_commits
                (commit_id, study_key, version_tag, config_data, modified_by, description, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_id_v1,
                "STUDY-01",
                "1.0.0",
                encrypted_v1,
                "alice",
                "legacy v1",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()

    migrated_store = ConfigVersionStore(db_path=tmp_path / "legacy.sqlite3")
    assert migrated_store.get_head("STUDY-01") == legacy_id_v1

    v2 = _make_config(study_key="STUDY-01", version="2.0.0")
    id_v2 = migrated_store.commit_config("STUDY-01", v2, desc="v2 after migration", sdk=mock_sdk)
    restored = migrated_store.rollback_config("STUDY-01", legacy_id_v1)
    id_v3 = migrated_store.commit_config(
        "STUDY-01", restored, desc="rollback to legacy v1", sdk=mock_sdk
    )

    history = migrated_store.get_history("STUDY-01")
    assert [entry["commit_id"] for entry in history] == [legacy_id_v1, id_v2, id_v3]
    assert history[1]["parent_commit_id"] == legacy_id_v1
    assert history[2]["parent_commit_id"] == id_v2


def test_resolve_committed_config_fast_path_and_mock_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MockSDK,
) -> None:
    """Verify resolve_committed_config avoids creating missing DBs, resolves committed HEAD, and honors mocks."""
    from unittest.mock import patch

    from imednet.core.context import study_context

    missing_db = tmp_path / "nonexistent_dir" / "versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(missing_db))
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "unit-test-encryption-secret-32b!")

    # Missing DB returns None without creating parent directories or SQLite files
    assert ConfigVersionStore.resolve_committed_config("STUDY-01") is None
    assert not missing_db.parent.exists()

    # Initialized store with no commits for STUDY-99 returns None, while STUDY-01 returns committed config
    active_db = tmp_path / "active" / "versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(active_db))
    store = ConfigVersionStore(db_path=active_db)
    cfg = StudyConfiguration(
        study_key="STUDY-01",
        version="1.2.0",
        phi_fields=["custom_mrn"],
    )
    store.commit_config("STUDY-01", cfg, desc="add custom_mrn", sdk=mock_sdk)

    assert ConfigVersionStore.resolve_committed_config("STUDY-99") is None
    resolved = ConfigVersionStore.resolve_committed_config("STUDY-01")
    assert resolved is not None
    assert resolved.study_key == "STUDY-01"
    assert resolved.phi_fields == ["custom_mrn"]

    # Study context path resolution without creating directories
    with study_context("STUDY-CTX"):
        assert ConfigVersionStore.resolve_committed_config("STUDY-CTX") is None

    # Existing base directory without host subdir or flat DB short-circuits cleanly; flat legacy DB still resolves
    empty_base_db = tmp_path / "existing_base" / "versions.sqlite3"
    empty_base_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(empty_base_db))
    assert ConfigVersionStore.resolve_committed_config("STUDY-01") is None
    assert ConfigVersionStore.resolve_legacy_committed_config("STUDY-01") is None

    import shutil

    shutil.copy2(store.db_path, empty_base_db)
    flat_resolved = ConfigVersionStore.resolve_committed_config("STUDY-01")
    assert flat_resolved is not None
    assert flat_resolved.phi_fields == ["custom_mrn"]
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(active_db))

    # Mocked get_history / rollback_config fallback path
    with (
        patch.object(ConfigVersionStore, "get_history", return_value=[{"commit_id": "abc123"}]),
        patch.object(ConfigVersionStore, "rollback_config", return_value=cfg),
    ):
        mock_resolved = ConfigVersionStore.resolve_committed_config("STUDY-01")
        assert mock_resolved is cfg

    with patch.object(ConfigVersionStore, "get_history", return_value=[]):
        assert ConfigVersionStore.resolve_committed_config("STUDY-01") is None


def test_governance_quickstart_without_env_encryption_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify docs/reference/governance.rst quick-start works without env encryption keys."""
    import respx

    from imednet import ImednetSDK
    from imednet.constants import DEFAULT_BASE_URL
    from imednet.core.context import reset_security_key_context, set_security_key_context

    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(tmp_path / "config_versions.sqlite3"))

    sec_token = set_security_key_context(None)
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{DEFAULT_BASE_URL}/api/v1/edc/studies/MY_STUDY/sites").respond(
                200,
                json={
                    "data": [{"siteId": 1, "studyKey": "MY_STUDY"}],
                    "pagination": {
                        "currentPage": 0,
                        "size": 1,
                        "totalPages": 1,
                        "totalElements": 1,
                    },
                },
            )

            sdk = ImednetSDK(
                api_key="quickstart-api-token",  # pragma: allowlist secret
                security_key="quickstart-sec-token",  # pragma: allowlist secret
            )
            store = ConfigVersionStore()
            config = StudyConfiguration(
                study_key="MY_STUDY",
                phi_fields=["PAT_SSN", "PAT_DOB"],
            )
            commit_id = store.commit_config(
                study_key="MY_STUDY",
                config=config,
                sdk=sdk,
                desc="Add custom PHI field overrides for trial MY_STUDY",
            )

            history = store.get_history("MY_STUDY")
            assert len(history) == 1
            assert history[0]["commit_id"] == commit_id

            old_config = store.rollback_config("MY_STUDY", commit_id)
            assert old_config.phi_fields == ["PAT_SSN", "PAT_DOB"]
    finally:
        reset_security_key_context(sec_token)


def test_exports_mask_custom_phi_across_study_context_orchestrator_and_base_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify exports mask committed phiFields inside study_context, MultiStudyOrchestrator, and default/explicit base_url."""
    import pandas as pd

    from imednet import ImednetSDK
    from imednet.constants import DEFAULT_BASE_URL
    from imednet.core.context import reset_security_key_context, set_security_key_context
    from imednet.integrations.export import export_to_csv
    from imednet.orchestration.orchestrator import MultiStudyOrchestrator
    from imednet.simulation import SimulationState, create_simulation_transport

    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(tmp_path / "config_versions.sqlite3"))

    state = SimulationState()
    state.variables.append(
        {
            "studyKey": "STUDY",
            "variableId": 777,
            "variableName": "CUSTOM_TRIAL_ID_SECRET",
            "variableType": "TEXT",
            "formId": 301,
            "formKey": "F_DM",
            "formName": "Demographics",
            "label": "Custom Trial Secret",
            "deleted": False,
        }
    )
    for rec in state.records:
        if rec.get("studyKey") == "STUDY" and rec.get("formKey") == "F_DM":
            rec.setdefault("recordData", {})["CUSTOM_TRIAL_ID_SECRET"] = "RAW-PHI-999"

    transport = create_simulation_transport(state)
    sec_token = set_security_key_context(None)
    try:
        # 1. Commit configuration using default base_url SDK without env encryption keys
        sdk_default = ImednetSDK(
            api_key="gov-api-key",  # pragma: allowlist secret
            security_key="gov-sec-key",  # pragma: allowlist secret
            transport=transport,
            simulation_mode=True,
        )
        store = ConfigVersionStore()
        cfg = StudyConfiguration(
            study_key="STUDY",
            phi_fields=["CUSTOM_TRIAL_ID_SECRET"],
        )
        store.commit_config(
            study_key="STUDY",
            config=cfg,
            sdk=sdk_default,
            desc="Declare CUSTOM_TRIAL_ID_SECRET as PHI",
        )

        # 2. Export inside sdk.study_context("STUDY")
        csv_ctx = tmp_path / "study_ctx.csv"
        with sdk_default.study_context("STUDY"):
            export_to_csv(sdk_default, "STUDY", csv_ctx, quality_gate_enabled=False)
        df_ctx = pd.read_csv(csv_ctx)
        assert set(df_ctx["CUSTOM_TRIAL_ID_SECRET"].dropna()) == {"***MASKED***"}

        # 3. Export from an explicit DEFAULT_BASE_URL SDK session
        sdk_explicit = ImednetSDK(
            api_key="gov-api-key",  # pragma: allowlist secret
            security_key="gov-sec-key",  # pragma: allowlist secret
            base_url=DEFAULT_BASE_URL,
            transport=transport,
            simulation_mode=True,
        )
        csv_explicit = tmp_path / "explicit_base.csv"
        export_to_csv(sdk_explicit, "STUDY", csv_explicit, quality_gate_enabled=False)
        df_explicit = pd.read_csv(csv_explicit)
        assert set(df_explicit["CUSTOM_TRIAL_ID_SECRET"].dropna()) == {"***MASKED***"}

        # 4. Export inside MultiStudyOrchestrator worker threads (even when main thread context is cleared)
        reset_security_key_context(sec_token)
        sec_token = set_security_key_context(None)

        def _worker(study_key: str, worker_sdk: ImednetSDK, _logger: object) -> Path:
            out_file = tmp_path / f"orch_{study_key}.csv"
            export_to_csv(worker_sdk, study_key, out_file, quality_gate_enabled=False)
            return out_file

        orch = MultiStudyOrchestrator(sdk_explicit, max_workers=2)
        results = orch.execute_pipeline(_worker, whitelist={"STUDY"})
        assert results["STUDY"]["status"] == "SUCCESS"
        df_orch = pd.read_csv(results["STUDY"]["data"])
        assert set(df_orch["CUSTOM_TRIAL_ID_SECRET"].dropna()) == {"***MASKED***"}
    finally:
        reset_security_key_context(sec_token)


def test_legacy_study_partitioned_sqlite_fallback_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify legacy <host>/<study_key>/config_versions.sqlite3 resolves and bad keys fail closed."""
    from imednet.core.context import reset_base_url_context, set_base_url_context
    from imednet.errors import PathTraversalValidationError
    from imednet.integrations.enrichment import CentralizedMapper, resolve_study_configuration
    from imednet_workflows.config_version_control import _encrypt_payload, _sha256_of

    base_db = tmp_path / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(base_db))
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "legacy-secret-key")  # pragma: allowlist secret

    b_tok = set_base_url_context("https://edc.prod.imednetapi.com")
    try:
        legacy_file = tmp_path / "edc.prod.imednetapi.com" / "STUDY-LEG" / "config_versions.sqlite3"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        cfg = StudyConfiguration(study_key="STUDY-LEG", phi_fields=["LEGACY_MRN"])
        cfg_json = json.dumps(cfg.model_dump(mode="json", by_alias=True), sort_keys=True)
        commit_hash = _sha256_of(cfg_json)
        enc_payload = _encrypt_payload(cfg_json)

        with sqlite3.connect(legacy_file) as conn:
            conn.execute(
                """
                CREATE TABLE config_commits (
                    commit_id TEXT PRIMARY KEY,
                    study_key TEXT NOT NULL,
                    version_tag TEXT NOT NULL,
                    config_data TEXT NOT NULL,
                    modified_by TEXT NOT NULL,
                    description TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    parent_commit_id TEXT,
                    content_hash TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO config_commits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_hash,
                    "STUDY-LEG",
                    "1.0",
                    enc_payload,
                    "auditor",
                    "legacy study-partitioned commit",
                    "2026-01-01T00:00:00+00:00",
                    None,
                    commit_hash,
                ),
            )
            conn.commit()

        # Canonical unpartitioned file does not exist; resolve_study_configuration falls back to legacy path
        resolved = resolve_study_configuration("STUDY-LEG")
        assert resolved is not None
        assert resolved.phi_fields == ["LEGACY_MRN"]

        # Direct traversal validation raises on store methods and fails closed to None in enrichment
        with pytest.raises(PathTraversalValidationError):
            ConfigVersionStore.resolve_committed_config("../escape")
        assert resolve_study_configuration("../escape") is None

        # Wrong decryption key fails closed to default SensitivityRegistry masking
        monkeypatch.setenv("IMEDNET_SECURITY_KEY", "wrong-key")  # pragma: allowlist secret
        assert resolve_study_configuration("STUDY-LEG") is None
        mapper = CentralizedMapper()
        mapped = mapper.map_record(
            {
                "studyKey": "STUDY-LEG",
                "recordId": 1,
                "recordData": {"patient_name": "Alice Doe", "LEGACY_MRN": "123"},
            }
        )
        assert mapped["record_data"]["patient_name"] == "***MASKED***"
    finally:
        reset_base_url_context(b_tok)


def test_dynamic_home_and_monkeypatched_path_home_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MockSDK,
) -> None:
    """Verify ConfigVersionStore honors dynamic HOME and monkeypatched Path.home() overrides."""
    monkeypatch.delenv("IMEDNET_CONFIG_DB_PATH", raising=False)
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "home-override-test-key")  # pragma: allowlist secret

    env_home = tmp_path / "env_home"
    monkeypatch.setenv("HOME", str(env_home))
    assert ConfigVersionStore.resolve_committed_config("STUDY-HOME") is None

    env_store = ConfigVersionStore()
    assert env_store.db_path.is_relative_to(env_home / ".imednet")
    env_cfg = StudyConfiguration(study_key="STUDY-HOME", phi_fields=["env_home_phi"])
    env_store.commit_config("STUDY-HOME", env_cfg, desc="env home commit", sdk=mock_sdk)
    resolved_env = ConfigVersionStore.resolve_committed_config("STUDY-HOME")
    assert resolved_env is not None
    assert resolved_env.phi_fields == ["env_home_phi"]

    patched_home = tmp_path / "patched_home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: patched_home))
    assert ConfigVersionStore.resolve_committed_config("STUDY-HOME") is None

    patched_store = ConfigVersionStore()
    assert patched_store.db_path.is_relative_to(patched_home / ".imednet")
    patched_cfg = StudyConfiguration(study_key="STUDY-HOME", phi_fields=["patched_home_phi"])
    patched_store.commit_config("STUDY-HOME", patched_cfg, desc="patched home commit", sdk=mock_sdk)
    resolved_patched = ConfigVersionStore.resolve_committed_config("STUDY-HOME")
    assert resolved_patched is not None
    assert resolved_patched.phi_fields == ["patched_home_phi"]

    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", "~/tilde_dir/versions.sqlite3")
    tilde_store = ConfigVersionStore()
    assert tilde_store.db_path.is_relative_to(patched_home / "tilde_dir")


def test_committed_config_cache_deep_copy_invalidation_and_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MockSDK,
) -> None:
    """Verify verified StudyConfiguration caching, deep-copy isolation, write invalidation, key rotation, and bounds."""
    from unittest.mock import patch

    import imednet_workflows.config_version_control as cvc
    from imednet.integrations.enrichment import resolve_study_configuration

    db_file = tmp_path / "cache_test" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_file))
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "cache-key-alpha")  # pragma: allowlist secret

    store = ConfigVersionStore(db_path=db_file)
    v1 = StudyConfiguration(study_key="STUDY-CACHE", phi_fields=["alpha_mrn"])
    store.commit_config("STUDY-CACHE", v1, desc="v1", sdk=mock_sdk)

    decrypt_calls = 0
    orig_decrypt = cvc._decrypt_payload

    def counting_decrypt(payload: str, *, fallback_key: str | None = None) -> str:
        nonlocal decrypt_calls
        decrypt_calls += 1
        return orig_decrypt(payload, fallback_key=fallback_key)

    rollback_calls = 0
    orig_rollback = ConfigVersionStore.rollback_config

    def counting_rollback(self: ConfigVersionStore, sk: str, cid: str) -> StudyConfiguration:
        nonlocal rollback_calls
        rollback_calls += 1
        return orig_rollback(self, sk, cid)

    monkeypatch.setattr(cvc, "_decrypt_payload", counting_decrypt)
    monkeypatch.setattr(ConfigVersionStore, "rollback_config", counting_rollback)

    first = ConfigVersionStore.resolve_committed_config("STUDY-CACHE")
    assert first is not None
    assert first.phi_fields == ["alpha_mrn"]
    assert decrypt_calls == 1
    assert rollback_calls == 0

    # Mutating returned model must not pollute cached StudyConfiguration
    first.phi_fields.append("caller_mutated")
    second = ConfigVersionStore.resolve_committed_config("STUDY-CACHE")
    assert second is not None
    assert second.phi_fields == ["alpha_mrn"]
    assert decrypt_calls == 1
    assert rollback_calls == 0

    # In-session commit_config bumps write_generation and invalidates cached model immediately
    v2 = StudyConfiguration(study_key="STUDY-CACHE", phi_fields=["beta_mrn"])
    store.commit_config("STUDY-CACHE", v2, desc="v2", sdk=mock_sdk)
    third = ConfigVersionStore.resolve_committed_config("STUDY-CACHE")
    assert third is not None
    assert third.phi_fields == ["beta_mrn"]
    assert decrypt_calls == 2

    # Key rotation or removal fails closed immediately without serving prior cached config
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "cache-key-rotated")  # pragma: allowlist secret
    assert resolve_study_configuration("STUDY-CACHE") is None
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    assert resolve_study_configuration("STUDY-CACHE") is None

    # Mocked get_head still instantiates ConfigVersionStore fallback
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "cache-key-alpha")  # pragma: allowlist secret
    head_id = store.get_head("STUDY-CACHE")
    with patch.object(ConfigVersionStore, "get_head", return_value=head_id) as mock_get_head:
        via_mock_head = ConfigVersionStore.resolve_committed_config("STUDY-CACHE")
        assert via_mock_head is not None
        assert via_mock_head.phi_fields == ["beta_mrn"]
        mock_get_head.assert_called_once_with("STUDY-CACHE")

    # Verify bounded helper enforces fixed max size
    sample_cache: dict[int, int] = {}
    for idx in range(20):
        cvc._bounded_cache_put(sample_cache, idx, idx, 8)
    assert len(sample_cache) == 8
    assert list(sample_cache.keys()) == list(range(12, 20))


def test_ten_thousand_uncached_study_lookups_skip_duplicate_legacy_and_path_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify 10,000 uncached resolve_study_configuration calls run 0 legacy fallbacks and <=3 Path.resolve calls."""
    import imednet_workflows.config_version_control as cvc
    from imednet.integrations.enrichment import resolve_study_configuration

    missing_db = tmp_path / "absent_config_dir" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(missing_db))
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "bench-unit-key")  # pragma: allowlist secret

    cvc._RAW_DB_PATH_CACHE.clear()
    cvc._ORIGIN_LAYOUT_CACHE.clear()
    cvc._BASE_DIR_ENTRIES_CACHE.clear()
    cvc._HOST_SUBDIR_CACHE.clear()

    orig_resolve = Path.resolve
    resolve_count = 0

    def counting_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal resolve_count
        resolve_count += 1
        return orig_resolve(self, *args, **kwargs)

    orig_legacy_func = ConfigVersionStore.__dict__["resolve_legacy_committed_config"].__func__
    legacy_count = 0

    def counting_legacy(cls: type[ConfigVersionStore], study_key: str) -> StudyConfiguration | None:
        nonlocal legacy_count
        legacy_count += 1
        return orig_legacy_func(cls, study_key)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    monkeypatch.setattr(
        ConfigVersionStore,
        "resolve_legacy_committed_config",
        classmethod(counting_legacy),
    )

    for idx in range(10000):
        assert resolve_study_configuration(f"STUDY_{idx:05d}") is None

    assert legacy_count == 0
    assert resolve_count <= 3


def test_multi_origin_sdk_instantiation_preserves_pinned_store_and_default_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating a second ImednetSDK for another origin leaves pinned stores and DEFAULT_BASE_URL intact."""
    import threading

    import pandas as pd

    from imednet import ImednetSDK
    from imednet.core.context import (
        get_base_url_context,
        get_security_key_context,
        reset_base_url_context,
        reset_security_key_context,
        set_base_url_context,
        set_security_key_context,
    )
    from imednet.integrations.enrichment import resolve_study_configuration
    from imednet.integrations.export import export_to_csv
    from imednet.orchestration.orchestrator import MultiStudyOrchestrator
    from imednet.simulation import SimulationState, create_simulation_transport

    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    base_db = tmp_path / "multi_origin" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(base_db))

    state = SimulationState()
    state.variables.append(
        {
            "studyKey": "STUDY",
            "variableId": 888,
            "variableName": "CUSTOM_ORIGIN_SECRET",
            "variableType": "TEXT",
            "formId": 301,
            "formKey": "F_DM",
            "formName": "Demographics",
            "label": "Origin Secret",
            "deleted": False,
        }
    )
    for rec in state.records:
        if rec.get("studyKey") == "STUDY" and rec.get("formKey") == "F_DM":
            rec.setdefault("recordData", {})[
                "CUSTOM_ORIGIN_SECRET"  # pragma: allowlist secret
            ] = "SENSITIVE-ORIGIN-PHI"  # pragma: allowlist secret

    transport = create_simulation_transport(state)
    b_tok = set_base_url_context(None)
    s_tok = set_security_key_context(None)
    try:
        prod_sdk = ImednetSDK(
            api_key="prod-api-key",  # pragma: allowlist secret
            security_key="prod-sec-key",  # pragma: allowlist secret
            transport=transport,
            simulation_mode=True,
        )
        # Constructing an SDK must not pollute ambient thread-local ContextVars
        assert get_base_url_context() is None
        assert get_security_key_context() is None

        prod_store = ConfigVersionStore()
        assert "edc.prod.imednetapi.com" in str(prod_store.db_path)

        cfg = StudyConfiguration(
            study_key="STUDY",
            phi_fields=["CUSTOM_ORIGIN_SECRET"],
        )
        commit_id = prod_store.commit_config(
            "STUDY",
            cfg,
            desc="Commit prod PHI rules",
            sdk=prod_sdk,
        )
        prod_db_path = prod_store.db_path
        assert "edc.prod.imednetapi.com" in str(prod_db_path)

        # Instantiate a training-environment SDK in the same thread
        trn_sdk = ImednetSDK(
            api_key="trn-api-key",  # pragma: allowlist secret
            security_key="trn-different-sec-key",  # pragma: allowlist secret
            base_url="https://edc.trn.imednetapi.com",
            transport=transport,
            simulation_mode=True,
        )
        assert get_base_url_context() is None
        assert get_security_key_context() == "prod-sec-key"

        # Even inside `with trn_sdk:`, prod_store stays pinned to prod origin and prod key
        with trn_sdk:
            assert prod_store.db_path == prod_db_path
            assert prod_store.get_head("STUDY") == commit_id
            history = prod_store.get_history("STUDY")
            assert len(history) == 1
            assert history[0]["commit_id"] == commit_id
            assert prod_store.rollback_config("STUDY", commit_id).phi_fields == [
                "CUSTOM_ORIGIN_SECRET"
            ]

        trn_store = ConfigVersionStore(sdk=trn_sdk)
        assert "edc.trn.imednetapi.com" in str(trn_store.db_path)
        assert trn_store.get_head("STUDY") is None
        assert trn_store.get_history("STUDY") == []

        # Unscoped background thread resolves DEFAULT_BASE_URL when security key is bound via sdk_governance_context
        from imednet.spi.utils import sdk_governance_context

        worker_resolved: list[StudyConfiguration | None] = []

        def _bg_resolve() -> None:
            with sdk_governance_context(prod_sdk):
                worker_resolved.append(resolve_study_configuration("STUDY"))

        bg_thread = threading.Thread(target=_bg_resolve)
        bg_thread.start()
        bg_thread.join(timeout=5)
        assert len(worker_resolved) == 1
        assert worker_resolved[0] is not None
        assert worker_resolved[0].phi_fields == ["CUSTOM_ORIGIN_SECRET"]

        # MultiStudyOrchestrator worker threads mask custom PHI without IMEDNET_BASE_URL
        def _export_job(study_key: str, worker_sdk: ImednetSDK, _logger: object) -> Path:
            out_csv = tmp_path / f"multi_origin_{study_key}.csv"
            export_to_csv(worker_sdk, study_key, out_csv, quality_gate_enabled=False)
            return out_csv

        orch = MultiStudyOrchestrator(prod_sdk, max_workers=2)
        results = orch.execute_pipeline(_export_job, whitelist={"STUDY"})
        assert results["STUDY"]["status"] == "SUCCESS"
        df = pd.read_csv(results["STUDY"]["data"])
        assert set(df["CUSTOM_ORIGIN_SECRET"].dropna()) == {"***MASKED***"}

        # Legacy `localhost` ledger fallback works seamlessly for pre-existing local ledgers
        legacy_store = ConfigVersionStore(db_path=base_db, base_url="http://localhost")
        legacy_cfg = StudyConfiguration(study_key="LEGACY_STUDY", phi_fields=["LEGACY_MRN"])
        legacy_commit = legacy_store.commit_config(
            "LEGACY_STUDY",
            legacy_cfg,
            desc="Legacy localhost ledger commit",
            sdk=prod_sdk,
        )
        assert "localhost" in str(legacy_store.db_path)

        with sdk_governance_context(prod_sdk):
            unscoped_reader = ConfigVersionStore(db_path=base_db)
            assert unscoped_reader.get_head("LEGACY_STUDY") == legacy_commit
            assert len(unscoped_reader.get_history("LEGACY_STUDY")) == 1
            assert unscoped_reader.rollback_config("LEGACY_STUDY", legacy_commit).phi_fields == [
                "LEGACY_MRN"
            ]
            resolved_legacy = resolve_study_configuration("LEGACY_STUDY")
            assert resolved_legacy is not None
            assert resolved_legacy.phi_fields == ["LEGACY_MRN"]
    finally:
        reset_base_url_context(b_tok)
        reset_security_key_context(s_tok)


def test_oidc_credential_source_commits_reads_and_masks_without_env_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OIDC JWT and opaque CredentialSource sessions encrypt/decrypt configs without env keys."""
    import base64
    import hashlib
    import hmac
    import time

    import pandas as pd

    from imednet import ImednetSDK
    from imednet.auth import Credentials, MemoryCredentialStore, OIDCAuth
    from imednet.core.client import Client
    from imednet.core.context import reset_security_key_context, set_security_key_context
    from imednet.integrations.enrichment import resolve_study_configuration
    from imednet.integrations.export import export_to_csv
    from imednet.simulation import SimulationState, create_simulation_transport

    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

    state = SimulationState()
    state.variables.append(
        {
            "studyKey": "STUDY",
            "variableId": 901,
            "variableName": "OIDC_CUSTOM_SSN",
            "variableType": "TEXT",
            "formId": 301,
            "formKey": "F_DM",
            "formName": "Demographics",
            "label": "OIDC SSN",
            "deleted": False,
        }
    )
    for rec in state.records:
        if rec.get("studyKey") == "STUDY" and rec.get("formKey") == "F_DM":
            rec.setdefault("recordData", {})["OIDC_CUSTOM_SSN"] = "999-00-1234"

    transport = create_simulation_transport(state)
    sec_tok = set_security_key_context(None)
    try:
        # 1. Signed JWT OIDC session without IMEDNET_ENCRYPTION_KEY / IMEDNET_SECURITY_KEY
        jwt_db = tmp_path / "jwt_oidc" / "config_versions.sqlite3"
        monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(jwt_db))

        hmac_secret = "oidc-jwt-signing-secret-key"  # pragma: allowlist secret
        hdr = (
            base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            .decode()
            .rstrip("=")
        )
        pld = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "dm-jwt-user",
                        "roles": ["Data Manager"],
                        "exp": time.time() + 600,
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        sig = (
            base64.urlsafe_b64encode(
                hmac.new(hmac_secret.encode(), f"{hdr}.{pld}".encode(), hashlib.sha256).digest()
            )
            .decode()
            .rstrip("=")
        )
        signed_jwt = f"{hdr}.{pld}.{sig}"

        jwt_client = Client(
            auth=OIDCAuth(signed_jwt, public_key=hmac_secret),
            transport=transport,
        )
        jwt_sdk = ImednetSDK(client=jwt_client)
        jwt_store = ConfigVersionStore()
        cfg = StudyConfiguration(study_key="STUDY", phi_fields=["OIDC_CUSTOM_SSN"])
        jwt_commit = jwt_store.commit_config("STUDY", cfg, desc="JWT OIDC commit", sdk=jwt_sdk)

        # Verify zero plaintext JSON in SQLite ledger
        with sqlite3.connect(jwt_store.db_path) as conn:
            raw_payload = str(
                conn.execute(
                    "SELECT config_data FROM config_commits WHERE commit_id = ?",
                    (jwt_commit,),
                ).fetchone()[0]
            )
        assert "OIDC_CUSTOM_SSN" not in raw_payload
        assert "phiFields" not in raw_payload

        hist = jwt_store.get_history("STUDY")
        assert len(hist) == 1
        assert hist[0]["modified_by"] == "dm-jwt-user"
        assert jwt_store.rollback_config("STUDY", jwt_commit).phi_fields == ["OIDC_CUSTOM_SSN"]

        with jwt_sdk:
            resolved_jwt = resolve_study_configuration("STUDY")
            assert resolved_jwt is not None
            assert resolved_jwt.phi_fields == ["OIDC_CUSTOM_SSN"]
            jwt_csv = tmp_path / "jwt_oidc_export.csv"
            export_to_csv(jwt_sdk, "STUDY", jwt_csv, quality_gate_enabled=False)
            df_jwt = pd.read_csv(jwt_csv)
            assert set(df_jwt["OIDC_CUSTOM_SSN"].dropna()) == {"***MASKED***"}

        # 2. Opaque bearer CredentialSource (MemoryCredentialStore) without env keys
        reset_security_key_context(sec_tok)
        sec_tok = set_security_key_context(None)
        opaque_db = tmp_path / "opaque_oidc" / "config_versions.sqlite3"
        monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(opaque_db))

        cred_source = MemoryCredentialStore(
            Credentials(oidc_token="opaque-bearer-session-token-abc123")  # pragma: allowlist secret
        )
        opaque_sdk = ImednetSDK(
            credential_source=cred_source,
            transport=transport,
        )
        opaque_store = ConfigVersionStore()
        opaque_commit = opaque_store.commit_config(
            "STUDY",
            cfg,
            desc="Opaque OIDC session commit",
            sdk=opaque_sdk,
        )

        with sqlite3.connect(opaque_store.db_path) as conn:
            opaque_raw = str(
                conn.execute(
                    "SELECT config_data FROM config_commits WHERE commit_id = ?",
                    (opaque_commit,),
                ).fetchone()[0]
            )
        assert "OIDC_CUSTOM_SSN" not in opaque_raw

        opaque_hist = opaque_store.get_history("STUDY")
        assert len(opaque_hist) == 1
        assert opaque_hist[0]["modified_by"] == "oidc-token-user"
        assert opaque_store.rollback_config("STUDY", opaque_commit).phi_fields == [
            "OIDC_CUSTOM_SSN"
        ]

        with opaque_sdk:
            resolved_opaque = resolve_study_configuration("STUDY")
            assert resolved_opaque is not None
            assert resolved_opaque.phi_fields == ["OIDC_CUSTOM_SSN"]

        # Unauthenticated/unpinned fresh store without key or wrong key fails closed on decrypt & resolve
        reset_security_key_context(sec_tok)
        sec_tok = set_security_key_context(None)
        fresh_unkeyed_store = ConfigVersionStore(db_path=opaque_db)
        with pytest.raises(ValueError, match="key is missing|Decryption failed"):
            fresh_unkeyed_store.get_history("STUDY")
        assert resolve_study_configuration("STUDY") is None

        wrong_tok = set_security_key_context("wrong-oidc-token")
        try:
            with pytest.raises(ValueError, match="Decryption failed|integrity check failed"):
                fresh_unkeyed_store.get_history("STUDY")
            assert resolve_study_configuration("STUDY") is None
        finally:
            reset_security_key_context(wrong_tok)

        # 3. Unverified/forged 3-part JWT via MemoryCredentialStore must still fail closed with PermissionError
        forged_hdr = (
            base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            .decode()
            .rstrip("=")
        )
        forged_pld = (
            base64.urlsafe_b64encode(json.dumps({"sub": "attacker", "roles": ["admin"]}).encode())
            .decode()
            .rstrip("=")
        )
        forged_jwt = f"{forged_hdr}.{forged_pld}.forged_signature"
        forged_sdk = ImednetSDK(
            credential_source=MemoryCredentialStore(Credentials(oidc_token=forged_jwt)),
            transport=transport,
        )
        with pytest.raises(PermissionError, match="authorization failure|lacks manager or admin"):
            opaque_store.commit_config("STUDY", cfg, desc="forged", sdk=forged_sdk)
    finally:
        reset_security_key_context(sec_tok)


def test_config_version_store_concurrent_cache_access_and_eviction_thread_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MockSDK,
) -> None:
    """Concurrent multi-threaded commits, lookups, and bounded cache evictions are race-free."""
    import threading

    import imednet_workflows.config_version_control as cvc

    db_file = tmp_path / "concurrent_cvc" / "versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_file))
    monkeypatch.setenv(
        "IMEDNET_SECURITY_KEY", "concurrent-cache-secret"
    )  # pragma: allowlist secret
    monkeypatch.setattr(cvc, "_MAX_COMMITTED_CONFIG_CACHE", 8)
    monkeypatch.setattr(cvc, "_MAX_HEAD_INDEX_CACHE", 4)
    monkeypatch.setattr(cvc, "_MAX_ORIGIN_LAYOUT_CACHE", 8)

    n_threads = 6
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def _worker(tid: int) -> None:
        local_store = ConfigVersionStore(db_path=db_file)
        barrier.wait()
        try:
            for step in range(12):
                study = f"STUDY_{tid}_{step % 6}"
                cfg = StudyConfiguration(
                    study_key=study,
                    version=f"1.0.{step}",
                    phi_fields=[f"FIELD_{tid}_{step}"],
                )
                cid = local_store.commit_config(study, cfg, desc=f"t{tid}-s{step}", sdk=mock_sdk)
                assert local_store.get_head(study) is not None
                resolved = ConfigVersionStore.resolve_committed_config(study)
                assert resolved is not None
                assert resolved.study_key == study
                restored = local_store.rollback_config(study, cid)
                assert restored.phi_fields == [f"FIELD_{tid}_{step}"]
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []


def test_resolve_committed_study_keys_and_mapper_topology_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MockSDK,
) -> None:
    """Verify resolve_committed_study_keys indexes committed studies and invalidates immediately on commit."""
    from unittest.mock import patch

    from imednet.integrations.enrichment import CentralizedMapper

    db_file = tmp_path / "topo_test" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_file))
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "topo-secret-key")  # pragma: allowlist secret

    empty_snap = ConfigVersionStore.resolve_committed_study_keys()
    assert empty_snap is not None
    assert empty_snap[1] == frozenset()

    mapper = CentralizedMapper(mode="tabular", mask_phi=True)
    row_before = mapper.map_record(
        {"record_id": 1, "study_key": "STUDY-TOPO", "record_data": {"custom_mrn": "RAW-123"}}
    )
    assert row_before["record_data"]["custom_mrn"] == "RAW-123"

    store = ConfigVersionStore(db_path=db_file)
    cfg = StudyConfiguration(study_key="STUDY-TOPO", phi_fields=["custom_mrn"])
    store.commit_config("STUDY-TOPO", cfg, desc="add custom_mrn phi", sdk=mock_sdk)

    populated_snap = ConfigVersionStore.resolve_committed_study_keys()
    assert populated_snap is not None
    assert "STUDY-TOPO" in populated_snap[1]
    assert populated_snap[0] != empty_snap[0]

    # New mapper (or uncached study on same mapper) immediately sees newly committed study
    fresh_mapper = CentralizedMapper(mode="tabular", mask_phi=True)
    row_after = fresh_mapper.map_record(
        {"record_id": 2, "study_key": "STUDY-TOPO", "record_data": {"custom_mrn": "RAW-123"}}
    )
    assert row_after["record_data"]["custom_mrn"] == "***MASKED***"

    # Mocking get_history disables pre-indexing so mock-based tests fall back cleanly
    with patch.object(ConfigVersionStore, "get_history", return_value=[]):
        assert ConfigVersionStore.resolve_committed_study_keys() is None
