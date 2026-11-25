"""Unit tests for Airflow optimistic Variable CAS, wildcard escaping, and target stream naming."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from apache_airflow_providers_imednet.operators import _stream_naming
from imednet_workflows import _airflow_cas
from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    InvalidStateTransitionError,
)


def _install_fake_sdk_variable(monkeypatch: pytest.MonkeyPatch, var_cls: type[Any]) -> None:
    airflow_mod = ModuleType("airflow")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.Variable = var_cls  # type: ignore[attr-defined]
    airflow_mod.sdk = sdk_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
    monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time", raising=False)
    monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time.task_runner", raising=False)


def test_airflow_sdk_concurrent_transaction_lease_token_and_monotonic_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task SDK AirflowStateProvider.transaction verifies lease_token and enforces monotonic watermarks."""
    store: dict[str, str] = {}
    interject_competing_writer = {"enabled": False}

    class RacingSdkVariable:
        @staticmethod
        def get(key: str, default: Any = None, deserialize_json: bool = False) -> Any:
            if key not in store:
                return default
            raw = store[key]
            return json.loads(raw) if deserialize_json else raw

        @staticmethod
        def set(
            key: str,
            value: Any,
            description: str | None = None,
            serialize_json: bool = False,
        ) -> None:
            _ = description
            store[key] = json.dumps(value) if serialize_json else str(value)
            if interject_competing_writer["enabled"]:
                interject_competing_writer["enabled"] = False
                hijacked = json.loads(store[key])
                hijacked["streams"]["records"]["lease_token"] = "competing-worker-token"
                store[key] = json.dumps(hijacked)

        @staticmethod
        def delete(key: str) -> None:
            store.pop(key, None)

        @staticmethod
        def keys(prefix: str | None = None) -> list[str]:
            if prefix is None:
                return sorted(store)
            return sorted(k for k in store if k.startswith(prefix))

    _install_fake_sdk_variable(monkeypatch, RacingSdkVariable)

    provider = AirflowStateProvider()
    t1 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)

    # 1. Immediate lease_token overwrite race on acquisition raises InvalidStateTransitionError
    interject_competing_writer["enabled"] = True
    with pytest.raises(InvalidStateTransitionError, match="lease token mismatch"):
        with provider.transaction("STUDY_RACE", "records"):
            pass

    # Clear hijacked state and commit t2 cleanly
    store.clear()
    with provider.transaction("STUDY_RACE", "records") as tx_init:
        # Overlapping second caller while RUNNING raises InvalidStateTransitionError
        with pytest.raises(InvalidStateTransitionError):
            with provider.transaction("STUDY_RACE", "records"):
                pass
        tx_init["new_timestamp"] = t2
        tx_init["records_processed"] = 10

    assert provider.get_last_timestamp("STUDY_RACE", "records") == t2

    # 2. Monotonic watermark enforcement: attempting to commit older t1 < t2 keeps t2
    with provider.transaction("STUDY_RACE", "records") as tx_older:
        assert tx_older["last_timestamp"] == t2
        tx_older["new_timestamp"] = t1
        tx_older["records_processed"] = 2

    assert provider.get_last_timestamp("STUDY_RACE", "records") == t2
    provider.set_last_timestamp("STUDY_RACE", "records", t1, force=False)
    assert provider.get_last_timestamp("STUDY_RACE", "records") == t2

    # 3. Pre-commit lease_token theft during yield raises InvalidStateTransitionError without regressing t2
    study_var_key = provider._get_study_variable_key("STUDY_RACE")
    with pytest.raises(InvalidStateTransitionError, match="lease token mismatch"):
        with provider.transaction("STUDY_RACE", "records") as tx_stolen:
            tx_stolen["new_timestamp"] = t1
            stolen_doc = json.loads(store[study_var_key])
            stolen_doc["streams"]["records"]["lease_token"] = "other-worker-lease"
            store[study_var_key] = json.dumps(stolen_doc)

    recovered_doc = json.loads(store[study_var_key])
    assert recovered_doc["streams"]["records"]["last_timestamp"] == t2.isoformat()

    # 4. Deleted checkpoint during yield raises missing checkpoint lease error
    provider.set_last_timestamp("STUDY_RACE", "records", t2, force=True)
    with pytest.raises(InvalidStateTransitionError, match="missing checkpoint lease"):
        with provider.transaction("STUDY_RACE", "records"):
            store.pop(study_var_key, None)


