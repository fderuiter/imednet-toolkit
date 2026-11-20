"""Unit tests for strict API path segment validation and path traversal prevention."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import respx

from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
from apache_airflow_providers_imednet.sensors import ImednetJobSensor
from imednet import AsyncImednetSDK, ImednetSDK, cli
from imednet.core.client import Client
from imednet.core.endpoint.base import GenericEndpoint
from imednet.errors import ClientError, PathTraversalValidationError
from imednet.models.base import ImednetBaseModel
from imednet_workflows.uat.engine import UATExecutionEngine


class MockModel(ImednetBaseModel):
    """Mock model for endpoint path security tests."""


class MockEndpoint(GenericEndpoint[MockModel]):
    """Mock endpoint for endpoint path security tests."""

    BASE_PATH = "/api/v1/edc/studies"
    MODEL = MockModel
    PATH = "/test"


UNSAFE_DYNAMIC_IDENTIFIERS = (
    "..",
    ".",
    "%2e",
    "%2e%2e",
    "../users",
    "../../CANARY_STUDY/records",
    "%2e%2e/%2e%2e/CANARY_STUDY/records",
    "CANARY_STUDY/records#",
    "CANARY_STUDY/users?",
    "my_study?role=admin",
    "my_study#fragment",
    "CANARY_STUDY\\records",
    "CANARY_STUDY%2Frecords",
    "CANARY_STUDY\x00records",
    "CANARY_STUDY%0arecords",
    "",
)

SYNC_LIST_ENDPOINTS = (
    "codings",
    "forms",
    "intervals",
    "queries",
    "record_revisions",
    "records",
    "sites",
    "studies",
    "subjects",
    "users",
    "variables",
    "visits",
)

SYNC_GET_ENDPOINTS = (
    "forms",
    "intervals",
    "jobs",
    "record_revisions",
    "records",
    "sites",
    "studies",
    "subjects",
    "variables",
)


@pytest.mark.parametrize(
    "unsafe_segment",
    [
        "../users",
        "..",
        "%2e%2e",
        "%2e%2e/users",
        "my_study?role=admin",
        "my_study#fragment",
        "my_study\\users",
        "my_study%2Fusers",
    ],
)
def test_build_path_security(unsafe_segment: str) -> None:
    """GenericEndpoint._build_path rejects dot-segments, encoded slashes, ?, #, and \\."""
    client = MagicMock(spec=Client)
    endpoint = MockEndpoint(client)
    endpoint.BASE_PATH = "api/v1/edc/studies"

    with pytest.raises(PathTraversalValidationError):
        endpoint._build_path(unsafe_segment, "subjects")


def test_endpoint_abc_separates_static_prefixes_from_dynamic_segments() -> None:
    """Static BASE_PATH/PATH collapse slashes while dynamic segments reject any slash."""
    client = MagicMock(spec=Client)
    endpoint = MockEndpoint(client)
    endpoint.BASE_PATH = "//api//v1/edc//studies//"
    endpoint.PATH = "//test//nested//"

    assert (
        endpoint._get_endpoint_path("ALLOWED_STUDY", "ITEM_1")
        == "/api/v1/edc/studies/ALLOWED_STUDY/test/nested/ITEM_1"
    )

    with pytest.raises(PathTraversalValidationError):
        endpoint._get_endpoint_path("ALLOWED_STUDY/extra")

    with pytest.raises(PathTraversalValidationError):
        endpoint._get_endpoint_path("ALLOWED_STUDY", "ITEM_1/extra")


@respx.mock
def test_acceptance_criteria_vectors_send_zero_http_requests() -> None:
    """Verify exact acceptance criteria traversal/truncation vectors fail preflight."""
    sdk = ImednetSDK(
        api_key="test-key",
        security_key="test-secret",
        base_url="https://edc.example.test",
    )

    with pytest.raises(PathTraversalValidationError):
        sdk.jobs.get("ALLOWED_STUDY", "../../CANARY_STUDY/records")

    with pytest.raises(PathTraversalValidationError):
        sdk.jobs.get("ALLOWED_STUDY", "%2e%2e/%2e%2e/CANARY_STUDY/records")

    with pytest.raises(PathTraversalValidationError):
        list(sdk.sites.list("CANARY_STUDY/records#"))

    with pytest.raises(PathTraversalValidationError):
        list(sdk.sites.list("CANARY_STUDY/users?"))

    with pytest.raises(PathTraversalValidationError):
        sdk.studies.get("../users")

    assert len(respx.calls) == 0
    sdk.close()


