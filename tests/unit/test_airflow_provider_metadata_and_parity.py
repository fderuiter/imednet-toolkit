"""Unit tests for Airflow 3 provider metadata, deferrable trigger, lineage, and SDK parity."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jinja2
import pytest

from imednet.errors.validation import PathTraversalValidationError
from imednet.sdk import AsyncImednetSDK

pytest.importorskip("airflow")


@pytest.fixture(autouse=True, scope="module")
def _cleanup_airflow_provider_modules() -> Any:
    """Unload stale dummy-patched provider modules before and after real-Airflow parity tests."""
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
            sys.modules.pop(mod, None)
    if (
        "airflow" not in sys.modules
        or getattr(sys.modules.get("airflow"), "__path__", None) is None
    ):
        for mod in list(sys.modules):
            if mod == "airflow" or mod.startswith("airflow."):
                sys.modules.pop(mod, None)

    yield

    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow")):
            sys.modules.pop(mod, None)


def test_provider_discovery_aliases_and_ui_metadata() -> None:
    """Verify Airflow 3 ProvidersManager registers hooks, UI widgets, and triggers cleanly."""
    from airflow.providers_manager import ProvidersManager
    from airflow.sdk.providers_manager_runtime import ProvidersManagerTaskRuntime

    from apache_airflow_providers_imednet.get_provider_info import get_provider_info
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from apache_airflow_providers_imednet.sensors import ImednetJobSensor
    from apache_airflow_providers_imednet.triggers import ImednetJobTrigger

    info = get_provider_info()
    assert info["package-name"] == "apache-airflow-providers-imednet"
    assert info["triggers"] == [
        {
            "integration-name": "iMednet",
            "python-modules": ["airflow.providers.imednet.triggers"],
        }
    ]

    # Dynamic MetaPathFinder maps airflow.providers.imednet.* to public provider classes
    hooks_alias = importlib.import_module("airflow.providers.imednet.hooks")
    ops_alias = importlib.import_module("airflow.providers.imednet.operators.export")
    sensors_alias = importlib.import_module("airflow.providers.imednet.sensors")
    triggers_alias = importlib.import_module("airflow.providers.imednet.triggers")
    root_alias = importlib.import_module("airflow.providers.imednet")

    assert hooks_alias.ImednetHook is ImednetHook
    assert ops_alias.ImednetExportOperator is ImednetExportOperator
    assert sensors_alias.ImednetJobSensor is ImednetJobSensor
    assert triggers_alias.ImednetJobTrigger is ImednetJobTrigger
    assert root_alias.ImednetJobTrigger is ImednetJobTrigger

    pm = ProvidersManager()
    pm.initialize_providers_list()
    pm.initialize_providers_hooks()
    pm.initialize_providers_extra_links()
    pm.initialize_providers_triggers()

    assert "apache-airflow-providers-imednet" in pm.providers
    assert any(
        trigger_info.package_name == "apache-airflow-providers-imednet"
        for trigger_info in pm.trigger
    )

    widgets = pm.connection_form_widgets
    assert "extra__imednet__oidc_token" in widgets
    assert widgets["extra__imednet__oidc_token"].is_sensitive is True
    assert "extra__imednet__base_url" in widgets
    assert "extra__imednet__timeout" in widgets
    assert pm.field_behaviours["imednet"]["hidden_fields"] == ["host", "schema", "port", "extra"]

    pm_runtime = ProvidersManagerTaskRuntime()
    pm_runtime.initialize_providers_hooks()
    hook_info = pm_runtime.hooks["imednet"]
    assert hook_info is not None
    assert hook_info.hook_class_name == "airflow.providers.imednet.hooks.ImednetHook"
    assert hook_info.connection_type == "imednet"
    assert hook_info.connection_testable is True


def test_ensure_airflow_provider_finder_reinserts_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _ensure_airflow_provider_finder restores finder if removed from sys.meta_path."""
    import apache_airflow_providers_imednet as provider_pkg
    from apache_airflow_providers_imednet import _ensure_airflow_provider_finder
    from apache_airflow_providers_imednet.get_provider_info import get_provider_info

    cleaned = [
        finder
        for finder in sys.meta_path
        if type(finder).__name__ != "_ImednetAirflowProviderFinder"
    ]
    monkeypatch.setattr(sys, "meta_path", cleaned)
    _ensure_airflow_provider_finder()
    assert any(type(finder).__name__ == "_ImednetAirflowProviderFinder" for finder in sys.meta_path)
    assert provider_pkg.get_provider_info() == get_provider_info()


