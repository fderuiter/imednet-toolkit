"""Unit tests for record mapper."""

import tracemalloc
from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from faker import Faker
from pydantic import BaseModel, Field, ValidationError

from imednet.models.codings import Coding
from imednet.models.intervals import Interval
from imednet.models.records import Record
from imednet.models.reporting import (
    AdverseEvent,
    ConcomitantMedication,
    Demographics,
    DeviceDeficiency,
    Disposition,
    EcgResult,
    Exposure,
    InclusionExclusion,
    LaboratoryResult,
    MedicalHistory,
    PhysicalExam,
    ProtocolDeviation,
    VitalSigns,
)
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet_workflows.record_mapper import (
    SDTM_DOMAIN_SPECS,
    RecordMapper,
    _get_model_field_aliases,
    _normalize_key,
    format_iso8601_dtc,
    transform_records_to_sdtm,
)

# A 5k-row chunk with two mapped columns should stay comfortably below this
# threshold; a fully materialized 50k-row mapping regression would be
# substantially larger.
_STREAMING_PEAK_BYTES_LIMIT = 35_000_000


def test_dataframe_builds_expected_structure() -> None:
    """Test that dataframe builds expected structure."""
    sdk = MagicMock()
    variables = [
        Variable(variable_name="VAR1", label="Label1", form_id=10),
        Variable(variable_name="VAR2", label="Label2", form_id=10),
    ]

    def var_list(**kwargs):
        """Helper function to var list."""
        assert kwargs == {"study_key": "STUDY"}
        return variables

    sdk.get_variables.side_effect = var_list
    records = [
        Record(
            record_id=1,
            subject_key="S1",
            visit_id=1,
            form_id=10,
            record_status="Complete",
            date_created=datetime(2021, 1, 1),
            record_data={"VAR1": "a", "VAR2": "b"},
        )
    ]
    sdk.get_records.return_value = records

    mapper = RecordMapper(sdk)
    df = mapper.dataframe("STUDY", visit_key="1")

    sdk.get_variables.assert_called_once_with(study_key="STUDY")
    sdk.get_records.assert_called_once_with(
        study_key="STUDY",
        record_data_filter=None,
        visitId=1,
    )

    expected_columns = [
        "recordId",
        "subjectKey",
        "visitId",
        "formId",
        "recordStatus",
        "dateCreated",
        "Label1",
        "Label2",
    ]
    assert list(df.columns) == expected_columns
    assert df.iloc[0]["Label1"] == "a"
    assert df.iloc[0]["Label2"] == "b"


def test_dataframe_whitelists_variables_and_forms() -> None:
    """Test that dataframe whitelists variables and forms."""
    sdk = MagicMock()
    variables = [
        Variable(variable_name="VAR1", label="Label1", form_id=10),
        Variable(variable_name="VAR2", label="Label2", form_id=20),
    ]

    def var_list(**kwargs):
        """Helper function to var list."""
        assert kwargs == {
            "study_key": "STUDY",
            "variableName": "VAR1",
            "formId": 10,
        }
        return [variables[0]]

    sdk.get_variables.side_effect = var_list
    record = Record(
        record_id=1,
        subject_key="S1",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        date_created=datetime(2021, 1, 1),
        record_data={"VAR1": "a", "VAR2": "b"},
    )

    def rec_list(**kwargs):
        """Helper function to rec list."""
        assert kwargs == {
            "study_key": "STUDY",
            "record_data_filter": None,
            "formId": 10,
        }
        return [record]

    sdk.get_records.side_effect = rec_list

    mapper = RecordMapper(sdk)
    df = mapper.dataframe("STUDY", variable_whitelist=["VAR1"], form_whitelist=[10])

    sdk.get_variables.assert_called_once_with(
        study_key="STUDY",
        variableName="VAR1",
        formId=10,
    )
    sdk.get_records.assert_called_once_with(
        study_key="STUDY",
        record_data_filter=None,
        formId=10,
    )
    assert list(df.columns) == [
        "recordId",
        "subjectKey",
        "visitId",
        "formId",
        "recordStatus",
        "dateCreated",
        "Label1",
    ]


def test_dataframe_empty_when_no_variables() -> None:
    """Test that dataframe empty when no variables."""
    sdk = MagicMock()
    sdk.get_variables.return_value = []
    mapper = RecordMapper(sdk)
    df = mapper.dataframe("STUDY")
    assert df.empty


def test_invalid_visit_key_logs_warning(caplog) -> None:
    """Test that invalid visit key logs warning."""
    sdk = MagicMock()
    sdk.get_variables.return_value = [Variable(variable_name="VAR", label="L", form_id=1)]
    sdk.get_records.return_value = []
    mapper = RecordMapper(sdk)

    with caplog.at_level("WARNING"):
        df = mapper.dataframe("S", visit_key="bad")

    assert df.empty
    assert "Invalid visit_key" in caplog.text
    sdk.get_records.assert_called_once_with(study_key="S", record_data_filter=None)


def test_records_fetch_error_returns_empty(caplog) -> None:
    """Test that records fetch error returns empty."""
    sdk = MagicMock()
    sdk.get_variables.return_value = [Variable(variable_name="VAR", label="L", form_id=1)]
    sdk.get_records.side_effect = Exception("boom")
    mapper = RecordMapper(sdk)

    with caplog.at_level("ERROR"):
        df = mapper.dataframe("S")

    assert df.empty
    assert "Failed to fetch records" in caplog.text


def test_fetch_records_propagates_value_error() -> None:
    """Test that _fetch_records re-raises ValueError instead of swallowing it."""
    sdk = MagicMock()
    sdk.get_records.side_effect = ValueError("Invalid filter operator")
    mapper = RecordMapper(sdk)

    with pytest.raises(ValueError, match="Invalid filter operator"):
        mapper._fetch_records("STUDY", extra_filters={"badKey!": "val"})


def test_dataframe_multi_form_and_variable_whitelists_batch_without_mixed_connectors() -> None:
    """Test multi-item variable_whitelist and form_whitelist emit singular filters and batch per formId."""
    import httpx
    import respx

    from imednet.sdk import ImednetSDK

    with respx.mock(assert_all_called=True) as mock_router:
        var_route = mock_router.get("https://api.test/api/v1/edc/studies/STUDY/variables").respond(
            200,
            json={
                "data": [
                    {"variableName": "AGE", "label": "Age", "formId": 10},
                    {"variableName": "WT", "label": "Weight", "formId": 20},
                    {"variableName": "IGNORED", "label": "Ignored", "formId": 20},
                ]
            },
        )
        rec_calls: list[dict[str, str]] = []

        def records_responder(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            rec_calls.append(params)
            flt = params.get("filter", "")
            assert "variableNames" not in flt
            assert "formIds" not in flt
            if "formId==10" in flt:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "recordId": 1,
                                "subjectKey": "S1",
                                "visitId": 1,
                                "formId": 10,
                                "recordStatus": "Complete",
                                "recordData": {"AGE": 45},
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "recordId": 2,
                            "subjectKey": "S1",
                            "visitId": 1,
                            "formId": 20,
                            "recordStatus": "Complete",
                            "recordData": {"WT": 72, "IGNORED": "x"},
                        }
                    ]
                },
            )

        mock_router.get("https://api.test/api/v1/edc/studies/STUDY/records").mock(
            side_effect=records_responder
        )

        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        mapper = RecordMapper(sdk)
        df = mapper.dataframe(
            "STUDY",
            use_labels_as_columns=False,
            variable_whitelist=["AGE", "WT"],
            form_whitelist=[10, 20],
        )

        var_filter = var_route.calls.last.request.url.params["filter"]
        assert var_filter == "formId==10,formId==20"
        assert len(rec_calls) == 2
        assert rec_calls[0]["filter"] == "formId==10;studyKey==STUDY"
        assert rec_calls[1]["filter"] == "formId==20;studyKey==STUDY"
        assert list(df.columns) == [
            "recordId",
            "subjectKey",
            "visitId",
            "formId",
            "recordStatus",
            "dateCreated",
            "AGE",
            "WT",
        ]
        assert len(df) == 2


