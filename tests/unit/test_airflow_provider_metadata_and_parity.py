"""Unit and Airflow 3.x compatibility tests for provider metadata, triggers, lineage, and XCom state."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet import AsyncImednetSDK
from imednet.spi.models import JobStatus
from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True, scope="module")
def _cleanup_airflow_provider_modules() -> Any:
    """Unload provider/Airflow modules on teardown so mock-based test suites stay hermetic."""
    yield
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow")):
            sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# 1. Provider metadata & extras discovery
# ---------------------------------------------------------------------------


def test_provider_pyproject_declares_entry_point_and_extras() -> None:
    """Provider pyproject.toml registers apache_airflow_provider and expected extras."""
    from apache_airflow_providers_imednet import get_provider_info

    pyproject_path = ROOT / "packages" / "providers-airflow" / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text())
    project = data["project"]

    entry_points = project["entry-points"]["apache_airflow_provider"]
    assert entry_points == {
        "provider_info": "apache_airflow_providers_imednet.get_provider_info:get_provider_info"
    }

    extras = project["optional-dependencies"]
    assert {"openlineage", "sinks", "workflows", "amazon", "all"} <= set(extras)

    eps = [
        ep
        for ep in importlib.metadata.entry_points(group="apache_airflow_provider")
        if ep.value == "apache_airflow_providers_imednet.get_provider_info:get_provider_info"
    ]
    assert len(eps) == 1
    assert eps[0].load() is get_provider_info


def test_get_provider_info_structure_and_airflow_discovery() -> None:
    """ProvidersManager discovers imednet provider, hook, widgets, and UI field behaviours."""
    from airflow.providers.imednet.hooks import ImednetHook as BridgeHook
    from airflow.providers.imednet.triggers import ImednetJobTrigger as BridgeTrigger

    from apache_airflow_providers_imednet import ImednetHook, ImednetJobTrigger, get_provider_info

    assert BridgeHook is ImednetHook
    assert BridgeTrigger is ImednetJobTrigger

    info = get_provider_info()
    assert info["package-name"] == "apache-airflow-providers-imednet"
    assert info["name"] == "iMednet"
    assert info["triggers"] == [
        {
            "integration-name": "iMednet",
            "python-modules": ["airflow.providers.imednet.triggers"],
        }
    ]
    conn_types = info["connection-types"]
    assert len(conn_types) == 1
    imednet_conn = conn_types[0]
    assert imednet_conn["connection-type"] == "imednet"
    assert imednet_conn["hook-class-name"] == "airflow.providers.imednet.hooks.ImednetHook"
    assert imednet_conn["conn-fields"]["oidc_token"]["schema"]["format"] == "password"
    assert imednet_conn["ui-field-behaviour"]["relabeling"] == {
        "login": "API Key",
        "password": "Security Key",  # pragma: allowlist secret
    }

    airflow_pm = pytest.importorskip("airflow.providers_manager")
    pm = airflow_pm.ProvidersManager()
    assert "apache-airflow-providers-imednet" in pm.providers
    try:
        from airflow.sdk.providers_manager_runtime import ProvidersManagerTaskRuntime

        hook_info = ProvidersManagerTaskRuntime().hooks.get("imednet")
    except ImportError:
        hook_info = pm.hooks.get("imednet")
    assert hook_info is not None
    assert hook_info.hook_class_name == "airflow.providers.imednet.hooks.ImednetHook"
    assert "extra__imednet__base_url" in pm.connection_form_widgets
    assert pm.connection_form_widgets["extra__imednet__oidc_token"].is_sensitive is True
    assert pm.field_behaviours["imednet"]["relabeling"]["login"] == "API Key"


# ---------------------------------------------------------------------------
# 2. ImednetHook connection attributes, test_connection, and get_async_conn
# ---------------------------------------------------------------------------


def test_imednet_hook_metadata_and_prefixed_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """ImednetHook exposes Airflow metadata attributes and resolves UI-prefixed extras."""
    from apache_airflow_providers_imednet import ImednetHook

    assert ImednetHook.conn_type == "imednet"
    assert ImednetHook.conn_name_attr == "imednet_conn_id"
    assert ImednetHook.default_conn_name == "imednet_default"
    assert ImednetHook.hook_name == "iMednet"
    ui_behaviour = ImednetHook.get_ui_field_behaviour()
    assert ui_behaviour["relabeling"] == {
        "login": "API Key",
        "password": "Security Key",  # pragma: allowlist secret
    }

    fake_conn = SimpleNamespace(
        login="ui-api-key",  # pragma: allowlist secret
        password="ui-sec-key",  # pragma: allowlist secret
        host=None,
        extra_dejson={
            "extra__imednet__base_url": "https://sandbox.imednet.example.com",
            "extra__imednet__timeout": "45",
            "extra__imednet__strict_mode": "true",
            "extra__imednet__simulation_mode": "false",
        },
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_conn),
    )

    hook = ImednetHook("custom_imednet")
    assert hook.get_base_url() == "https://sandbox.imednet.example.com"
    sdk = hook.get_conn()
    try:
        assert sdk.config.base_url == "https://sandbox.imednet.example.com"
        assert sdk.config.timeout == 45.0
        assert sdk.config.strict_mode is True
    finally:
        sdk.close()

    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: (_ for _ in ()).throw(RuntimeError("no airflow db"))),
    )
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    assert ImednetHook("missing").get_base_url() == "https://edc.prod.imednetapi.com"


def test_imednet_hook_test_connection_success_and_redacted_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """test_connection validates study listing and redacts secrets when errors occur."""
    from apache_airflow_providers_imednet import ImednetHook

    fake_conn = SimpleNamespace(
        login="my-secret-api-key-123",  # pragma: allowlist secret
        password="my-secret-sec-key-456",  # pragma: allowlist secret
        extra_dejson={"oidc_token": "oidc-tok-789"},  # pragma: allowlist secret
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_conn),
    )

    mock_sdk = MagicMock()
    mock_sdk.studies.list.return_value = iter([{"studyKey": "STUDY_1"}])
    monkeypatch.setattr(
        "apache_airflow_providers_imednet.hooks.ImednetSDK",
        MagicMock(return_value=mock_sdk),
    )

    hook = ImednetHook("imednet_default")
    ok, msg = hook.test_connection()
    assert ok is True
    assert msg == "Connection successfully tested"
    mock_sdk.close.assert_called_once()

    failing_sdk = MagicMock()
    failing_sdk.studies.list.side_effect = RuntimeError(
        "Auth failed for api_key=my-secret-api-key-123 "  # pragma: allowlist secret
        "and raw my-secret-sec-key-456 Bearer oidc-tok-789"  # pragma: allowlist secret
    )
    monkeypatch.setattr(
        "apache_airflow_providers_imednet.hooks.ImednetSDK",
        MagicMock(return_value=failing_sdk),
    )

    ok_fail, err_msg = hook.test_connection()
    assert ok_fail is False
    assert "my-secret-api-key-123" not in err_msg
    assert "my-secret-sec-key-456" not in err_msg
    assert "oidc-tok-789" not in err_msg
    assert "***" in err_msg
    failing_sdk.close.assert_called_once()


def test_imednet_hook_get_async_conn(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_async_conn resolves credentials asynchronously and returns AsyncImednetSDK."""
    from apache_airflow_providers_imednet import ImednetHook

    fake_conn = SimpleNamespace(
        login="async-api-key",  # pragma: allowlist secret
        password="async-sec-key",  # pragma: allowlist secret
        extra_dejson={"base_url": "https://async.imednet.example.com"},
    )
    aget_mock = AsyncMock(return_value=fake_conn)
    monkeypatch.setattr(
        ImednetHook,
        "_aget_airflow_connection",
        classmethod(lambda cls, conn_id: aget_mock(conn_id)),
    )

    async def _exercise() -> None:
        hook = ImednetHook("async_conn")
        sdk = await hook.get_async_conn()
        assert isinstance(sdk, AsyncImednetSDK)
        async with sdk:
            assert sdk.config.base_url == "https://async.imednet.example.com"
        aget_mock.assert_awaited_once_with("async_conn")

    asyncio.run(_exercise())