def test_airflow_sdk_wildcard_prefix_and_exact_study_delete_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AirflowStateProvider escapes wildcards and verifies exact study_key matches in SDK mode."""
    store: dict[str, str] = {}

    class UnescapedLikeSdkVariable:
        """Simulates a SQL LIKE backend where '_' and '%' act as wildcards if unguarded."""

        @staticmethod
        def get(key: str, default: Any = None, deserialize_json: bool = False) -> Any:
            if key not in store:
                return default
            raw = store[key]
            return json.loads(raw) if deserialize_json else raw

        @staticmethod
        def set(
            key: str,
            value: Any,
            description: str | None = None,
            serialize_json: bool = False,
        ) -> None:
            _ = description
            store[key] = json.dumps(value) if serialize_json else str(value)

        @staticmethod
        def delete(key: str) -> None:
            store.pop(key, None)

        @staticmethod
        def keys(prefix: str | None = None) -> list[str]:
            if prefix is None:
                return sorted(store)
            glob_pat = prefix.replace("_", "?").replace("%", "*") + "*"
            return sorted(k for k in store if fnmatch.fnmatchcase(k, glob_pat))

    _install_fake_sdk_variable(monkeypatch, UnescapedLikeSdkVariable)

    provider = AirflowStateProvider()
    ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    provider.set_last_timestamp("STUDY_1", "records", ts)
    provider.set_last_timestamp("STUDYA1", "records", ts)
    provider.set_last_timestamp("STUDY%1", "records", ts)
    # Also seed raw unencoded legacy key with '%'
    store["imednet_state:STUDY%1:legacy_stream"] = json.dumps(
        {
            "study_key": "STUDY%1",
            "stream_name": "legacy_stream",
            "last_timestamp": ts.isoformat(),
            "status": "success",
        }
    )

    assert provider._list_variable_keys("imednet_state:STUDY_1") == ["imednet_state:STUDY_1"]
    assert provider.delete_entry("STUDY_1") is True
    assert provider.get_last_timestamp("STUDY_1", "records") is None
    assert provider.get_last_timestamp("STUDYA1", "records") == ts
    assert provider.get_last_timestamp("STUDY%1", "records") == ts
    assert "imednet_state:STUDY%1:legacy_stream" in store

    assert provider.delete_entry("STUDY%1") is True
    assert provider.get_last_timestamp("STUDY%1", "records") is None
    assert "imednet_state:STUDY%1:legacy_stream" not in store
    assert provider.get_last_timestamp("STUDYA1", "records") == ts


def test_airflow_cas_edge_cases_lock_retries_and_active_lease_delete_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _airflow_cas helpers handle backslash prefixes and active-lease delete guards."""
    assert _airflow_cas.escape_like_prefix(r"pfx\a_b%c") == r"pfx\\a\_b\%c"

    store: dict[str, str] = {r"imednet\custom:S1": json.dumps({"study_key": "S1", "streams": {}})}
    seen_prefixes: list[str | None] = []

    class BackslashSdkVariable:
        @staticmethod
        def get(key: str, default: Any = None, deserialize_json: bool = False) -> Any:
            if key not in store:
                return default
            raw = store[key]
            return json.loads(raw) if deserialize_json else raw

        @staticmethod
        def set(
            key: str,
            value: Any,
            description: str | None = None,
            serialize_json: bool = False,
        ) -> None:
            _ = description
            store[key] = json.dumps(value) if serialize_json else str(value)

        @staticmethod
        def delete(key: str) -> None:
            store.pop(key, None)

        @staticmethod
        def keys(prefix: str | None = None) -> list[str]:
            seen_prefixes.append(prefix)
            return sorted(store)

    _install_fake_sdk_variable(monkeypatch, BackslashSdkVariable)
    bs_provider = AirflowStateProvider(variable_prefix=r"imednet\custom")
    assert _airflow_cas.list_airflow_variable_keys(bs_provider, r"imednet\custom:S1") == [
        r"imednet\custom:S1"
    ]
    assert seen_prefixes == ["imednet"]

    # Active lease guard on unforced delete_entry(study_key, stream_name) vs force=True
    with (
        pytest.raises(InvalidStateTransitionError, match="missing checkpoint lease"),
        bs_provider.transaction("S1", "dm"),
    ):
        with pytest.raises(InvalidStateTransitionError, match="active RUNNING lease"):
            bs_provider.delete_entry("S1", "dm")
        with pytest.raises(InvalidStateTransitionError, match="active RUNNING lease"):
            bs_provider.delete_entry("S1")
        assert bs_provider.delete_entry("S1", "dm", force=True) is True


