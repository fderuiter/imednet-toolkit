"""Tests for tabular DataFrame/CSV record import, masked PHI rejection, and multi-input writes."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

import imednet.spi.utils as spi_utils
import imednet.spi.validation as spi_validation
import imednet.utils.pandas as pandas_mod
from imednet import AsyncImednetSDK, ImednetSDK, RecordsInput
from imednet.core.client import Client
from imednet.core.context import reset_strict_mode_context, set_strict_mode_context
from imednet.datadict.models import DataDictionary
from imednet.errors import ValidationError
from imednet.models.jobs import Job
from imednet.simulation import SimulationState, SimulationTransport
from imednet.utils.pandas import (
    MASKED_SENTINEL,
    csv_to_records,
    dataframe_to_records,
    import_records_csv,
)
from imednet_workflows.record_update import RecordUpdateWorkflow


@pytest.fixture(autouse=True)
def disable_strict_mode():
    """Ensure contextvar strict mode does not leak across tests."""
    token = set_strict_mode_context(False)
    try:
        yield
    finally:
        reset_strict_mode_context(token)


def _make_sim_sdk() -> tuple[ImednetSDK, SimulationState, list[httpx.Request]]:
    """Build an ImednetSDK wired to an isolated SimulationTransport with request spy."""
    state = SimulationState()
    base_transport = SimulationTransport(state=state)
    captured_requests: list[httpx.Request] = []

    class SpySimulationTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return base_transport.handle_request(request)

    core_client = Client(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-secret",  # pragma: allowlist secret
        base_url="https://edc.sim.imednet.com",
        transport=SpySimulationTransport(),
    )
    sdk = ImednetSDK(client=core_client)
    return sdk, state, captured_requests


def _make_async_sim_sdk() -> tuple[AsyncImednetSDK, SimulationState, list[httpx.Request]]:
    """Build an AsyncImednetSDK wired to an isolated SimulationTransport with request spy."""
    from imednet.core.async_client import AsyncClient

    state = SimulationState()
    base_transport = SimulationTransport(state=state)
    captured_requests: list[httpx.Request] = []

    class SpyAsyncSimulationTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return base_transport.handle_request(request)

    async_client = AsyncClient(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-secret",  # pragma: allowlist secret
        base_url="https://edc.sim.imednet.com",
        transport=SpyAsyncSimulationTransport(),
    )
    sdk = AsyncImednetSDK(async_client=async_client)
    return sdk, state, captured_requests


def test_flattened_dataframe_roundtrip_succeeds_in_simulation_transport() -> None:
    """AC 1: Passing sdk.records.to_dataframe(..., flatten=True, mask_phi=False) to create_batch succeeds."""
    sdk, state, captured_requests = _make_sim_sdk()

    initial_records = state.get_records("STUDY")
    initial_revisions = state.get_record_revisions("STUDY")
    assert len(initial_records) >= 1

    df = sdk.records.to_dataframe("STUDY", flatten=True, mask_phi=False)
    assert not df.empty
    assert "record_id" in df.columns
    assert "study_key" in df.columns

    summary = sdk.records.create_batch("STUDY", df)

    assert summary.total_records == len(df)
    assert len(summary.batch_ids) == 1

    post_requests = [r for r in captured_requests if r.method == "POST"]
    assert len(post_requests) == 1
    sent_payload = json.loads(post_requests[0].content)
    assert isinstance(sent_payload, list)
    assert len(sent_payload) == len(df)

    for item in sent_payload:
        assert "data" in item
        assert isinstance(item["data"], dict)
        for forbidden_key in (
            "study_key",
            "studyKey",
            "record_id",
            "recordId",
            "record_oid",
            "recordOid",
            "record_type",
            "recordType",
            "record_status",
            "recordStatus",
            "deleted",
            "date_created",
            "dateCreated",
            "date_modified",
            "dateModified",
            "last_updated",
            "lastUpdated",
            "visit_id",
            "visitId",
            "parent_record_id",
            "parentRecordId",
            "keywords",
        ):
            assert forbidden_key not in item
            assert forbidden_key not in item["data"]

    updated_records = state.get_records("STUDY")
    updated_revisions = state.get_record_revisions("STUDY")
    assert len(updated_records) == len(initial_records)
    assert len(updated_revisions) == len(initial_revisions) + len(df)


def test_csv_with_masked_sentinel_raises_validation_error_before_any_http_request(
    tmp_path: Path,
) -> None:
    """AC 2: Importing a CSV containing ***MASKED*** raises ValidationError before HTTP traffic."""
    sdk, _state, captured_requests = _make_sim_sdk()
    csv_file = tmp_path / "masked_export.csv"
    csv_file.write_text(
        f"form_key,subject_key,site_name,AGE,INITIALS\nF_DM,SUB1,SITE,45,{MASKED_SENTINEL}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        sdk.records.create_batch("STUDY", csv_file)
    assert captured_requests == []

    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        sdk.records.create("STUDY", csv_file, skip_validation=True)
    assert captured_requests == []

    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        import_records_csv(sdk, "STUDY", csv_file)
    assert captured_requests == []


def test_record_create_operation_rejects_masked_dict_payload_before_http() -> None:
    """RecordCreateOperation also blocks ***MASKED*** in direct list[dict] payloads."""
    sdk, _state, captured_requests = _make_sim_sdk()
    payload = [
        {
            "formKey": "F_DM",
            "subjectKey": "SUB1",
            "data": {"AGE": 42, "NOTES": f"Patient {MASKED_SENTINEL}"},
        }
    ]

    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        sdk.records.create("STUDY", payload, skip_validation=True)
    assert captured_requests == []


def test_dataframe_to_records_identifier_mapping_nan_drop_and_float_coercion() -> None:
    """Req 1 & 2: Identifier mapping, NaN/empty drop, integer float coercion, metadata stripping."""
    df = pd.DataFrame(
        [
            {
                "study_key": "STUDY",
                "record_id": 900,
                "record_oid": "OID-900",
                "record_type": "Standard",
                "record_status": "Complete",
                "deleted": False,
                "date_created": "2026-01-01T00:00:00Z",
                "date_modified": "2026-01-02T00:00:00Z",
                "last_updated": "2026-01-02T00:00:00Z",
                "visit_id": 401.0,
                "parent_record_id": None,
                "keywords": ["tag"],
                "form_id": 301.0,
                "subject_oid": "SUBJ-OID-1",
                "site_id": 101.0,
                "interval_id": 501.0,
                "AGE": 45.0,
                "TEMP": 98.6,
                "EMPTY_COL": float("nan"),
                "BLANK_STR": "   ",
            },
            {
                "study_key": None,
                "record_id": float("nan"),
                "record_oid": "",
                "record_type": None,
                "record_status": None,
                "deleted": None,
                "date_created": None,
                "date_modified": None,
                "date_updated": None,
                "last_updated": None,
                "visit_id": float("nan"),
                "parent_record_id": None,
                "keywords": None,
                "form_id": float("nan"),
                "subject_oid": "",
                "site_id": float("nan"),
                "interval_id": float("nan"),
                "AGE": float("nan"),
                "TEMP": float("nan"),
                "EMPTY_COL": None,
                "BLANK_STR": "",
            },
        ]
    )

    records = dataframe_to_records(df)
    assert len(records) == 1
    rec = records[0]
    assert rec == {
        "formId": 301,
        "subjectOid": "SUBJ-OID-1",
        "siteId": 101,
        "intervalId": 501,
        "data": {"AGE": 45, "TEMP": 98.6},
    }
    assert isinstance(rec["data"]["AGE"], int)
    assert isinstance(rec["data"]["TEMP"], float)


def test_csv_to_records_and_import_records_csv_work_without_pandas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req 1: csv_to_records and import_records_csv use stdlib CSV when pandas is absent."""
    monkeypatch.setattr(pandas_mod, "pd", None)

    schema = DataDictionary.model_validate(
        {
            "forms": [
                {
                    "formKey": "F_DM",
                    "variables": [
                        {"variableName": "CODE", "variableType": "text"},
                        {"variableName": "AGE", "variableType": "integer"},
                    ],
                }
            ]
        }
    )

    csv_path = tmp_path / "records.csv"
    csv_path.write_text(
        "form_key,subject_key,site_name,interval_name,CODE,AGE,NOTES,record_id\n"
        "F_DM,SUB-10,SITE,Baseline,007,45.0,,999\n",
        encoding="utf-8",
    )

    parsed = csv_to_records(csv_path, schema=schema)
    assert parsed == [
        {
            "formKey": "F_DM",
            "subjectKey": "SUB-10",
            "siteName": "SITE",
            "intervalName": "Baseline",
            "data": {"CODE": "007", "AGE": 45},
        }
    ]

    sdk, _state, captured = _make_sim_sdk()
    summary = import_records_csv(sdk, "STUDY", csv_path, schema=schema)
    assert summary.total_records == 1
    assert any(r.method == "POST" for r in captured)