# ---------------------------------------------------------------------------
# 3. ImednetJobTrigger & Deferrable ImednetJobSensor
# ---------------------------------------------------------------------------


def test_imednet_job_trigger_serialize_excludes_secrets_and_validates_inputs() -> None:
    """ImednetJobTrigger serializes non-secret args only and rejects embedded credentials."""
    from apache_airflow_providers_imednet import ImednetJobTrigger
    from apache_airflow_providers_imednet._airflow_compat import AirflowException

    trigger = ImednetJobTrigger(
        study_key="STUDY_A",
        batch_id="BATCH_42",
        imednet_conn_id="imednet_prod",
        poke_interval=15.0,
        timeout=300.0,
    )
    classpath, kwargs = trigger.serialize()
    assert classpath == "apache_airflow_providers_imednet.triggers.ImednetJobTrigger"
    assert kwargs == {
        "study_key": "STUDY_A",
        "batch_id": "BATCH_42",
        "imednet_conn_id": "imednet_prod",
        "poke_interval": 15.0,
        "timeout": 300.0,
    }
    serialized_json = json.dumps(kwargs)
    for forbidden in ("api_key", "security_key", "password", "oidc_token"):
        assert forbidden not in serialized_json

    with pytest.raises(AirflowException, match="non-empty"):
        ImednetJobTrigger(study_key=" ", batch_id="B1")

    with pytest.raises(AirflowException, match="Plaintext URI credentials"):
        ImednetJobTrigger(
            study_key="https://user:secret@example.com/study",  # pragma: allowlist secret
            batch_id="B1",
        )

    with pytest.raises(AirflowException, match="Sensitive credentials"):
        ImednetJobTrigger(
            study_key="STUDY_A",
            batch_id="api_key=leak123",  # pragma: allowlist secret
        )

    with pytest.raises(AirflowException, match="poke_interval"):
        ImednetJobTrigger(study_key="STUDY_A", batch_id="B1", poke_interval=0)

    with pytest.raises(AirflowException, match="timeout"):
        ImednetJobTrigger(study_key="STUDY_A", batch_id="B1", timeout=-5)