def test_parsing_error_logs_warning(monkeypatch, caplog) -> None:
    """Test that parsing error logs warning."""
    sdk = MagicMock()
    sdk.get_variables.return_value = [Variable(variable_name="V", label="L", form_id=1)]
    record = Record(
        record_id=1,
        subject_key="S1",
        visit_id=1,
        form_id=1,
        record_status="Complete",
        date_created=datetime.now(),
        record_data={"V": "x"},
    )
    sdk.get_records.return_value = [record]

    class DummyModel(BaseModel):
        """Test suite for DummyModel."""

        def __init__(self, **kwargs):
            """Initialize the test object."""
            raise ValidationError([], DummyModel)

    monkeypatch.setattr("imednet_workflows.record_mapper.create_model", lambda *a, **k: DummyModel)

    mapper = RecordMapper(sdk)
    with caplog.at_level("WARNING"):
        df = mapper.dataframe("S")

    assert df.empty
    assert "Failed to parse record data" in caplog.text


def test_parse_records_counts_errors() -> None:
    """Test that parse records counts errors."""
    mapper = RecordMapper(MagicMock())
    records = [
        Record(
            record_id=1,
            subject_key="S1",
            visit_id=1,
            form_id=1,
            record_status="Complete",
            date_created=datetime.now(),
            record_data={},
        ),
        Record(
            record_id=2,
            subject_key="S1",
            visit_id=1,
            form_id=1,
            record_status="Complete",
            date_created=datetime.now(),
            record_data={},
        ),
    ]

    class Dummy(BaseModel):
        """Test suite for Dummy."""

        def __init__(self, **_: Any) -> None:
            """Always fail."""
            raise ValidationError([], Dummy)

    rows, count = mapper._parse_records(records, Dummy)

    assert rows == []
    assert count == 2


def test_dataframe_raises_importerror_when_pandas_missing(monkeypatch) -> None:
    """Test that dataframe raises importerror when pandas missing."""
    monkeypatch.setattr("imednet_workflows.record_mapper.pd", None)
    mapper = RecordMapper(MagicMock())
    with pytest.raises(ImportError, match="pandas is required for RecordMapper.dataframe"):
        mapper.dataframe("STUDY")


def test_iter_dataframes_streams_large_study_with_bounded_memory() -> None:
    """Test that iter dataframes streams large study with bounded memory."""
    fake = Faker()

    class _StreamingLoader:
        """Test suite for  StreamingLoader."""

        def __init__(self) -> None:
            """Initialize the test object."""
            self.sync_records = MagicMock()

        def iter_cached_records(self, study_key: str, *, chunk_size: int = 5_000):
            """Helper function to iter cached records."""
            assert study_key == "STUDY"
            assert chunk_size == 5_000
            for record_id in range(50_000):
                yield Record(
                    study_key="STUDY",
                    record_id=record_id,
                    subject_key=f"S{record_id}",
                    visit_id=record_id % 5,
                    form_id=10,
                    form_key="LAB",
                    record_status="Complete",
                    date_created=datetime(2024, 1, 1),
                    record_data={
                        "AGE": fake.pyint(min_value=18, max_value=90),
                        "COMMENT": fake.lexify(text="????????"),
                    },
                )

    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(variable_name="AGE", label="Age", form_id=10),
        Variable(variable_name="COMMENT", label="Comment", form_id=10),
    ]
    loader = _StreamingLoader()
    mapper = RecordMapper(sdk, loader=loader, chunk_size=5_000)

    tracemalloc.start()
    chunk_sizes: list[int] = []
    peak_bytes = 0
    for frame in mapper.iter_dataframes("STUDY", use_labels_as_columns=False):
        chunk_sizes.append(len(frame))
        _, current_peak = tracemalloc.get_traced_memory()
        peak_bytes = max(peak_bytes, current_peak)
    tracemalloc.stop()

    loader.sync_records.assert_called_once_with("STUDY")
    assert chunk_sizes == [5_000] * 10
    assert peak_bytes < _STREAMING_PEAK_BYTES_LIMIT


def test_parse_record_preserves_raw_date_strings() -> None:
    """Test that general exports preserve raw strings and normalize only metadata."""
    sdk = MagicMock()
    mapper = RecordMapper(sdk)

    record_model = mapper._build_record_model(
        ["DATE_COL1", "DATE_COL2", "TEXT_COL"],
        {"DATE_COL1": "Date 1", "DATE_COL2": "Date 2", "TEXT_COL": "Text"},
    )

    rec = Record(
        record_id=42,
        subject_key="  SUBJ-001  ",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        date_created=datetime(2024, 3, 15, 10, 0, 0),
        record_data={
            "DATE_COL1": "2024/03/15",
            "DATE_COL2": "15-MAR-2024",
            "TEXT_COL": "Non-date string",
        },
    )

    parsed = mapper._parse_record(rec, record_model)

    assert parsed["recordId"] == 42
    assert parsed["subjectKey"] == "SUBJ-001"
    assert parsed["dateCreated"] == str(rec.date_created)
    assert parsed["DATE_COL1"] == "2024/03/15"
    assert parsed["DATE_COL2"] == "15-MAR-2024"
    assert parsed["TEXT_COL"] == "Non-date string"


def test_format_iso8601_dtc() -> None:
    """Test standard ISO 8601 DTC timestamp conversion."""
    # Datetime object
    assert format_iso8601_dtc(datetime(2026, 1, 15, 9, 30, 0)) == "2026-01-15T09:30:00"
    # Date object
    assert format_iso8601_dtc(date(2026, 1, 15)) == "2026-01-15"
    # ISO string
    assert format_iso8601_dtc("2026-01-15T09:30:00") == "2026-01-15T09:30:00"
    # Standard format with space
    assert format_iso8601_dtc("2026-01-15 09:30:00") == "2026-01-15T09:30:00"
    # Date-only patterns
    assert format_iso8601_dtc("01/15/2026") == "2026-01-15"
    assert format_iso8601_dtc("2026-01-15") == "2026-01-15"
    assert format_iso8601_dtc("2026/01/15") == "2026-01-15"
    assert format_iso8601_dtc("01-15-2026") == "2026-01-15"
    assert format_iso8601_dtc("15-Jan-2026") == "2026-01-15"
    assert format_iso8601_dtc("15-MAR-2024") == "2024-03-15"
    assert format_iso8601_dtc("15-January-2026") == "2026-01-15"
    # Datetime patterns
    assert format_iso8601_dtc("2026-01-15 09:30:00.123456") == "2026-01-15T09:30:00.123456"
    assert format_iso8601_dtc("2026/01/15 09:30:00") == "2026-01-15T09:30:00"
    assert format_iso8601_dtc("01/15/2026 09:30:00") == "2026-01-15T09:30:00"
    # None or empty
    assert format_iso8601_dtc(None) is None
    assert format_iso8601_dtc("") is None
    assert format_iso8601_dtc("   ") is None
    assert format_iso8601_dtc(float("nan")) is None
    # Unmatched / non-date strings gracefully preserved
    assert format_iso8601_dtc("invalid-date") == "invalid-date"
    assert format_iso8601_dtc("not-a-date") == "not-a-date"
    # Numeric values converted to str
    assert format_iso8601_dtc(12345) == "12345"