def test_sync_airflow_provider_aliases_handles_provider_reload_and_eviction() -> None:
    """Verify airflow.providers.imednet aliases stay identical across provider unloads/reloads."""
    import airflow.providers
    import airflow.providers.imednet.hooks as initial_alias_hooks

    from apache_airflow_providers_imednet import _ensure_airflow_provider_finder

    assert initial_alias_hooks.ImednetHook is not None

    # Simulate partial unload of apache_airflow_providers_imednet* while alias entries remain cached
    for mod in list(sys.modules):
        if mod == "apache_airflow_providers_imednet" or mod.startswith(
            "apache_airflow_providers_imednet."
        ):
            sys.modules.pop(mod, None)

    _ensure_airflow_provider_finder()
    assert "airflow.providers.imednet.hooks" not in sys.modules
    assert not hasattr(airflow.providers, "imednet")

    # Re-import canonical package while pre-seeding stale alias entries to verify rebinding
    stale_pkg = SimpleNamespace()
    stale_hooks = SimpleNamespace()
    sys.modules["airflow.providers.imednet"] = stale_pkg  # type: ignore[assignment]
    sys.modules["airflow.providers.imednet.hooks"] = stale_hooks  # type: ignore[assignment]
    airflow.providers.imednet = stale_pkg  # type: ignore[attr-defined]

    reloaded_pkg = importlib.import_module("apache_airflow_providers_imednet")
    from airflow.providers.imednet.hooks import ImednetHook as ReloadedAliasHook

    from apache_airflow_providers_imednet.hooks import ImednetHook as ReloadedCanonicalHook

    assert ReloadedAliasHook is ReloadedCanonicalHook
    assert getattr(airflow.providers, "imednet") is reloaded_pkg


def test_hook_extra_prefix_base_url_and_test_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify extra__imednet__* lookup, get_base_url, and redacted test_connection."""
    from apache_airflow_providers_imednet.hooks import ImednetHook

    conn = SimpleNamespace(
        login="api-key-alpha",  # pragma: allowlist secret
        password="",
        extra_dejson={
            "extra__imednet__security_key": "sec-key-bravo",  # pragma: allowlist secret
            "extra__imednet__oidc_token": "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.c2ln",  # pragma: allowlist secret
            "extra__imednet__base_url": "https://edc-staging.example.org/",
            "extra__imednet__timeout": "45.5",
            "extra__imednet__strict_mode": "true",
            "extra__imednet__simulation_mode": 0,
        },
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, cid: conn),
    )

    hook = ImednetHook("imednet_custom")
    assert hook.get_base_url() == "https://edc-staging.example.org"

    fake_client = MagicMock()
    fake_client.studies.list.return_value = iter([SimpleNamespace(study_key="S1")])
    with patch.object(ImednetHook, "get_conn", return_value=fake_client):
        ok, message = hook.test_connection()

    assert ok is True
    assert "successfully tested" in message.lower()
    fake_client.close.assert_called_once()

    # Verify secret redaction when test_connection encounters an exception exposing credentials
    failing_client = MagicMock()
    failing_client.studies.list.side_effect = RuntimeError(
        "401 Unauthorized for api-key-alpha / sec-key-bravo / "  # pragma: allowlist secret
        "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.c2ln "  # pragma: allowlist secret
        "at https://user:inline-pass@edc-staging.example.org"  # pragma: allowlist secret
    )
    with patch.object(ImednetHook, "get_conn", return_value=failing_client):
        ok_fail, err_msg = hook.test_connection()

    assert ok_fail is False
    assert "api-key-alpha" not in err_msg
    assert "sec-key-bravo" not in err_msg
    assert "eyJhbGciOiJub25lIn0" not in err_msg
    assert "inline-pass" not in err_msg
    assert "***" in err_msg
    failing_client.close.assert_called_once()


def test_hook_get_base_url_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_base_url host and config fallbacks when connection extra is absent or fails."""
    from apache_airflow_providers_imednet.hooks import ImednetHook

    host_conn = SimpleNamespace(host="edc-host.example.org", extra_dejson={})
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, cid: host_conn),
    )
    assert ImednetHook("host_conn").get_base_url() == "https://edc-host.example.org"

    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, cid: (_ for _ in ()).throw(RuntimeError("connection missing"))),
    )
    assert ImednetHook("missing_conn").get_base_url() == "https://edc.prod.imednetapi.com"


