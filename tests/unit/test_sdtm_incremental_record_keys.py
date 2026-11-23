"""Tests for SDTM incremental record keys, multi-key sort, envelope handling, and Hive Parquet."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import httpx
import pandas as pd
import pyarrow.parquet as pq
import pytest

from imednet import ImednetAPIError, ImednetSDK
from imednet.cli import app as cli_app
from imednet.cli.utils.args import prepare_list_query
from imednet.core.endpoint.strategies import DefaultParamProcessor, MappingParamProcessor
from imednet.errors import BadRequestError
from imednet.integrations.enrichment import CentralizedMapper
from imednet.integrations.export import (
    export_to_csv,
    export_to_duckdb,
    export_to_parquet,
    export_to_sql,
)
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.integrations.sink_base import evaluate_standards_readiness
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.records import Record
from imednet.models.sdtm import (
    RECORD_ENVELOPE_KEYS,
    get_sdtm_domain_variables,
    transform_records_to_sdtm,
    transform_records_to_sdtm_rows,
)
from imednet.models.variables import Variable
from imednet.simulation import SimulationState, SimulationTransport
from imednet.spi.models import RECORD_ENVELOPE_KEYS as SPI_MODELS_ENVELOPE_KEYS
from imednet.spi.sinks import (
    RECORD_ENVELOPE_KEYS as SPI_SINKS_ENVELOPE_KEYS,
)
from imednet.spi.sinks import (
    SinkConfig,
    build_sink_mapper,
)
from imednet_workflows.record_mapper import RecordMapper
from imednet_workflows.state_ledger import FileStateProvider


def test_param_processors_serialize_multi_key_sort_as_repeated_params() -> None:
    """DefaultParamProcessor and MappingParamProcessor emit repeated <property>,asc|desc sort params."""
    default_proc = DefaultParamProcessor()
    rem_default, params_default = default_proc.process_filters(
        {"sort": [("subjectKey", "asc"), ("recordId", "DESC")], "size": 50, "subjectKey": "S-01"}
    )
    assert rem_default == {"subjectKey": "S-01"}
    assert params_default["sort"] == ["subjectKey,asc", "recordId,desc"]
    assert params_default["size"] == 50

    mapping_proc = MappingParamProcessor(
        {"study_key": "studyKey"},
    )
    rem_mapped, params_mapped = mapping_proc.process_filters(
        {"sort": [("subjectKey", "asc"), "recordId,desc"], "study_key": "ST-1", "formId": 10}
    )
    assert rem_mapped == {"formId": 10}
    assert params_mapped["sort"] == ["subjectKey,asc", "recordId,desc"]
    assert params_mapped["studyKey"] == "ST-1"


def test_sdtm_dataframe_multi_key_sort_simulation_400_and_api_error_propagation() -> None:
    """sdtm_dataframe sends repeated sort params, SimulationTransport rejects ;/:, and 400s propagate."""
    state = SimulationState()
    state.studies = [{"studyKey": "STUDY-SORT", "studyName": "Sort Study"}]
    state.forms = [
        {"studyKey": "STUDY-SORT", "formId": 10, "formKey": "AE", "formName": "Adverse Events"}
    ]
    state.variables = [
        {
            "studyKey": "STUDY-SORT",
            "formId": 10,
            "formKey": "AE",
            "variableId": 1,
            "variableName": "aeTerm",
            "label": "AE Term",
            "variableType": "text",
        }
    ]
    state.records = [
        {
            "studyKey": "STUDY-SORT",
            "recordId": 20,
            "formId": 10,
            "formKey": "AE",
            "subjectKey": "SUBJ-02",
            "deleted": False,
            "recordData": {"aeTerm": "Nausea"},
        },
        {
            "studyKey": "STUDY-SORT",
            "recordId": 10,
            "formId": 10,
            "formKey": "AE",
            "subjectKey": "SUBJ-01",
            "deleted": False,
            "recordData": {"aeTerm": "Headache"},
        },
    ]

    transport = SimulationTransport(state=state)
    captured_urls: list[str] = []
    orig_handle = transport.handle_request

    def spy_handle(request: httpx.Request) -> httpx.Response:
        if request.method.upper() == "GET" and request.url.path.endswith("/records"):
            captured_urls.append(str(request.url))
        return orig_handle(request)

    transport.handle_request = spy_handle  # type: ignore[method-assign]
    sdk = ImednetSDK(simulation_mode=True, transport=transport)
    mapper = RecordMapper(sdk, chunk_size=2)

    df = mapper.sdtm_dataframe("STUDY-SORT", domain="AE")
    assert captured_urls
    assert all("sort=subjectKey%2Casc&sort=recordId%2Casc" in url for url in captured_urls)
    assert list(df["USUBJID"]) == ["SUBJ-01", "SUBJ-02"]
    assert "RECORD_ID" not in df.columns

    # Direct HTTP checks against SimulationTransport for invalid ';' and ':' sort delimiters
    with httpx.Client(transport=transport, base_url="https://sim.local") as http_client:
        semicolon_resp = http_client.get(
            "/api/v1/edc/studies/STUDY-SORT/records",
            params={"sort": "subjectKey,asc;recordId,asc"},
        )
        colon_resp = http_client.get(
            "/api/v1/edc/studies/STUDY-SORT/records",
            params={"sort": "subjectKey:asc"},
        )
    assert semicolon_resp.status_code == 400
    assert colon_resp.status_code == 400

    # RecordMapper must propagate ImednetAPIError (HTTP 400) rather than returning empty SDTM frames
    def fail_records_400(request: httpx.Request) -> httpx.Response:
        if request.method.upper() == "GET" and request.url.path.endswith("/records"):
            return httpx.Response(
                400,
                json={"error": "Invalid sort expression"},
                request=request,
            )
        return orig_handle(request)

    transport.handle_request = fail_records_400  # type: ignore[method-assign]
    failing_sdk = ImednetSDK(simulation_mode=True, transport=transport)
    failing_mapper = RecordMapper(failing_sdk, chunk_size=2)

    with pytest.raises(ImednetAPIError) as exc_info:
        failing_mapper.sdtm_dataframe("STUDY-SORT", domain="AE")
    assert isinstance(exc_info.value, BadRequestError)
    assert exc_info.value.status_code == 400


def test_cli_repeated_sort_flags_preserve_multi_key_order() -> None:
    """Repeated --sort CLI options preserve multi-key sort order on records and generic list cmds."""
    list_kwargs, _ = prepare_list_query(
        sort=["subjectKey,asc", "recordId,desc"],
        allowed_attributes={"subjectKey", "recordId"},
    )
    assert list_kwargs["sort"] == ["subjectKey,asc", "recordId,desc"]

    mock_sdk = MagicMock()
    mock_sdk.records.list.return_value = iter([])
    mock_sdk.subjects.list.return_value = iter([])

    with patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk):
        cli_app(
            [
                "records",
                "list",
                "STUDY-CLI",
                "--sort",
                "subjectKey,asc",
                "--sort",
                "recordId,desc",
                "--no-interactive",
            ]
        )
        cli_app(
            [
                "subjects",
                "list",
                "STUDY-CLI",
                "--sort",
                "subjectKey,asc",
                "--sort",
                "siteName,desc",
                "--no-interactive",
            ]
        )
    assert mock_sdk.records.list.call_args.kwargs["sort"] == ["subjectKey,asc", "recordId,desc"]
    assert mock_sdk.subjects.list.call_args.kwargs["sort"] == ["subjectKey,asc", "siteName,desc"]


def test_opt_in_include_record_id_across_sdtm_apis_and_sink_mappers() -> None:
    """In-memory SDTM APIs omit RECORD_ID by default while include_record_id=True and sinks include it."""
    rec = Record(
        study_key="STUDY-1",
        record_id=501,
        form_id=10,
        form_key="AE",
        subject_key="SUBJ-01",
        record_data={"aeTerm": "Fatigue", "aeSerious": "No"},
    )

    assert "RECORD_ID" not in get_sdtm_domain_variables("AE")
    assert get_sdtm_domain_variables("AE", include_record_id=True)[:2] == [
        "STUDYID",
        "RECORD_ID",
    ]

    default_rows = transform_records_to_sdtm_rows([rec], domain="AE", study_key="STUDY-1")
    assert "RECORD_ID" not in default_rows[0]

    opt_in_rows = transform_records_to_sdtm_rows(
        [rec], domain="AE", study_key="STUDY-1", include_record_id=True
    )
    assert opt_in_rows[0]["RECORD_ID"] == 501
    assert list(opt_in_rows[0].keys())[:2] == ["STUDYID", "RECORD_ID"]

    default_df = transform_records_to_sdtm([rec], domain="AE", study_key="STUDY-1")
    assert "RECORD_ID" not in default_df.columns

    opt_in_df = transform_records_to_sdtm(
        [rec], domain="AE", study_key="STUDY-1", include_record_id=True
    )
    assert list(opt_in_df.columns[:2]) == ["STUDYID", "RECORD_ID"]
    assert int(opt_in_df.loc[0, "RECORD_ID"]) == 501

    in_mem_mapper = CentralizedMapper(mode="tabular", as_sdtm=True, sdtm_domain="AE")
    assert "RECORD_ID" not in in_mem_mapper.map_record(rec, study_key="STUDY-1")

    sink_mapper = build_sink_mapper(
        SinkConfig(study_key="STUDY-1", as_sdtm=True, sdtm_domain="AE"),
        mode="tabular",
    )
    sink_row = sink_mapper.map_record(rec, study_key="STUDY-1")
    assert sink_row["RECORD_ID"] == 501


def test_raw_records_api_20_envelope_keys_dm_subjid_and_standards_readiness() -> None:
    """Raw 20-key Records API payloads map DM.SUBJID from subjectKey, omit dateCreated, and mask PHI."""
    raw_api_record: dict[str, Any] = {
        "studyKey": "STUDY-DM",
        "intervalId": 1,
        "formId": 100,
        "formKey": "DM",
        "siteId": 7,
        "recordId": 9001,
        "recordOid": "REC-OID-9001",
        "recordType": "SUBJECT",
        "recordStatus": "Complete",
        "deleted": False,
        "dateCreated": "2026-01-15T08:30:00Z",
        "dateModified": "2026-01-16T09:45:00Z",
        "subjectId": 42,
        "subjectOid": "SUBJ-OID-42",
        "subjectKey": "SUBJ-ALPHA-01",
        "visitId": 1,
        "parentRecordId": 0,
        "lastUpdated": "2026-01-16T09:45:00Z",
        "keywords": [{"keywordName": "Enrolled"}],
        "recordData": {
            "birthDate": "1984-07-19",
            "sex": "F",
            "race": "ASIAN",
            "patientName": "Alice Example",
        },
    }
    envelope_keys_only = {k for k in raw_api_record if k != "recordData"}
    assert len(envelope_keys_only) == 19  # + recordData == all 20 Records API attributes
    assert set(raw_api_record).issubset(RECORD_ENVELOPE_KEYS)
    assert RECORD_ENVELOPE_KEYS == SPI_MODELS_ENVELOPE_KEYS == SPI_SINKS_ENVELOPE_KEYS

    dm_df = transform_records_to_sdtm([raw_api_record], domain="DM", study_key="STUDY-DM")
    assert len(dm_df) == 1
    row = dm_df.iloc[0]
    assert row["STUDYID"] == "STUDY-DM"
    assert row["DOMAIN"] == "DM"
    assert row["USUBJID"] == "SUBJ-ALPHA-01"
    assert row["SUBJID"] == "SUBJ-ALPHA-01"
    assert row["BRTHDTC"] == "1984"
    assert row["SEX"] == "F"
    assert row["RACE"] == "ASIAN"
    assert row["patientName"] == "***MASKED***"

    # Zero envelope keys or fabricated dateCreated columns leak into SDTM output
    leaked = set(dm_df.columns) & (envelope_keys_only | {"date_created", "DATECREATED", "DMDTC"})
    assert leaked == set()

    # Standards readiness emits zero false CDASH variable warnings for both nested and flat 20-key records
    nested_ae_record = dict(
        raw_api_record,
        formKey="AE",
        recordData={"AETERM": "Headache", "AESTDTC": "2026-01-15"},
    )
    nested_report = evaluate_standards_readiness(
        [nested_ae_record],
        study_key="STUDY-DM",
        sdtm_domain="AE",
    )
    assert nested_report.warnings == []

    report = evaluate_standards_readiness(
        [
            {k: v for k, v in raw_api_record.items() if k != "recordData"}
            | {"AETERM": "Headache", "AESTDTC": "2026-01-15"}
        ],
        study_key="STUDY-DM",
        sdtm_domain="AE",
    )
    assert report.warnings == []


def test_missing_date_modified_and_zero_or_null_parent_record_id_preserved_as_none() -> None:
    """CentralizedMapper preserves None date_modified and build_hierarchy preserves None parent_record_id."""
    mapper = CentralizedMapper(mode="tabular")
    rec_null_mod = {"recordId": 1, "studyKey": "ST", "dateModified": None, "recordData": {"x": 1}}
    rec_empty_mod = {"recordId": 2, "studyKey": "ST", "dateModified": "   ", "recordData": {"x": 2}}
    rec_valid_mod = {
        "recordId": 3,
        "studyKey": "ST",
        "dateModified": "2026-03-10T12:00:00Z",
        "recordData": {"x": 3},
    }

    assert mapper.map_record(rec_null_mod)["date_modified"] is None
    assert mapper.map_record(rec_empty_mod)["date_modified"] is None
    assert mapper.map_record(rec_valid_mod)["date_modified"] == "2026-03-10T12:00:00Z"

    sdk = MagicMock()
    mock_struct = MagicMock()
    form_meta = MagicMock()
    form_meta.form_id = 10
    form_meta.variables = [Variable(variable_name="V1", label="L1", form_id=10)]
    interval_meta = MagicMock()
    interval_meta.forms = [form_meta]
    mock_struct.intervals = [interval_meta]

    records: list[Any] = [
        Record(
            record_id=101,
            subject_key="S1",
            visit_id=1,
            form_id=10,
            parent_record_id=0,
            record_data={"V1": "root_zero"},
        ),
        Record(
            record_id=102,
            subject_key="S1",
            visit_id=1,
            form_id=10,
            parent_record_id=None,
            record_data={"V1": "root_none"},
        ),
        Record.model_validate(
            {
                "recordId": 103,
                "subjectKey": "S1",
                "visitId": 1,
                "formId": 10,
                "parentRecordId": 0,
                "recordData": {"V1": "camel_zero"},
            }
        ),
        Record(
            record_id=104,
            subject_key="S1",
            visit_id=1,
            form_id=10,
            parent_record_id=101,
            record_data={"V1": "child"},
        ),
    ]

    wf_mapper = RecordMapper(sdk)
    wf_mapper._iter_records = MagicMock(return_value=records)
    with patch("imednet_workflows.study_structure.get_study_structure", return_value=mock_struct):
        hierarchy = wf_mapper.build_hierarchy("STUDY-HIER")

    out_records = hierarchy[0]["visits"][0]["forms"][0]["records"]
    by_v1 = {r["V1"]: r["parent_record_id"] for r in out_records}
    assert by_v1 == {
        "root_zero": None,
        "root_none": None,
        "camel_zero": None,
        "child": 101,
    }


def _make_incremental_sdtm_sdk(upstream_records: dict[int, Record], study_key: str) -> MagicMock:
    """Create a stateful SDK mock supporting watermarks, deleted filtering, and formId filtering."""
    forms = [
        Form(form_id=10, form_key="DM", form_name="Demographics"),
        Form(form_id=20, form_key="AE", form_name="Adverse Events"),
        Form(form_id=30, form_key="VS", form_name="Vital Signs"),
    ]
    intervals = [
        Interval(
            interval_id=1,
            interval_name="Visit 1",
            interval_sequence=1,
            forms=[
                FormSummary(form_id=10, form_key="DM", form_name="Demographics"),
                FormSummary(form_id=20, form_key="AE", form_name="Adverse Events"),
                FormSummary(form_id=30, form_key="VS", form_name="Vital Signs"),
            ],
        )
    ]
    variables = [
        Variable(
            variable_id=1,
            variable_name="sex",
            label="Sex",
            variable_type="text",
            form_id=10,
            form_key="DM",
        ),
        Variable(
            variable_id=2,
            variable_name="aeTerm",
            label="AE Term",
            variable_type="text",
            form_id=20,
            form_key="AE",
        ),
        Variable(
            variable_id=3,
            variable_name="aeSerious",
            label="Serious",
            variable_type="text",
            form_id=20,
            form_key="AE",
        ),
        Variable(
            variable_id=4,
            variable_name="patientName",
            label="Patient Name",
            variable_type="text",
            form_id=20,
            form_key="AE",
        ),
        Variable(
            variable_id=5,
            variable_name="vsTest",
            label="VS Test",
            variable_type="text",
            form_id=30,
            form_key="VS",
        ),
        Variable(
            variable_id=6,
            variable_name="vsResult",
            label="VS Result",
            variable_type="float",
            form_id=30,
            form_key="VS",
        ),
    ]

    def _list_records(**kw: Any) -> Any:
        from imednet.integrations.sink_base import (
            extract_record_timestamp,
            normalize_watermark_datetime,
        )

        flt = str(kw.get("filter", "") or "")
        items = list(upstream_records.values())
        if kw.get("deleted") is False:
            items = [r for r in items if not r.deleted]
        if kw.get("formId") is not None:
            raw_fids = kw["formId"]
            fids = {int(x) for x in raw_fids} if isinstance(raw_fids, list) else {int(raw_fids)}
            items = [r for r in items if r.form_id in fids]
        for clause in (c.strip() for c in flt.split(";") if c.strip()):
            if clause == "deleted==false":
                items = [r for r in items if not r.deleted]
            elif clause.startswith("dateModified>="):
                wm_dt = normalize_watermark_datetime(clause.split(">=", 1)[1])
                if wm_dt is not None:
                    items = [
                        r
                        for r in items
                        if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                    ]
            elif clause.startswith("formId=="):
                fid = int(clause.split("==", 1)[1])
                items = [r for r in items if r.form_id == fid]
        return iter([r.model_copy(deep=True) for r in items])

    sdk = MagicMock()
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.get_intervals.return_value = intervals
    sdk.get_variables.return_value = variables
    sdk.variables.list.side_effect = lambda **kw: (
        [
            v
            for v in variables
            if v.form_id in (kw["formId"] if isinstance(kw["formId"], list) else [kw["formId"]])
        ]
        if "formId" in kw
        else list(variables)
    )
    sdk.records.list.side_effect = _list_records
    return sdk


def test_export_to_hive_parquet_as_sdtm_writes_complete_partitioned_forms_and_reconciles(
    tmp_path: Path,
) -> None:
    """export_to_hive_parquet(as_sdtm=True) writes complete form partitions and reconciles deltas."""
    study_key = "STUDY_HIVE_SDTM"
    upstream: dict[int, Record] = {
        1: Record(
            study_key=study_key,
            record_id=1,
            form_id=10,
            form_key="DM",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-03-01T10:00:00Z",
            record_data={"sex": "Female"},
        ),
        2: Record(
            study_key=study_key,
            record_id=2,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-03-01T10:00:00Z",
            record_data={"aeTerm": "Headache", "aeSerious": "No", "patientName": "John Doe"},
        ),
        3: Record(
            study_key=study_key,
            record_id=3,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-03-01T10:05:00Z",
            record_data={"aeTerm": "Nausea", "aeSerious": "No", "patientName": "John Doe"},
        ),
        4: Record(
            study_key=study_key,
            record_id=4,
            form_id=30,
            form_key="VS",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-03-01T10:10:00Z",
            record_data={"vsTest": "SYSBP", "vsResult": 120},
        ),
    }
    sdk = _make_incremental_sdtm_sdk(upstream, study_key)
    snap_dir = tmp_path / "hive_sdtm_snapshot"
    export_to_hive_parquet(sdk, study_key, str(snap_dir), as_sdtm=True)
    snap_counts = {
        fk: sum(
            pq.read_table(f).num_rows
            for f in (snap_dir / f"study_key={study_key}" / f"form_key={fk}").rglob("*.parquet")
        )
        for fk in ("DM", "AE", "VS")
    }
    assert snap_counts == {"DM": 1, "AE": 2, "VS": 1}

    hive_dir = tmp_path / "hive_sdtm"
    ledger = tmp_path / "hive_ledger.json"
    provider = FileStateProvider(ledger)

    # Initial snapshot across all 3 forms
    export_to_hive_parquet(
        sdk,
        study_key,
        str(hive_dir),
        as_sdtm=True,
        incremental=True,
        state_provider=provider,
    )

    dm_files = list((hive_dir / f"study_key={study_key}" / "form_key=DM").rglob("*.parquet"))
    ae_files = list((hive_dir / f"study_key={study_key}" / "form_key=AE").rglob("*.parquet"))
    vs_files = list((hive_dir / f"study_key={study_key}" / "form_key=VS").rglob("*.parquet"))
    assert dm_files and ae_files and vs_files

    dm_df = pd.concat([pq.read_table(f).to_pandas() for f in dm_files], ignore_index=True)
    ae_df = pd.concat([pq.read_table(f).to_pandas() for f in ae_files], ignore_index=True)
    vs_df = pd.concat([pq.read_table(f).to_pandas() for f in vs_files], ignore_index=True)
    assert len(dm_df) == 1
    assert len(ae_df) == 2
    assert len(vs_df) == 1
    assert set(ae_df["RECORD_ID"].astype(int)) == {2, 3}
    assert dict(zip(ae_df["RECORD_ID"].astype(int), ae_df["AESEQ"].astype(int), strict=True)) == {
        2: 1,
        3: 2,
    }
    assert set(ae_df["patientName"]) == {"***MASKED***"}

    # Incremental delta: update record 3, void record 2, insert new record 5
    upstream[2] = upstream[2].model_copy(
        update={"deleted": True, "date_modified": "2026-03-02T12:00:00Z"}
    )
    upstream[3] = upstream[3].model_copy(
        update={
            "date_modified": "2026-03-02T12:05:00Z",
            "record_data": {
                "aeTerm": "Severe Nausea",
                "aeSerious": "Yes",
                "patientName": "John Doe",
            },
        }
    )
    upstream[5] = Record(
        study_key=study_key,
        record_id=5,
        form_id=20,
        form_key="AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-03-02T12:10:00Z",
        record_data={"aeTerm": "Dizziness", "aeSerious": "No", "patientName": "John Doe"},
    )

    export_to_hive_parquet(
        sdk,
        study_key,
        str(hive_dir),
        as_sdtm=True,
        incremental=True,
        state_provider=provider,
    )

    ae_files_after = list((hive_dir / f"study_key={study_key}" / "form_key=AE").rglob("*.parquet"))
    ae_after = pd.concat(
        [pq.read_table(f).to_pandas() for f in ae_files_after], ignore_index=True
    ).sort_values("RECORD_ID")
    assert list(ae_after["RECORD_ID"].astype(int)) == [3, 5]
    # Updated record 3 retains AESEQ=2; new record 5 is seeded after max (2) -> AESEQ=3
    assert dict(
        zip(ae_after["RECORD_ID"].astype(int), ae_after["AESEQ"].astype(int), strict=True)
    ) == {3: 2, 5: 3}
    assert dict(zip(ae_after["RECORD_ID"].astype(int), ae_after["AETERM"], strict=True)) == {
        3: "Severe Nausea",
        5: "Dizziness",
    }


def test_incremental_sdtm_preserves_seq_seeds_max_and_deletes_voided_across_tabular_sinks(
    tmp_path: Path,
) -> None:
    """CSV, Parquet, SQL, and DuckDB preserve unrelated <DOM>SEQ, seed new <DOM>SEQ, and delete voided."""
    study_key = "STUDY_TAB_SDTM"
    upstream: dict[int, Record] = {
        10: Record(
            study_key=study_key,
            record_id=10,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-03-01T10:00:00Z",
            record_data={"aeTerm": "Headache", "aeSerious": "No", "patientName": "Jane Doe"},
        ),
        20: Record(
            study_key=study_key,
            record_id=20,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-03-01T10:05:00Z",
            record_data={"aeTerm": "Nausea", "aeSerious": "No", "patientName": "Jane Doe"},
        ),
        30: Record(
            study_key=study_key,
            record_id=30,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-03-01T10:10:00Z",
            record_data={"aeTerm": "Rash", "aeSerious": "No", "patientName": "Jane Doe"},
        ),
    }
    sdk = _make_incremental_sdtm_sdk(upstream, study_key)

    csv_path = tmp_path / "ae.csv"
    pq_path = tmp_path / "ae.parquet"
    sql_db = tmp_path / "ae.sqlite"
    sql_url = f"sqlite:///{sql_db}"
    ddb_path = tmp_path / "ae.duckdb"

    csv_prov = FileStateProvider(tmp_path / "ledger_csv.json")
    pq_prov = FileStateProvider(tmp_path / "ledger_pq.json")
    sql_prov = FileStateProvider(tmp_path / "ledger_sql.json")
    ddb_prov = FileStateProvider(tmp_path / "ledger_ddb.json")

    # Run 1: initial incremental snapshot
    export_to_csv(
        sdk,
        study_key,
        str(csv_path),
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=csv_prov,
    )
    export_to_parquet(
        sdk,
        study_key,
        str(pq_path),
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=pq_prov,
    )
    export_to_sql(
        sdk,
        study_key,
        "ae_sdtm",
        sql_url,
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=sql_prov,
    )
    export_to_duckdb(
        sdk,
        study_key,
        str(ddb_path),
        "ae_sdtm",
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=ddb_prov,
    )

    # Run 2: leave record 10 untouched (AESEQ=1), update record 20 (AESEQ=2), void record 30 (AESEQ=3), insert record 40 (must get AESEQ=4!)
    upstream[20] = upstream[20].model_copy(
        update={
            "date_modified": "2026-03-02T11:00:00Z",
            "record_data": {
                "aeTerm": "Moderate Nausea",
                "aeSerious": "Yes",
                "patientName": "Jane Doe",
            },
        }
    )
    upstream[30] = upstream[30].model_copy(
        update={
            "deleted": True,
            "date_modified": "2026-03-02T11:05:00Z",
        }
    )
    upstream[40] = Record(
        study_key=study_key,
        record_id=40,
        form_id=20,
        form_key="AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-03-02T11:10:00Z",
        record_data={"aeTerm": "Insomnia", "aeSerious": "No", "patientName": "Jane Doe"},
    )

    export_to_csv(
        sdk,
        study_key,
        str(csv_path),
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=csv_prov,
    )
    export_to_parquet(
        sdk,
        study_key,
        str(pq_path),
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=pq_prov,
    )
    export_to_sql(
        sdk,
        study_key,
        "ae_sdtm",
        sql_url,
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=sql_prov,
    )
    export_to_duckdb(
        sdk,
        study_key,
        str(ddb_path),
        "ae_sdtm",
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=ddb_prov,
    )

    frames: dict[str, pd.DataFrame] = {
        "csv": pd.read_csv(csv_path),
        "parquet": pq.read_table(pq_path).to_pandas(),
    }
    with sqlite3.connect(sql_db) as conn:
        frames["sql"] = pd.read_sql_query("SELECT * FROM ae_sdtm", conn)
    with duckdb.connect(str(ddb_path)) as dconn:
        frames["duckdb"] = dconn.execute("SELECT * FROM ae_sdtm").df()

    expected_by_id = {
        10: (1, "Headache", "N"),
        20: (2, "Moderate Nausea", "Y"),
        40: (4, "Insomnia", "N"),
    }
    for sink_name, df in frames.items():
        actual = {
            int(row["RECORD_ID"]): (int(row["AESEQ"]), str(row["AETERM"]), str(row["AESER"]))
            for _, row in df.iterrows()
        }
        assert actual == expected_by_id, f"Mismatch in {sink_name}: {actual}"