def test_record_mapper_sdtm_ae_domain() -> None:
    """Test SDTM transformation for Adverse Events (AE domain)."""
    sdk = MagicMock()
    records = [
        Record(
            record_id=101,
            subject_key="SUBJ-001",
            visit_id=1,
            form_id=20,
            record_status="Complete",
            date_created=datetime(2026, 1, 1),
            record_data={
                "aeTerm": "Headache",
                "aeDecod": "Headache Preferred",
                "aeSeverity": "MILD",
                "aeSerious": False,
                "aeStartDate": "2026-01-15 09:30:00",
                "aeEndDate": "2026-01-16 18:00:00",
                "aeOutcome": "Recovered",
                "aeRelationship": "Not Related",
                "aeActionTaken": "None",
            },
        ),
        Record(
            record_id=102,
            subject_key="SUBJ-001",
            visit_id=2,
            form_id=20,
            record_status="Complete",
            date_created=datetime(2026, 2, 1),
            record_data={
                "aeTerm": "Nausea",
                "aeDecod": "Nausea",
                "aeSeverity": "SEVERE",
                "aeSerious": True,
                "aeStartDate": datetime(2026, 2, 1, 8, 0, 0),
                "aeEndDate": None,
                "aeOutcome": "Not Recovered",
                "aeRelationship": "Probable",
                "aeActionTaken": "Dose Reduced",
            },
        ),
    ]
    sdk.get_records.return_value = records

    mapper = RecordMapper(sdk)
    df = mapper.sdtm_dataframe(study_key="STUDY_ONCO_01", domain="AE")

    assert not df.empty
    # Validate required domain variables
    expected_cols = [
        "STUDYID",
        "DOMAIN",
        "USUBJID",
        "AESEQ",
        "AETERM",
        "AEDECOD",
        "AESEV",
        "AESER",
        "AESTDTC",
        "AEENDTC",
        "AEOUT",
        "AEREL",
        "AEACN",
    ]
    for col in expected_cols:
        assert col in df.columns

    # Row 1 assertions
    assert df.iloc[0]["STUDYID"] == "STUDY_ONCO_01"
    assert df.iloc[0]["DOMAIN"] == "AE"
    assert df.iloc[0]["USUBJID"] == "STUDY_ONCO_01-SUBJ-001"
    assert df.iloc[0]["AESEQ"] == 1
    assert df.iloc[0]["AETERM"] == "Headache"
    assert df.iloc[0]["AESEV"] == "MILD"
    assert df.iloc[0]["AESER"] == "N"
    assert df.iloc[0]["AESTDTC"] == "2026-01-15T09:30:00"
    assert df.iloc[0]["AEENDTC"] == "2026-01-16T18:00:00"

    # Row 2 assertions
    assert df.iloc[1]["AESEQ"] == 2
    assert df.iloc[1]["AETERM"] == "Nausea"
    assert df.iloc[1]["AESER"] == "Y"
    assert df.iloc[1]["AESTDTC"] == "2026-02-01T08:00:00"


def test_record_mapper_dataframe_mode_as_sdtm() -> None:
    """Test calling mapper.dataframe with as_sdtm=True."""
    sdk = MagicMock()
    records = [
        Record(
            record_id=201,
            subject_key="SUBJ-002",
            visit_id=1,
            form_id=20,
            record_status="Complete",
            date_created=datetime(2026, 1, 1),
            record_data={
                "aeTerm": "Dizziness",
                "aeSeverity": "MODERATE",
                "aeSerious": False,
            },
        )
    ]
    sdk.get_records.return_value = records

    mapper = RecordMapper(sdk)
    df = mapper.dataframe(study_key="STUDY_TEST", as_sdtm=True, domain="AE")

    assert df.iloc[0]["STUDYID"] == "STUDY_TEST"
    assert df.iloc[0]["USUBJID"] == "STUDY_TEST-SUBJ-002"
    assert df.iloc[0]["AETERM"] == "Dizziness"
    assert df.iloc[0]["AESEV"] == "MODERATE"
    assert df.iloc[0]["AESER"] == "N"