def test_hook_get_async_conn_uses_aget_connection_and_thread_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify get_async_conn resolves AsyncImednetSDK via async and fallback hooks."""
    from apache_airflow_providers_imednet.hooks import ImednetHook

    conn = SimpleNamespace(
        login="async-api-key",  # pragma: allowlist secret
        password="async-sec-key",  # pragma: allowlist secret
        extra_dejson={
            "extra__imednet__base_url": "https://async.imednet.example.com",
        },
    )

    async def _run_checks() -> None:
        hook = ImednetHook("imednet_async")
        with patch.object(
            ImednetHook,
            "aget_connection",
            new=AsyncMock(return_value=conn),
            create=True,
        ):
            sdk = await hook.get_async_conn()
            try:
                assert isinstance(sdk, AsyncImednetSDK)
                assert sdk._async_client.base_url == "https://async.imednet.example.com"
                assert sdk.auth.get_headers() == {
                    "x-api-key": "async-api-key",  # pragma: allowlist secret
                    "x-imn-security-key": "async-sec-key",  # pragma: allowlist secret
                }
            finally:
                await sdk.aclose()

        # Fallback to asyncio.to_thread(_get_airflow_connection) when aget_connection raises
        monkeypatch.setattr(
            ImednetHook,
            "_get_airflow_connection",
            classmethod(lambda cls, cid: conn),
        )
        with patch.object(
            ImednetHook,
            "aget_connection",
            new=AsyncMock(side_effect=RuntimeError("Task runner comms unavailable")),
            create=True,
        ):
            sdk_fallback = await hook.get_async_conn()
            try:
                assert isinstance(sdk_fallback, AsyncImednetSDK)
            finally:
                await sdk_fallback.aclose()

    asyncio.run(_run_checks())


def test_trigger_validation_and_secret_free_serialization() -> None:
    """Verify ImednetJobTrigger validates parameters, rejects traversal/secrets, and serializes."""
    from apache_airflow_providers_imednet._airflow_compat import AirflowException
    from apache_airflow_providers_imednet.triggers import ImednetJobTrigger

    trigger = ImednetJobTrigger(
        study_key="STUDY_01",
        batch_id="BATCH_99",
        imednet_conn_id="imednet_prod",
        poke_interval=15.0,
        timeout=120.0,
    )
    classpath, serialized_kwargs = trigger.serialize()
    assert classpath == "apache_airflow_providers_imednet.triggers.ImednetJobTrigger"
    assert serialized_kwargs == {
        "study_key": "STUDY_01",
        "batch_id": "BATCH_99",
        "imednet_conn_id": "imednet_prod",
        "poke_interval": 15.0,
        "timeout": 120.0,
    }
    for forbidden in ("api_key", "security_key", "password", "oidc_token", "token"):
        assert forbidden not in serialized_kwargs

    with pytest.raises(AirflowException, match="non-empty"):
        ImednetJobTrigger(study_key="  ", batch_id="B1")

    with pytest.raises(AirflowException, match="positive finite"):
        ImednetJobTrigger(study_key="S1", batch_id="B1", poke_interval=0)

    with pytest.raises(AirflowException, match="positive finite"):
        ImednetJobTrigger(study_key="S1", batch_id="B1", timeout=-5)

    with pytest.raises(AirflowException, match="Sensitive credentials"):
        ImednetJobTrigger(
            study_key="api_key=leak",  # pragma: allowlist secret
            batch_id="B1",
        )

    with pytest.raises(AirflowException, match="Plaintext URI credentials"):
        ImednetJobTrigger(
            study_key="S1",
            batch_id="B1",
            imednet_conn_id="https://user:secret@example.com",  # pragma: allowlist secret
        )

    with pytest.raises(PathTraversalValidationError, match="delimiters are not allowed"):
        ImednetJobTrigger(study_key="../etc/passwd", batch_id="B1")

    with pytest.raises(PathTraversalValidationError, match="delimiters are not allowed"):
        ImednetJobTrigger(study_key="S1", batch_id="nested/batch")


def test_trigger_run_transitions_failure_redaction_and_cancellation() -> None:
    """Verify ImednetJobTrigger polls AsyncJobPoller, redacts errors, and closes on cancel."""
    from apache_airflow_providers_imednet._airflow_compat import TriggerEvent
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.triggers import ImednetJobTrigger

    def _mock_async_sdk(get_side_effect: Any) -> MagicMock:
        sdk = MagicMock()
        sdk.jobs.get = AsyncMock(side_effect=get_side_effect)
        sdk.__aenter__ = AsyncMock(return_value=sdk)
        sdk.__aexit__ = AsyncMock(return_value=None)
        return sdk

    async def _exercise_trigger() -> None:
        states = iter(
            [
                SimpleNamespace(
                    batch_id="B_OK",
                    state="PROCESSING",
                    progress=50,
                    result_url="",
                    is_terminal=False,
                    is_failed=False,
                    is_successful=False,
                    model_dump=lambda mode="json": {"batch_id": "B_OK", "state": "PROCESSING"},
                ),
                SimpleNamespace(
                    batch_id="B_OK",
                    state="COMPLETED",
                    progress=100,
                    result_url="",
                    is_terminal=True,
                    is_failed=False,
                    is_successful=True,
                    model_dump=lambda mode="json": {"batch_id": "B_OK", "state": "COMPLETED"},
                ),
            ]
        )
        async_sdk = _mock_async_sdk(lambda sk, bid: next(states))

        trigger = ImednetJobTrigger(study_key="S_OK", batch_id="B_OK", poke_interval=0.01)
        with patch.object(ImednetHook, "get_async_conn", new=AsyncMock(return_value=async_sdk)):
            events = [event async for event in trigger.run()]

        assert len(events) == 1
        assert isinstance(events[0], TriggerEvent)
        assert events[0].payload["status"] == "success"
        assert events[0].payload["state"] == "COMPLETED"
        assert events[0].payload["job"] == {"batch_id": "B_OK", "state": "COMPLETED"}
        async_sdk.__aexit__.assert_awaited_once()

        # Terminal job failure emits redacted error TriggerEvent
        failed_job = SimpleNamespace(
            batch_id="B_FAIL",
            state="FAILED",
            progress=100,
            result_url="",
            is_terminal=True,
            is_failed=True,
            is_successful=False,
        )
        fail_sdk = _mock_async_sdk(lambda sk, bid: failed_job)
        fail_trigger = ImednetJobTrigger(study_key="S_OK", batch_id="B_FAIL", poke_interval=0.01)
        with patch.object(ImednetHook, "get_async_conn", new=AsyncMock(return_value=fail_sdk)):
            fail_events = [event async for event in fail_trigger.run()]

        assert len(fail_events) == 1
        assert fail_events[0].payload["status"] == "error"
        assert "B_FAIL" in fail_events[0].payload["message"]
        fail_sdk.__aexit__.assert_awaited_once()

        # Terminal COMPLETED job with failed Record item in resultUrl emits redacted error event
        from imednet.spi.models import JobStatus

        result_url_job = JobStatus.model_validate(
            {
                "batchId": "B_RES",
                "state": "COMPLETED",
                "progress": 100,
                "resultUrl": "https://edc.trn.imednetapi.com/results/B_RES.json?api_key=RAW_TRIG_SECRET",  # pragma: allowlist secret
            }
        )
        res_sdk = _mock_async_sdk(lambda sk, bid: result_url_job)
        res_sdk.jobs.fetch_result = AsyncMock(
            return_value=[
                {
                    "recordKey": "REC-99",
                    "status": "FAILED",
                    "error": "Invalid Variable token=RAW_TRIG_SECRET",  # pragma: allowlist secret
                }
            ]
        )
        res_trigger = ImednetJobTrigger(study_key="S_OK", batch_id="B_RES", poke_interval=0.01)
        with patch.object(ImednetHook, "get_async_conn", new=AsyncMock(return_value=res_sdk)):
            res_events = [event async for event in res_trigger.run()]

        assert len(res_events) == 1
        assert res_events[0].payload["status"] == "error"
        assert "Invalid Variable" in res_events[0].payload["message"]
        assert "RAW_TRIG_SECRET" not in res_events[0].payload["message"]
        res_sdk.jobs.fetch_result.assert_awaited_once()
        res_sdk.__aexit__.assert_awaited_once()

        # Secret-bearing connection exception is scrubbed before yielding TriggerEvent
        conn_leak = SimpleNamespace(
            login="top-secret-key",  # pragma: allowlist secret
            password="top-secret-sec",  # pragma: allowlist secret
            extra_dejson={
                "oidc_token": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0cmlnIn0.c2ln"  # pragma: allowlist secret
            },
        )
        leak_sdk = _mock_async_sdk(
            RuntimeError(
                "Auth error for top-secret-key / top-secret-sec / "  # pragma: allowlist secret
                "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0cmlnIn0.c2ln "  # pragma: allowlist secret
                "via https://admin:uri-secret@edc.example.org"  # pragma: allowlist secret
            )
        )
        leak_trigger = ImednetJobTrigger(study_key="S_OK", batch_id="B_ERR", poke_interval=0.01)
        with (
            patch.object(ImednetHook, "get_async_conn", new=AsyncMock(return_value=leak_sdk)),
            patch.object(
                ImednetHook,
                "_aget_airflow_connection",
                new=AsyncMock(return_value=conn_leak),
            ),
        ):
            leak_events = [event async for event in leak_trigger.run()]

        err_text = leak_events[0].payload["message"]
        assert leak_events[0].payload["status"] == "error"
        assert "top-secret-key" not in err_text
        assert "top-secret-sec" not in err_text
        assert "eyJhbGciOiJub25lIn0" not in err_text
        assert "uri-secret" not in err_text
        assert "***" in err_text
        leak_sdk.__aexit__.assert_awaited_once()

        # Task cancellation exits AsyncImednetSDK context cleanly and propagates CancelledError
        gate = asyncio.Event()

        async def _hang(sk: str, bid: str) -> Any:
            gate.set()
            await asyncio.sleep(30)

        cancel_sdk = _mock_async_sdk(_hang)
        cancel_trigger = ImednetJobTrigger(study_key="S_OK", batch_id="B_WAIT", poke_interval=1.0)

        async def _consume() -> list[TriggerEvent]:
            return [ev async for ev in cancel_trigger.run()]

        with patch.object(ImednetHook, "get_async_conn", new=AsyncMock(return_value=cancel_sdk)):
            task = asyncio.create_task(_consume())
            await gate.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        cancel_sdk.__aexit__.assert_awaited_once()

    asyncio.run(_exercise_trigger())


def test_sensor_deferrable_execution_traversal_and_execute_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ImednetJobSensor defers to ImednetJobTrigger and resumes via execute_complete."""
    from airflow.configuration import conf

    from apache_airflow_providers_imednet._airflow_compat import AirflowException, TaskDeferred
    from apache_airflow_providers_imednet.sensors import ImednetJobSensor
    from apache_airflow_providers_imednet.triggers import ImednetJobTrigger

    # Verify default_deferrable config resolution when deferrable is omitted
    monkeypatch.setattr(
        conf,
        "getboolean",
        lambda section, key, fallback=False: True if key == "default_deferrable" else fallback,
    )
    auto_def_sensor = ImednetJobSensor(
        task_id="auto_def_job",
        study_key="STUDY_AUTO",
        batch_id="BATCH_AUTO",
    )
    assert auto_def_sensor.deferrable is True

    sensor = ImednetJobSensor(
        task_id="wait_job",
        study_key="STUDY_DEF",
        batch_id="BATCH_DEF",
        imednet_conn_id="imednet_default",
        poke_interval=30,
        timeout=timedelta(minutes=5),
        deferrable=True,
    )
    with pytest.raises(TaskDeferred) as deferred_exc:
        sensor.execute({})

    assert isinstance(deferred_exc.value.trigger, ImednetJobTrigger)
    assert deferred_exc.value.method_name == "execute_complete"
    assert deferred_exc.value.trigger.study_key == "STUDY_DEF"
    assert deferred_exc.value.trigger.batch_id == "BATCH_DEF"
    assert deferred_exc.value.trigger.timeout == 300.0

    # Also cover numeric timeout and fallback raise TaskDeferred when self.defer is not present
    numeric_sensor = ImednetJobSensor(
        task_id="wait_job_numeric",
        study_key="STUDY_DEF",
        batch_id="BATCH_DEF",
        poke_interval=10,
        timeout=90,
        deferrable=True,
    )
    with (
        patch.object(ImednetJobSensor, "defer", None),
        pytest.raises(TaskDeferred) as fallback_exc,
    ):
        numeric_sensor.execute({})
    assert fallback_exc.value.trigger.timeout == 90.0

    # Path traversal is rejected before deferring
    bad_sensor = ImednetJobSensor(
        task_id="bad_job",
        study_key="../secret",
        batch_id="BATCH_DEF",
        deferrable=True,
    )
    with pytest.raises(PathTraversalValidationError, match="delimiters are not allowed"):
        bad_sensor.execute({})

    # execute_complete handles success, redacted error, non-dict, and unexpected status
    completed = sensor.execute_complete(
        {},
        {"status": "success", "state": "COMPLETED", "job": {"batch_id": "BATCH_DEF"}},
    )
    assert completed == {"batch_id": "BATCH_DEF"}

    with pytest.raises(AirflowException, match=r"\*\*\*") as err_info:
        sensor.execute_complete(
            {},
            {
                "status": "error",
                "message": "Failed with api_key=raw-secret-val",  # pragma: allowlist secret
            },
        )
    assert "raw-secret-val" not in str(err_info.value)

    with pytest.raises(AirflowException, match="valid trigger event"):
        sensor.execute_complete({}, None)

    with pytest.raises(AirflowException, match="Unexpected trigger event status"):
        sensor.execute_complete({}, {"status": "unknown"})

    # Synchronous mode (deferrable=False) runs poke() via super().execute()
    sync_sensor = ImednetJobSensor(
        task_id="sync_job",
        study_key="STUDY_SYNC",
        batch_id="BATCH_SYNC",
        poke_interval=0.01,
        deferrable=False,
    )
    with patch.object(ImednetJobSensor, "poke", return_value=True) as poke_mock:
        sync_sensor.execute({})
    poke_mock.assert_called_once()