def test_airflow_cas_orm_retry_exhaustion_and_sqlite_locked_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify try_commit_orm_cas retries SQLite 'database is locked' and cas_apply_orm bounds retries."""
    from sqlalchemy import Column, String, Text, create_engine
    from sqlalchemy.exc import IntegrityError, OperationalError
    from sqlalchemy.orm import DeclarativeBase, configure_mappers, sessionmaker

    engine = create_engine("sqlite:///:memory:")

    class _Base(DeclarativeBase):
        pass

    class _FakeModelVar(_Base):
        __tablename__ = "variable"
        key = Column(String(250), primary_key=True)
        val = Column(Text, nullable=True)

    _Base.metadata.create_all(engine)
    configure_mappers()
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    attempts = {"count": 0, "mode": "locked_once"}

    @contextlib.contextmanager
    def _fake_create_session() -> Generator[Any, None, None]:
        attempts["count"] += 1
        if attempts["mode"] == "locked_once" and attempts["count"] == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        if attempts["mode"] == "always_integrity":
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        with sess_factory() as sess, sess.begin():
            yield sess

    af_mod = ModuleType("airflow")
    models_mod = ModuleType("airflow.models")
    var_mod = ModuleType("airflow.models.variable")
    var_mod.Variable = _FakeModelVar  # type: ignore[attr-defined]
    utils_mod = ModuleType("airflow.utils")
    sess_mod = ModuleType("airflow.utils.session")
    sess_mod.create_session = _fake_create_session  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "airflow", af_mod)
    monkeypatch.setitem(sys.modules, "airflow.models", models_mod)
    monkeypatch.setitem(sys.modules, "airflow.models.variable", var_mod)
    monkeypatch.setitem(sys.modules, "airflow.utils", utils_mod)
    monkeypatch.setitem(sys.modules, "airflow.utils.session", sess_mod)
    monkeypatch.delitem(sys.modules, "airflow.sdk", raising=False)
    monkeypatch.setattr(_airflow_cas.time, "sleep", lambda _s: None)

    provider = AirflowStateProvider()
    monkeypatch.setattr(provider, "_airflow_available", lambda: True)
    monkeypatch.setattr(provider, "_has_active_task_sdk_comms", lambda: False)
    monkeypatch.setattr(provider, "_ensure_orm_session", lambda: None)

    @contextlib.contextmanager
    def _read_sess() -> Generator[Any, None, None]:
        with sess_factory() as sess:
            yield sess

    monkeypatch.setattr(provider, "_open_orm_read_session", _read_sess)

    # 1. OperationalError('database is locked') retries once and commits cleanly
    snap0 = _airflow_cas.load_orm_cas_snapshot(provider, "ST_LOCK")
    ok, snap1 = _airflow_cas.try_commit_orm_cas(
        provider,
        "ST_LOCK",
        {"records": {"status": "success"}},
        snap0,
    )
    assert ok is True and snap1 is not None
    assert attempts["count"] == 2

    # 2. IntegrityError returns (False, None) and cas_apply_orm raises after max_retries
    attempts["mode"] = "always_integrity"
    with pytest.raises(InvalidStateTransitionError, match="after 3 CAS retries"):
        _airflow_cas.cas_apply_orm(
            provider,
            "ST_CONFLICT",
            lambda _study, streams: {**streams, "records": {"status": "success"}},
            max_retries=3,
        )


def test_stream_naming_long_segment_truncation_dag_resolution_and_minimal_provider_seeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _stream_naming SHA-256 truncation, DAG ID fallbacks, target scoping, and minimal provider seeding."""
    # 1. Long segment > 80 chars truncates deterministically with sha256 suffix
    long_raw = "warehouse_schema_" + ("very_long_clinical_table_segment_" * 4)
    sanitized_a = _stream_naming.sanitize_stream_segment(long_raw)
    sanitized_b = _stream_naming.sanitize_stream_segment(long_raw)
    assert sanitized_a == sanitized_b
    assert len(sanitized_a) <= 80
    assert _stream_naming.sanitize_stream_segment("___") == ""

    # 2. DAG ID priority across ti, dag, dag_id, operator.dag_id, and AIRFLOW_CTX_DAG_ID
    op_stub = SimpleNamespace(task_id="export_t", dag_id="op_attr_dag", output_path=None)
    assert (
        _stream_naming.resolve_operator_dag_id(
            op_stub, {"ti": SimpleNamespace(dag_id="ti_dag"), "dag_id": "ctx_dag"}
        )
        == "ti_dag"
    )
    assert (
        _stream_naming.resolve_operator_dag_id(
            op_stub, {"dag": SimpleNamespace(dag_id="dag_obj_id")}
        )
        == "dag_obj_id"
    )
    assert _stream_naming.resolve_operator_dag_id(op_stub, {}) == "op_attr_dag"
    monkeypatch.setenv("AIRFLOW_CTX_DAG_ID", "env_fallback_dag")
    assert (
        _stream_naming.resolve_operator_dag_id(SimpleNamespace(task_id="t", dag_id=""), {})
        == "env_fallback_dag"
    )

    # 3. Target identifier resolution for by-form destinations and external sinks
    by_form_op = SimpleNamespace(
        task_id="export_t", dag_id="clin_dag", output_path="sqlite:////tmp/forms.db"
    )
    assert "sqlite" in _stream_naming.resolve_export_target_identifier(
        by_form_op,
        "sql_by_form",
        runtime_kwargs={},
        conn_settings={},
    )
    sf_stream = _stream_naming.build_default_airflow_stream_name(
        op_stub,
        "snowflake",
        sink_prefix=True,
        context={"dag_id": "clin_dag"},
        runtime_kwargs={"schema": "RAW", "table": "AE"},
        conn_settings={"database": "CLIN"},
    )
    assert sf_stream.startswith("airflow_snowflake_clin_dag_export_t_")
    assert all(seg in sf_stream for seg in ("CLIN", "RAW", "AE"))

    # 4. Minimal custom StateProvider without _load_study_state or read_state
    seeded: dict[tuple[str, str], datetime] = {
        ("STUDY_MIN", "csv_export_t"): datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    }

    class _MinimalProvider:
        def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
            return seeded.get((study_key, stream_name))

        def set_last_timestamp(
            self,
            study_key: str,
            stream_name: str,
            timestamp: datetime | None,
            **kwargs: Any,
        ) -> None:
            _ = kwargs
            if timestamp is not None:
                seeded[(study_key, stream_name)] = timestamp

    min_prov = _MinimalProvider()
    min_op = SimpleNamespace(
        task_id="export_t",
        dag_id="clin_dag",
        study_key="STUDY_MIN",
        output_path="/tmp/out.csv",
    )
    _stream_naming.seed_pre_upgrade_watermark(
        min_op,
        dest="csv",
        scoped_stream="csv_clin_dag_export_t_tmp_out_csv",
        state_provider=min_prov,
        ledger_path=None,
        default_provider_name="airflow",
    )
    assert seeded[("STUDY_MIN", "csv_clin_dag_export_t_tmp_out_csv")] == datetime(
        2026, 5, 1, 12, 0, tzinfo=UTC
    )