def test_record_mapper_reporting_models_transformation() -> None:
    """Test transforming reporting Pydantic models (AdverseEvent, ProtocolDeviation, DeviceDeficiency)."""
    # 1. AdverseEvent model
    ae = AdverseEvent(
        subjectKey="SUBJ-AE-10",
        aeTerm="Rash",
        aeDecod="Dermatitis",
        aeSeverity="MILD",
        aeSerious=False,
        aeStartDate=datetime(2026, 3, 1, 10, 0),
        aeEndDate=datetime(2026, 3, 2, 12, 0),
        aeOutcome="Recovered",
        aeRelationship="Related",
        aeActionTaken="None",
    )
    df_ae = transform_records_to_sdtm([ae], domain="AE", study_key="STUDY_REP_01")
    assert df_ae.iloc[0]["STUDYID"] == "STUDY_REP_01"
    assert df_ae.iloc[0]["USUBJID"] == "STUDY_REP_01-SUBJ-AE-10"
    assert df_ae.iloc[0]["DOMAIN"] == "AE"
    assert df_ae.iloc[0]["AETERM"] == "Rash"
    assert df_ae.iloc[0]["AEDECOD"] == "Dermatitis"
    assert df_ae.iloc[0]["AESEV"] == "MILD"
    assert df_ae.iloc[0]["AESER"] == "N"
    assert df_ae.iloc[0]["AESTDTC"] == "2026-03-01T10:00:00"
    assert df_ae.iloc[0]["AEENDTC"] == "2026-03-02T12:00:00"
    assert list(df_ae.columns) == [
        "STUDYID",
        "DOMAIN",
        "USUBJID",
        "AESEQ",
        "AETERM",
        "AEDECOD",
        "AESEV",
        "AESER",
        "AESTDTC",
        "AEENDTC",
        "AEOUT",
        "AEREL",
        "AEACN",
    ]

    # 2. ProtocolDeviation model (DV domain)
    pd_item = ProtocolDeviation(
        subjectKey="SUBJ-PD-20",
        dvTerm="Eligibility criteria violation",
        dvCategory="ELIGIBILITY",
        dvSeverity="MAJOR",
        dvDate=datetime(2026, 3, 5, 14, 30),
        dvStatus="Escalated",
    )
    df_pd = transform_records_to_sdtm([pd_item], domain="DV", study_key="STUDY_REP_01")
    assert df_pd.iloc[0]["STUDYID"] == "STUDY_REP_01"
    assert df_pd.iloc[0]["USUBJID"] == "STUDY_REP_01-SUBJ-PD-20"
    assert df_pd.iloc[0]["DOMAIN"] == "DV"
    assert df_pd.iloc[0]["DVTERM"] == "Eligibility criteria violation"
    assert df_pd.iloc[0]["DVCAT"] == "ELIGIBILITY"
    assert df_pd.iloc[0]["DVSEV"] == "MAJOR"
    assert df_pd.iloc[0]["DVSTDTC"] == "2026-03-05T14:30:00"
    assert df_pd.iloc[0]["DVSTAT"] == "Escalated"
    assert list(df_pd.columns) == [
        "STUDYID",
        "DOMAIN",
        "USUBJID",
        "DVSEQ",
        "DVTERM",
        "DVCAT",
        "DVSEV",
        "DVSTDTC",
        "DVSTAT",
    ]

    # Also verify 'PD' domain code alias maps to 'DV'
    df_pd_alias = transform_records_to_sdtm([pd_item], domain="PD", study_key="STUDY_REP_01")
    assert df_pd_alias.iloc[0]["DOMAIN"] == "DV"
    assert "DVTERM" in df_pd_alias.columns

    # 3. DeviceDeficiency model (DD domain)
    dd_item = DeviceDeficiency(
        subjectKey="SUBJ-DD-30",
        ddTerm="Software freeze",
        ddCategory="SOFTWARE",
        ddSerious=True,
        ddDate=datetime(2026, 3, 10, 9, 0),
    )
    df_dd = transform_records_to_sdtm([dd_item], domain="DD", study_key="STUDY_REP_01")
    assert df_dd.iloc[0]["STUDYID"] == "STUDY_REP_01"
    assert df_dd.iloc[0]["USUBJID"] == "STUDY_REP_01-SUBJ-DD-30"
    assert df_dd.iloc[0]["DOMAIN"] == "DD"
    assert df_dd.iloc[0]["DDTERM"] == "Software freeze"
    assert df_dd.iloc[0]["DDCAT"] == "SOFTWARE"
    assert df_dd.iloc[0]["DDSER"] == "Y"
    assert df_dd.iloc[0]["DDSTDTC"] == "2026-03-10T09:00:00"
    assert list(df_dd.columns) == [
        "STUDYID",
        "DOMAIN",
        "USUBJID",
        "DDSEQ",
        "DDTERM",
        "DDCAT",
        "DDSER",
        "DDSTDTC",
    ]


def test_record_mapper_concomitant_medications_domain() -> None:
    """Test SDTM transformation for Concomitant Medications (CM domain)."""
    raw_records = [
        {
            "subjectKey": "SUBJ-CM-01",
            "cmTerm": "Aspirin",
            "cmDecod": "Acetylsalicylic acid",
            "cmDose": "100",
            "cmDoseUnit": "mg",
            "cmRoute": "ORAL",
            "cmStartDate": "2026-01-01",
            "cmEndDate": "2026-01-10",
            "cmIndication": "Pain relief",
        }
    ]
    df_cm = transform_records_to_sdtm(raw_records, domain="CM", study_key="STUDY_CM_01")
    assert df_cm.iloc[0]["STUDYID"] == "STUDY_CM_01"
    assert df_cm.iloc[0]["DOMAIN"] == "CM"
    assert df_cm.iloc[0]["USUBJID"] == "STUDY_CM_01-SUBJ-CM-01"
    assert df_cm.iloc[0]["CMSEQ"] == 1
    assert df_cm.iloc[0]["CMTRT"] == "Aspirin"
    assert df_cm.iloc[0]["CMDECOD"] == "Acetylsalicylic acid"
    assert df_cm.iloc[0]["CMDOSE"] == "100"
    assert df_cm.iloc[0]["CMDOSU"] == "mg"
    assert df_cm.iloc[0]["CMROUTE"] == "ORAL"
    assert df_cm.iloc[0]["CMSTDTC"] == "2026-01-01"
    assert df_cm.iloc[0]["CMENDTC"] == "2026-01-10"
    assert df_cm.iloc[0]["CMINDC"] == "Pain relief"


def test_record_mapper_vital_signs_domain() -> None:
    """Test SDTM transformation for Vital Signs (VS domain) with models and raw dicts."""
    # Canonical VitalSigns model
    vs_item = VitalSigns(
        subjectKey="SUBJ-VS-01",
        vsTest="Diastolic Blood Pressure",
        vsTestCd="DIABP",
        vsResult="80.0",
        vsUnit="mmHg",
        vsDate=datetime(2026, 3, 12, 8, 30),
    )
    df_model = transform_records_to_sdtm([vs_item], domain="VS", study_key="STUDY_VS_01")
    assert df_model.iloc[0]["STUDYID"] == "STUDY_VS_01"
    assert df_model.iloc[0]["DOMAIN"] == "VS"
    assert df_model.iloc[0]["USUBJID"] == "STUDY_VS_01-SUBJ-VS-01"
    assert df_model.iloc[0]["VSSEQ"] == 1
    assert df_model.iloc[0]["VSTESTCD"] == "DIABP"
    assert df_model.iloc[0]["VSTEST"] == "Diastolic Blood Pressure"
    assert df_model.iloc[0]["VSORRES"] == 80.0
    assert df_model.iloc[0]["VSORRESU"] == "mmHg"
    assert df_model.iloc[0]["VSDTC"] == "2026-03-12T08:30:00"
    assert list(df_model.columns) == [
        "STUDYID",
        "DOMAIN",
        "USUBJID",
        "VSSEQ",
        "VSTESTCD",
        "VSTEST",
        "VSORRES",
        "VSORRESU",
        "VSDTC",
    ]

    # Raw dictionary with standard aliases
    raw_records = [
        {
            "subjectKey": "SUBJ-VS-02",
            "vsTestCd": "SYSBP",
            "vsTest": "Systolic Blood Pressure",
            "vsResult": "120.5",
            "vsUnit": "mmHg",
            "vsDate": "2026-03-12T08:35:00",
        }
    ]
    df_raw = transform_records_to_sdtm(raw_records, domain="VS", study_key="STUDY_VS_01")
    assert df_raw.iloc[0]["STUDYID"] == "STUDY_VS_01"
    assert df_raw.iloc[0]["DOMAIN"] == "VS"
    assert df_raw.iloc[0]["USUBJID"] == "STUDY_VS_01-SUBJ-VS-02"
    assert df_raw.iloc[0]["VSSEQ"] == 1
    assert df_raw.iloc[0]["VSTESTCD"] == "SYSBP"
    assert df_raw.iloc[0]["VSTEST"] == "Systolic Blood Pressure"
    assert df_raw.iloc[0]["VSORRES"] == "120.5"
    assert df_raw.iloc[0]["VSORRESU"] == "mmHg"
    assert df_raw.iloc[0]["VSDTC"] == "2026-03-12T08:35:00"

    # Missing optional fields (vsTestCd, vsUnit omitted)
    vs_partial = VitalSigns(
        subjectKey="SUBJ-VS-03",
        vsTest="Heart Rate",
        vsResult=72,
        vsDate=datetime(2026, 3, 12, 8, 40),
    )
    df_partial = transform_records_to_sdtm([vs_partial], domain="VS", study_key="STUDY_VS_01")
    assert df_partial.iloc[0]["USUBJID"] == "STUDY_VS_01-SUBJ-VS-03"
    assert df_partial.iloc[0]["VSTEST"] == "Heart Rate"
    assert df_partial.iloc[0]["VSTESTCD"] is None
    assert df_partial.iloc[0]["VSORRESU"] is None
    assert df_partial.iloc[0]["VSORRES"] == 72.0

    # Empty records return empty DataFrame retaining all standard VS headers
    df_empty = transform_records_to_sdtm([], domain="VS", study_key="STUDY_VS_01")
    assert df_empty.empty
    expected_cols = [
        "STUDYID",
        "DOMAIN",
        "USUBJID",
        "VSSEQ",
        "VSTESTCD",
        "VSTEST",
        "VSORRES",
        "VSORRESU",
        "VSDTC",
    ]
    assert list(df_empty.columns) == expected_cols


