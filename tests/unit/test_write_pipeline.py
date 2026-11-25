"""Tests for WriteIngestionPipeline, deferred schema binding, and Subject enrollment."""

from __future__ import annotations

import datetime as dt
import importlib
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest

from imednet import AsyncImednetSDK, ImednetSDK, cli
from imednet.cli.utils.write_helpers import parse_key_value_pairs
from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.context import reset_strict_mode_context, set_strict_mode_context
from imednet.datadict.models import (
    Choice,
    DataDictionary,
    Metadata,
)
from imednet.datadict.models import (
    Form as DictForm,
)
from imednet.datadict.models import (
    Variable as DictVariable,
)
from imednet.errors import ValidationError
from imednet.models.jobs import Job
from imednet.models.records import RegisterSubjectRequest
from imednet.models.sites import Site
from imednet.models.variables import Variable
from imednet.utils.enrollment import normalize_subject_registration_inputs
from imednet.utils.tabular import csv_to_records, dataframe_to_records, normalize_records_input
from imednet.utils.write_pipeline import (
    MASKED_SENTINEL,
    RawRecordEnvelope,
    WriteIngestionPipeline,
)
from imednet.validation.cache import AsyncSchemaCache, SchemaCache
from imednet_workflows.record_update import RecordUpdateWorkflow
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow


class _CliResult:
    """Captured CLI invocation result."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _invoke_cli(capsys: pytest.CaptureFixture[str], args: list[str]) -> _CliResult:
    capsys.readouterr()
    exit_code = 0
    try:
        cli.app(args)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return _CliResult(exit_code, captured.out, captured.err)


@pytest.fixture(autouse=True)
def disable_strict_mode(monkeypatch: pytest.MonkeyPatch):
    """Ensure contextvar strict mode and CLI env do not leak across tests."""
    token = set_strict_mode_context(False)
    monkeypatch.setenv("IMEDNET_API_KEY", "test-key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-secret")  # pragma: allowlist secret
    importlib.reload(cli)
    try:
        yield
    finally:
        importlib.reload(cli)
        reset_strict_mode_context(token)


def _populate_study_schema(
    cache: SchemaCache | AsyncSchemaCache,
    *,
    study_key: str = "STUDY_A",
    form_id: int = 101,
    form_key: str = "DEMOG",
    variables: list[tuple[str, str]] | None = None,
) -> None:
    """Populate a SchemaCache or AsyncSchemaCache via its public populate API."""
    var_specs = variables or [
        ("CONSENT", "Radio"),
        ("YN_CHOICE", "Yes/No"),
        ("DROPDOWN_CODE", "Dropdown"),
        ("SINGLE_CHOICE", "Single Select"),
        ("MULTI_CHOICE", "Multi Select"),
        ("CHECK_CHOICE", "Checkbox"),
        ("CODED_VAR", "Coded"),
        ("PATIENT_CODE", "Text"),
        ("MEMO_NOTE", "Memo"),
        ("IS_ACTIVE", "Logic"),
        ("AGE", "Integer"),
        ("DOSE", "Numeric"),
        ("VISIT_DATE", "Date"),
    ]
    var_models = [
        Variable.model_validate(
            {
                "studyKey": study_key,
                "formId": form_id,
                "formKey": form_key,
                "variableId": idx + 1,
                "variableName": name,
                "variableType": vtype,
            }
        )
        for idx, (name, vtype) in enumerate(var_specs)
    ]
    cache.populate(var_models, study_key=study_key)


def _build_datadict() -> DataDictionary:
    """Build an offline DataDictionary with categorical, string, logic, numeric, and date fields."""
    choices = [
        Choice(choiceValue="true", choiceText="True Option"),
        Choice(choiceValue="false", choiceText="False Option"),
        Choice(choiceValue="01", choiceText="Code 01"),
    ]
    return DataDictionary(
        metadata=Metadata(studyKey="STUDY_A"),
        forms=[
            DictForm(
                formKey="DEMOG",
                formName="Demographics",
                formId=101,
                variables=[
                    DictVariable(
                        variableName="CONSENT",
                        variableType="Radio",
                        choices=choices,
                    ),
                    DictVariable(
                        variableName="YN_CHOICE",
                        variableType="Yes/No",
                        choices=choices,
                    ),
                    DictVariable(
                        variableName="PATIENT_CODE",
                        variableType="String",
                    ),
                    DictVariable(
                        variableName="IS_ACTIVE",
                        variableType="Logic",
                    ),
                    DictVariable(
                        variableName="AGE",
                        variableType="Integer",
                    ),
                    DictVariable(
                        variableName="DOSE",
                        variableType="Float",
                    ),
                    DictVariable(
                        variableName="VISIT_DATE",
                        variableType="Date",
                    ),
                ],
            )
        ],
    )


def test_stage1_raw_envelopes_preserve_uncoerced_values_across_all_formats(
    tmp_path: Path,
) -> None:
    """Stage 1 parses CSV, JSON, Parquet, DataFrame, stdin, and CLI -d into uncoerced envelopes."""
    csv_path = tmp_path / "records.csv"
    csv_path.write_text(
        "formKey,subjectKey,CONSENT,YN_CHOICE,PATIENT_CODE,AGE\nDEMOG,SUB-01,true,false,007,42\n",
        encoding="utf-8",
    )
    json_path = tmp_path / "records.json"
    json_path.write_text(
        json.dumps(
            [
                {
                    "formKey": "DEMOG",
                    "subjectKey": "SUB-01",
                    "data": {
                        "CONSENT": "true",
                        "YN_CHOICE": "false",
                        "PATIENT_CODE": "007",
                        "AGE": "42",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    df = pd.DataFrame(
        [
            {
                "formKey": "DEMOG",
                "subjectKey": "SUB-01",
                "CONSENT": "true",
                "YN_CHOICE": "false",
                "PATIENT_CODE": "007",
                "AGE": "42",
            }
        ]
    )
    parquet_path = tmp_path / "records.parquet"
    df.to_parquet(parquet_path, index=False)

    csv_envelopes = WriteIngestionPipeline.read_input(csv_path)
    json_envelopes = WriteIngestionPipeline.read_input(json_path)
    parquet_envelopes = WriteIngestionPipeline.read_input(parquet_path)
    df_envelopes = WriteIngestionPipeline.read_input(df)
    cli_envelopes = WriteIngestionPipeline.read_cli_data_pairs(
        ["CONSENT=true", "YN_CHOICE=false", "PATIENT_CODE=007", "AGE=42"],
        envelope_overrides={"formKey": "DEMOG", "subjectKey": "SUB-01"},
    )
    stdin_envelopes = WriteIngestionPipeline.read_stdin(
        io.StringIO(
            json.dumps(
                [
                    {
                        "formKey": "DEMOG",
                        "subjectKey": "SUB-01",
                        "data": {
                            "CONSENT": "true",
                            "YN_CHOICE": "false",
                            "PATIENT_CODE": "007",
                            "AGE": "42",
                        },
                    }
                ]
            )
        )
    )

    for source_name, envs in [
        ("csv", csv_envelopes),
        ("json", json_envelopes),
        ("parquet", parquet_envelopes),
        ("dataframe", df_envelopes),
        ("cli", cli_envelopes),
        ("stdin", stdin_envelopes),
    ]:
        assert len(envs) == 1, source_name
        env = envs[0]
        assert isinstance(env, RawRecordEnvelope), source_name
        # Stage 1 MUST NOT coerce 'true', 'false', '007', or '42' before schema binding
        assert env.data["CONSENT"] == "true" and isinstance(env.data["CONSENT"], str), source_name
        assert env.data["YN_CHOICE"] == "false" and isinstance(env.data["YN_CHOICE"], str), (
            source_name
        )
        assert env.data["PATIENT_CODE"] == "007", source_name
        assert env.data["AGE"] == "42", source_name

    # parse_key_value_pairs also preserves raw strings when unschematized before deferred binding
    raw_cli_data = parse_key_value_pairs(["CONSENT=true", "YN_CHOICE=false", "CODE=007"])
    assert raw_cli_data == {"CONSENT": "true", "YN_CHOICE": "false", "CODE": "007"}


def test_stage1_rejects_masked_phi_sentinel_across_all_sources_before_binding(
    tmp_path: Path,
) -> None:
    """***MASKED*** sentinels fail immediately during Stage 1 RawRecordEnvelope scanning."""
    csv_path = tmp_path / "masked.csv"
    csv_path.write_text(
        f"formKey,subjectKey,PATIENT_CODE\nDEMOG,SUB-1,{MASKED_SENTINEL}\n",
        encoding="utf-8",
    )
    json_path = tmp_path / "masked.json"
    json_path.write_text(
        json.dumps([{"formKey": "DEMOG", "subjectKey": MASKED_SENTINEL, "data": {"AGE": 30}}]),
        encoding="utf-8",
    )
    parquet_path = tmp_path / "masked.parquet"
    pd.DataFrame([{"formKey": "DEMOG", "NOTE": MASKED_SENTINEL}]).to_parquet(
        parquet_path, index=False
    )

    for reader_call in [
        lambda: WriteIngestionPipeline.read_input(csv_path),
        lambda: WriteIngestionPipeline.read_input(json_path),
        lambda: WriteIngestionPipeline.read_input(parquet_path),
        lambda: WriteIngestionPipeline.read_input(
            pd.DataFrame([{"formKey": "DEMOG", "NOTE": MASKED_SENTINEL}])
        ),
        lambda: WriteIngestionPipeline.read_input(
            [{"formKey": "DEMOG", "data": {"NOTE": MASKED_SENTINEL}}]
        ),
        lambda: WriteIngestionPipeline.read_cli_data_pairs([f"NOTE={MASKED_SENTINEL}"]),
    ]:
        with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
            reader_call()


@pytest.mark.parametrize("schema_kind", ["schema_cache", "datadict"])
def test_stage2_binds_categorical_and_string_variables_without_miscoercion(
    tmp_path: Path,
    schema_kind: str,
) -> None:
    """Choice ('true', 'false', '01') and String ('007') variables bind without miscoercion."""
    if schema_kind == "schema_cache":
        schema: Any = SchemaCache()
        _populate_study_schema(schema, study_key="STUDY_A")
    else:
        schema = _build_datadict()

    csv_path = tmp_path / "categorical.csv"
    csv_path.write_text(
        "formKey,subjectKey,CONSENT,YN_CHOICE,PATIENT_CODE,IS_ACTIVE,AGE,DOSE,VISIT_DATE\n"
        "DEMOG,SUB-01,true,false,007,true,42,12.5,2026-03-15\n",
        encoding="utf-8",
    )
    bound_csv = normalize_records_input(csv_path, schema=schema, study_key="STUDY_A")
    assert len(bound_csv) == 1
    data = bound_csv[0]["data"]
    assert data["CONSENT"] == "true" and isinstance(data["CONSENT"], str)
    assert data["YN_CHOICE"] == "false" and isinstance(data["YN_CHOICE"], str)
    assert data["PATIENT_CODE"] == "007" and isinstance(data["PATIENT_CODE"], str)
    assert data["IS_ACTIVE"] is True
    assert data["AGE"] == 42 and isinstance(data["AGE"], int)
    assert data["DOSE"] == 12.5 and isinstance(data["DOSE"], float)
    assert data["VISIT_DATE"] == "2026-03-15"

    # Also verify CLI -d assignment binding with 'true' and 'false' on categorical variables
    cli_envelopes = WriteIngestionPipeline.read_cli_data_pairs(
        [
            "CONSENT=true",
            "YN_CHOICE=false",
            "PATIENT_CODE=007",
            "IS_ACTIVE=false",
            "AGE=35",
        ],
        envelope_overrides={"formKey": "DEMOG", "subjectKey": "SUB-02"},
    )
    bound_cli = WriteIngestionPipeline.bind(cli_envelopes, schema=schema, study_key="STUDY_A")
    cli_data = bound_cli[0]["data"]
    assert cli_data["CONSENT"] == "true" and isinstance(cli_data["CONSENT"], str)
    assert cli_data["YN_CHOICE"] == "false" and isinstance(cli_data["YN_CHOICE"], str)
    assert cli_data["PATIENT_CODE"] == "007"
    assert cli_data["IS_ACTIVE"] is False
    assert cli_data["AGE"] == 35


def test_timestamps_dates_and_decimals_serialize_cleanly_in_dataframe_and_dict_payloads() -> None:
    """Timestamp, datetime, date, time, and Decimal serialize cleanly and pass SchemaCache."""
    schema = SchemaCache()
    _populate_study_schema(
        schema,
        study_key="STUDY_A",
        variables=[
            ("VISIT_DATE", "Date"),
            ("EVENT_TS", "Text"),
            ("DOSE", "Numeric"),
            ("COUNT", "Integer"),
            ("TIME_STR", "Text"),
        ],
    )

    ts_date = pd.Timestamp("2026-04-10")
    ts_utc = pd.Timestamp("2026-04-10 15:45:30+00:00")
    py_dt = dt.datetime(2026, 4, 10, 9, 30, tzinfo=dt.UTC)
    py_date = dt.date(2026, 4, 10)
    py_time = dt.time(8, 15, 0)

    df = pd.DataFrame(
        [
            {
                "formKey": "DEMOG",
                "subjectKey": "SUB-DF",
                "VISIT_DATE": ts_date,
                "EVENT_TS": ts_utc,
                "DOSE": Decimal("19.75"),
                "COUNT": Decimal(12),
                "TIME_STR": py_time,
            }
        ]
    )
    dict_payload = [
        {
            "formKey": "DEMOG",
            "subjectKey": "SUB-DICT",
            "data": {
                "VISIT_DATE": py_date,
                "EVENT_TS": py_dt,
                "DOSE": Decimal("19.75"),
                "COUNT": Decimal(12),
                "TIME_STR": py_time,
            },
        }
    ]

    bound_df = dataframe_to_records(df, schema=schema, study_key="STUDY_A")
    bound_dict = normalize_records_input(dict_payload, schema=schema, study_key="STUDY_A")

    for bound in (bound_df, bound_dict):
        rec = bound[0]
        # Must pass json.dumps without custom encoder
        encoded = json.dumps(rec)
        decoded = json.loads(encoded)
        assert decoded["data"]["VISIT_DATE"] == "2026-04-10"
        assert decoded["data"]["DOSE"] == 19.75
        assert decoded["data"]["COUNT"] == 12
        assert decoded["data"]["TIME_STR"] == "08:15:00"
        # Must pass SchemaCache validation
        dry_sdk = ImednetSDK(
            client=Client(
                api_key="k",  # pragma: allowlist secret
                security_key="s",  # pragma: allowlist secret
                base_url="https://edc.example.test",
            )
        )
        dry_summary = dry_sdk.records.create_batch("STUDY_A", [rec], schema=schema, dry_run=True)
        assert dry_summary.validation_report is not None
        assert dry_summary.validation_report.is_valid


def test_multi_study_schema_cache_resolves_shared_form_keys_and_ids_by_study_key() -> None:
    """Multi-Study SchemaCache resolves colliding formKey and formId values using study_key."""
    schema = SchemaCache()
    # STUDY_A: DEMOG (formId=101) has FLAG as Dropdown (categorical string) and formId=500 -> VITALS_A
    schema.populate(
        [
            Variable.model_validate(
                {
                    "studyKey": "STUDY_A",
                    "formId": 101,
                    "formKey": "DEMOG",
                    "variableId": 1,
                    "variableName": "FLAG",
                    "variableType": "Dropdown",
                }
            ),
            Variable.model_validate(
                {
                    "studyKey": "STUDY_A",
                    "formId": 500,
                    "formKey": "VITALS_A",
                    "variableId": 2,
                    "variableName": "CODE",
                    "variableType": "Coded",
                }
            ),
        ],
        study_key="STUDY_A",
    )

    # STUDY_B: DEMOG (formId=202) has FLAG as Logic (boolean) and formId=500 -> VITALS_B
    schema.populate(
        [
            Variable.model_validate(
                {
                    "studyKey": "STUDY_B",
                    "formId": 202,
                    "formKey": "DEMOG",
                    "variableId": 1,
                    "variableName": "FLAG",
                    "variableType": "Logic",
                }
            ),
            Variable.model_validate(
                {
                    "studyKey": "STUDY_B",
                    "formId": 500,
                    "formKey": "VITALS_B",
                    "variableId": 2,
                    "variableName": "CODE",
                    "variableType": "Integer",
                }
            ),
        ],
        study_key="STUDY_B",
    )

    assert schema.form_id_for_key("DEMOG", study_key="STUDY_A") == 101
    assert schema.form_id_for_key("DEMOG", study_key="STUDY_B") == 202
    assert schema.form_key_from_id(500, study_key="STUDY_A") == "VITALS_A"
    assert schema.form_key_from_id(500, study_key="STUDY_B") == "VITALS_B"
    assert schema.variables_for_form("DEMOG", study_key="STUDY_A")["FLAG"].variable_type == (
        "Dropdown"
    )
    assert schema.variables_for_form("DEMOG", study_key="STUDY_B")["FLAG"].variable_type == "Logic"

    envelopes = WriteIngestionPipeline.read_input(
        [
            {"formKey": "DEMOG", "subjectKey": "S1", "data": {"FLAG": "true"}},
            {"formId": 500, "subjectKey": "S1", "data": {"CODE": "01"}},
        ]
    )
    bound_a = WriteIngestionPipeline.bind(envelopes, schema=schema, study_key="STUDY_A")
    bound_b = WriteIngestionPipeline.bind(envelopes, schema=schema, study_key="STUDY_B")

    # STUDY_A preserves categorical strings 'true' and '01'
    assert bound_a[0]["data"]["FLAG"] == "true" and isinstance(bound_a[0]["data"]["FLAG"], str)
    assert bound_a[1]["data"]["CODE"] == "01" and isinstance(bound_a[1]["data"]["CODE"], str)

    # STUDY_B coerces Logic -> True and Integer -> 1
    assert bound_b[0]["data"]["FLAG"] is True
    assert bound_b[1]["data"]["CODE"] == 1 and isinstance(bound_b[1]["data"]["CODE"], int)


def _make_mock_edc_transport(
    posted_batches: list[list[dict[str, Any]]],
    refresh_counts: dict[str, int],
) -> httpx.MockTransport:
    """Create a mock EDC transport serving forms, variables, sites, subjects, and record POSTs."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        study_in_url = (
            path.split("/studies/")[1].split("/")[0] if "/studies/" in path else "STUDY_COLD"
        )
        if path.endswith("/forms"):
            refresh_counts["forms"] = refresh_counts.get("forms", 0) + 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": study_in_url,
                            "formId": 101,
                            "formKey": "DEMOG",
                            "formName": "Demographics",
                        }
                    ]
                },
            )
        if path.endswith("/variables"):
            refresh_counts["variables"] = refresh_counts.get("variables", 0) + 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": study_in_url,
                            "formId": 101,
                            "formKey": "DEMOG",
                            "variableId": 1,
                            "variableName": "CONSENT",
                            "variableType": "Radio",
                        },
                        {
                            "studyKey": study_in_url,
                            "formId": 101,
                            "formKey": "DEMOG",
                            "variableId": 2,
                            "variableName": "PATIENT_CODE",
                            "variableType": "Text",
                        },
                        {
                            "studyKey": study_in_url,
                            "formId": 101,
                            "formKey": "DEMOG",
                            "variableId": 3,
                            "variableName": "DOSE",
                            "variableType": "Numeric",
                        },
                        {
                            "studyKey": study_in_url,
                            "formId": 101,
                            "formKey": "DEMOG",
                            "variableId": 4,
                            "variableName": "INITIALS",
                            "variableType": "Text",
                        },
                    ]
                },
            )
        if path.endswith("/sites") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY_A",
                            "siteId": 1,
                            "siteName": "Site Alpha",
                            "siteEnrollmentStatus": "Enrollment Open",
                        },
                        {
                            "studyKey": "STUDY_A",
                            "siteId": 2,
                            "siteName": "Site Beta",
                            "siteEnrollmentStatus": "Enrollment Open",
                        },
                        {
                            "studyKey": "STUDY_A",
                            "siteId": 3,
                            "siteName": "Site Gamma",
                            "siteEnrollmentStatus": "Enrollment Open",
                        },
                    ]
                },
            )
        if path.endswith("/subjects") and request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if path.endswith("/records") and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            posted_batches.append(body)
            idx = len(posted_batches)
            return httpx.Response(
                202,
                json={"jobId": f"JOB-{idx}", "batchId": f"BATCH-{idx}", "state": "PROCESSING"},
            )
        if "/jobs/" in path:
            bid = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={"jobId": f"J-{bid}", "batchId": bid, "state": "COMPLETED"},
            )
        return httpx.Response(404, json={"message": f"Unhandled {path}"})

    return httpx.MockTransport(handler)