def test_imednet_job_trigger_run_success_failure_timeout_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ImednetJobTrigger polls via AsyncJobPoller, closes SDK transport, and redacts errors."""
    from apache_airflow_providers_imednet import ImednetJobTrigger
    from apache_airflow_providers_imednet._airflow_compat import TriggerEvent

    def _make_async_sdk(jobs_side_effect: Any) -> MagicMock:
        sdk = MagicMock()
        sdk.jobs.get = AsyncMock(side_effect=jobs_side_effect)
        sdk.__aenter__ = AsyncMock(return_value=sdk)
        sdk.__aexit__ = AsyncMock(return_value=None)
        return sdk

    # 1. Success after polling transition PROCESSING -> COMPLETED
    sdk_ok = _make_async_sdk(
        [
            JobStatus(batch_id="B1", state="PROCESSING"),
            JobStatus(batch_id="B1", state="COMPLETED"),
        ]
    )
    hook_ok = MagicMock(get_async_conn=AsyncMock(return_value=sdk_ok))
    monkeypatch.setattr(
        "apache_airflow_providers_imednet.triggers.ImednetHook", lambda **_: hook_ok
    )

    async def _run_success() -> list[TriggerEvent]:
        trig = ImednetJobTrigger("STUDY_A", "B1", poke_interval=0.01)
        return [ev async for ev in trig.run()]

    events = asyncio.run(_run_success())
    assert len(events) == 1
    assert events[0].payload == {
        "status": "success",
        "study_key": "STUDY_A",
        "batch_id": "B1",
        "state": "COMPLETED",
    }
    sdk_ok.__aexit__.assert_awaited_once()

    # 2. Failed job yields error event
    sdk_fail = _make_async_sdk([JobStatus(batch_id="B2", state="FAILED")])
    hook_fail = MagicMock(get_async_conn=AsyncMock(return_value=sdk_fail))
    monkeypatch.setattr(
        "apache_airflow_providers_imednet.triggers.ImednetHook", lambda **_: hook_fail
    )

    async def _run_failed() -> list[TriggerEvent]:
        trig = ImednetJobTrigger("STUDY_A", "B2", poke_interval=0.01)
        return [ev async for ev in trig.run()]

    fail_events = asyncio.run(_run_failed())
    assert len(fail_events) == 1
    assert fail_events[0].payload["status"] == "error"
    assert "FAILED" in fail_events[0].payload["message"]
    sdk_fail.__aexit__.assert_awaited_once()

    # 3. Secret-bearing exception is redacted in TriggerEvent
    sdk_err = _make_async_sdk(
        RuntimeError("HTTP 401 token=super-secret-token")  # pragma: allowlist secret
    )
    hook_err = MagicMock(get_async_conn=AsyncMock(return_value=sdk_err))
    monkeypatch.setattr(
        "apache_airflow_providers_imednet.triggers.ImednetHook", lambda **_: hook_err
    )

    async def _run_secret_error() -> list[TriggerEvent]:
        trig = ImednetJobTrigger("STUDY_A", "B3", poke_interval=0.01, timeout=1.0)
        return [ev async for ev in trig.run()]

    err_events = asyncio.run(_run_secret_error())
    assert err_events[0].payload["status"] == "error"
    assert "super-secret-token" not in err_events[0].payload["message"]
    assert "***" in err_events[0].payload["message"]
    sdk_err.__aexit__.assert_awaited_once()

    # 4. Cancellation closes async SDK cleanly and re-raises CancelledError
    async def _slow_job(_study: str, _batch: str) -> JobStatus:
        await asyncio.sleep(10)
        return JobStatus(batch_id="B4", state="PROCESSING")

    sdk_cancel = _make_async_sdk(_slow_job)
    hook_cancel = MagicMock(get_async_conn=AsyncMock(return_value=sdk_cancel))
    monkeypatch.setattr(
        "apache_airflow_providers_imednet.triggers.ImednetHook", lambda **_: hook_cancel
    )

    async def _run_cancelled() -> None:
        trig = ImednetJobTrigger("STUDY_A", "B4", poke_interval=0.5)
        gen = trig.run()
        task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_cancelled())
    sdk_cancel.__aexit__.assert_awaited_once()


def test_imednet_job_sensor_deferrable_and_synchronous_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ImednetJobSensor defers to ImednetJobTrigger when deferrable=True and pokes when False."""
    from apache_airflow_providers_imednet import ImednetJobSensor, ImednetJobTrigger
    from apache_airflow_providers_imednet._airflow_compat import AirflowException, TaskDeferred

    sensor_defer = ImednetJobSensor(
        task_id="wait_defer",
        study_key="STUDY_A",
        batch_id="BATCH_1",
        imednet_conn_id="imednet_custom",
        poke_interval=12,
        timeout=timedelta(seconds=90),
        deferrable=True,
    )
    with pytest.raises(TaskDeferred) as exc_info:
        sensor_defer.execute(context={})

    deferred = exc_info.value
    assert isinstance(deferred.trigger, ImednetJobTrigger)
    assert deferred.method_name == "execute_complete"
    assert deferred.timeout == timedelta(seconds=90)
    _, trigger_kwargs = deferred.trigger.serialize()
    assert trigger_kwargs == {
        "study_key": "STUDY_A",
        "batch_id": "BATCH_1",
        "imednet_conn_id": "imednet_custom",
        "poke_interval": 12.0,
        "timeout": 90.0,
    }

    # Resume via execute_complete
    sensor_defer.execute_complete(
        context={},
        event={"status": "success", "study_key": "STUDY_A", "batch_id": "BATCH_1"},
    )

    with pytest.raises(AirflowException, match="Did not receive a trigger event"):
        sensor_defer.execute_complete(context={}, event=None)

    with pytest.raises(AirflowException) as err_exc:
        sensor_defer.execute_complete(
            context={},
            event={
                "status": "error",
                "message": "Job failed password=topsecret",  # pragma: allowlist secret
            },
        )
    assert "topsecret" not in str(err_exc.value)
    assert "***" in str(err_exc.value)

    with pytest.raises(AirflowException, match="Unexpected trigger event status"):
        sensor_defer.execute_complete(context={}, event={"status": "unknown"})

    # Synchronous mode preserves poke
    sensor_sync = ImednetJobSensor(
        task_id="wait_sync",
        study_key="STUDY_A",
        batch_id="BATCH_2",
        deferrable=False,
    )
    mock_sdk = MagicMock()
    mock_sdk.jobs.get.return_value = JobStatus(batch_id="BATCH_2", state="COMPLETED")
    monkeypatch.setattr(sensor_sync, "_get_sdk", lambda: mock_sdk)
    assert sensor_sync.poke(context={}) is True