def test_record_mapper_sdtm_empty() -> None:
    """Test sdtm_dataframe when no records are returned."""
    sdk = MagicMock()
    sdk.get_records.return_value = []
    mapper = RecordMapper(sdk)
    df = mapper.sdtm_dataframe(study_key="STUDY_EMPTY", domain="AE")
    assert df.empty
    assert "STUDYID" in df.columns
    assert "USUBJID" in df.columns
    assert "AETERM" in df.columns

    df_vs = mapper.sdtm_dataframe(study_key="STUDY_EMPTY", domain="VS")
    assert df_vs.empty
    assert list(df_vs.columns) == [
        "STUDYID",
        "DOMAIN",
        "USUBJID",
        "VSSEQ",
        "VSTESTCD",
        "VSTEST",
        "VSORRES",
        "VSORRESU",
        "VSDTC",
    ]


def test_record_mapper_iter_sdtm_dataframes() -> None:
    """Test streaming chunked SDTM DataFrames."""
    sdk = MagicMock()
    records = [
        Record(
            record_id=i,
            subject_key=f"SUBJ-{i}",
            visit_id=1,
            form_id=10,
            record_status="Complete",
            date_created=datetime(2026, 1, 1),
            record_data={"aeTerm": f"Event {i}", "aeSeverity": "MILD", "aeSerious": False},
        )
        for i in range(5)
    ]
    sdk.get_records.return_value = records

    mapper = RecordMapper(sdk, chunk_size=2)
    chunks = list(mapper.iter_sdtm_dataframes(study_key="STUDY_STREAM", domain="AE"))
    assert len(chunks) == 3
    assert len(chunks[0]) == 2
    assert len(chunks[1]) == 2
    assert len(chunks[2]) == 1
    assert all("AETERM" in c.columns for c in chunks)
    assert all(c.iloc[0]["STUDYID"] == "STUDY_STREAM" for c in chunks)


def test_sdtm_transformation_deduplicates_model_aliases() -> None:
    """Assert that AdverseEvent, VitalSigns, and ProtocolDeviation produce datasets without duplicate raw attributes."""
    ae = AdverseEvent(
        subjectKey="SUBJ-001",
        aeTerm="Nausea",
        aeSeverity="MILD",
        aeStartDate=datetime(2026, 1, 1, 8, 0),
    )
    df_ae = transform_records_to_sdtm([ae], domain="AE", study_key="STUDY01")
    for raw_attr in ("ae_term", "ae_severity", "ae_start_date", "subject_key", "aeTerm"):
        assert raw_attr not in df_ae.columns

    vs = VitalSigns(
        subjectKey="SUBJ-001",
        vsTest="Body Temperature",
        vsTestCd="TEMP",
        vsResult="37.2",
        vsUnit="C",
        vsDate=datetime(2026, 1, 1, 8, 30),
    )
    df_vs = transform_records_to_sdtm([vs], domain="VS", study_key="STUDY01")
    for raw_attr in (
        "vs_test",
        "vs_test_cd",
        "vs_result",
        "vs_unit",
        "vs_date",
        "subject_key",
        "vsTest",
    ):
        assert raw_attr not in df_vs.columns

    pd_item = ProtocolDeviation(
        subjectKey="SUBJ-001",
        dvTerm="Window deviation",
        dvCategory="PROCEDURE",
        dvSeverity="MINOR",
        dvDate=datetime(2026, 1, 2, 9, 0),
        dvStatus="Approved",
    )
    df_pd = transform_records_to_sdtm([pd_item], domain="DV", study_key="STUDY01")
    for raw_attr in (
        "dv_term",
        "dv_category",
        "dv_severity",
        "dv_date",
        "dv_status",
        "subject_key",
        "dvTerm",
    ):
        assert raw_attr not in df_pd.columns

    # Verify custom unmapped fields are retained
    raw_with_custom = {
        "subjectKey": "SUBJ-002",
        "aeTerm": "Fatigue",
        "custom_note": "Patient reported after lunch",
        "custom_score": 5,
    }
    df_custom = transform_records_to_sdtm([raw_with_custom], domain="AE", study_key="STUDY01")
    assert "custom_note" in df_custom.columns
    assert "custom_score" in df_custom.columns
    assert df_custom.iloc[0]["custom_note"] == "Patient reported after lunch"
    assert df_custom.iloc[0]["custom_score"] == 5

    # Verify model with identical field name and alias
    class SimpleModel(BaseModel):
        subject_key: str = Field(alias="subjectKey")
        term: str = Field(alias="term")

    m = SimpleModel(subjectKey="SUBJ-003", term="Headache")
    df_simple = transform_records_to_sdtm([m], domain="AE", study_key="STUDY01")
    assert df_simple.iloc[0]["AETERM"] == "Headache"
    assert "term" not in df_simple.columns