def test_cold_schema_cache_refreshes_before_coercion_in_endpoints_and_workflow(
    tmp_path: Path,
) -> None:
    """RecordsEndpoint and RecordUpdateWorkflow refresh cold SchemaCache before coercing CSV/DF."""
    csv_path = tmp_path / "cold_import.csv"
    csv_path.write_text(
        "formKey,subjectKey,CONSENT,PATIENT_CODE,DOSE\n"
        "DEMOG,SUB-1,true,001,10.5\n"
        "DEMOG,SUB-2,false,002,20.0\n"
        "DEMOG,SUB-3,true,003,30.25\n",
        encoding="utf-8",
    )

    # 1. RecordsEndpoint.create_batch with a cold SchemaCache()
    posted_endpoint: list[list[dict[str, Any]]] = []
    endpoint_refreshes: dict[str, int] = {}
    client = Client(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://edc.example.test",
        transport=_make_mock_edc_transport(posted_endpoint, endpoint_refreshes),
    )
    sdk = ImednetSDK(client=client)
    cold_schema = SchemaCache()
    summary = sdk.records.create_batch(
        "STUDY_COLD",
        csv_path,
        schema=cold_schema,
        chunk_size=2,
    )
    assert summary.batch_ids == ["BATCH-1", "BATCH-2"]
    assert endpoint_refreshes["variables"] == 1
    all_posted = [rec for chunk in posted_endpoint for rec in chunk]
    assert all_posted[0]["data"]["CONSENT"] == "true"
    assert all_posted[1]["data"]["CONSENT"] == "false"
    assert all_posted[0]["data"]["PATIENT_CODE"] == "001"
    assert all_posted[0]["data"]["DOSE"] == 10.5

    # 2. RecordUpdateWorkflow.create_or_update_records_batch with cold internal & explicit schema
    posted_wf: list[list[dict[str, Any]]] = []
    wf_refreshes: dict[str, int] = {}
    wf_sdk = ImednetSDK(
        client=Client(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.example.test",
            transport=_make_mock_edc_transport(posted_wf, wf_refreshes),
        )
    )
    workflow = RecordUpdateWorkflow(wf_sdk)
    wf_summary = workflow.create_or_update_records_batch(
        "STUDY_COLD",
        csv_path,
        chunk_size=2,
        wait_for_completion=True,
        poll_interval=0,
    )
    assert wf_summary.batch_ids == ["BATCH-1", "BATCH-2"]
    assert wf_refreshes["variables"] == 1
    assert [r["data"]["CONSENT"] for chunk in posted_wf for r in chunk] == [
        "true",
        "false",
        "true",
    ]


