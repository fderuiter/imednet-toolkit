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
        assert "recordId" in item
        assert "recordOid" not in item
        assert "visitId" in item
        assert "recordStatus" in item
        for envelope_preserved_key in ("recordId", "recordOid", "visitId", "recordStatus"):
            assert envelope_preserved_key not in item["data"]
        for forbidden_key in (
            "study_key",
            "studyKey",
            "record_id",
            "record_oid",
            "record_type",
            "recordType",
            "record_status",
            "deleted",
            "date_created",
            "dateCreated",
            "date_modified",
            "dateModified",
            "last_updated",
            "lastUpdated",
            "visit_id",
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
        "recordId": 900,
        "visitId": 401,
        "recordStatus": "Complete",
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
        "form_key,subject_key,site_name,interval_name,CODE,AGE,NOTES,record_id,reason_for_change\n"
        "F_DM,SUB-10,SITE,Baseline,007,45.0,,999,Initial correction\n",
        encoding="utf-8",
    )

    parsed = csv_to_records(csv_path, schema=schema)
    assert parsed == [
        {
            "recordId": 999,
            "reasonForChange": "Initial correction",
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
        "RecordsInput",
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


def test_tabular_preserves_categorical_and_text_strings_and_serializes_timestamps(
    tmp_path: Path,
) -> None:
    """AC 1 & Req 1: Preserve '1.0' strings with/without schema, scope by study_key, serialize timestamps."""
    from datetime import UTC, date, datetime, time

    from imednet.models.variables import Variable as SdkVariable
    from imednet.utils.tabular import normalize_records_input
    from imednet.validation.cache import SchemaCache

    csv_path = tmp_path / "categorical.csv"
    csv_path.write_text(
        "formKey,subjectKey,CAT_CODE,SCORE_TEXT,VISIT_COUNT,IS_ACTIVE\n"
        "F_ASSESS,SUB-1,1.0,true,7.0,true\n",
        encoding="utf-8",
    )

    # Without schema: all scalar strings like "1.0" and "true" stay exact strings
    untyped = csv_to_records(csv_path, schema=None)
    assert untyped[0]["data"] == {
        "CAT_CODE": "1.0",
        "SCORE_TEXT": "true",
        "VISIT_COUNT": "7.0",
        "IS_ACTIVE": "true",
    }

    # Shared SchemaCache with two studies defining F_ASSESS differently
    cache = SchemaCache()
    cache.populate(
        [
            SdkVariable(
                study_key="STUDY_TEXT",
                form_id=10,
                form_key="F_ASSESS",
                variable_id=1,
                variable_name="CAT_CODE",
                variable_type="radio",
            ),
            SdkVariable(
                study_key="STUDY_TEXT",
                form_id=10,
                form_key="F_ASSESS",
                variable_id=2,
                variable_name="SCORE_TEXT",
                variable_type="text",
            ),
            SdkVariable(
                study_key="STUDY_TEXT",
                form_id=10,
                form_key="F_ASSESS",
                variable_id=3,
                variable_name="VISIT_COUNT",
                variable_type="integer",
            ),
            SdkVariable(
                study_key="STUDY_TEXT",
                form_id=10,
                form_key="F_ASSESS",
                variable_id=4,
                variable_name="IS_ACTIVE",
                variable_type="boolean",
            ),
            SdkVariable(
                study_key="STUDY_NUM",
                form_id=20,
                form_key="F_ASSESS",
                variable_id=5,
                variable_name="CAT_CODE",
                variable_type="float",
            ),
        ],
        study_key=None,
    )

    typed_text_study = csv_to_records(csv_path, schema=cache, study_key="STUDY_TEXT")
    assert typed_text_study[0]["data"] == {
        "CAT_CODE": "1.0",
        "SCORE_TEXT": "true",
        "VISIT_COUNT": 7,
        "IS_ACTIVE": True,
    }

    typed_num_study = csv_to_records(csv_path, schema=cache, study_key="STUDY_NUM")
    assert typed_num_study[0]["data"]["CAT_CODE"] == 1.0
    assert isinstance(typed_num_study[0]["data"]["CAT_CODE"], float)

    # Timestamp serialization across datetime, date, time, and pd.Timestamp
    ts_df = pd.DataFrame(
        [
            {
                "formKey": "F_ASSESS",
                "subjectKey": "SUB-TS",
                "DT_UTC": datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
                "VISIT_DATE": date(2026, 5, 1),
                "VISIT_TIME": time(9, 15, 0),
                "PD_TS": pd.Timestamp("2026-05-02T08:00:00Z"),
            }
        ]
    )
    ts_records = normalize_records_input(ts_df)
    assert ts_records[0]["data"] == {
        "DT_UTC": "2026-05-01T14:30:00+00:00",
        "VISIT_DATE": "2026-05-01",
        "VISIT_TIME": "09:15:00",
        "PD_TS": "2026-05-02T08:00:00+00:00",
    }
    json.dumps(ts_records)  # Guaranteed JSON-serializable


@pytest.mark.asyncio
async def test_record_update_workflow_refreshes_schema_cache_per_study_before_csv_normalization(
    tmp_path: Path,
) -> None:
    """AC 3 & Req 2: RecordUpdateWorkflow refreshes SchemaCache across two Study keys before CSV coercion."""
    from unittest.mock import AsyncMock, MagicMock

    from imednet.models.variables import Variable as SdkVariable

    csv_study_a = tmp_path / "study_a.csv"
    csv_study_a.write_text("formKey,subjectKey,CODE\nF_SHARED,SUB-A,1.0\n", encoding="utf-8")

    csv_study_b = tmp_path / "study_b.csv"
    csv_study_b.write_text("formKey,subjectKey,CODE\nF_SHARED,SUB-B,42.0\n", encoding="utf-8")

    vars_by_study = {
        "STUDY_A": [
            SdkVariable(
                study_key="STUDY_A",
                form_id=101,
                form_key="F_SHARED",
                variable_id=1,
                variable_name="CODE",
                variable_type="text",
            )
        ],
        "STUDY_B": [
            SdkVariable(
                study_key="STUDY_B",
                form_id=202,
                form_key="F_SHARED",
                variable_id=2,
                variable_name="CODE",
                variable_type="integer",
            )
        ],
    }

    # Sync workflow across two studies
    sync_submitted: list[tuple[str, list[dict[str, object]]]] = []
    sync_sdk = MagicMock()
    del sync_sdk.async_create_record
    sync_sdk.get_variables.side_effect = lambda study_key, **_: vars_by_study[study_key]

    def _capture_sync_create(
        study_key: str, records_data: list[dict[str, object]], **_kw: object
    ) -> Job:
        sync_submitted.append((study_key, records_data))
        return Job(jobId=f"J-{study_key}", batchId=f"B-{study_key}", state="COMPLETED")

    sync_sdk.create_record.side_effect = _capture_sync_create
    sync_wf = RecordUpdateWorkflow(sync_sdk)
    sync_wf.create_or_update_records("STUDY_A", csv_study_a)
    sync_wf.create_or_update_records("STUDY_B", csv_study_b)

    assert sync_submitted[0][0] == "STUDY_A"
    assert sync_submitted[0][1][0]["data"] == {"CODE": "1.0"}
    assert isinstance(sync_submitted[0][1][0]["data"]["CODE"], str)  # type: ignore[index]
    assert sync_submitted[1][0] == "STUDY_B"
    assert sync_submitted[1][1][0]["data"] == {"CODE": 42}
    assert isinstance(sync_submitted[1][1][0]["data"]["CODE"], int)  # type: ignore[index]

    # Async batch workflow across two studies
    async_submitted: list[tuple[str, list[dict[str, object]]]] = []
    async_sdk = MagicMock()
    async_sdk.async_get_variables = AsyncMock(
        side_effect=lambda study_key, **_: vars_by_study[study_key]
    )

    async def _capture_async_create(
        study_key: str, records_data: list[dict[str, object]], **_kw: object
    ) -> Job:
        async_submitted.append((study_key, records_data))
        return Job(jobId=f"AJ-{study_key}", batchId=f"AB-{study_key}", state="COMPLETED")

    async_sdk.async_create_record = AsyncMock(side_effect=_capture_async_create)
    async_wf = RecordUpdateWorkflow(async_sdk)
    await async_wf.async_create_or_update_records_batch("STUDY_A", csv_study_a)
    await async_wf.async_create_or_update_records_batch("STUDY_B", csv_study_b)

    assert async_submitted[0][1][0]["data"] == {"CODE": "1.0"}
    assert async_submitted[1][1][0]["data"] == {"CODE": 42}


def test_register_subject_request_optional_identifiers_exclude_none() -> None:
    """Req 6: RegisterSubjectRequest supports subjectKey, subjectOid, and siteId with exclude_none."""
    from imednet.models.records import RegisterSubjectRequest
    from imednet.models.sites import Site
    from imednet.utils.enrollment import (
        normalize_subject_registration_inputs,
        validate_subject_registration_sites,
    )

    req_minimal = RegisterSubjectRequest(formKey="F_DM", siteName="SITE")
    dumped_min = req_minimal.model_dump(by_alias=True)
    assert dumped_min == {"formKey": "F_DM", "siteName": "SITE", "data": {}}
    assert "subjectKey" not in dumped_min
    assert "subjectOid" not in dumped_min
    assert "siteId" not in dumped_min

    req_by_site_id = RegisterSubjectRequest(
        formKey="F_DM",
        siteId=101,
        subjectKey="SUB-500",
        subjectOid="OID-500",
        data={"V_AGE": 33},
    )
    dumped_site_id = req_by_site_id.model_dump(by_alias=True)
    assert dumped_site_id == {
        "formKey": "F_DM",
        "siteId": 101,
        "subjectKey": "SUB-500",
        "subjectOid": "OID-500",
        "data": {"V_AGE": 33},
    }
    assert "siteName" not in dumped_site_id

    pairs = normalize_subject_registration_inputs([req_by_site_id])
    assert pairs[0][0] is req_by_site_id
    assert pairs[0][1].model_dump(by_alias=True) == dumped_site_id

    sites = [Site(studyKey="STUDY", siteId=101, siteName="SITE", siteEnrollmentStatus="Active")]
    validate_subject_registration_sites(pairs, sites)


@pytest.mark.asyncio
async def test_dry_run_without_schema_loads_study_datadict_in_endpoint_and_import_csv(
    tmp_path: Path,
) -> None:
    """Req 5: dry_run=True without schema loads Study Data Dictionary via load_from_api."""
    sdk, _state, captured = _make_sim_sdk()

    bad_form_summary = sdk.records.create_batch(
        "STUDY",
        [{"formKey": "NO_SUCH_FORM", "subjectKey": "SUB1", "data": {"V_AGE": 30}}],
        dry_run=True,
        collect_errors=True,
    )
    assert bad_form_summary.dry_run is True
    assert bad_form_summary.validation_report is not None
    assert bad_form_summary.validation_report.is_valid is False
    assert any("NO_SUCH_FORM" in e.message for e in bad_form_summary.validation_report.errors)
    assert not any(r.method == "POST" for r in captured)

    bad_var_csv = tmp_path / "bad_var.csv"
    bad_var_csv.write_text(
        "formKey,subjectKey,siteName,UNKNOWN_VAR\nF_DM,SUB1,SITE,99\n",
        encoding="utf-8",
    )
    csv_summary = import_records_csv(
        sdk,
        "STUDY",
        bad_var_csv,
        dry_run=True,
        collect_errors=True,
    )
    assert csv_summary.validation_report is not None
    assert csv_summary.validation_report.is_valid is False
    assert any("UNKNOWN_VAR" in e.message for e in csv_summary.validation_report.errors)

    async_sdk, _astate, async_captured = _make_async_sim_sdk()
    async_summary = await async_sdk.records.create_batch(
        "STUDY",
        [{"formKey": "F_DM", "subjectKey": "SUB1", "data": {"NON_EXISTENT": 1}}],
        dry_run=True,
        collect_errors=True,
    )
    assert async_summary.validation_report is not None
    assert async_summary.validation_report.is_valid is False
    assert not any(r.method == "POST" for r in async_captured)


def test_normalize_records_input_and_parse_json_map_record_model_dumps_and_guard_masked() -> None:
    """AC 1 & AC 2: normalize_records_input and parse_json_records_text map Record dumps cleanly."""
    from types import MappingProxyType

    from imednet.cli.utils.write_helpers import parse_json_records_text
    from imednet.models.records import Keyword, Record
    from imednet.utils.tabular import normalize_records_input

    record = Record(
        study_key="STUDY",
        interval_id=10,
        form_id=1,
        form_key="F_DM",
        site_id=100,
        subject_id=200,
        subject_oid="SUBJ-OID-1",
        subject_key="SUB-001",
        record_id=601,
        record_oid="REC-OID-601",
        record_type="SUBJECT",
        record_status="Record Complete",
        deleted=False,
        date_created="2026-01-01 00:00:00",
        date_modified="2026-01-02 00:00:00",
        visit_id=401,
        parent_record_id=500,
        keywords=[Keyword(keyword_name="K", keyword_key="KK", keyword_id=1)],
        record_data={"V_AGE": 42},
    )

    expected = [
        {
            "formKey": "F_DM",
            "recordId": 601,
            "subjectKey": "SUB-001",
            "siteId": 100,
            "intervalId": 10,
            "visitId": 401,
            "recordStatus": "Record Complete",
            "data": {"V_AGE": 42},
        }
    ]

    for raw_dump in (record.model_dump(), record.model_dump(by_alias=True)):
        assert normalize_records_input([raw_dump]) == expected
        assert normalize_records_input((MappingProxyType(raw_dump),)) == expected
        json_text = json.dumps([raw_dump], default=str)
        assert parse_json_records_text(json_text) == expected

    # Dual identifiers across all 5 resource pairs resolve to preferred single identifier
    dual_dict = {
        "form_key": "F_DM",
        "form_id": 1,
        "record_id": 601,
        "record_oid": "REC-OID-601",
        "subject_key": "SUB-001",
        "subject_oid": "SUBJ-OID-1",
        "subject_id": 200,
        "site_name": "SITE-01",
        "site_id": 100,
        "interval_name": "Baseline",
        "interval_id": 10,
        "record_data": {"V_AGE": 42},
    }
    assert normalize_records_input([dual_dict]) == [
        {
            "formKey": "F_DM",
            "recordId": 601,
            "subjectKey": "SUB-001",
            "siteName": "SITE-01",
            "intervalName": "Baseline",
            "data": {"V_AGE": 42},
        }
    ]

    # Empty data defaults to {}
    status_only = normalize_records_input([{"record_id": 601, "record_status": "Complete"}])
    assert status_only == [{"recordId": 601, "recordStatus": "Complete", "data": {}}]

    # Explicit write keyword tags are preserved, while empty/read-only keyword lists are stripped
    uat_tagged = {
        "formKey": "F_DM",
        "siteName": "SITE",
        "data": {"V_AGE": 40},
        "keywords": [{"keywordName": "UAT", "keywordKey": "UAT"}],
    }
    assert normalize_records_input([uat_tagged]) == [uat_tagged]
    assert normalize_records_input([{"formKey": "F_DM", "keywords": []}]) == [
        {"formKey": "F_DM", "data": {}}
    ]

    # Masked sentinel is checked BEFORE stripping read-only columns
    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        normalize_records_input([{"studyKey": MASKED_SENTINEL, "formKey": "F_DM"}])
    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        parse_json_records_text(
            json.dumps([{"formKey": "F_DM", "recordData": {"V_NOTES": MASKED_SENTINEL}}])
        )


def test_subject_registration_form_id_without_schema_and_strict_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4: normalize_subject_registration_inputs preserves formId without schema and passes strict mode."""
    from imednet.datadict import DataDictionary, Form, Metadata, Variable
    from imednet.models.records import BaseRecordRequest, RegisterSubjectRequest
    from imednet.models.subjects import Subject
    from imednet.utils.enrollment import (
        filter_existing_subject_registrations,
        normalize_subject_registration_inputs,
    )

    base_req = BaseRecordRequest(formId=301)
    assert base_req.model_dump(by_alias=True) == {"formId": 301, "data": {}}

    reg_req = RegisterSubjectRequest(formId=301, siteName="SITE")
    assert reg_req.model_dump(by_alias=True) == {"formId": 301, "siteName": "SITE", "data": {}}

    dd = DataDictionary(
        metadata=Metadata.create(study_key="STUDY"),
        forms=[
            Form(
                formId=301,
                formKey="F_DM",
                variables=[
                    Variable(variableName="V_AGE", variableType="integer"),
                    Variable(variableName="CB_CONSENT", variableType="Checkbox"),
                ],
            )
        ],
    )

    for strict_val in ("0", "1"):
        monkeypatch.setenv("IMEDNET_STRICT_MODE", strict_val)

        # Without schema: keeps formId, does NOT emit empty formKey
        no_schema_pairs = normalize_subject_registration_inputs(
            [{"site_name": "SITE", "subject_key": "SUB-1", "record_data": {"V_AGE": 35}}],
            default_form_id=301,
            schema=None,
            study_key="STUDY",
        )
        assert len(no_schema_pairs) == 1
        raw_no_schema = no_schema_pairs[0][0]
        assert isinstance(raw_no_schema, dict)
        assert raw_no_schema == {
            "formId": 301,
            "siteName": "SITE",
            "subjectKey": "SUB-1",
            "data": {"V_AGE": 35},
        }
        assert "formKey" not in raw_no_schema
        assert no_schema_pairs[0][1].model_dump(by_alias=True) == raw_no_schema

        # With schema: resolves formId -> formKey, coerces Checkbox int, and strips formId
        with_schema_pairs = normalize_subject_registration_inputs(
            [
                {
                    "formId": 301,
                    "siteName": "SITE",
                    "subjectKey": "SUB-1",
                    "data": {"V_AGE": 35.0, "CB_CONSENT": 1},
                }
            ],
            schema=dd,
            study_key="STUDY",
        )
        assert len(with_schema_pairs) == 1
        raw_with_schema = with_schema_pairs[0][0]
        assert isinstance(raw_with_schema, dict)
        assert raw_with_schema == {
            "formKey": "F_DM",
            "siteName": "SITE",
            "subjectKey": "SUB-1",
            "data": {"V_AGE": 35, "CB_CONSENT": "1"},
        }
        assert "formId" not in raw_with_schema
        assert with_schema_pairs[0][1].model_dump(by_alias=True) == raw_with_schema

    # Single subject identifier preserved when filtering existing subjects
    dual_subj_req = RegisterSubjectRequest(
        formKey="F_DM",
        siteName="SITE",
        subjectKey="SUB-NEW",
        subjectOid="OID-NEW",
        data={"V_AGE": 40},
    )
    pairs = normalize_subject_registration_inputs([dual_subj_req])
    filtered, skipped = filter_existing_subject_registrations(
        pairs, [Subject(studyKey="STUDY", subjectKey="SUB-EXISTING")]
    )
    assert skipped == 0
    assert filtered == [
        {
            "formKey": "F_DM",
            "siteName": "SITE",
            "subjectKey": "SUB-NEW",
            "data": {"V_AGE": 40},
        }
    ]


@pytest.mark.asyncio
async def test_create_batch_and_import_csv_match_dry_run_coercion_and_refresh_partial_schema_cache(
    tmp_path: Path,
) -> None:
    """AC 5: sdk.records.create_batch and import_records_csv refresh missing Form schemas before coercion."""
    from imednet.models.variables import Variable as ApiVariable
    from imednet.validation.cache import AsyncSchemaCache, SchemaCache

    def _add_checkbox_form(st: SimulationState) -> None:
        st.forms.append(
            {
                "formId": 99,
                "formKey": "F_CHK",
                "formName": "Check Form",
                "formType": "Standard",
                "studyKey": "STUDY",
                "revision": 1,
                "disabled": False,
                "eproForm": False,
                "allowCopy": False,
            }
        )
        st.variables.extend(
            [
                {
                    "variableId": 901,
                    "variableName": "CB_CODE",
                    "variableKey": "CB_CODE",
                    "variableType": "Checkbox",
                    "formId": 99,
                    "formKey": "F_CHK",
                    "studyKey": "STUDY",
                    "label": "Checkbox Code",
                },
                {
                    "variableId": 902,
                    "variableName": "TXT_CODE",
                    "variableKey": "TXT_CODE",
                    "variableType": "Text",
                    "formId": 99,
                    "formKey": "F_CHK",
                    "studyKey": "STUDY",
                    "label": "Text Code",
                },
            ]
        )

    sdk, sync_state, sync_captured = _make_sim_sdk()
    _add_checkbox_form(sync_state)

    df = pd.DataFrame(
        [
            {
                "formKey": "F_CHK",
                "subjectKey": "SUB1",
                "siteName": "SITE",
                "CB_CODE": 1,
                "TXT_CODE": 7,
            }
        ]
    )

    # 1. Dry-run without schema vs live create_batch without schema produce identical coercion
    dry_summary = sdk.records.create_batch("STUDY", df, dry_run=True)
    assert dry_summary.dry_run is True
    assert dry_summary.validation_report is not None and dry_summary.validation_report.is_valid

    sync_captured.clear()
    live_summary = sdk.records.create_batch("STUDY", df)
    assert len(live_summary.batch_ids) == 1
    post_reqs = [r for r in sync_captured if r.method == "POST"]
    assert len(post_reqs) == 1
    assert json.loads(post_reqs[0].content)[0]["data"] == {"CB_CODE": "1", "TXT_CODE": "7"}

    # 2. Partial SchemaCache (populated only with F_DM, missing F_CHK) refreshes before coercion
    dm_vars = [
        ApiVariable.model_validate(v)
        for v in sync_state.variables
        if v.get("studyKey") == "STUDY" and v.get("formKey") == "F_DM"
    ]
    partial_cache = SchemaCache()
    partial_cache.populate(dm_vars, study_key="STUDY")
    assert partial_cache.variables_for_form("F_CHK", study_key="STUDY") == {}

    sync_captured.clear()
    sdk.records.create_batch("STUDY", df, schema=partial_cache)
    post_reqs = [r for r in sync_captured if r.method == "POST"]
    assert len(post_reqs) == 1
    assert json.loads(post_reqs[0].content)[0]["data"] == {"CB_CODE": "1", "TXT_CODE": "7"}

    # 3. import_records_csv with partial SchemaCache refreshes before CSV coercion
    partial_csv_cache = SchemaCache()
    partial_csv_cache.populate(dm_vars, study_key="STUDY")
    csv_path = tmp_path / "chk.csv"
    csv_path.write_text(
        "formKey,subjectKey,siteName,CB_CODE,TXT_CODE\nF_CHK,SUB1,SITE,1,007\n",
        encoding="utf-8",
    )
    sync_captured.clear()
    import_records_csv(sdk, "STUDY", csv_path, schema=partial_csv_cache)
    post_reqs = [r for r in sync_captured if r.method == "POST"]
    assert len(post_reqs) == 1
    assert json.loads(post_reqs[0].content)[0]["data"] == {"CB_CODE": "1", "TXT_CODE": "007"}

    # 4. Async create_batch with partial AsyncSchemaCache refreshes before coercion
    async_sdk, async_state, async_captured = _make_async_sim_sdk()
    _add_checkbox_form(async_state)
    partial_async_cache = AsyncSchemaCache()
    partial_async_cache.populate(dm_vars, study_key="STUDY")
    await async_sdk.records.create_batch("STUDY", df, schema=partial_async_cache)
    async_posts = [r for r in async_captured if r.method == "POST"]
    assert len(async_posts) == 1
    assert json.loads(async_posts[0].content)[0]["data"] == {"CB_CODE": "1", "TXT_CODE": "7"}


def test_checkbox_multiselect_int_coercion_and_inf_nan_rejection(tmp_path: Path) -> None:
    """AC 6: Checkbox & Multi Select coerce non-bool ints to strings; Inf/-Inf/NaN raise ValidationError."""
    from imednet.datadict import Choice, DataDictionary, Form, Metadata, Variable
    from imednet.utils.tabular import normalize_records_input

    dd = DataDictionary(
        metadata=Metadata.create(study_key="STUDY"),
        forms=[
            Form(
                formId=10,
                formKey="F_CHOICE",
                variables=[
                    Variable(
                        variableName="CB_VAR",
                        variableType="Checkbox",
                        choices=[
                            Choice(choiceValue="0", choiceText="No"),
                            Choice(choiceValue="1", choiceText="Yes"),
                        ],
                    ),
                    Variable(
                        variableName="MS_VAR",
                        variableType="Multi Select",
                        choices=[
                            Choice(choiceValue="1", choiceText="Opt 1"),
                            Choice(choiceValue="2", choiceText="Opt 2"),
                        ],
                    ),
                ],
            )
        ],
    )

    df = pd.DataFrame(
        [
            {"formKey": "F_CHOICE", "subjectKey": "S1", "CB_VAR": 1, "MS_VAR": 2},
            {"formKey": "F_CHOICE", "subjectKey": "S2", "CB_VAR": True, "MS_VAR": "1,2"},
        ]
    )
    coerced = dataframe_to_records(df, schema=dd, study_key="STUDY")
    assert coerced[0]["data"] == {"CB_VAR": "1", "MS_VAR": "2"}
    assert coerced[1]["data"] == {"CB_VAR": True, "MS_VAR": "1,2"}
    assert isinstance(coerced[1]["data"]["CB_VAR"], bool)

    dict_coerced = normalize_records_input(
        [{"formKey": "F_CHOICE", "subjectKey": "S1", "data": {"CB_VAR": 0, "MS_VAR": 1}}],
        schema=dd,
        study_key="STUDY",
    )
    assert dict_coerced == [
        {"formKey": "F_CHOICE", "subjectKey": "S1", "data": {"CB_VAR": "0", "MS_VAR": "1"}}
    ]

    # Inf and -Inf in DataFrame cells raise ValidationError (while NaN drops cleanly)
    for non_finite in (float("inf"), float("-inf")):
        bad_df = pd.DataFrame([{"formKey": "F_DM", "subjectKey": "S1", "V_AGE": non_finite}])
        with pytest.raises(ValidationError, match="finite"):
            dataframe_to_records(bad_df)

    # Inf, -Inf, and NaN in dict inputs and RecordCreateOperation raise ValidationError before HTTP
    sdk, _state, captured = _make_sim_sdk()
    for bad_val in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError, match="finite"):
            normalize_records_input([{"formKey": "F_DM", "data": {"V_AGE": bad_val}}])
        with pytest.raises(ValidationError, match="finite"):
            sdk.records.create(
                "STUDY",
                [{"recordId": 601, "data": {"V_AGE": bad_val}}],
                skip_validation=True,
            )
    assert captured == []