def test_sdtm_transformation_custom_alias_and_metadata_resolution() -> None:
    """Verify that models with custom aliases or serialization_alias resolve values and suppress duplicate columns."""

    # 1. Recognized field names with unrelated aliases
    class CustomAEWithAlias(BaseModel):
        subject_key: str = Field(alias="patientId")
        ae_term: str = Field(alias="reportedTerm")
        ae_severity: str = Field(alias="grade")
        ae_start_date: str = Field(alias="onset")
        unmapped_attr: str = Field("extra_val", alias="customExtra")

    rec_alias = CustomAEWithAlias(
        patientId="S100",
        reportedTerm="Headache",
        grade="MILD",
        onset="2026-05-01T10:00:00",
        customExtra="keep_this",
    )
    df_alias = transform_records_to_sdtm([rec_alias], domain="AE", study_key="STUDY01")
    assert df_alias.iloc[0]["USUBJID"] == "STUDY01-S100"
    assert df_alias.iloc[0]["AETERM"] == "Headache"
    assert df_alias.iloc[0]["AESEV"] == "MILD"
    assert df_alias.iloc[0]["AESTDTC"] == "2026-05-01T10:00:00"
    assert df_alias.iloc[0]["customExtra"] == "keep_this"
    for col in (
        "patientId",
        "reportedTerm",
        "grade",
        "onset",
        "ae_term",
        "ae_severity",
        "ae_start_date",
        "subject_key",
    ):
        assert col not in df_alias.columns
    assert "customExtra" in df_alias.columns

    # 2. Recognized field names with serialization_alias
    class CustomAEWithSerializationAlias(BaseModel):
        subject_key: str
        ae_term: str = Field(serialization_alias="customSerializationTerm")
        ae_severity: str = Field(serialization_alias="customSerializationSeverity")
        extra_metric: float = 98.6

    rec_serialized = CustomAEWithSerializationAlias(
        subject_key="S200",
        ae_term="Dizziness",
        ae_severity="MODERATE",
        extra_metric=99.1,
    )
    df_serialized = transform_records_to_sdtm([rec_serialized], domain="AE", study_key="STUDY01")
    assert df_serialized.iloc[0]["USUBJID"] == "STUDY01-S200"
    assert df_serialized.iloc[0]["AETERM"] == "Dizziness"
    assert df_serialized.iloc[0]["AESEV"] == "MODERATE"
    assert df_serialized.iloc[0]["extra_metric"] == 99.1
    for col in (
        "customSerializationTerm",
        "customSerializationSeverity",
        "ae_term",
        "ae_severity",
        "subject_key",
    ):
        assert col not in df_serialized.columns
    assert "extra_metric" in df_serialized.columns

    # 3. Ordinary SDK camelCase aliases
    ae_sdk = AdverseEvent(
        subjectKey="SUBJ-SDK",
        aeTerm="Fever",
        aeSeverity="SEVERE",
        aeSerious=True,
        aeStartDate=datetime(2026, 4, 1, 12, 0),
    )
    df_sdk = transform_records_to_sdtm([ae_sdk], domain="AE", study_key="STUDY01")
    assert df_sdk.iloc[0]["USUBJID"] == "STUDY01-SUBJ-SDK"
    assert df_sdk.iloc[0]["AETERM"] == "Fever"
    assert df_sdk.iloc[0]["AESEV"] == "SEVERE"
    assert df_sdk.iloc[0]["AESER"] == "Y"
    assert df_sdk.iloc[0]["AESTDTC"] == "2026-04-01T12:00:00"
    for col in (
        "aeTerm",
        "ae_term",
        "aeSeverity",
        "ae_severity",
        "aeSerious",
        "ae_serious",
        "subjectKey",
        "subject_key",
    ):
        assert col not in df_sdk.columns

    # 4. Dictionary input with camelCase keys
    dict_camel = {
        "subjectKey": "DICT-01",
        "aeTerm": "Nausea",
        "aeSeverity": "MILD",
        "extra_note": "Post-dose",
    }
    df_dict_camel = transform_records_to_sdtm([dict_camel], domain="AE", study_key="STUDY01")
    assert df_dict_camel.iloc[0]["USUBJID"] == "STUDY01-DICT-01"
    assert df_dict_camel.iloc[0]["AETERM"] == "Nausea"
    assert df_dict_camel.iloc[0]["AESEV"] == "MILD"
    assert df_dict_camel.iloc[0]["extra_note"] == "Post-dose"
    for col in ("subjectKey", "aeTerm", "aeSeverity", "subject_key", "ae_term", "ae_severity"):
        assert col not in df_dict_camel.columns
    assert "extra_note" in df_dict_camel.columns

    # 5. Dictionary input with snake_case keys
    dict_snake = {
        "subject_key": "DICT-02",
        "ae_term": "Cough",
        "ae_severity": "MILD",
        "unmapped_val": 42,
    }
    df_dict_snake = transform_records_to_sdtm([dict_snake], domain="AE", study_key="STUDY01")
    assert df_dict_snake.iloc[0]["USUBJID"] == "STUDY01-DICT-02"
    assert df_dict_snake.iloc[0]["AETERM"] == "Cough"
    assert df_dict_snake.iloc[0]["AESEV"] == "MILD"
    assert df_dict_snake.iloc[0]["unmapped_val"] == 42
    for col in ("subject_key", "ae_term", "ae_severity", "subjectKey", "aeTerm", "aeSeverity"):
        assert col not in df_dict_snake.columns
    assert "unmapped_val" in df_dict_snake.columns


def test_sdtm_transformation_large_batch_benchmark() -> None:
    """Benchmark SDTM transformation on a large batch verifying cached aliases and date parsing dispatch."""
    import time

    class BenchmarkAE(BaseModel):
        subject_key: str = Field(alias="patientId")
        ae_term: str = Field(alias="reportedTerm")
        ae_severity: str = Field(alias="grade")
        ae_start_date: str = Field(alias="onset")
        ae_end_date: str = Field(alias="resolution")
        extra_score: int = 100

    batch_size = 5000
    records = [
        BenchmarkAE(
            patientId=f"SUBJ-{i % 250}",
            reportedTerm="Headache",
            grade="MILD",
            onset="2026-01-15 09:30:00",
            resolution="2026-01-16 18:00:00",
        )
        for i in range(batch_size)
    ]

    _get_model_field_aliases.cache_clear()
    _normalize_key.cache_clear()

    start = time.perf_counter()
    df = transform_records_to_sdtm(records, domain="AE", study_key="STUDY01")
    elapsed = time.perf_counter() - start

    assert len(df) == batch_size
    assert "STUDYID" in df.columns
    assert "DOMAIN" in df.columns
    assert "USUBJID" in df.columns
    assert "AESEQ" in df.columns
    assert "AETERM" in df.columns
    assert "AESEV" in df.columns
    assert "AESTDTC" in df.columns
    assert "AEENDTC" in df.columns
    assert "extra_score" in df.columns

    # Verify first row transformations
    assert df.iloc[0]["USUBJID"] == "STUDY01-SUBJ-0"
    assert df.iloc[0]["AETERM"] == "Headache"
    assert df.iloc[0]["AESEV"] == "MILD"
    assert df.iloc[0]["AESTDTC"] == "2026-01-15T09:30:00"
    assert df.iloc[0]["AEENDTC"] == "2026-01-16T18:00:00"
    assert df.iloc[0]["extra_score"] == 100

    # Verify model aliases and normalized keys were effectively cached
    alias_cache = _get_model_field_aliases.cache_info()
    normalize_cache = _normalize_key.cache_info()
    assert alias_cache.hits >= batch_size - 1
    assert normalize_cache.hits > 100_000

    # Assert processing time is well under threshold (typically ~0.4-0.6s for 5,000 records)
    assert elapsed < 2.5