# ---------------------------------------------------------------------------
# 4. ImednetExportOperator watermark/filter templating & OpenLineage facets
# ---------------------------------------------------------------------------


def test_export_operator_renders_watermark_and_filter_templates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ImednetExportOperator renders Jinja watermarks/filters and rejects embedded secrets."""
    from apache_airflow_providers_imednet import ImednetExportOperator

    out_file = tmp_path / "out.csv"
    op = ImednetExportOperator(
        task_id="templated_export",
        study_key="{{ params.study }}",
        output_path=str(out_file),
        destination="csv",
        modified_since="{{ params.watermark }}",
        record_filter="form_key={{ params.form }}",
        record_data_filter="AGE>={{ params.min_age }}",
        stream_name="stream_{{ params.study }}",
    )

    op.render_template_fields(
        context={
            "params": {
                "study": "CLIN_01",
                "watermark": "2026-03-01T00:00:00Z",
                "form": "DEMO",
                "min_age": 21,
            }
        }
    )
    assert op.study_key == "CLIN_01"
    assert op.modified_since == "2026-03-01T00:00:00Z"
    assert op.record_filter == "form_key=DEMO"
    assert op.record_data_filter == "AGE>=21"
    assert op.stream_name == "stream_CLIN_01"

    captured_kwargs: dict[str, Any] = {}
    mock_sdk = MagicMock()
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    def _fake_csv(_sdk: Any, _study: str, _out: str | None, **kwargs: Any) -> None:
        captured_kwargs.update(kwargs)

    monkeypatch.setattr("apache_airflow_providers_imednet.export.export_to_csv", _fake_csv)
    result = op.execute(context={})
    assert result == str(out_file)
    assert captured_kwargs["modified_since"] == "2026-03-01T00:00:00Z"
    assert captured_kwargs["record_filter"] == "form_key=DEMO"
    assert captured_kwargs["record_data_filter"] == "AGE>=21"
    assert captured_kwargs["stream_name"] == "stream_CLIN_01"

    # Blank rendered modified_since (e.g., initial scheduled run) becomes None cleanly
    op_blank = ImednetExportOperator(
        task_id="initial_export",
        study_key="CLIN_01",
        output_path=str(out_file),
        destination="csv",
        modified_since="",
        stream_name="   ",
    )
    captured_blank: dict[str, Any] = {}
    monkeypatch.setattr(op_blank, "_get_sdk", lambda: mock_sdk)
    monkeypatch.setattr(
        "apache_airflow_providers_imednet.export.export_to_csv",
        lambda _s, _k, _o, **kw: captured_blank.update(kw),
    )
    op_blank.execute(context={})
    assert "modified_since" not in captured_blank
    assert "stream_name" not in captured_blank


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("modified_since", "https://admin:pass123@edc.example.com"),  # pragma: allowlist secret
        ("record_filter", "api_key=raw_secret_value"),  # pragma: allowlist secret
        ("record_data_filter", "Bearer eyJhbGciOiJIUzI1NiJ9"),  # pragma: allowlist secret
        ("stream_name", "postgresql://u:p@localhost/db"),  # pragma: allowlist secret
    ],
)
def test_export_operator_rejects_secrets_in_new_templated_fields(
    field_name: str, bad_value: str
) -> None:
    """Templated watermark, filter, and stream_name fields reject embedded credentials."""
    from apache_airflow_providers_imednet import ImednetExportOperator
    from apache_airflow_providers_imednet._airflow_compat import AirflowException

    with pytest.raises(AirflowException):
        ImednetExportOperator(
            task_id="bad_op",
            study_key="STUDY_A",
            **{field_name: bad_value},
        )


def test_export_operator_openlineage_facets_redaction_and_optional_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenLineage methods emit redacted source/destination Datasets and degrade when absent."""
    pytest.importorskip("airflow.providers.openlineage.extractors")
    from apache_airflow_providers_imednet import ImednetExportOperator, ImednetHook

    monkeypatch.setattr(
        ImednetHook,
        "get_base_url",
        lambda self: "https://***@edc.custom.example.com",
    )
    monkeypatch.setattr(
        ImednetHook,
        "resolve_destination_connection",
        classmethod(
            lambda cls, conn_id, dest: (
                {
                    "conn_str": "postgresql://etl_user:db_s3cr3t@warehouse.internal:5432/analytics",  # pragma: allowlist secret
                    "table": "subject_records",
                }
                if dest == "sql"
                else {
                    "account": "xy12345.us-east-1",
                    "user": "sf_user",
                    "password": "sf_password_secret",  # pragma: allowlist secret
                    "database": "CLINICAL_DW",
                    "schema": "RAW",
                    "table": "IMEDNET_RECORDS",
                }
            )
        ),
    )

    sql_op = ImednetExportOperator(
        task_id="sql_lineage",
        study_key="STUDY_OL",
        destination="sql",
        destination_conn_id="pg_warehouse",
    )
    start_lineage = sql_op.get_openlineage_facets_on_start()
    complete_lineage = sql_op.get_openlineage_facets_on_complete(task_instance=MagicMock())
    assert start_lineage is not None
    assert complete_lineage is not None
    assert len(start_lineage.inputs) == 1
    assert start_lineage.inputs[0].namespace == "https://***@edc.custom.example.com"
    assert start_lineage.inputs[0].name == "studies/STUDY_OL/records"
    assert len(start_lineage.outputs) == 1
    assert start_lineage.outputs[0].namespace == "postgresql://***@warehouse.internal:5432"
    assert start_lineage.outputs[0].name == "analytics.subject_records"
    assert "db_s3cr3t" not in repr(start_lineage)
    assert "db_s3cr3t" not in repr(complete_lineage)

    sf_op = ImednetExportOperator(
        task_id="sf_lineage",
        study_key="STUDY_OL",
        destination="snowflake",
        destination_conn_id="sf_warehouse",
    )
    sf_lineage = sf_op.get_openlineage_facets_on_start()
    assert sf_lineage.outputs[0].namespace == "snowflake://xy12345.us-east-1"
    assert sf_lineage.outputs[0].name == "CLINICAL_DW.RAW.IMEDNET_RECORDS"
    assert "sf_password_secret" not in repr(sf_lineage)

    s3_op = ImednetExportOperator(
        task_id="s3_lineage",
        study_key="STUDY_OL",
        destination="parquet",
        output_path="s3://clinical-lake/exports/study_ol.parquet",
    )
    s3_lineage = s3_op.get_openlineage_facets_on_start()
    assert s3_lineage.outputs[0].namespace == "s3://clinical-lake"
    assert s3_lineage.outputs[0].name == "exports/study_ol.parquet"

    local_op = ImednetExportOperator(
        task_id="local_lineage",
        study_key="STUDY_OL",
        destination="csv",
        output_path="/tmp/exports/study_ol.csv",
    )
    local_lineage = local_op.get_openlineage_facets_on_start()
    assert local_lineage.outputs[0].namespace == "file"
    assert local_lineage.outputs[0].name == "/tmp/exports/study_ol.csv"

    sqlite_op = ImednetExportOperator(
        task_id="sqlite_lineage",
        study_key="STUDY_OL",
        destination="sql",
        output_path="sqlite:////tmp/local_warehouse.db",
        export_kwargs={"table": "records_tbl"},
    )
    sqlite_lineage = sqlite_op.get_openlineage_facets_on_start()
    assert sqlite_lineage.outputs[0].namespace == "sqlite"
    assert "records_tbl" in sqlite_lineage.outputs[0].name

    mongo_op = ImednetExportOperator(
        task_id="mongo_lineage",
        study_key="STUDY_OL",
        destination="mongodb",
        destination_conn_id="mongo_conn",
    )
    monkeypatch.setattr(
        ImednetHook,
        "resolve_destination_connection",
        classmethod(
            lambda cls, conn_id, dest: (
                {
                    "uri": "mongodb://muser:mpass@mongo.internal:27017",  # pragma: allowlist secret
                    "database": "clinical",
                    "collection": "subjects",
                }
                if dest == "mongodb"
                else {
                    "uri": "neo4j://nuser:npass@neo4j.internal:7687",  # pragma: allowlist secret
                    "database": "graphdb",
                }
            )
        ),
    )
    mongo_lineage = mongo_op.get_openlineage_facets_on_start()
    assert mongo_lineage.outputs[0].namespace == "mongodb://***@mongo.internal:27017"
    assert mongo_lineage.outputs[0].name == "clinical.subjects"
    assert "mpass" not in repr(mongo_lineage)

    neo4j_op = ImednetExportOperator(
        task_id="neo4j_lineage",
        study_key="STUDY_OL",
        destination="neo4j",
        destination_conn_id="neo4j_conn",
    )
    neo4j_lineage = neo4j_op.get_openlineage_facets_on_start()
    assert neo4j_lineage.outputs[0].namespace == "neo4j://***@neo4j.internal:7687"
    assert neo4j_lineage.outputs[0].name == "graphdb/STUDY_OL"
    assert "npass" not in repr(neo4j_lineage)

    # Graceful fallback when destination connection lookup raises at lineage inspection time
    monkeypatch.setattr(
        ImednetHook,
        "resolve_destination_connection",
        classmethod(lambda cls, conn_id, dest: (_ for _ in ()).throw(RuntimeError("missing conn"))),
    )
    fallback_lineage = neo4j_op.get_openlineage_facets_on_start()
    assert fallback_lineage.outputs[0].namespace == "neo4j://localhost"

    # Sensor numeric timeout & TaskDeferred fallback when defer() is unavailable
    from apache_airflow_providers_imednet import ImednetJobSensor
    from apache_airflow_providers_imednet._airflow_compat import TaskDeferred

    sensor_num = ImednetJobSensor(
        task_id="wait_numeric_timeout",
        study_key="STUDY_A",
        batch_id="BATCH_9",
        poke_interval=5,
        timeout=30,
        deferrable=True,
    )
    monkeypatch.setattr(sensor_num, "defer", None, raising=False)
    with pytest.raises(TaskDeferred) as num_exc:
        sensor_num.execute(context={})
    assert num_exc.value.timeout == timedelta(seconds=30)

    # When OpenLineage package is absent, methods return None and operator executes cleanly
    monkeypatch.setitem(sys.modules, "airflow.providers.openlineage.extractors", None)
    assert s3_op.get_openlineage_facets_on_start() is None
    assert s3_op.get_openlineage_facets_on_complete() is None