async def test_async_cold_schema_cache_refreshes_before_coercion_in_endpoint_and_workflow(
    tmp_path: Path,
) -> None:
    """AsyncRecordsEndpoint and AsyncRecordUpdateWorkflow refresh cold AsyncSchemaCache first."""
    parquet_path = tmp_path / "cold_async.parquet"
    pd.DataFrame(
        [
            {"formId": 101, "subjectKey": "SUB-1", "CONSENT": "true", "PATIENT_CODE": "007"},
            {"formId": 101, "subjectKey": "SUB-2", "CONSENT": "false", "PATIENT_CODE": "008"},
            {"formId": 101, "subjectKey": "SUB-3", "CONSENT": "true", "PATIENT_CODE": "009"},
        ]
    ).to_parquet(parquet_path, index=False)

    posted: list[list[dict[str, Any]]] = []
    refreshes: dict[str, int] = {}
    sync_transport = _make_mock_edc_transport(posted, refreshes)

    class _AsyncAdapter(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return sync_transport.handle_request(request)

    async_sdk = AsyncImednetSDK(
        async_client=AsyncClient(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.example.test",
            transport=_AsyncAdapter(),
        )
    )
    wf = RecordUpdateWorkflow(async_sdk)
    explicit_cold = AsyncSchemaCache()
    summary = await wf.async_create_or_update_records_batch(
        "STUDY_COLD",
        parquet_path,
        schema=explicit_cold,
        chunk_size=2,
    )
    assert summary.batch_ids == ["BATCH-1", "BATCH-2"]
    assert refreshes["variables"] == 1
    flat = [rec for chunk in posted for rec in chunk]
    assert [r["data"]["CONSENT"] for r in flat] == ["true", "false", "true"]
    assert [r["data"]["PATIENT_CODE"] for r in flat] == ["007", "008", "009"]


def test_register_subject_request_and_enrollment_preserve_form_id_without_empty_form_key() -> None:
    """RegisterSubjectRequest preserves numeric formId without emitting formKey=''."""
    req = RegisterSubjectRequest(
        formId=42,
        siteName="Main Clinic",
        subjectKey="SUB-100",
        subjectOid="OID-100",
        data={"INITIALS": "JD"},
    )
    dumped = req.model_dump(by_alias=True, exclude_none=True)
    assert dumped == {
        "formId": 42,
        "siteName": "Main Clinic",
        "subjectKey": "SUB-100",
        "subjectOid": "OID-100",
        "data": {"INITIALS": "JD"},
    }
    assert "formKey" not in dumped

    # normalize_subject_registration_inputs applies default_form_id without formKey=""
    requests = normalize_subject_registration_inputs(
        [{"siteName": "Main Clinic", "subjectKey": "SUB-200", "INITIALS": "AB"}],
        default_form_id=77,
    )
    assert len(requests) == 1
    raw_payload, model_req = requests[0]
    assert raw_payload["formId"] == 77
    assert "formKey" not in raw_payload
    payload = model_req.model_dump(by_alias=True, exclude_none=True)
    assert payload["formId"] == 77
    assert "formKey" not in payload
    assert payload["subjectKey"] == "SUB-200"

    # RegisterSubjectsWorkflow forwards default_form_id and rejects missing formKey/formId
    posted_payloads: list[Any] = []
    mock_sdk = MagicMock()
    mock_sdk.get_sites.return_value = [
        Site(
            studyKey="STUDY_A",
            siteId=1,
            siteName="Main Clinic",
            siteEnrollmentStatus="Enrollment Open",
        )
    ]
    mock_sdk.get_subjects.return_value = []
    mock_sdk.create_record.side_effect = lambda study_key, records_data, **kw: (
        posted_payloads.append(records_data) or Job(jobId="J1", batchId="B1", state="PROCESSING")
    )
    reg_wf = RegisterSubjectsWorkflow(mock_sdk)
    study_schema = SchemaCache()
    _populate_study_schema(study_schema, study_key="STUDY_A", form_id=88, form_key="ENROLL")

    with pytest.raises(ValidationError, match="formKey|formId"):
        reg_wf.register_subjects(
            "STUDY_A",
            [{"siteName": "Main Clinic", "subjectKey": "SUB-NO-FORM"}],
            skip_existing=False,
            schema=study_schema,
        )

    reg_wf.register_subjects(
        "STUDY_A",
        [{"siteName": "Main Clinic", "subjectKey": "SUB-300", "subjectOid": "OID-300"}],
        default_form_id=88,
        skip_existing=False,
        schema=study_schema,
    )
    assert len(posted_payloads) == 1
    sent_rec = posted_payloads[0][0]
    assert sent_rec["formId"] == 88
    assert "formKey" not in sent_rec
    assert sent_rec["subjectKey"] == "SUB-300"
    assert sent_rec["subjectOid"] == "OID-300"


async def test_async_register_subjects_workflow_preserves_form_id_and_subject_identifiers() -> None:
    """RegisterSubjectsWorkflow.async_register_subjects preserves formId, subjectKey, subjectOid."""
    posted_payloads: list[Any] = []

    class _AsyncFacadeStub:
        async def async_get_sites(self, study_key: str | None = None) -> list[Site]:
            return [
                Site(
                    studyKey="STUDY_A",
                    siteId=1,
                    siteName="Main Clinic",
                    siteEnrollmentStatus="Enrollment Open",
                )
            ]

        async def async_get_subjects(self, study_key: str | None = None) -> list[Any]:
            return []

        async def async_create_record(
            self, study_key: str, records_data: Any, **kwargs: Any
        ) -> Any:
            posted_payloads.append(records_data)
            return Job(jobId="J1", batchId="B1", state="PROCESSING")

    wf = RegisterSubjectsWorkflow(_AsyncFacadeStub())
    await wf.async_register_subjects(
        "STUDY_A",
        [{"siteName": "Main Clinic", "subjectKey": "SUB-400", "subjectOid": "OID-400"}],
        default_form_id=99,
        skip_existing=False,
    )
    sent = posted_payloads[0][0]
    assert sent["formId"] == 99
    assert "formKey" not in sent
    assert sent["subjectKey"] == "SUB-400"
    assert sent["subjectOid"] == "OID-400"


def test_cli_subjects_register_applies_form_id_and_subject_overrides_across_file_json_and_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI subjects register applies --form-id, --subject-key, --subject-oid to single-item inputs."""
    posted_batches: list[list[dict[str, Any]]] = []
    refreshes: dict[str, int] = {}

    def _build_sdk(**_kwargs: Any) -> ImednetSDK:
        return ImednetSDK(
            client=Client(
                api_key="k",  # pragma: allowlist secret
                security_key="s",  # pragma: allowlist secret
                base_url="https://edc.example.test",
                transport=_make_mock_edc_transport(posted_batches, refreshes),
            )
        )

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _build_sdk)

    # 1. Single-item --file CSV with --form-id, --subject-key, and --subject-oid overrides
    csv_file = tmp_path / "single_subject.csv"
    csv_file.write_text("siteName,INITIALS\nSite Alpha,JD\n", encoding="utf-8")
    res_csv = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY_A",
            "--file",
            str(csv_file),
            "--form-id",
            "101",
            "--subject-key",
            "SUB-CSV",
            "--subject-oid",
            "OID-CSV",
            "--format",
            "json",
        ],
    )
    assert res_csv.exit_code == 0, res_csv.stderr
    assert posted_batches[-1] == [
        {
            "formId": 101,
            "siteName": "Site Alpha",
            "subjectKey": "SUB-CSV",
            "subjectOid": "OID-CSV",
            "data": {"INITIALS": "JD"},
        }
    ]

    # 2. Single-item --json with --form-id, --subject-key, --subject-oid, and deferred categorical binding!
    res_json = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY_A",
            "--json",
            '{"siteName": "Site Beta", "data": {"INITIALS": "AB", "CONSENT": "true"}}',
            "--form-id",
            "101",
            "--subject-key",
            "SUB-JSON",
            "--subject-oid",
            "OID-JSON",
            "--schema",
            "api",
            "--format",
            "json",
        ],
    )
    assert res_json.exit_code == 0, res_json.stderr
    assert posted_batches[-1] == [
        {
            "formId": 101,
            "siteName": "Site Beta",
            "subjectKey": "SUB-JSON",
            "subjectOid": "OID-JSON",
            "data": {"INITIALS": "AB", "CONSENT": "true"},
        }
    ]

    # 3. CLI -d with --form-id, --site-name, --subject-key, --subject-oid, and categorical 'false'
    res_d = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY_A",
            "--form-id",
            "101",
            "--site-name",
            "Site Gamma",
            "--subject-key",
            "SUB-CLI",
            "--subject-oid",
            "OID-CLI",
            "-d",
            "INITIALS=XY",
            "-d",
            "CONSENT=false",
            "--schema",
            "api",
            "--format",
            "json",
        ],
    )
    assert res_d.exit_code == 0, res_d.stderr
    assert posted_batches[-1] == [
        {
            "formId": 101,
            "siteName": "Site Gamma",
            "subjectKey": "SUB-CLI",
            "subjectOid": "OID-CLI",
            "data": {"INITIALS": "XY", "CONSENT": "false"},
        }
    ]

    # 4. Multi-item --json rejects --subject-key override to prevent duplicate subjectKey collision
    res_multi_err = _invoke_cli(
        capsys,
        [
            "--no-interactive",
            "subjects",
            "register",
            "STUDY_A",
            "--json",
            '[{"siteName": "Site Alpha", "INITIALS": "A"}, {"siteName": "Site Alpha", "INITIALS": "B"}]',
            "--form-id",
            "101",
            "--subject-key",
            "COLLIDE",
        ],
    )
    assert res_multi_err.exit_code != 0


def test_cli_records_import_supports_parquet_and_deferred_categorical_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI records import ingests .parquet files and preserves categorical 'true'/'false' strings."""
    parquet_file = tmp_path / "import_records.parquet"
    pd.DataFrame(
        [
            {"formKey": "DEMOG", "subjectKey": "S-1", "CONSENT": "true", "PATIENT_CODE": "007"},
            {"formKey": "DEMOG", "subjectKey": "S-2", "CONSENT": "false", "PATIENT_CODE": "008"},
        ]
    ).to_parquet(parquet_file, index=False)

    posted: list[list[dict[str, Any]]] = []
    refreshes: dict[str, int] = {}
    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kwargs: ImednetSDK(
            client=Client(
                api_key="k",  # pragma: allowlist secret
                security_key="s",  # pragma: allowlist secret
                base_url="https://edc.example.test",
                transport=_make_mock_edc_transport(posted, refreshes),
            )
        ),
    )

    res = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY_COLD",
            str(parquet_file),
            "--schema",
            "api",
            "--format",
            "json",
        ],
    )
    assert res.exit_code == 0, res.stderr
    envelope = json.loads(res.stdout)
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["status"] == "complete"
    assert envelope["items"][0]["batchIds"] == ["BATCH-1"]
    assert posted[0][0]["data"]["CONSENT"] == "true"
    assert posted[0][1]["data"]["CONSENT"] == "false"
    assert posted[0][0]["data"]["PATIENT_CODE"] == "007"

    # Also validate CSV thin wrapper parity
    csv_file = tmp_path / "thin_wrapper.csv"
    csv_file.write_text("formKey,subjectKey,CONSENT\nDEMOG,S-1,true\n", encoding="utf-8")
    schema = SchemaCache()
    _populate_study_schema(schema, study_key="STUDY_COLD")
    wrapped = csv_to_records(csv_file, schema=schema, study_key="STUDY_COLD")
    assert wrapped[0]["data"]["CONSENT"] == "true"