def test_all_thirteen_sdtm_domains_and_dmseq_omission() -> None:
    """Verify transform_records_to_sdtm and sdtm_dataframe map all 13 domains and omit DMSEQ on DM."""
    expected_domains = {
        "AE",
        "DV",
        "DD",
        "CM",
        "DM",
        "VS",
        "LB",
        "EX",
        "MH",
        "DS",
        "EG",
        "PE",
        "IE",
    }
    assert set(SDTM_DOMAIN_SPECS.keys()) == expected_domains

    domain_models: list[tuple[str, Any, str, Any]] = [
        ("AE", AdverseEvent(subjectKey="S01", aeTerm="Rash", aeSeverity="MILD"), "AETERM", "Rash"),
        (
            "DV",
            ProtocolDeviation(
                subjectKey="S01",
                dvTerm="Late visit",
                dvCategory="VISIT SCHEDULE",
                dvSeverity="MINOR",
                dvDate=datetime(2026, 1, 2),
            ),
            "DVTERM",
            "Late visit",
        ),
        (
            "DD",
            DeviceDeficiency(
                subjectKey="S01",
                ddTerm="Sensor fault",
                ddCategory="HARDWARE",
                ddSerious=False,
                ddDate=datetime(2026, 1, 3),
            ),
            "DDTERM",
            "Sensor fault",
        ),
        (
            "CM",
            ConcomitantMedication(subjectKey="S01", cmTrt="Ibuprofen", cmDose=200, cmDosU="mg"),
            "CMTRT",
            "Ibuprofen",
        ),
        ("DM", Demographics(subjectKey="S01", sex="F", age=41), "SEX", "F"),
        (
            "VS",
            VitalSigns(
                subjectKey="S01",
                vsTest="Heart Rate",
                vsTestCd="HR",
                vsResult=70,
                vsUnit="bpm",
                vsDate=datetime(2026, 1, 4),
            ),
            "VSTEST",
            "Heart Rate",
        ),
        (
            "LB",
            LaboratoryResult(
                subjectKey="S01",
                lbTest="Hemoglobin",
                lbTestCd="HGB",
                lbCat="HEMATOLOGY",
                lbResult="13.8",
                lbUnit="g/dL",
                lbOrnrlo="12.0",
                lbOrnrhi="16.0",
                lbDate=datetime(2026, 1, 5),
            ),
            "LBTESTCD",
            "HGB",
        ),
        (
            "EX",
            Exposure(
                subjectKey="S01",
                exTrt="Study Drug A",
                exDose=50,
                exDosU="mg",
                exRoute="ORAL",
                exStartDate=datetime(2026, 1, 6),
            ),
            "EXTRT",
            "Study Drug A",
        ),
        (
            "MH",
            MedicalHistory(
                subjectKey="S01",
                mhTerm="Asthma",
                mhCat="GENERAL",
                mhPresp=True,
                mhOccur=True,
                mhStartDate=datetime(2020, 5, 1),
            ),
            "MHTERM",
            "Asthma",
        ),
        (
            "DS",
            Disposition(
                subjectKey="S01",
                dsTerm="Completed Study",
                dsDecod="COMPLETED",
                dsCat="DISPOSITION EVENT",
                dsStartDate=datetime(2026, 6, 1),
            ),
            "DSTERM",
            "Completed Study",
        ),
        (
            "EG",
            EcgResult(
                subjectKey="S01",
                egTest="QTcF Interval",
                egTestCd="QTCF",
                egResult=410,
                egUnit="ms",
                egDate=datetime(2026, 1, 7),
            ),
            "EGTESTCD",
            "QTCF",
        ),
        (
            "PE",
            PhysicalExam(
                subjectKey="S01",
                peTest="Cardiovascular",
                peTestCd="CV",
                peResult="Normal",
                peDate=datetime(2026, 1, 8),
            ),
            "PETEST",
            "Cardiovascular",
        ),
        (
            "IE",
            InclusionExclusion(
                subjectKey="S01",
                ieTest="Age >= 18",
                ieTestCd="INCL01",
                ieCat="INCLUSION",
                ieResult="N",
                ieDate=datetime(2026, 1, 1),
            ),
            "IETESTCD",
            "INCL01",
        ),
    ]

    for domain_code, model_obj, check_col, expected_val in domain_models:
        df_direct = transform_records_to_sdtm([model_obj], domain=domain_code, study_key="STD01")
        assert df_direct.iloc[0]["STUDYID"] == "STD01"
        assert df_direct.iloc[0]["DOMAIN"] == domain_code
        assert df_direct.iloc[0]["USUBJID"] == "STD01-S01"
        assert df_direct.iloc[0][check_col] == expected_val

        sdk = MagicMock()
        sdk.get_records.return_value = [
            Record(
                record_id=1,
                subject_key="S01",
                visit_id=1,
                form_id=10,
                record_status="Complete",
                record_data=model_obj.model_dump(by_alias=True, exclude={"subject_key"}),
            )
        ]
        mapper = RecordMapper(sdk)
        df_via_mapper = mapper.sdtm_dataframe(study_key="STD01", domain=domain_code)
        assert df_via_mapper.iloc[0]["DOMAIN"] == domain_code
        assert df_via_mapper.iloc[0]["USUBJID"] == "STD01-S01"
        assert df_via_mapper.iloc[0][check_col] == expected_val

        if domain_code == "DM":
            assert "DMSEQ" not in df_direct.columns
            assert "DMSEQ" not in df_via_mapper.columns
            assert df_direct.iloc[0]["SUBJID"] == "S01"
            df_empty_dm = transform_records_to_sdtm([], domain="DM", study_key="STD01")
            assert "DMSEQ" not in df_empty_dm.columns
        else:
            seq_col = f"{domain_code}SEQ"
            assert seq_col in df_direct.columns
            assert df_direct.iloc[0][seq_col] == 1


def test_usubjid_preserves_explicit_value_and_supports_subject_id_map() -> None:
    """Verify explicit USUBJID stays untouched while subject_id_map overrides derived USUBJID."""
    records = [
        {"USUBJID": "ExplicitSite01-SubjA", "subjectKey": "S1", "aeTerm": "Headache"},
        {"subjectKey": "S2", "aeTerm": "Nausea"},
        {"subjectKey": "S3", "aeTerm": "Fatigue"},
    ]
    df = transform_records_to_sdtm(
        records,
        domain="AE",
        study_key="TRIAL_99",
        subject_id_map={"S2": "CUSTOM-USUBJID-002"},
    )
    assert list(df["USUBJID"]) == [
        "ExplicitSite01-SubjA",
        "CUSTOM-USUBJID-002",
        "TRIAL_99-S3",
    ]