# ---------------------------------------------------------------------------
# 5. AirflowStateProvider on Airflow 3.3.2 & Reference DAG parsing
# ---------------------------------------------------------------------------


def test_airflow_state_provider_prefers_task_sdk_get_current_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AirflowStateProvider uses airflow.sdk.get_current_context inside Airflow 3 tasks."""
    provider = AirflowStateProvider()
    mock_ti = MagicMock()
    mock_ti.xcom_pull.return_value = {
        "last_timestamp": "2026-04-01T10:30:00+00:00",
        "status": "success",
    }

    sdk_mod = SimpleNamespace(get_current_context=lambda: {"ti": mock_ti})
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)

    ts = provider.get_last_timestamp("STUDY_SDK", "records")
    assert ts == datetime(2026, 4, 1, 10, 30, tzinfo=UTC)
    assert provider.get_last_status("STUDY_SDK", "records") == StreamExecutionStatus.SUCCESS

    new_ts = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
    provider.set_last_timestamp("STUDY_SDK", "records", new_ts, records_processed=17)
    mock_ti.xcom_push.assert_called_once()
    pushed = mock_ti.xcom_push.call_args.kwargs
    assert pushed["key"] == "state_STUDY_SDK_records"
    assert pushed["value"]["records_processed"] == 17


def test_airflow_state_provider_roundtrip_on_real_airflow_3_db(tmp_path: Path) -> None:
    """AirflowStateProvider commits, reads, lists, and deletes XComModel checkpoints on Airflow 3."""
    pytest.importorskip("airflow.models.xcom")
    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir()
    db_file = airflow_home / "airflow.db"

    script = """