def test_tabular_thin_wrappers_and_write_pipeline_scalar_edge_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise tabular thin wrappers, nested cells, temporal/Decimal binding, and reader errors."""
    import io
    import sys

    import numpy as np

    from imednet.models.records import RegisterSubjectRequest
    from imednet.utils.tabular import (
        _read_csv_stream_records,
        csv_text_to_records,
        normalize_tabular_row,
    )
    from imednet.utils.write_pipeline import (
        _bind_scalar_value,
        _coerce_int_identifier,
        _match_choice_case,
        _parse_nested_data_cell,
        _read_parquet_rows,
        _resolve_expected_var_type,
        _serialize_decimal_value,
        _serialize_temporal_value,
        _unquote_double_quoted_literal,
    )

    schema = SchemaCache()
    _populate_study_schema(schema, study_key="STUDY_EDGE")

    # Tabular thin wrappers
    assert normalize_tabular_row({"formKey": "", "subjectKey": None}) is None
    row = normalize_tabular_row(
        {"formKey": "DEMOG", "subjectKey": "S-1", "CONSENT": "true"},
        from_csv_str=True,
        schema=schema,
        study_key="STUDY_EDGE",
    )
    assert row is not None and row["data"]["CONSENT"] == "true"
    stream_recs = _read_csv_stream_records(
        io.StringIO("formKey,subjectKey,CONSENT\nDEMOG,S-2,false\n"),
        schema=schema,
        study_key="STUDY_EDGE",
    )
    assert stream_recs[0]["data"]["CONSENT"] == "false"
    text_recs = csv_text_to_records(
        "formKey,subjectKey,PATIENT_CODE\nDEMOG,S-3,009\n",
        schema=schema,
        study_key="STUDY_EDGE",
    )
    assert text_recs[0]["data"]["PATIENT_CODE"] == "009"
    assert WriteIngestionPipeline.read_csv_stream(io.StringIO("")) == []
    assert WriteIngestionPipeline.read_dataframe(pd.DataFrame()) == []

    # Identifier & nested data cell helpers
    assert _coerce_int_identifier(5.0, row_index=0, column_name="formId") == 5
    assert _coerce_int_identifier(Decimal(7), row_index=0, column_name="formId") == 7
    assert _coerce_int_identifier("8.0", row_index=0, column_name="formId") == 8
    with pytest.raises(ValidationError):
        _coerce_int_identifier(True, row_index=0, column_name="formId")
    with pytest.raises(ValidationError):
        _coerce_int_identifier("bad", row_index=0, column_name="formId")

    assert _parse_nested_data_cell({"X": 1}, row_index=0, column_name="data") == {"X": 1}
    assert _parse_nested_data_cell("   ", row_index=0, column_name="data") == {}
    assert _parse_nested_data_cell("{'X': 'ok'}", row_index=0, column_name="data") == {"X": "ok"}
    with pytest.raises(ValidationError):
        _parse_nested_data_cell("{not valid", row_index=0, column_name="data")
    with pytest.raises(ValidationError):
        _parse_nested_data_cell("[1, 2]", row_index=0, column_name="data")

    assert _unquote_double_quoted_literal('"hello"') == "hello"
    assert _unquote_double_quoted_literal('"bad\\q"') == "bad\\q"
    assert _resolve_expected_var_type(schema, "DEMOG", None, "PATIENT_CODE", study_key="STUDY_EDGE")
    assert _match_choice_case("true", ["True", "False"]) == "True"

    # Temporal & Decimal edge cases
    assert _serialize_temporal_value(pd.NaT, expected_var_type="date") is None
    assert _serialize_temporal_value(np.datetime64("NaT"), expected_var_type="date") is None
    assert (
        _serialize_temporal_value(np.datetime64("2026-04-10T00:00:00"), expected_var_type="date")
        == "2026-04-10"
    )
    assert (
        _serialize_temporal_value(np.datetime64("2026-04-10"), expected_var_type="datetime")
        == "2026-04-10T00:00:00"
    )
    assert (
        _serialize_temporal_value(dt.date(2026, 4, 10), expected_var_type="datetime")
        == "2026-04-10T00:00:00"
    )
    assert (
        _serialize_temporal_value(dt.datetime(2026, 4, 10, 14, 30), expected_var_type="time")
        == "14:30:00"
    )

    with pytest.raises(ValidationError, match="non-finite Decimal"):
        _serialize_decimal_value(
            Decimal("NaN"),
            expected_var_type="float",
            choices=[],
            row_index=0,
            var_name="DOSE",
        )
    assert _serialize_decimal_value(Decimal("1.50"), expected_var_type="text", choices=[]) == "1.50"
    assert (
        _serialize_decimal_value(Decimal("1.0"), expected_var_type="radio", choices=["1", "2"])
        == "1"
    )
    assert (
        _serialize_decimal_value(Decimal("1.50"), expected_var_type="radio", choices=["1.50"])
        == "1.50"
    )
    assert _serialize_decimal_value(Decimal(5), expected_var_type="calculation", choices=[]) == 5.0

    assert _bind_scalar_value((Decimal(3), dt.date(2026, 1, 2)), source_kind="mapping") == (
        3,
        "2026-01-02",
    )
    assert _bind_scalar_value({"nested": Decimal(4)}, source_kind="mapping") == {"nested": 4}
    assert _bind_scalar_value(3.0, source_kind="tabular", expected_var_type="checkbox") == "3"
    assert _bind_scalar_value(4.0, source_kind="tabular", expected_var_type="radio") == "4"
    assert _bind_scalar_value(True, source_kind="tabular", expected_var_type="text") == "true"
    assert _bind_scalar_value(6.0, source_kind="tabular", expected_var_type="text") == "6"
    assert _bind_scalar_value(7.0, source_kind="tabular", expected_var_type="integer") == 7
    assert _bind_scalar_value(8.0, source_kind="tabular", expected_var_type="calculation") == 8

    # Reader validations & Parquet pandas fallback
    with pytest.raises(ValueError, match="Invalid JSON"):
        WriteIngestionPipeline.read_json_text("{bad")
    with pytest.raises(ValueError, match="list response envelope"):
        WriteIngestionPipeline.read_json_text('{"schemaVersion": "1.0", "items": []}')
    with pytest.raises(ValueError, match="JSON object or array"):
        WriteIngestionPipeline.read_json_text("42")
    with pytest.raises(ValueError, match="Empty input"):
        WriteIngestionPipeline.read_stdin_text("   ")
    with pytest.raises(ValueError, match="JSON object or array"):
        WriteIngestionPipeline.read_stdin_text("123")
    with pytest.raises(TypeError, match="must be mappings"):
        WriteIngestionPipeline.read_mappings(["not-a-mapping"])

    req = RegisterSubjectRequest(formId=101, siteId=10, data={"CONSENT": "true"})
    mapped_envs = WriteIngestionPipeline.read_mappings([req])
    assert mapped_envs[0].form_id == 101
    reused_envs = WriteIngestionPipeline.read_mappings(mapped_envs)
    assert reused_envs[0] is mapped_envs[0]

    pq_path = tmp_path / "fallback.parquet"
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    monkeypatch.setattr(
        pd,
        "read_parquet",
        lambda _p: pd.DataFrame([{"formKey": "DEMOG", "subjectKey": "S-PQ", "AGE": 30}]),
    )
    pq_rows = _read_parquet_rows(pq_path)
    assert pq_rows[0]["subjectKey"] == "S-PQ"


def test_enrollment_normalization_site_validation_and_deduplication(tmp_path: Path) -> None:
    """Exercise normalize_subject_registration_inputs, site validation, and subject deduplication."""
    from imednet.models.records import RegisterSubjectRequest
    from imednet.models.sites import Site
    from imednet.models.subjects import Subject
    from imednet.utils.enrollment import (
        extract_candidate_subject_key,
        filter_existing_subject_registrations,
        normalize_subject_registration_inputs,
        validate_local_subject_registration_sites,
        validate_subject_registration_sites,
    )

    schema = SchemaCache()
    _populate_study_schema(schema, study_key="STUDY_ENROLL")

    assert (
        extract_candidate_subject_key({"data": {"SUBJ_ID": "CAND-1"}}, subject_key_field="SUBJ_ID")
        == "CAND-1"
    )

    # CLI envelope JSON file rejected cleanly
    bad_json = tmp_path / "envelope.json"
    bad_json.write_text('{"schemaVersion": "1.0", "items": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="CLI list response envelope"):
        normalize_subject_registration_inputs(bad_json)

    # CSV and DataFrame inputs with default formId and siteId
    csv_path = tmp_path / "subjects.csv"
    csv_path.write_text("subjectKey,CONSENT\nSUB-CSV,true\n", encoding="utf-8")
    csv_pairs = normalize_subject_registration_inputs(
        csv_path,
        default_form_id=101,
        default_site_id=55,
        schema=schema,
        study_key="STUDY_ENROLL",
    )
    assert csv_pairs[0][1].form_id == 101
    assert csv_pairs[0][1].site_id == 55
    assert csv_pairs[0][1].model_dump(by_alias=True)["data"]["CONSENT"] == "true"

    df_pairs = normalize_subject_registration_inputs(
        pd.DataFrame([{"subjectKey": "SUB-DF", "CONSENT": "false"}]),
        default_form_key="DEMOG",
        default_site_name="General Hospital",
        schema=schema,
        study_key="STUDY_ENROLL",
    )
    assert df_pairs[0][1].form_key == "DEMOG"
    assert df_pairs[0][1].site_name == "General Hospital"
    assert df_pairs[0][1].model_dump(by_alias=True)["data"]["CONSENT"] == "false"

    # RegisterSubjectRequest model defaults + temporal binding
    model_req = RegisterSubjectRequest(data={"VISIT_DATE": dt.date(2026, 5, 1)})
    model_pairs = normalize_subject_registration_inputs(
        [model_req],
        default_form_id=101,
        default_site_id=55,
        schema=schema,
        study_key="STUDY_ENROLL",
    )
    assert model_pairs[0][1].form_id == 101
    assert model_pairs[0][1].site_id == 55
    assert model_pairs[0][1].model_dump(by_alias=True)["data"]["VISIT_DATE"] == "2026-05-01"

    with pytest.raises(TypeError, match="RegisterSubjectRequest or dict"):
        normalize_subject_registration_inputs([123])

    # Site validation (offline and online)
    missing_site_pair = [(model_req, model_req)]
    with pytest.raises(ValueError, match="siteName is required"):
        validate_local_subject_registration_sites(missing_site_pair)

    active_site = Site(
        siteId=55, siteName="General Hospital", siteEnrollmentStatus="Enrollment Open"
    )
    closed_site = Site(siteId=99, siteName="Closed Clinic", siteEnrollmentStatus="Closed")
    validate_subject_registration_sites(csv_pairs, [active_site])

    closed_pair = normalize_subject_registration_inputs(
        [{"formId": 101, "siteId": 99, "data": {}}],
    )
    with pytest.raises(ValueError, match="not eligible for enrollment"):
        validate_subject_registration_sites(closed_pair, [closed_site])

    missing_id_pair = normalize_subject_registration_inputs(
        [{"formId": 101, "siteId": 404, "data": {}}],
    )
    with pytest.raises(ValueError, match="siteId 404 not found"):
        validate_subject_registration_sites(missing_id_pair, [active_site])

    # Subject deduplication filtering
    batch_pairs = normalize_subject_registration_inputs(
        [
            {"formId": 101, "siteId": 55, "subjectKey": "ENROLLED-1", "data": {}},
            {"formId": 101, "siteId": 55, "subjectKey": "DELETED-1", "data": {}},
            {"formId": 101, "siteId": 55, "subjectKey": "NEW-1", "data": {}},
            {"formId": 101, "siteId": 55, "subjectKey": "NEW-1", "data": {}},
        ]
    )
    existing = [
        Subject(subjectKey="ENROLLED-1", subjectStatus="Enrolled"),
        Subject(subjectKey="DELETED-1", deleted=True),
    ]
    filtered, skipped = filter_existing_subject_registrations(
        batch_pairs, existing, should_filter=True
    )
    assert skipped == 2
    assert [r["subjectKey"] for r in filtered] == ["DELETED-1", "NEW-1"]