def test_export_operator_jinja_templating_and_secret_rejection() -> None:
    """Verify Jinja templating for watermarks/filters, blank normalization, and secret rejection."""
    from apache_airflow_providers_imednet._airflow_compat import AirflowException
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators import export as export_ops
    from apache_airflow_providers_imednet.operators.export import (
        ImednetExportOperator,
        _ValidatedExportKwargs,
    )

    # Exercise _ValidatedExportKwargs mutation guards (__setitem__, update, setdefault)
    validated = _ValidatedExportKwargs({"table": "SAFE"})
    validated["schema"] = "PUBLIC"
    validated.update({"stage_prefix": "imednet/S1"})
    assert validated.setdefault("batch_size", 250) == 250
    with pytest.raises(AirflowException, match="Secret key"):
        validated["password"] = "forbidden"  # pragma: allowlist secret
    with pytest.raises(AirflowException, match="Secret key"):
        validated.update({"api_key": "forbidden"})  # pragma: allowlist secret
    with pytest.raises(AirflowException, match="Secret key"):
        validated.setdefault("token", "forbidden")  # pragma: allowlist secret

    op = ImednetExportOperator(
        task_id="templated_export",
        study_key="{{ params.study_key }}",
        destination="parquet",
        output_path="/tmp/{{ params.study_key }}.parquet",
        incremental=True,
        modified_since="{{ data_interval_start.isoformat() }}",
        record_filter={"form_key": "{{ params.form_key }}"},
        record_data_filter="VISIT='{{ params.visit }}'",
        stream_name="{{ params.stream }}",
        export_kwargs={"compression": "{{ params.compression }}"},
    )
    jinja_env = jinja2.Environment(autoescape=False)
    context: dict[str, Any] = {
        "params": {
            "study_key": "STUDY_JINJA",
            "form_key": "F_VITALS",
            "visit": "SCREENING",
            "stream": "vitals_stream",
            "compression": "snappy",
        },
        "data_interval_start": datetime(2026, 3, 15, 8, 30, tzinfo=timezone.utc),
    }
    op.render_template_fields(context, jinja_env=jinja_env)

    assert op.study_key == "STUDY_JINJA"
    assert op.output_path == "/tmp/STUDY_JINJA.parquet"
    assert op.modified_since == "2026-03-15T08:30:00+00:00"
    assert op.record_filter == {"form_key": "F_VITALS"}
    assert op.record_data_filter == "VISIT='SCREENING'"
    assert op.stream_name == "vitals_stream"
    assert op.export_kwargs == {"compression": "snappy"}

    # Blank rendered modified_since and stream_name normalize to None during execute()
    blank_op = ImednetExportOperator(
        task_id="blank_watermark_export",
        study_key="STUDY_JINJA",
        destination="csv",
        output_path="/tmp/out.csv",
        incremental=True,
        modified_since="   ",
        stream_name="  ",
    )
    fake_sdk = MagicMock()
    with (
        patch.object(ImednetHook, "get_sdk_client", return_value=fake_sdk),
        patch.object(export_ops.export, "export_to_csv") as export_csv_mock,
    ):
        blank_op.execute({})

    _, call_kwargs = export_csv_mock.call_args
    assert "modified_since" not in call_kwargs
    assert call_kwargs["stream_name"] == "csv_blank_watermark_export"

    # Inline secrets in newly templated fields are rejected eagerly at construction and execute()
    with pytest.raises(AirflowException, match="Secret key"):
        ImednetExportOperator(
            task_id="secret_filter_export",
            study_key="STUDY_JINJA",
            destination="csv",
            output_path="/tmp/out.csv",
            record_filter=[{"api_key": "do-not-allow"}],  # pragma: allowlist secret
        )

    with pytest.raises(AirflowException, match="Embedded credentials"):
        ImednetExportOperator(
            task_id="secret_token_export",
            study_key="STUDY_JINJA",
            destination="csv",
            output_path="/tmp/out.csv",
            modified_since="api_key=leak",  # pragma: allowlist secret
        )

    with pytest.raises(AirflowException, match="Plaintext URI credentials"):
        ImednetExportOperator(
            task_id="secret_uri_export",
            study_key="STUDY_JINJA",
            destination="csv",
            output_path="/tmp/out.csv",
            record_data_filter="https://user:secret@internal.example.org",  # pragma: allowlist secret
        )