import os
from datetime import UTC, datetime
from airflow.utils import db
from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

db.initdb()
provider = AirflowStateProvider()

with provider.transaction(
    "STUDY_DB",
    "records",
    fallback_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
) as tx:
    assert tx["last_timestamp"] == datetime(2026, 1, 1, tzinfo=UTC)
    tx["new_timestamp"] = datetime(2026, 2, 15, 8, 30, tzinfo=UTC)
    tx["records_processed"] = 42
    tx["metadata"] = {"batch": "b1"}

assert provider.get_last_timestamp("STUDY_DB", "records") == datetime(2026, 2, 15, 8, 30, tzinfo=UTC)
assert provider.get_last_status("STUDY_DB", "records") == StreamExecutionStatus.SUCCESS

state = provider.read_state()
assert "STUDY_DB" in state.studies
stream_state = state.studies["STUDY_DB"].streams["records"]
assert stream_state.records_processed == 42
assert stream_state.last_run_status == StreamExecutionStatus.SUCCESS

assert provider.delete_entry("STUDY_DB", "records") is True
assert provider.get_last_timestamp("STUDY_DB", "records") is None
"""
    env = os.environ.copy()
    env["AIRFLOW_HOME"] = str(airflow_home)
    env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = f"sqlite:///{db_file}"
    env["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
    env["AIRFLOW__CORE__UNIT_TEST_MODE"] = "True"

    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_all_example_airflow_dags_parse_cleanly_on_airflow_3() -> None:
    """All DAGs in examples/airflow parse with zero import errors and schedule=None on Airflow 3."""
    dagbag_mod = pytest.importorskip("airflow.dag_processing.dagbag")
    examples_dir = ROOT / "examples" / "airflow"

    dag_bag = dagbag_mod.DagBag(dag_folder=str(examples_dir))
    assert dag_bag.import_errors == {}, f"Unexpected DAG import errors: {dag_bag.import_errors}"

    expected_dags = {
        "imednet_multi_study_reference",
        "imednet_export_example",
        "imednet_job_sensor_example",
    }
    assert expected_dags <= set(dag_bag.dags)
    for dag_id in expected_dags:
        dag = dag_bag.dags[dag_id]
        assert dag.schedule is None