@respx.mock
@pytest.mark.parametrize("ep_attr", SYNC_LIST_ENDPOINTS)
def test_all_sync_list_endpoints_reject_unsafe_study_key(ep_attr: str) -> None:
    """All 12 sync list endpoints reject unsafe study_key values before HTTP dispatch."""
    sdk = ImednetSDK(
        api_key="test-key",
        security_key="test-secret",
        base_url="https://edc.example.test",
    )
    try:
        endpoint = getattr(sdk, ep_attr)
        for unsafe_id in UNSAFE_DYNAMIC_IDENTIFIERS:
            with pytest.raises(PathTraversalValidationError):
                list(endpoint.list(unsafe_id))
        assert len(respx.calls) == 0
    finally:
        sdk.close()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("ep_attr", SYNC_LIST_ENDPOINTS)
async def test_all_async_list_endpoints_reject_unsafe_study_key(ep_attr: str) -> None:
    """All 12 async list endpoints reject unsafe study_key values before HTTP dispatch."""
    async with AsyncImednetSDK(
        api_key="test-key",
        security_key="test-secret",
        base_url="https://edc.example.test",
    ) as sdk:
        endpoint = getattr(sdk, ep_attr)
        for unsafe_id in UNSAFE_DYNAMIC_IDENTIFIERS:
            with pytest.raises(PathTraversalValidationError):
                _ = [item async for item in endpoint.list(unsafe_id)]
        assert len(respx.calls) == 0


@respx.mock
@pytest.mark.parametrize("ep_attr", SYNC_GET_ENDPOINTS)
def test_all_sync_get_endpoints_reject_unsafe_identifiers(ep_attr: str) -> None:
    """All sync get endpoints (including jobs and studies) reject unsafe study_key and item_id."""
    sdk = ImednetSDK(
        api_key="test-key",
        security_key="test-secret",
        base_url="https://edc.example.test",
    )
    try:
        endpoint = getattr(sdk, ep_attr)
        for unsafe_id in UNSAFE_DYNAMIC_IDENTIFIERS:
            if ep_attr == "studies":
                with pytest.raises(PathTraversalValidationError):
                    endpoint.get(unsafe_id)
                with pytest.raises(PathTraversalValidationError):
                    endpoint.get(None, unsafe_id)
            else:
                valid_item_id: int | str = 1 if ep_attr == "subjects" else "VALID_ID"
                with pytest.raises(PathTraversalValidationError):
                    endpoint.get(unsafe_id, valid_item_id)
                with pytest.raises(PathTraversalValidationError):
                    endpoint.get("ALLOWED_STUDY", unsafe_id)
        assert len(respx.calls) == 0
    finally:
        sdk.close()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("ep_attr", SYNC_GET_ENDPOINTS)
async def test_all_async_get_endpoints_reject_unsafe_identifiers(ep_attr: str) -> None:
    """All async get endpoints (including jobs and studies) reject unsafe inputs."""
    async with AsyncImednetSDK(
        api_key="test-key",
        security_key="test-secret",
        base_url="https://edc.example.test",
    ) as sdk:
        endpoint = getattr(sdk, ep_attr)
        for unsafe_id in UNSAFE_DYNAMIC_IDENTIFIERS:
            if ep_attr == "studies":
                with pytest.raises(PathTraversalValidationError):
                    await endpoint.get(unsafe_id)
                with pytest.raises(PathTraversalValidationError):
                    await endpoint.get(None, unsafe_id)
            else:
                valid_item_id: int | str = 1 if ep_attr == "subjects" else "VALID_ID"
                with pytest.raises(PathTraversalValidationError):
                    await endpoint.get(unsafe_id, valid_item_id)
                with pytest.raises(PathTraversalValidationError):
                    await endpoint.get("ALLOWED_STUDY", unsafe_id)
        assert len(respx.calls) == 0


@respx.mock
@pytest.mark.asyncio
async def test_records_create_and_jobs_poll_reject_unsafe_segments() -> None:
    """Sync and async record creation and job polling reject unsafe study_key and batch_id."""
    sdk = ImednetSDK(
        api_key="test-key",
        security_key="test-secret",
        base_url="https://edc.example.test",
    )
    async with AsyncImednetSDK(
        api_key="test-key",
        security_key="test-secret",
        base_url="https://edc.example.test",
    ) as async_sdk:
        try:
            for bad in ("../CANARY", "%2e%2e", "CANARY/records#", "CANARY?x=1", ""):
                with pytest.raises(PathTraversalValidationError):
                    sdk.records.create(bad, [{"formKey": "DEMO", "data": {}}], skip_validation=True)
                with pytest.raises(PathTraversalValidationError):
                    sdk.records.create_batch(bad, [], skip_validation=True)
                with pytest.raises(PathTraversalValidationError):
                    await async_sdk.records.create(
                        bad, [{"formKey": "DEMO", "data": {}}], skip_validation=True
                    )
                with pytest.raises(PathTraversalValidationError):
                    await async_sdk.records.create_batch(bad, [], skip_validation=True)
                with pytest.raises(PathTraversalValidationError):
                    sdk.jobs.poll(bad, "BATCH_1", interval=0, timeout=1)
                with pytest.raises(PathTraversalValidationError):
                    sdk.jobs.poll("ALLOWED_STUDY", bad, interval=0, timeout=1)
                with pytest.raises(PathTraversalValidationError):
                    sdk.jobs.poll_many("ALLOWED_STUDY", [bad], interval=0, timeout=1)
                with pytest.raises(PathTraversalValidationError):
                    await async_sdk.jobs.poll(bad, "BATCH_1", interval=0, timeout=1)
                with pytest.raises(PathTraversalValidationError):
                    await async_sdk.jobs.poll("ALLOWED_STUDY", bad, interval=0, timeout=1)
                with pytest.raises(PathTraversalValidationError):
                    await async_sdk.jobs.poll_many("ALLOWED_STUDY", [bad], interval=0, timeout=1)

            assert len(respx.calls) == 0
        finally:
            sdk.close()