@pytest.mark.asyncio
async def test_multi_input_acceptance_across_endpoints_convenience_and_workflows(
    tmp_path: Path,
) -> None:
    """Req 4: Endpoints, SDK convenience, and RecordUpdateWorkflow accept DataFrame, CSV, and list[dict]."""
    csv_path = tmp_path / "batch.csv"
    csv_path.write_text(
        "formKey,subjectKey,siteName,V_AGE\nF_DM,SUB1,SITE,51\n",
        encoding="utf-8",
    )
    df = pd.DataFrame(
        [{"form_key": "F_DM", "subject_key": "SUB1", "site_name": "SITE", "V_AGE": 52.0}]
    )
    dict_list: list[dict[str, object]] = [
        {"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "data": {"V_AGE": 53}}
    ]

    # Sync SDK convenience & endpoint
    sdk, _state, _captured = _make_sim_sdk()
    inputs: list[RecordsInput] = [csv_path, str(csv_path), df, dict_list]  # type: ignore[list-item]
    for inp in inputs:
        job = sdk.create_record("STUDY", inp)
        assert isinstance(job, Job)
        summary = sdk.create_records_batch("STUDY", inp)
        assert summary.total_records == 1

    # Async SDK convenience & endpoint
    async_sdk, _astate, _acaptured = _make_async_sim_sdk()
    for inp in inputs:
        ajob = await async_sdk.async_create_record("STUDY", inp)
        assert isinstance(ajob, Job)
        asummary = await async_sdk.async_create_records_batch("STUDY", inp)
        assert asummary.total_records == 1

    # RecordUpdateWorkflow sync & async
    wf = RecordUpdateWorkflow(sdk)
    wf_job = wf.create_or_update_records("STUDY", df)
    assert isinstance(wf_job, Job)
    wf_batch = wf.create_or_update_records_batch("STUDY", csv_path)
    assert wf_batch.total_records == 1

    awf = RecordUpdateWorkflow(async_sdk)
    res_job = await awf.async_create_or_update_records("STUDY", csv_path)
    assert isinstance(res_job, Job)
    assert res_job.batch_id
    res_summary = await awf.async_create_or_update_records_batch("STUDY", df)
    assert res_summary.total_records == 1


def test_spi_reexports_all_tabular_enrollment_and_validation_helpers() -> None:
    """Req 9: spi.utils and spi.validation expose all new helpers."""
    expected_utils = {
        "MASKED_SENTINEL",
        "NormalizedRecordBatch",
        "RecordsInput",
        "build_site_validation_hook",
        "csv_to_records",
        "dataframe_to_records",
        "import_records_csv",
        "ensure_no_masked_sentinel",
        "normalize_records_input",
        "normalize_tabular_row",
        "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
        "is_site_enrollment_eligible",
        "extract_candidate_subject_key",
        "normalize_subject_registration_inputs",
        "validate_subject_registration_sites",
        "filter_existing_subject_registrations",
    }
    for name in expected_utils:
        assert hasattr(spi_utils, name), f"Missing {name} in imednet.spi.utils"

    expected_validation = {
        "BatchValidationReport",
        "RecordValidationIssue",
        "validate_records_batch",
    }
    for name in expected_validation:
        assert hasattr(spi_validation, name), f"Missing {name} in imednet.spi.validation"


@pytest.mark.asyncio
async def test_two_masked_records_dry_run_collect_errors_returns_error_count_two(
    tmp_path: Path,
) -> None:
    """AC 1: Sync and async batch entrypoints return errorCount == 2 for two masked records."""
    sdk, _state, sync_requests = _make_sim_sdk()
    async_sdk, _astate, async_requests = _make_async_sim_sdk()

    two_masked_dicts = [
        {"formKey": "F_DM", "subjectKey": "SUB1", "data": {"V_AGE": 40, "NOTES": MASKED_SENTINEL}},
        {"formKey": "F_DM", "subjectKey": "SUB2", "data": {"V_AGE": 41, "NOTES": MASKED_SENTINEL}},
    ]
    csv_path = tmp_path / "two_masked.csv"
    csv_path.write_text(
        "form_key,subject_key,site_name,V_AGE,NOTES\n"
        f"F_DM,SUB1,SITE,40,{MASKED_SENTINEL}\n"
        f"F_DM,SUB2,SITE,41,{MASKED_SENTINEL}\n",
        encoding="utf-8",
    )
    dd = DataDictionary.model_validate(
        {
            "forms": [
                {
                    "formKey": "F_DM",
                    "variables": [
                        {"variableName": "V_AGE", "variableType": "integer"},
                        {"variableName": "NOTES", "variableType": "text"},
                    ],
                }
            ]
        }
    )

    try:
        for schema_arg in (None, dd):
            s1 = sdk.records.create_batch(
                "STUDY",
                two_masked_dicts,
                schema=schema_arg,
                dry_run=True,
                collect_errors=True,
            )
            assert s1.validation_report is not None
            assert s1.validation_report.to_dict()["errorCount"] == 2

            s2 = sdk.create_records_batch(
                "STUDY",
                two_masked_dicts,
                schema=schema_arg,
                dry_run=True,
                collect_errors=True,
            )
            assert s2.validation_report is not None
            assert s2.validation_report.to_dict()["errorCount"] == 2

            s3 = import_records_csv(
                sdk,
                "STUDY",
                csv_path,
                schema=schema_arg,
                dry_run=True,
                collect_errors=True,
            )
            assert s3.validation_report is not None
            assert s3.validation_report.to_dict()["errorCount"] == 2

            a1 = await async_sdk.records.create_batch(
                "STUDY",
                two_masked_dicts,
                schema=schema_arg,
                dry_run=True,
                collect_errors=True,
            )
            assert a1.validation_report is not None
            assert a1.validation_report.to_dict()["errorCount"] == 2

            a2 = await async_sdk.async_create_records_batch(
                "STUDY",
                csv_path,
                schema=schema_arg,
                dry_run=True,
                collect_errors=True,
            )
            assert a2.validation_report is not None
            assert a2.validation_report.to_dict()["errorCount"] == 2

        assert not any(r.method == "POST" for r in sync_requests)
        assert not any(r.method == "POST" for r in async_requests)
    finally:
        sdk.close()
        await async_sdk.aclose()


def test_csv_without_form_header_with_default_form_key_preserves_text_strings(
    tmp_path: Path,
) -> None:
    """AC 3 & Req 2/3: default_form_key/default_form_id resolve text types before scalar coercion."""
    dd = DataDictionary.model_validate(
        {
            "forms": [
                {
                    "formKey": "F_DM",
                    "formId": "301",
                    "variables": [
                        {"variableName": "LOT_CODE", "variableType": "text"},
                        {"variableName": "FLAG_TEXT", "variableType": "text"},
                        {"variableName": "V_AGE", "variableType": "integer"},
                    ],
                }
            ]
        }
    )
    csv_no_form = tmp_path / "no_form.csv"
    csv_no_form.write_text(
        "subject_key,site_name,LOT_CODE,FLAG_TEXT,V_AGE\nSUB1,SITE,12345,true,42.0\n",
        encoding="utf-8",
    )

    by_key = csv_to_records(csv_no_form, schema=dd, default_form_key="F_DM")
    assert by_key == [
        {
            "formKey": "F_DM",
            "subjectKey": "SUB1",
            "siteName": "SITE",
            "data": {"LOT_CODE": "12345", "FLAG_TEXT": "true", "V_AGE": 42},
        }
    ]
    assert isinstance(by_key[0]["data"]["LOT_CODE"], str)
    assert isinstance(by_key[0]["data"]["FLAG_TEXT"], str)

    by_id = csv_to_records(csv_no_form, schema=dd, default_form_id=301)
    assert by_id[0]["formId"] == 301
    assert by_id[0]["data"]["LOT_CODE"] == "12345"
    assert by_id[0]["data"]["FLAG_TEXT"] == "true"

    sdk, _state, captured = _make_sim_sdk()
    try:
        summary = import_records_csv(
            sdk,
            "STUDY",
            csv_no_form,
            schema=dd,
            default_form_key="F_DM",
        )
        assert summary.total_records == 1
        post_reqs = [r for r in captured if r.method == "POST"]
        sent = json.loads(post_reqs[0].content)
        assert sent[0]["formKey"] == "F_DM"
        assert sent[0]["data"]["LOT_CODE"] == "12345"
        assert sent[0]["data"]["FLAG_TEXT"] == "true"
    finally:
        sdk.close()


@pytest.mark.asyncio
async def test_multi_study_schema_cache_disambiguates_forms_during_normalization() -> None:
    """AC 4 & Req 5: dataframe_to_records, records.create_batch, and RecordUpdateWorkflow pass study_key."""
    from imednet.models.variables import Variable
    from imednet.validation.cache import AsyncSchemaCache, SchemaCache

    vars_multi = [
        Variable(
            studyKey="STUDY",
            formId=301,
            formKey="F_DM",
            variableName="LOT_CODE",
            variableType="text",
        ),
        Variable(
            studyKey="OTHER_STUDY",
            formId=301,
            formKey="F_DM",
            variableName="LOT_CODE",
            variableType="integer",
        ),
    ]
    cache = SchemaCache()
    cache.populate(vars_multi)
    async_cache = AsyncSchemaCache()
    async_cache.populate(vars_multi)

    # Sanity check: without study_key, BaseSchemaCache raises Ambiguous form
    with pytest.raises(ValidationError, match="Ambiguous form"):
        cache.variables_for_form("F_DM")

    df = pd.DataFrame(
        [{"form_key": "F_DM", "subject_key": "SUB1", "site_name": "SITE", "LOT_CODE": 12345.0}]
    )
    study_a_records = dataframe_to_records(df, schema=cache, study_key="STUDY")
    assert study_a_records[0]["data"]["LOT_CODE"] == "12345"

    study_b_records = dataframe_to_records(df, schema=cache, study_key="OTHER_STUDY")
    assert study_b_records[0]["data"]["LOT_CODE"] == 12345

    sdk, _state, _req = _make_sim_sdk()
    async_sdk, _astate, _areq = _make_async_sim_sdk()
    try:
        batch_res = sdk.records.create_batch("STUDY", df, schema=cache)
        assert batch_res.total_records == 1

        abatch_res = await async_sdk.records.create_batch("STUDY", df, schema=async_cache)
        assert abatch_res.total_records == 1

        wf = RecordUpdateWorkflow(sdk, schema=cache)
        wf_res = wf.create_or_update_records_batch("STUDY", df)
        assert wf_res.total_records == 1

        awf = RecordUpdateWorkflow(async_sdk, schema=async_cache)
        awf_res = await awf.async_create_or_update_records_batch("STUDY", df)
        assert awf_res.total_records == 1
    finally:
        sdk.close()
        await async_sdk.aclose()
