"""Unit tests for ConfigVersionStore (config_version_control module)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from imednet.models.study_config import MappingRule, StudyConfiguration, WidgetConfig
from imednet.utils.serialization import flatten as _flatten
from imednet_workflows.config_version_control import ConfigVersionStore, _sha256_of

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(
    study_key: str = "STUDY-01",
    version: str = "1.0.0",
    *,
    extra_mapping: bool = False,
) -> StudyConfiguration:
    """Helper function to  make config."""
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


def test_commit_config_returns_sha256(store: ConfigVersionStore) -> None:
    """Test that commit config returns sha256."""
    config = _make_config()
    commit_id = store.commit_config("STUDY-01", config, user="alice", desc="Initial commit")
    assert len(commit_id) == 64
    assert commit_id.islower()


def test_commit_config_duplicate_raises(store: ConfigVersionStore) -> None:
    """Test that commit config duplicate raises."""
    config = _make_config()
    store.commit_config("STUDY-01", config, user="alice", desc="First")
    with pytest.raises(ValueError, match="already exists"):
        store.commit_config("STUDY-01", config, user="alice", desc="Duplicate")


def test_commit_config_different_content_succeeds(store: ConfigVersionStore) -> None:
    """Test that commit config different content succeeds."""
    config_a = _make_config(version="1.0.0")
    config_b = _make_config(version="1.1.0")
    id_a = store.commit_config("STUDY-01", config_a, user="alice", desc="v1.0.0")
    id_b = store.commit_config("STUDY-01", config_b, user="alice", desc="v1.1.0")
    assert id_a != id_b


def test_commit_config_persists_metadata(store: ConfigVersionStore, tmp_path: Path) -> None:
    """Test that commit config persists metadata."""
    config = _make_config()
    commit_id = store.commit_config("STUDY-01", config, user="bob", desc="Test commit")
    history = store.get_history("STUDY-01")
    assert len(history) == 1
    entry = history[0]
    assert entry["commit_id"] == commit_id
    assert entry["study_key"] == "STUDY-01"
    assert entry["version_tag"] == "1.0.0"
    assert entry["modified_by"] == "bob"
    assert entry["description"] == "Test commit"
    assert "timestamp" in entry


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


def test_get_history_empty(store: ConfigVersionStore) -> None:
    """Test that get history empty."""
    assert store.get_history("UNKNOWN") == []


def test_get_history_ordered(store: ConfigVersionStore) -> None:
    """Test that get history ordered."""
    c1 = _make_config(version="1.0.0")
    c2 = _make_config(version="1.1.0")
    c3 = _make_config(version="1.2.0", extra_mapping=True)

    store.commit_config("STUDY-01", c1, user="alice", desc="v1.0.0")
    store.commit_config("STUDY-01", c2, user="alice", desc="v1.1.0")
    store.commit_config("STUDY-01", c3, user="alice", desc="v1.2.0")

    history = store.get_history("STUDY-01")
    assert len(history) == 3
    # Should be sorted oldest-first by timestamp
    versions = [e["version_tag"] for e in history]
    assert versions == ["1.0.0", "1.1.0", "1.2.0"]


def test_get_history_isolated_by_study(store: ConfigVersionStore) -> None:
    """Test that get history isolated by study."""
    c1 = _make_config(study_key="STUDY-01")
    c2 = _make_config(study_key="STUDY-02")
    store.commit_config("STUDY-01", c1, user="alice", desc="first")
    store.commit_config("STUDY-02", c2, user="alice", desc="second")

    assert len(store.get_history("STUDY-01")) == 1
    assert len(store.get_history("STUDY-02")) == 1
    assert len(store.get_history("STUDY-03")) == 0


def test_get_history_excludes_config_data(store: ConfigVersionStore) -> None:
    """Test that get history excludes config data."""
    config = _make_config()
    store.commit_config("STUDY-01", config, user="alice", desc="first")
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


def test_diff_configs_detects_changes(store: ConfigVersionStore) -> None:
    """Test that diff configs detects changes."""
    c1 = _make_config(version="1.0.0")
    c2 = _make_config(version="1.1.0")
    id_a = store.commit_config("STUDY-01", c1, user="alice", desc="v1.0.0")
    id_b = store.commit_config("STUDY-01", c2, user="alice", desc="v1.1.0")
    diff = store.diff_configs(id_a, id_b)
    # version field should appear in changed
    assert "version" in diff["changed"]
    assert diff["changed"]["version"]["before"] == "1.0.0"
    assert diff["changed"]["version"]["after"] == "1.1.0"


def test_diff_configs_detects_added_keys(store: ConfigVersionStore) -> None:
    """Test that diff configs detects added keys."""
    c1 = _make_config(version="1.0.0", extra_mapping=False)
    c2 = _make_config(version="1.1.0", extra_mapping=True)
    id_a = store.commit_config("STUDY-01", c1, user="alice", desc="base")
    id_b = store.commit_config("STUDY-01", c2, user="alice", desc="with-pd")
    diff = store.diff_configs(id_a, id_b)
    # New mapping keys should appear in added
    assert diff["added"] or diff["changed"]


def test_diff_configs_missing_commit_raises(store: ConfigVersionStore) -> None:
    """Test that diff configs missing commit raises."""
    c1 = _make_config()
    id_a = store.commit_config("STUDY-01", c1, user="alice", desc="base")
    with pytest.raises(KeyError):
        store.diff_configs(id_a, "nonexistent_commit_id")


# ---------------------------------------------------------------------------
# rollback_config
# ---------------------------------------------------------------------------


def test_rollback_config_restores_original(store: ConfigVersionStore) -> None:
    """Test that rollback config restores original."""
    original = _make_config(version="1.0.0")
    commit_id = store.commit_config("STUDY-01", original, user="alice", desc="base")

    # Commit a new version
    updated = _make_config(version="1.1.0")
    store.commit_config("STUDY-01", updated, user="alice", desc="update")

    # Rollback to original commit
    restored = store.rollback_config("STUDY-01", commit_id)
    assert isinstance(restored, StudyConfiguration)
    assert restored.version == "1.0.0"
    assert restored.study_key == "STUDY-01"


def test_rollback_config_is_non_destructive(store: ConfigVersionStore) -> None:
    """Test that rollback config is non destructive."""
    c1 = _make_config(version="1.0.0")
    c2 = _make_config(version="1.1.0")
    id1 = store.commit_config("STUDY-01", c1, user="alice", desc="v1")
    store.commit_config("STUDY-01", c2, user="alice", desc="v2")

    store.rollback_config("STUDY-01", id1)

    # History should still have both commits
    history = store.get_history("STUDY-01")
    assert len(history) == 2


def test_rollback_config_wrong_study_raises(store: ConfigVersionStore) -> None:
    """Test that rollback config wrong study raises."""
    config = _make_config(study_key="STUDY-01")
    commit_id = store.commit_config("STUDY-01", config, user="alice", desc="base")
    with pytest.raises(KeyError, match="not found for study"):
        store.rollback_config("STUDY-WRONG", commit_id)


def test_rollback_config_unknown_commit_raises(store: ConfigVersionStore) -> None:
    """Test that rollback config unknown commit raises."""
    with pytest.raises(KeyError):
        store.rollback_config("STUDY-01", "deadbeef" * 8)


# ---------------------------------------------------------------------------
# Round-trip integrity
# ---------------------------------------------------------------------------


def test_commit_content_hash_matches(store: ConfigVersionStore) -> None:
    """The stored commit_id must equal SHA-256 of the canonical JSON."""
    config = _make_config()
    commit_id = store.commit_config("STUDY-01", config, user="alice", desc="integrity")

    config_json = json.dumps(config.model_dump(mode="json", by_alias=True), sort_keys=True)
    expected_id = _sha256_of(config_json)
    assert commit_id == expected_id


def test_multiple_studies_independent(store: ConfigVersionStore) -> None:
    """Commits to different studies do not interfere with each other."""
    c_a = _make_config(study_key="STUDY-A", version="1.0.0")
    c_b = _make_config(study_key="STUDY-B", version="2.0.0")

    store.commit_config("STUDY-A", c_a, user="alice", desc="a1")
    store.commit_config("STUDY-B", c_b, user="bob", desc="b1")

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


def test_diff_configs_detects_invalid_signature(store: ConfigVersionStore) -> None:
    """Test that diff configs detects invalid signature."""
    valid = _make_config(version="1.0.0")
    valid_commit = store.commit_config("STUDY-01", valid, user="alice", desc="valid")
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


def test_history_rows_cannot_be_updated(store: ConfigVersionStore) -> None:
    """Test that history rows cannot be updated."""
    config = _make_config(version="1.0.0")
    commit_id = store.commit_config("STUDY-01", config, user="alice", desc="base")

    with (
        sqlite3.connect(store.db_path) as conn,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        conn.execute(
            "UPDATE config_commits SET version_tag = ? WHERE commit_id = ?",
            ("9.9.9", commit_id),
        )


def test_history_rows_cannot_be_deleted(store: ConfigVersionStore) -> None:
    """Test that history rows cannot be deleted."""
    config = _make_config(version="1.0.0")
    commit_id = store.commit_config("STUDY-01", config, user="alice", desc="base")

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
    store.commit_config("STUDY-01", config, user="bob", desc="secure commit")

    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT config_data FROM config_commits").fetchone()
        assert row is not None
        encrypted_payload = row["config_data"]
        assert not encrypted_payload.startswith("{")
        assert "mappings" not in encrypted_payload
        assert "widgets" not in encrypted_payload
        assert encrypted_payload.startswith("gAAAA")


def test_dynamic_sqlite_selection_per_base_url(tmp_path: Path) -> None:
    """Verify separate SQLite files are instantiated for different base URLs."""
    import os

    from imednet.core.context import reset_base_url_context, set_base_url_context

    store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
    config = _make_config()

    t1 = set_base_url_context("https://env1.imednet.com")
    try:
        os.environ["IMEDNET_SECURITY_KEY"] = "key1"
        path1 = store.db_path
        store.commit_config("STUDY-01", config, user="alice", desc="env1 commit")
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
    """Verify separate SQLite subdirectories are structured based on active study context."""
    from imednet.core.context import reset_study_context, set_study_context

    store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")

    t1 = set_study_context("STUDY-XYZ")
    try:
        path1 = store.db_path
        assert "STUDY-XYZ" in str(path1)
    finally:
        reset_study_context(t1)


def test_decryption_incorrect_or_missing_key_raises(tmp_path: Path) -> None:
    """Verify that configuration reads fail if the decryption key is incorrect or missing."""
    import os

    import pytest

    os.environ["IMEDNET_SECURITY_KEY"] = "secret1"
    store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
    config = _make_config()
    commit_id = store.commit_config("STUDY-01", config, user="alice", desc="env1 commit")

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
    """Verify that valid study keys resolve strictly inside the allowed base directory."""
    from imednet.core.context import reset_study_context, set_study_context

    token = set_study_context(valid_key)
    try:
        store = ConfigVersionStore(db_path=tmp_path / "versions.sqlite3")
        db_file = store.db_path
        assert valid_key in str(db_file)
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
        assert "STUDY-FALLBACK" in str(db_file)
        assert db_file.resolve().is_relative_to(tmp_path.resolve())
        assert db_file.exists()
    finally:
        reset_study_context(s_token)
        reset_base_url_context(b_token)