def test_airflow_sensor_and_export_operator_reject_unsafe_segments_before_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ImednetJobSensor.poke and ImednetExportOperator.execute fail preflight before _get_sdk."""
    fail_get_sdk = MagicMock(side_effect=AssertionError("_get_sdk must not be called"))
    monkeypatch.setattr(ImednetJobSensor, "_get_sdk", fail_get_sdk)
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", fail_get_sdk)

    # Construction allows Airflow Jinja templates; validation occurs in poke/execute
    templated_sensor = ImednetJobSensor(
        task_id="templated_sensor",
        study_key="{{ dag_run.conf['study_key'] }}",
        batch_id="{{ ti.xcom_pull(task_ids='submit') }}",
    )
    assert templated_sensor.study_key.startswith("{{")

    sensor = ImednetJobSensor(
        task_id="verify_job_sensor",
        study_key="ALLOWED_STUDY",
        batch_id="../../CANARY_STUDY/records",
    )
    with pytest.raises(PathTraversalValidationError) as exc_info:
        sensor.poke({})
    assert isinstance(exc_info.value, ClientError)

    bad_study_sensor = ImednetJobSensor(
        task_id="verify_job_sensor_study",
        study_key="CANARY_STUDY/records#",
        batch_id="BATCH_1",
    )
    with pytest.raises(PathTraversalValidationError):
        bad_study_sensor.poke({})

    op = ImednetExportOperator(
        task_id="verify_export_op",
        study_key="CANARY_STUDY/users?",
        destination="csv",
        output_path="/tmp/unused_export.csv",
    )
    with pytest.raises(PathTraversalValidationError):
        op.execute({})

    fail_get_sdk.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["jobs", "status", "ALLOWED_STUDY", "../../CANARY_STUDY/records", "--format", "json"],
        ["jobs", "wait", "ALLOWED_STUDY", "%2e%2e/CANARY_STUDY/records", "--format", "json"],
        ["sites", "list", "CANARY_STUDY/records#", "--format", "json"],
        ["records", "list", "CANARY_STUDY/users?", "--format", "json"],
        ["export", "csv", "../CANARY_STUDY", "/tmp/unused.csv"],
        ["datadict", "from-api", "--study-key", "../../CANARY_STUDY"],
        ["workflows", "extract-records", "../CANARY_STUDY"],
        ["workflows", "sync-worker", "../CANARY_STUDY", "--once"],
        ["workflows", "uat", "run", "../CANARY_STUDY"],
        ["subject-data", "../CANARY_STUDY", "SUBJ-1"],
    ],
)
def test_cli_commands_reject_unsafe_segments_before_sdk_dispatch(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """CLI commands reject unsafe study_key and batch_id before creating SDK or HTTP traffic."""
    fail_get_sdk = MagicMock(side_effect=AssertionError("get_sdk must not be called"))
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", fail_get_sdk)
    monkeypatch.setattr("imednet.cli.datadict.get_sdk", fail_get_sdk)

    with pytest.raises(SystemExit) as exc_info:
        cli.app(argv)

    assert exc_info.value.code == 1
    fail_get_sdk.assert_not_called()
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "Error:" in captured.err


def test_uat_execution_engine_rejects_unsafe_study_key_and_batch_id() -> None:
    """UATExecutionEngine raises PathTraversalValidationError instead of classifying as PASS."""
    sdk = MagicMock()
    mock_dd = MagicMock()
    mock_dd.business_logic = []
    engine = UATExecutionEngine(sdk=sdk, data_dictionary=mock_dd)
    engine._rules = [
        {
            "rule_name": "AGE_GT_18",
            "form": "DEMO",
            "variable": "AGE",
            "operator": ">",
            "value": "18",
            "data_type": "integer",
        }
    ]

    with pytest.raises(PathTraversalValidationError):
        engine.run_verification("../CANARY_STUDY")
    sdk.create_record.assert_not_called()

    sdk.create_record.return_value = {"batchId": "../../CANARY_STUDY/records"}
    with pytest.raises(PathTraversalValidationError):
        engine.run_verification("ALLOWED_STUDY")
    sdk.get_job.assert_not_called()