def test_export_operator_openlineage_facets_across_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify OpenLineage input/output facets across warehouse, DuckDB, SQL, NoSQL, and files."""
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    monkeypatch.setattr(
        ImednetHook,
        "get_base_url",
        lambda self: "https://EDC.Corp.Example.org:8443/api/v1",
    )

    # 1. Snowflake destination via resolved connection
    sf_op = ImednetExportOperator(
        task_id="ol_sf",
        study_key="STUDY_SF",
        destination="snowflake",
        destination_conn_id="sf_conn",
        export_kwargs={"table": "VITALS"},
    )
    with patch.object(
        ImednetHook,
        "resolve_destination_connection",
        return_value={
            "account": "xy12345.us-east-1",
            "user": "svc_user",
            "password": "super-secret-sf-password",  # pragma: allowlist secret
            "database": "CLINICAL",
            "schema": "RAW",
            "table": "VITALS",
        },
    ):
        sf_facets = sf_op.get_openlineage_facets_on_start()

    assert sf_facets is not None
    assert len(sf_facets.inputs) == 1
    assert sf_facets.inputs[0].namespace == "https://edc.corp.example.org:8443"
    assert sf_facets.inputs[0].name == "study.STUDY_SF.records"
    assert len(sf_facets.outputs) == 1
    assert sf_facets.outputs[0].namespace == "snowflake://xy12345.us-east-1"
    assert sf_facets.outputs[0].name == "CLINICAL.RAW.VITALS"
    assert "super-secret-sf-password" not in repr(sf_facets)

    # Snowflake fallback when destination connection lookup raises
    with patch.object(
        ImednetHook,
        "resolve_destination_connection",
        side_effect=RuntimeError("missing sf_conn"),
    ):
        sf_fallback = sf_op.get_openlineage_facets_on_start()
    assert sf_fallback is not None
    assert sf_fallback.outputs[0].namespace == "snowflake://unknown"

    # 2. DuckDB destination (preserved upstream support)
    duck_op = ImednetExportOperator(
        task_id="ol_duckdb",
        study_key="STUDY_DUCK",
        destination="duckdb",
        output_path="/var/data/clinical.duckdb",
        export_kwargs={"table_name": "adverse_events"},
    )
    duck_facets = duck_op.get_openlineage_facets_on_complete(None)
    assert duck_facets is not None
    assert duck_facets.outputs[0].namespace == "duckdb"
    assert duck_facets.outputs[0].name == "/var/data/clinical.duckdb.adverse_events"

    # 3. SQL destination strips driver suffix, userinfo, and query string
    sql_op = ImednetExportOperator(
        task_id="ol_sql",
        study_key="STUDY_SQL",
        destination="sql",
        destination_conn_id="pg_conn",
        export_kwargs={"table_name": "enrollments"},
    )
    with patch.object(
        ImednetHook,
        "resolve_destination_connection",
        return_value={
            "conn_str": (
                "postgresql+psycopg2://etl_user:pg-secret-pw@db.internal:5432/warehouse"  # pragma: allowlist secret
                "?sslmode=require"
            )
        },
    ):
        sql_facets = sql_op.get_openlineage_facets_on_start()

    assert sql_facets is not None
    assert sql_facets.outputs[0].namespace == "postgres://db.internal:5432"
    assert sql_facets.outputs[0].name == "warehouse.enrollments"
    assert "pg-secret-pw" not in repr(sql_facets)
    assert "etl_user" not in repr(sql_facets)

    # 4. SQLite SQL destination
    sqlite_op = ImednetExportOperator(
        task_id="ol_sqlite",
        study_key="STUDY_SQLITE",
        destination="sql_by_form",
        output_path="sqlite:////tmp/forms.sqlite",
    )
    sqlite_facets = sqlite_op.get_openlineage_facets_on_start()
    assert sqlite_facets is not None
    assert sqlite_facets.outputs[0].namespace == "sqlite"
    assert sqlite_facets.outputs[0].name == "/tmp/forms.sqlite.records"

    # 5. MongoDB and Neo4j destinations
    mongo_op = ImednetExportOperator(
        task_id="ol_mongo",
        study_key="STUDY_MONGO",
        destination="mongodb",
        destination_conn_id="mongo_conn",
    )
    with patch.object(
        ImednetHook,
        "resolve_destination_connection",
        return_value={
            "uri": "mongodb+srv://muser:mpass@cluster0.example.mongodb.net/?retryWrites=true",  # pragma: allowlist secret
            "database": "edc_mirror",
            "collection": "subjects",
        },
    ):
        mongo_facets = mongo_op.get_openlineage_facets_on_start()

    assert mongo_facets is not None
    assert mongo_facets.outputs[0].namespace == "mongodb+srv://cluster0.example.mongodb.net"
    assert mongo_facets.outputs[0].name == "edc_mirror.subjects"
    assert "mpass" not in repr(mongo_facets)
    assert "muser" not in repr(mongo_facets)

    neo4j_op = ImednetExportOperator(
        task_id="ol_neo4j",
        study_key="STUDY_GRAPH",
        destination="neo4j",
        destination_conn_id="neo4j_conn",
        export_kwargs={"label": "ClinicalSubject"},
    )
    with patch.object(
        ImednetHook,
        "resolve_destination_connection",
        return_value={
            "uri": "neo4j+s://graph.example.org:7687",
            "auth": ("neo4j", "neo4j-secret"),  # pragma: allowlist secret
            "database": "clinicalgraph",
        },
    ):
        neo4j_facets = neo4j_op.get_openlineage_facets_on_start()

    assert neo4j_facets is not None
    assert neo4j_facets.outputs[0].namespace == "neo4j+s://graph.example.org:7687"
    assert neo4j_facets.outputs[0].name == "clinicalgraph.ClinicalSubject"
    assert "neo4j-secret" not in repr(neo4j_facets)

    # 6. S3 object store URI and local file path
    s3_op = ImednetExportOperator(
        task_id="ol_s3",
        study_key="STUDY_S3",
        destination="parquet",
        output_path="s3://clinical-lake/bronze/study_s3/part-000.parquet",
    )
    s3_facets = s3_op.get_openlineage_facets_on_start()
    assert s3_facets is not None
    assert s3_facets.outputs[0].namespace == "s3://clinical-lake"
    assert s3_facets.outputs[0].name == "bronze/study_s3/part-000.parquet"

    file_op = ImednetExportOperator(
        task_id="ol_file",
        study_key="STUDY_FILE",
        destination="csv",
        output_path="/tmp/exports/study_file.csv",
    )
    file_facets = file_op.get_openlineage_facets_on_start()
    assert file_facets is not None
    assert file_facets.outputs[0].namespace == "file"
    assert file_facets.outputs[0].name == "/tmp/exports/study_file.csv"

    # 7. Graceful None fallback when OpenLineage packages are absent
    real_import = __import__

    def _block_openlineage(name: str, *args: Any, **kwargs: Any) -> Any:
        if "openlineage" in name:
            raise ImportError(f"Simulated missing {name}")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_block_openlineage):
        assert file_op.get_openlineage_facets_on_start() is None
        assert file_op.get_openlineage_facets_on_complete(None) is None


def test_airflow_state_provider_and_example_dags_airflow3_compat(tmp_path: Path) -> None:
    """Verify AirflowStateProvider SQLite round-trip (with underscores) and example DagBag."""
    from airflow.dag_processing.dagbag import DagBag

    examples_dir = Path(__file__).resolve().parents[2] / "examples" / "airflow"
    dag_bag = DagBag(dag_folder=str(examples_dir))
    assert dag_bag.import_errors == {}
    assert len(dag_bag.dags) >= 3
    for dag_id, dag in dag_bag.dags.items():
        assert dag.schedule is None, f"{dag_id} should use Airflow 3 schedule=None"

    # Run real Airflow 3 SQLite metadata round-trip in an isolated subprocess DB
    db_file = tmp_path / "airflow_state_parity.db"
    script = textwrap.dedent(
        """
        from datetime import datetime, timezone
        import airflow.utils.db as db
        from imednet_workflows.state_ledger import AirflowStateProvider

        db.initdb()
        provider = AirflowStateProvider()
        with provider.transaction("STUDY_A_1", "records_v1") as tx:
            tx["new_timestamp"] = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
            tx["records_processed"] = 12

        committed = provider.get_last_timestamp("STUDY_A_1", "records_v1")
        assert committed is not None
        assert committed.isoformat() == "2026-04-01T00:00:00+00:00"
        state = provider.read_state()
        assert "STUDY_A_1" in state.studies
        assert "records_v1" in state.studies["STUDY_A_1"].streams
        assert provider.delete_entry("STUDY_A_1", "records_v1") is True
        """
    )
    env = {
        **dict(__import__("os").environ),
        "AIRFLOW_HOME": str(tmp_path),
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{db_file}",
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