def test_sdtm_dataframe_enriches_codings_visits_and_intervals() -> None:
    """Verify sdtm_dataframe joins Coding, Visit, and Interval lookups via SPI facade."""
    sdk = MagicMock()
    sdk.get_codings.return_value = [
        Coding(record_id=101, variable="aeTerm", value="HEADACHE", code="10019211"),
        Coding(record_id=201, variable="cmTerm", value="ACETYLSALICYLIC ACID", code="N02BA01"),
        Coding(record_id=301, variable="mhTerm", value="ASTHMA BRONCHIAL", code="10003553"),
    ]
    sdk.get_visits.return_value = [
        Visit(visit_id=11, interval_id=2, interval_name="Week 4", visit_date="2026-02-10"),
    ]
    sdk.get_intervals.return_value = [
        Interval(interval_id=2, interval_name="Week 4", interval_sequence=4),
    ]

    ae_records = [
        Record(
            record_id=101,
            subject_key="SUBJ-01",
            visit_id=11,
            form_id=10,
            record_status="Complete",
            record_data={"aeTerm": "mild headache", "aeSeverity": "MILD", "aeSerious": False},
        )
    ]
    cm_records = [
        Record(
            record_id=201,
            subject_key="SUBJ-01",
            visit_id=11,
            form_id=12,
            record_status="Complete",
            record_data={"cmTerm": "Baby Aspirin", "cmDose": "81", "cmDoseUnit": "mg"},
        )
    ]
    mh_records = [
        Record(
            record_id=301,
            subject_key="SUBJ-01",
            visit_id=11,
            form_id=14,
            record_status="Complete",
            record_data={"mhTerm": "Childhood asthma"},
        )
    ]

    mapper = RecordMapper(sdk)

    sdk.get_records.return_value = ae_records
    df_ae = mapper.sdtm_dataframe(study_key="STUDY_SPI", domain="AE")
    assert df_ae.iloc[0]["AEDECOD"] == "HEADACHE"
    assert df_ae.iloc[0]["VISIT"] == "Week 4"
    assert df_ae.iloc[0]["VISITNUM"] == 4
    assert df_ae.iloc[0]["VISITDTC"] == "2026-02-10"

    sdk.get_records.return_value = cm_records
    df_cm = mapper.sdtm_dataframe(study_key="STUDY_SPI", domain="CM")
    assert df_cm.iloc[0]["CMDECOD"] == "ACETYLSALICYLIC ACID"
    assert df_cm.iloc[0]["VISIT"] == "Week 4"
    assert df_cm.iloc[0]["VISITNUM"] == 4
    assert df_cm.iloc[0]["VISITDTC"] == "2026-02-10"

    sdk.get_records.return_value = mh_records
    df_mh = mapper.sdtm_dataframe(study_key="STUDY_SPI", domain="MH")
    assert df_mh.iloc[0]["MHDECOD"] == "ASTHMA BRONCHIAL"
    assert df_mh.iloc[0]["VISIT"] == "Week 4"
    assert df_mh.iloc[0]["VISITNUM"] == 4


def test_sdtm_dataframe_graceful_fallback_when_lookup_methods_omitted() -> None:
    """Verify stub SDKs without get_codings/get_visits/get_intervals degrade gracefully."""

    class MinimalStubSDK:
        def get_records(self, study_key: str, **filters: Any) -> list[Record]:
            return [
                Record(
                    record_id=1,
                    subject_key="S01",
                    visit_id=5,
                    form_id=10,
                    record_status="Complete",
                    record_data={"aeTerm": "Cough", "aeSeverity": "MILD"},
                )
            ]

    mapper = RecordMapper(MinimalStubSDK())  # type: ignore[arg-type]
    df = mapper.sdtm_dataframe(study_key="STUB_STUDY", domain="AE")
    assert len(df) == 1
    assert df.iloc[0]["USUBJID"] == "STUB_STUDY-S01"
    assert df.iloc[0]["AETERM"] == "Cough"
    assert df.iloc[0]["AEDECOD"] is None


def test_iter_sdtm_dataframes_maintains_continuous_seq_across_chunks() -> None:
    """Verify per-subject sequence numbers increment continuously across chunk boundaries."""
    sdk = MagicMock()
    sdk.get_records.return_value = [
        Record(
            record_id=i,
            subject_key="SUBJ-SAME",
            visit_id=1,
            form_id=10,
            record_status="Complete",
            record_data={"aeTerm": f"Event {i}", "aeSeverity": "MILD"},
        )
        for i in range(1, 5)
    ]
    mapper = RecordMapper(sdk, chunk_size=2)
    chunks = list(mapper.iter_sdtm_dataframes(study_key="STUDY_SEQ", domain="AE"))
    assert len(chunks) == 2
    assert list(chunks[0]["AESEQ"]) == [1, 2]
    assert list(chunks[1]["AESEQ"]) == [3, 4]


def test_dataframe_and_sdtm_dataframe_return_matching_rows_with_combined_filters() -> None:
    """Verify dataframe and sdtm_dataframe return matching rows for form, variable, and visit filters."""
    import httpx
    import respx

    from imednet.sdk import ImednetSDK

    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get("https://api.test/api/v1/edc/studies/STUDY/variables").respond(
            200,
            json={
                "data": [
                    {"variableName": "aeTerm", "label": "AE Term", "formId": 10},
                    {"variableName": "aeSeverity", "label": "Severity", "formId": 20},
                    {"variableName": "otherVar", "label": "Other", "formId": 30},
                ]
            },
        )
        mock_router.get("https://api.test/api/v1/edc/studies/STUDY/codings").respond(
            200, json={"data": []}
        )
        mock_router.get("https://api.test/api/v1/edc/studies/STUDY/visits").respond(
            200, json={"data": []}
        )
        mock_router.get("https://api.test/api/v1/edc/studies/STUDY/intervals").respond(
            200, json={"data": []}
        )

        def records_responder(request: httpx.Request) -> httpx.Response:
            flt = request.url.params.get("filter", "")
            assert not (";" in flt and "," in flt)
            all_rows = [
                {
                    "recordId": 1,
                    "subjectKey": "S1",
                    "visitId": 2,
                    "formId": 10,
                    "recordStatus": "Complete",
                    "recordData": {"aeTerm": "Headache", "extra": "drop"},
                },
                {
                    "recordId": 2,
                    "subjectKey": "S2",
                    "visitId": 2,
                    "formId": 20,
                    "recordStatus": "Complete",
                    "recordData": {"aeSeverity": "MODERATE", "extra": "drop"},
                },
                {
                    "recordId": 3,
                    "subjectKey": "S3",
                    "visitId": 99,
                    "formId": 10,
                    "recordStatus": "Complete",
                    "recordData": {"aeTerm": "Wrong Visit"},
                },
                {
                    "recordId": 4,
                    "subjectKey": "S4",
                    "visitId": 2,
                    "formId": 30,
                    "recordStatus": "Complete",
                    "recordData": {"otherVar": "Wrong Form"},
                },
            ]
            return httpx.Response(200, json={"data": all_rows})

        mock_router.get("https://api.test/api/v1/edc/studies/STUDY/records").mock(
            side_effect=records_responder
        )

        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        mapper = RecordMapper(sdk)

        df_raw = mapper.dataframe(
            "STUDY",
            visit_key="2",
            use_labels_as_columns=False,
            variable_whitelist=["aeTerm", "aeSeverity"],
            form_whitelist=[10, 20],
        )
        df_sdtm = mapper.sdtm_dataframe(
            "STUDY",
            domain="AE",
            visit_key="2",
            variable_whitelist=["aeTerm", "aeSeverity"],
            form_whitelist=[10, 20],
        )

        assert len(df_raw) == 2
        assert len(df_sdtm) == 2
        assert list(df_raw["subjectKey"]) == ["S1", "S2"]
        assert list(df_sdtm["USUBJID"]) == ["STUDY-S1", "STUDY-S2"]
        assert "extra" not in df_sdtm.columns

        # Secondary list filters in _fetch_records evaluate locally without mixing ';' and ','
        sec_records = mapper._fetch_records(
            "STUDY",
            visit_key="2",
            extra_filters={"formId": [10, 20], "subjectKey": ["S2"]},
        )
        assert [r.subject_key for r in sec_records] == ["S2"]

        sec_multi = mapper._fetch_records(
            "STUDY",
            visit_key="2",
            extra_filters={"formIds": [10, 20], "subjectKey": ["S1", "S2"]},
        )
        assert [r.subject_key for r in sec_multi] == ["S1", "S2"]
