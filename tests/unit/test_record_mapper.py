"""Unit tests for record mapper."""

import tracemalloc
from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from faker import Faker
from pydantic import BaseModel, Field, ValidationError

from imednet.models.records import Record
from imednet.models.reporting import (
    AdverseEvent,
    DeviceDeficiency,
    ProtocolDeviation,
    VitalSigns,
)
from imednet.models.sdtm import _get_model_field_aliases, _normalize_key
from imednet.models.variables import Variable
from imednet_workflows.record_mapper import (
    RecordMapper,
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
        "recordOid",
        "subjectKey",
        "visitId",
        "intervalId",
        "intervalName",
        "formId",
        "formKey",
        "siteId",
        "siteName",
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
        "recordOid",
        "subjectKey",
        "visitId",
        "intervalId",
        "intervalName",
        "formId",
        "formKey",
        "siteId",
        "siteName",
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
        assert rec_calls[0]["filter"] == "formId==10"
        assert rec_calls[1]["filter"] == "formId==20"
        assert list(df.columns) == [
            "recordId",
            "recordOid",
            "subjectKey",
            "visitId",
            "intervalId",
            "intervalName",
            "formId",
            "formKey",
            "siteId",
            "siteName",
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
    assert df.iloc[0]["USUBJID"] == "SUBJ-001"
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
    assert df.iloc[0]["USUBJID"] == "SUBJ-002"
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
    assert df_ae.iloc[0]["USUBJID"] == "SUBJ-AE-10"
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
    assert df_pd.iloc[0]["USUBJID"] == "SUBJ-PD-20"
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
    assert df_dd.iloc[0]["USUBJID"] == "SUBJ-DD-30"
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
    assert df_cm.iloc[0]["USUBJID"] == "SUBJ-CM-01"
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
    assert df_model.iloc[0]["USUBJID"] == "SUBJ-VS-01"
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
    assert df_raw.iloc[0]["USUBJID"] == "SUBJ-VS-02"
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
    assert df_partial.iloc[0]["USUBJID"] == "SUBJ-VS-03"
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
    assert df_alias.iloc[0]["USUBJID"] == "S100"
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
    assert df_serialized.iloc[0]["USUBJID"] == "S200"
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
    assert df_sdk.iloc[0]["USUBJID"] == "SUBJ-SDK"
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
    assert df_dict_camel.iloc[0]["USUBJID"] == "DICT-01"
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
    assert df_dict_snake.iloc[0]["USUBJID"] == "DICT-02"
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
    assert df.iloc[0]["USUBJID"] == "SUBJ-0"
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


def test_record_mapper_masks_phi_before_applying_question_labels() -> None:
    """Verify RecordMapper masks canonical PHI variables before renaming columns to question labels."""
    from imednet.utils.security import global_sensitivity_registry

    registry_snapshot = set(global_sensitivity_registry._sensitive_keys)
    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(variable_name="patientName", label="Full Legal Participant Name", form_id=10),
        Variable(variable_name="DOB", label="Calendar Day", form_id=10),
        Variable(variable_name="SSN", label="National Identifier", form_id=10),
        Variable(variable_name="email", label="Electronic Contact", form_id=10),
        Variable(variable_name="systolicBp", label="Systolic Blood Pressure (mmHg)", form_id=10),
    ]
    raw_record = Record(
        record_id=101,
        subject_key="SUBJ-101",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        date_created=datetime(2025, 1, 1),
        record_data={
            "patientName": "Jane Doe",
            "DOB": "1985-07-19",
            "SSN": "123-45-6789",
            "email": "jane@clinical.example",
            "systolicBp": 119,
        },
    )
    sdk.get_records.return_value = [raw_record]

    mapper = RecordMapper(sdk)
    df_labeled = mapper.dataframe("STUDY1", use_labels_as_columns=True)

    assert df_labeled.iloc[0]["recordId"] == 101
    assert df_labeled.iloc[0]["subjectKey"] == "SUBJ-101"
    assert df_labeled.iloc[0]["Full Legal Participant Name"] == "***MASKED***"
    assert df_labeled.iloc[0]["Calendar Day"] == "***MASKED***"
    assert df_labeled.iloc[0]["National Identifier"] == "***MASKED***"
    assert df_labeled.iloc[0]["Electronic Contact"] == "***MASKED***"
    assert df_labeled.iloc[0]["Systolic Blood Pressure (mmHg)"] == 119
    assert raw_record.record_data["patientName"] == "Jane Doe"

    # Explicit opt-out via method parameter or constructor preserves raw values without mutating global registry
    df_unmasked = mapper.dataframe("STUDY1", use_labels_as_columns=True, mask_phi=False)
    assert df_unmasked.iloc[0]["Full Legal Participant Name"] == "Jane Doe"
    assert df_unmasked.iloc[0]["Calendar Day"] == "1985-07-19"
    assert df_unmasked.iloc[0]["National Identifier"] == "123-45-6789"
    assert df_unmasked.iloc[0]["Electronic Contact"] == "jane@clinical.example"

    opt_out_mapper = RecordMapper(sdk, mask_phi=False)
    df_ctor_unmasked = opt_out_mapper.dataframe("STUDY1", use_labels_as_columns=True)
    assert df_ctor_unmasked.iloc[0]["Full Legal Participant Name"] == "Jane Doe"
    assert global_sensitivity_registry._sensitive_keys == registry_snapshot


def test_record_mapper_applies_committed_study_config_phi_in_dataframe_and_hierarchy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify RecordMapper resolves committed StudyConfiguration phi_fields in dataframe and build_hierarchy."""
    from unittest.mock import patch

    import imednet_workflows.record_mapper as rm_mod
    from imednet.integrations.enrichment import EnrichmentPipeline
    from imednet.models.study_config import MappingRule, StudyConfiguration

    cfg = StudyConfiguration(
        study_key="STUDY-GOV",
        phi_fields=["chart_code"],
        mappings=[
            MappingRule(
                domain="DM",
                target_field="normalized_status",
                source_form_key="DM",
                source_variable_name="raw_status",
                business_logic="value.upper()",
            )
        ],
    )
    monkeypatch.setattr(
        rm_mod,
        "resolve_enrichment_pipeline",
        lambda sk: EnrichmentPipeline(cfg) if sk == "STUDY-GOV" else None,
    )

    sdk = MagicMock()
    variables = [
        Variable(variable_name="chart_code", label="Hospital Chart Code", form_id=10),
        Variable(variable_name="raw_status", label="Raw Enrollment Status", form_id=10),
        Variable(variable_name="heartRate", label="Heart Rate", form_id=10),
    ]
    sdk.get_variables.return_value = variables
    rec = Record(
        record_id=202,
        subject_key="SUBJ-202",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        date_created=datetime(2025, 2, 1),
        record_data={
            "chart_code": "MRN-7788",
            "raw_status": "active",
            "heartRate": 71,
        },
    )
    sdk.get_records.return_value = [rec]

    mapper = RecordMapper(sdk)
    df = mapper.dataframe("STUDY-GOV", use_labels_as_columns=True)
    assert df.iloc[0]["Hospital Chart Code"] == "***MASKED***"
    assert df.iloc[0]["Heart Rate"] == 71

    df_unmasked = mapper.dataframe("STUDY-GOV", use_labels_as_columns=True, mask_phi=False)
    assert df_unmasked.iloc[0]["Hospital Chart Code"] == "MRN-7788"

    mock_struct = MagicMock()
    interval = MagicMock()
    form = MagicMock()
    form.form_id = 10
    form.variables = variables
    interval.forms = [form]
    mock_struct.intervals = [interval]

    with patch("imednet_workflows.study_structure.get_study_structure", return_value=mock_struct):
        mapper._iter_records = MagicMock(return_value=[rec])  # type: ignore[method-assign]
        tree = mapper.build_hierarchy("STUDY-GOV", use_labels_as_keys=True)
        out_rec = tree[0]["visits"][0]["forms"][0]["records"][0]
        assert out_rec["Hospital Chart Code"] == "***MASKED***"
        assert out_rec["Heart Rate"] == 71


def test_format_iso8601_dtc_phi_redaction_and_masked_sentinel() -> None:
    """Verify format_iso8601_dtc drops ***MASKED*** and emits ISO YYYY or None when redacting."""
    assert format_iso8601_dtc("***MASKED***") is None
    assert format_iso8601_dtc("  ***MASKED***  ", mask_phi=True) is None
    assert format_iso8601_dtc(datetime(1985, 7, 19, 9, 30, 0), mask_phi=True) == "1985"
    assert format_iso8601_dtc(date(1972, 11, 3), redact_to_year=True) == "1972"
    assert format_iso8601_dtc("1985-07-19T09:30:00", mask_phi=True) == "1985"
    assert format_iso8601_dtc("07/19/1985", mask_phi=True) == "1985"
    assert format_iso8601_dtc("19-JUL-1985", mask_phi=True) == "1985"
    assert format_iso8601_dtc("1985", mask_phi=True) == "1985"
    assert format_iso8601_dtc("1985-07", mask_phi=True) == "1985"
    assert format_iso8601_dtc("1985-UN-UN", mask_phi=True) == "1985"
    assert format_iso8601_dtc(1985, mask_phi=True) == "1985"
    assert format_iso8601_dtc(12345, mask_phi=True) is None
    assert format_iso8601_dtc("invalid-date", mask_phi=True) is None
    assert format_iso8601_dtc(None, mask_phi=True) is None
    assert format_iso8601_dtc("", mask_phi=True) is None
    assert format_iso8601_dtc(True, mask_phi=True) is None


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5000])
def test_sdtm_streaming_and_full_export_produce_identical_unique_seq_across_chunk_sizes(
    chunk_size: int,
) -> None:
    """Verify iter_sdtm_dataframes and sdtm_dataframe match identically across chunk sizes."""
    import pandas as pd

    ungrouped_ae_records = [
        Record(
            record_id=105,
            subject_key="SUBJ-02",
            form_id=20,
            record_data={"aeTerm": "Nausea", "aeStartDate": "2026-03-10", "aeSerious": False},
        ),
        Record(
            record_id=103,
            subject_key="SUBJ-01",
            form_id=20,
            record_data={"aeTerm": "Headache", "aeStartDate": "2026-02-05", "aeSerious": False},
        ),
        Record(
            record_id=101,
            subject_key="SUBJ-01",
            form_id=20,
            record_data={"aeTerm": "Fatigue", "aeStartDate": "2026-02-05", "aeSerious": True},
        ),
        Record(
            record_id=104,
            subject_key="SUBJ-02",
            form_id=20,
            record_data={"aeTerm": "Dizziness", "aeStartDate": "2026-01-15", "aeSerious": False},
        ),
        Record(
            record_id=102,
            subject_key="SUBJ-01",
            form_id=20,
            record_data={"aeTerm": "Fatigue", "aeStartDate": "2026-02-05", "aeSerious": False},
        ),
        Record(
            record_id=106,
            subject_key="SUBJ-01",
            form_id=20,
            record_data={"aeTerm": "Cough", "aeStartDate": None, "aeSerious": False},
        ),
    ]
    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: iter(ungrouped_ae_records)

    mapper = RecordMapper(sdk, chunk_size=chunk_size)
    full_df = mapper.sdtm_dataframe("STUDY-SEQ", domain="AE")
    streamed_df = pd.concat(
        list(mapper.iter_sdtm_dataframes("STUDY-SEQ", domain="AE")),
        ignore_index=True,
    )

    pd.testing.assert_frame_equal(streamed_df, full_df)
    key_tuples = list(full_df[["STUDYID", "USUBJID", "AESEQ"]].itertuples(index=False, name=None))
    assert len(key_tuples) == len(set(key_tuples))
    assert key_tuples == [
        ("STUDY-SEQ", "SUBJ-01", 1),
        ("STUDY-SEQ", "SUBJ-01", 2),
        ("STUDY-SEQ", "SUBJ-01", 3),
        ("STUDY-SEQ", "SUBJ-01", 4),
        ("STUDY-SEQ", "SUBJ-02", 1),
        ("STUDY-SEQ", "SUBJ-02", 2),
    ]
    subj1 = full_df[full_df["USUBJID"] == "SUBJ-01"]
    # Tie-broken by (--DTC, AETERM, recordId), with missing --DTC last
    assert list(subj1["AETERM"]) == ["Fatigue", "Fatigue", "Headache", "Cough"]
    assert list(subj1["AESER"]) == ["Y", "N", "N", "N"]
    subj2 = full_df[full_df["USUBJID"] == "SUBJ-02"]
    assert list(subj2["AETERM"]) == ["Dizziness", "Nausea"]


def test_sdtm_sort_query_simulation_transport_multi_form_and_cached_loader_parity(
    tmp_path,
) -> None:
    """Verify MappingParamProcessor sort query, multi-form merge, and CachedRecordsLoader parity."""
    import httpx
    import pandas as pd

    from imednet.sdk import ImednetSDK
    from imednet.simulation import SimulationState, SimulationTransport
    from imednet_workflows.cached_loader import CachedRecordsLoader

    state = SimulationState()
    state.studies = [{"studyKey": "STUDY-SIM", "studyName": "Sim Study"}]
    state.forms = [
        {"studyKey": "STUDY-SIM", "formId": 10, "formKey": "AE1", "formName": "AE Form 1"},
        {"studyKey": "STUDY-SIM", "formId": 20, "formKey": "AE2", "formName": "AE Form 2"},
    ]
    state.variables = [
        {"studyKey": "STUDY-SIM", "variableName": "aeTerm", "label": "AE Term", "formId": 10},
        {"studyKey": "STUDY-SIM", "variableName": "aeStartDate", "label": "Start", "formId": 10},
        {"studyKey": "STUDY-SIM", "variableName": "aeTerm", "label": "AE Term", "formId": 20},
        {"studyKey": "STUDY-SIM", "variableName": "aeStartDate", "label": "Start", "formId": 20},
    ]
    state.records = [
        {
            "studyKey": "STUDY-SIM",
            "recordId": 30,
            "formId": 20,
            "formKey": "AE2",
            "subjectKey": "SUBJ-02",
            "visitId": 1,
            "deleted": False,
            "recordStatus": "Complete",
            "dateModified": "2026-03-01T10:00:00Z",
            "recordData": {"aeTerm": "Rash", "aeStartDate": "2026-03-02"},
        },
        {
            "studyKey": "STUDY-SIM",
            "recordId": 10,
            "formId": 10,
            "formKey": "AE1",
            "subjectKey": "SUBJ-01",
            "visitId": 1,
            "deleted": False,
            "recordStatus": "Complete",
            "dateModified": "2026-03-01T10:00:00Z",
            "recordData": {"aeTerm": "Nausea", "aeStartDate": "2026-03-05"},
        },
        {
            "studyKey": "STUDY-SIM",
            "recordId": 20,
            "formId": 20,
            "formKey": "AE2",
            "subjectKey": "SUBJ-01",
            "visitId": 1,
            "deleted": False,
            "recordStatus": "Complete",
            "dateModified": "2026-03-01T10:00:00Z",
            "recordData": {"aeTerm": "Headache", "aeStartDate": "2026-03-01"},
        },
        {
            "studyKey": "STUDY-SIM",
            "recordId": 40,
            "formId": 10,
            "formKey": "AE1",
            "subjectKey": "SUBJ-02",
            "visitId": 1,
            "deleted": False,
            "recordStatus": "Complete",
            "dateModified": "2026-03-01T10:00:00Z",
            "recordData": {"aeTerm": "Chills", "aeStartDate": "2026-03-01"},
        },
    ]

    transport = SimulationTransport(state=state)
    record_query_params: list[dict[str, str]] = []
    orig_handle = transport.handle_request

    def spy_handle(request: httpx.Request) -> httpx.Response:
        if request.method.upper() == "GET" and request.url.path.endswith("/records"):
            record_query_params.append(dict(request.url.params))
        return orig_handle(request)

    transport.handle_request = spy_handle  # type: ignore[method-assign]
    sdk = ImednetSDK(simulation_mode=True, transport=transport)

    # Non-SDTM calls preserve existing query contract without injecting sort
    non_sdtm_mapper = RecordMapper(sdk, chunk_size=2)
    _ = non_sdtm_mapper.dataframe("STUDY-SIM", use_labels_as_columns=False)
    assert record_query_params
    assert all("sort" not in params for params in record_query_params)

    # Single-query and multi-form SDTM streams request sort="subjectKey,asc;recordId,asc"
    record_query_params.clear()
    sdk_sdtm_chunks = list(
        non_sdtm_mapper.iter_sdtm_dataframes("STUDY-SIM", domain="AE", form_whitelist=[10, 20])
    )
    assert record_query_params
    assert all(
        params.get("sort") == "subjectKey,asc;recordId,asc" for params in record_query_params
    )
    sdk_sdtm_df = pd.concat(sdk_sdtm_chunks, ignore_index=True)

    # CachedRecordsLoader produces identical <DOM>SEQ assignments
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "sim-sdtm-cache")
    cached_mapper = RecordMapper(sdk, loader=loader, chunk_size=1)
    cached_sdtm_df = pd.concat(
        list(cached_mapper.iter_sdtm_dataframes("STUDY-SIM", domain="AE", form_whitelist=[10, 20])),
        ignore_index=True,
    )

    pd.testing.assert_frame_equal(sdk_sdtm_df, cached_sdtm_df)
    assert list(
        sdk_sdtm_df[["USUBJID", "AESEQ", "AETERM", "AESTDTC"]].itertuples(index=False, name=None)
    ) == [
        ("SUBJ-01", 1, "Headache", "2026-03-01"),
        ("SUBJ-01", 2, "Nausea", "2026-03-05"),
        ("SUBJ-02", 1, "Chills", "2026-03-01"),
        ("SUBJ-02", 2, "Rash", "2026-03-02"),
    ]

    # Legacy/ungrouped custom loader without order_by_subject still sorts before assigning <DOM>SEQ
    class _LegacyUngroupedLoader:
        def sync_records(self, study_key: str) -> None:
            loader.sync_records(study_key)

        def iter_cached_records(self, study_key: str, *, chunk_size: int = 2):
            return loader.iter_cached_records(
                study_key, chunk_size=chunk_size, order_by_subject=False
            )

    legacy_mapper = RecordMapper(sdk, loader=_LegacyUngroupedLoader(), chunk_size=1)  # type: ignore[arg-type]
    legacy_df = pd.concat(
        list(legacy_mapper.iter_sdtm_dataframes("STUDY-SIM", domain="AE")),
        ignore_index=True,
    )
    pd.testing.assert_frame_equal(legacy_df, sdk_sdtm_df)


def test_dm_exports_enforce_iso_brthdtc_omit_dmseq_and_apply_study_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify all 6 SDTM export paths apply StudyConfiguration, ISO BRTHDTC redaction, and omit DMSEQ."""
    import pandas as pd

    import imednet_workflows.record_mapper as rm_mod
    from imednet.integrations.enrichment import EnrichmentPipeline
    from imednet.models.study_config import MappingRule, StudyConfiguration

    cfg = StudyConfiguration(
        study_key="STUDY-DM",
        phi_fields=["siteChartCode", "investigatorNote"],
        terminology_lookups={
            "SEX": {"1": "F", "2": "M"},
            "DM.ETHNIC": {"H": "HISPANIC OR LATINO", "NH": "NOT HISPANIC OR LATINO"},
        },
        mappings=[
            MappingRule(
                domain="DM",
                target_field="ARM",
                source_form_key="DM",
                source_variable_name="rawArm",
                business_logic="value.strip().upper()",
            ),
            MappingRule(
                domain="DM",
                target_field="INVESTIGATOR_NOTE",
                source_form_key="DM",
                source_variable_name="investigatorNote",
            ),
        ],
    )
    monkeypatch.setattr(
        rm_mod,
        "resolve_enrichment_pipeline",
        lambda sk: EnrichmentPipeline(cfg) if sk == "STUDY-DM" else None,
    )

    records = [
        Record(
            record_id=2,
            study_key="STUDY-DM",
            subject_key="SUBJ-02",
            form_id=10,
            record_data={
                "subjid": "002",
                "birthDate": "",
                "sex": "2",
                "ethnic": "NH",
                "rawArm": " placebo ",
                "SUBJINIT": "AB",
                "site_chart_code": "CH-200",
                "investigatorNote": "Private note 2",
            },
        ),
        Record(
            record_id=1,
            study_key="STUDY-DM",
            subject_key="SUBJ-01",
            form_id=10,
            record_data={
                "subjid": "001",
                "birthDate": "1985-07-19",
                "sex": "1",
                "ethnic": "H",
                "rawArm": " active ",
                "SUBJINIT": "JD",
                "site_chart_code": "CH-100",
                "investigatorNote": "Private note 1",
                "DMSEQ": 99,
            },
        ),
    ]
    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: iter(records)

    mapper = RecordMapper(sdk, chunk_size=1)
    dfs_masked = [
        mapper.sdtm_dataframe("STUDY-DM", domain="DM"),
        pd.concat(list(mapper.iter_sdtm_dataframes("STUDY-DM", domain="DM")), ignore_index=True),
        mapper.dataframe("STUDY-DM", as_sdtm=True, domain="DM"),
        pd.concat(
            list(mapper.iter_dataframes("STUDY-DM", as_sdtm=True, domain="DM")),
            ignore_index=True,
        ),
        mapper.transform_to_sdtm(records, domain="DM", study_key="STUDY-DM"),
        transform_records_to_sdtm(records, domain="DM", study_key="STUDY-DM"),
    ]

    for df in dfs_masked:
        assert "DMSEQ" not in df.columns
        assert list(df["STUDYID"]) == ["STUDY-DM", "STUDY-DM"]
        assert list(df["USUBJID"]) == ["SUBJ-01", "SUBJ-02"]
        assert list(df["SUBJID"]) == ["001", "002"]
        assert list(df["BRTHDTC"]) == ["1985", None]
        assert list(df["SEX"]) == ["F", "M"]
        assert list(df["ETHNIC"]) == ["HISPANIC OR LATINO", "NOT HISPANIC OR LATINO"]
        assert list(df["ARM"]) == ["ACTIVE", "PLACEBO"]
        assert list(df["SUBJINIT"]) == ["***MASKED***", "***MASKED***"]
        assert list(df["site_chart_code"]) == ["***MASKED***", "***MASKED***"]
        assert list(df["INVESTIGATOR_NOTE"]) == ["***MASKED***", "***MASKED***"]

    # Empty DM export also omits DMSEQ
    sdk.records.list.side_effect = lambda **_kw: iter([])
    empty_dm = mapper.sdtm_dataframe("STUDY-DM", domain="DM")
    assert empty_dm.empty
    assert "DMSEQ" not in empty_dm.columns

    # Explicit mask_phi=False per call and via constructor preserves full BRTHDTC and PHI text
    sdk.records.list.side_effect = lambda **_kw: iter(records)
    opt_out_dfs = [
        mapper.sdtm_dataframe("STUDY-DM", domain="DM", mask_phi=False),
        pd.concat(
            list(mapper.iter_sdtm_dataframes("STUDY-DM", domain="DM", mask_phi=False)),
            ignore_index=True,
        ),
        mapper.dataframe("STUDY-DM", as_sdtm=True, domain="DM", mask_phi=False),
        pd.concat(
            list(mapper.iter_dataframes("STUDY-DM", as_sdtm=True, domain="DM", mask_phi=False)),
            ignore_index=True,
        ),
        mapper.transform_to_sdtm(records, domain="DM", study_key="STUDY-DM", mask_phi=False),
        transform_records_to_sdtm(records, domain="DM", study_key="STUDY-DM", mask_phi=False),
        RecordMapper(sdk, mask_phi=False).sdtm_dataframe("STUDY-DM", domain="DM"),
    ]
    for df_raw in opt_out_dfs:
        assert "DMSEQ" not in df_raw.columns
        assert list(df_raw["BRTHDTC"]) == ["1985-07-19", None]
        assert list(df_raw["SUBJINIT"]) == ["JD", "AB"]
        assert list(df_raw["site_chart_code"]) == ["CH-100", "CH-200"]
        assert list(df_raw["INVESTIGATOR_NOTE"]) == ["Private note 1", "Private note 2"]
        assert list(df_raw["SEX"]) == ["F", "M"]
        assert list(df_raw["ARM"]) == ["ACTIVE", "PLACEBO"]


def test_sdtm_generator_streams_and_form_whitelist_variants() -> None:
    """Verify ordered and ungrouped generator streams, voided records, and form_whitelist variants."""
    import pandas as pd

    # 1. Ordered generator stream across multiple subject groups and chunks, skipping voided records
    def _ordered_gen():
        yield Record(
            record_id=999,
            subject_key="SUBJ-00",
            form_id=10,
            deleted=True,
            record_data={"aeTerm": "Voided", "aeStartDate": "2026-01-01"},
        )
        for subj_idx in range(1, 7):
            subj = f"SUBJ-{subj_idx:02d}"
            yield Record(
                record_id=subj_idx * 10 + 2,
                subject_key=subj,
                form_id=10,
                record_data={"aeTerm": "Later", "aeStartDate": "2026-02-02"},
            )
            yield Record(
                record_id=subj_idx * 10 + 1,
                subject_key=subj,
                form_id=10,
                record_data={"aeTerm": "Earlier", "aeStartDate": "2026-02-01"},
            )

    sdk_ordered = MagicMock()
    sdk_ordered.records.list.side_effect = lambda **_kw: _ordered_gen()
    mapper_ordered = RecordMapper(sdk_ordered, chunk_size=2)
    ordered_chunks = list(mapper_ordered.iter_sdtm_dataframes("STUDY-GEN", domain="AE"))
    assert len(ordered_chunks) == 6
    ordered_df = pd.concat(ordered_chunks, ignore_index=True)
    assert len(ordered_df) == 12
    assert list(ordered_df["AESEQ"]) == [1, 2] * 6
    assert list(ordered_df["AETERM"]) == ["Earlier", "Later"] * 6

    # 2. Ungrouped generator stream triggers pre-yield drain and global sort
    def _ungrouped_gen():
        yield Record(
            record_id=2,
            subject_key="SUBJ-02",
            form_id=10,
            record_data={"aeTerm": "B2", "aeStartDate": "2026-03-02"},
        )
        yield Record(
            record_id=1,
            subject_key="SUBJ-01",
            form_id=10,
            record_data={"aeTerm": "A1", "aeStartDate": "2026-03-01"},
        )
        yield Record(
            record_id=888,
            subject_key="SUBJ-01",
            form_id=10,
            deleted=True,
            record_data={"aeTerm": "Skip", "aeStartDate": "2026-03-01"},
        )
        yield Record(
            record_id=3,
            subject_key="SUBJ-02",
            form_id=10,
            record_data={"aeTerm": "B1", "aeStartDate": "2026-03-01"},
        )

    sdk_ungrouped = MagicMock()
    sdk_ungrouped.records.list.side_effect = lambda **_kw: _ungrouped_gen()
    mapper_ungrouped = RecordMapper(sdk_ungrouped, chunk_size=2)
    ungrouped_df = pd.concat(
        list(mapper_ungrouped.iter_sdtm_dataframes("STUDY-GEN", domain="AE")),
        ignore_index=True,
    )
    assert list(
        ungrouped_df[["USUBJID", "AESEQ", "AETERM"]].itertuples(index=False, name=None)
    ) == [
        ("SUBJ-01", 1, "A1"),
        ("SUBJ-02", 1, "B1"),
        ("SUBJ-02", 2, "B2"),
    ]

    # 3. Legacy sdk.get_records returning lists for multi-form, single-form, and empty form_whitelist
    sdk_legacy = MagicMock(spec=["get_records"])
    form_rows = {
        10: [
            Record(
                record_id=20,
                subject_key="SUBJ-02",
                form_id=10,
                record_data={"aeTerm": "Form10-S2", "aeStartDate": "2026-04-02"},
            ),
            Record(
                record_id=10,
                subject_key="SUBJ-01",
                form_id=10,
                record_data={"aeTerm": "Form10-S1", "aeStartDate": "2026-04-02"},
            ),
        ],
        20: [
            Record(
                record_id=15,
                subject_key="SUBJ-01",
                form_id=20,
                record_data={"aeTerm": "Form20-S1", "aeStartDate": "2026-04-01"},
            ),
        ],
    }
    sdk_legacy.get_records.side_effect = lambda **kw: list(form_rows.get(kw.get("formId"), []))
    legacy_mapper = RecordMapper(sdk_legacy, chunk_size=2)

    empty_whitelist_df = legacy_mapper.sdtm_dataframe("STUDY-LEG", domain="AE", form_whitelist=[])
    assert empty_whitelist_df.empty

    single_form_df = legacy_mapper.sdtm_dataframe("STUDY-LEG", domain="AE", form_whitelist=[10])
    assert list(single_form_df["USUBJID"]) == ["SUBJ-01", "SUBJ-02"]

    multi_form_df = legacy_mapper.sdtm_dataframe("STUDY-LEG", domain="AE", form_whitelist=[10, 20])
    assert list(
        multi_form_df[["USUBJID", "AESEQ", "AETERM"]].itertuples(index=False, name=None)
    ) == [
        ("SUBJ-01", 1, "Form20-S1"),
        ("SUBJ-01", 2, "Form10-S1"),
        ("SUBJ-02", 1, "Form10-S2"),
    ]


def test_sdtm_variable_whitelist_projection_and_custom_domain_phi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify variable_whitelist projection, mapped USUBJID, and custom domain nested/date PHI redaction."""
    import imednet_workflows.record_mapper as rm_mod
    from imednet.integrations.enrichment import EnrichmentPipeline
    from imednet.models.study_config import MappingRule, StudyConfiguration

    cfg = StudyConfiguration(
        study_key="STUDY-LB",
        phi_fields=["AESER", "AESTDTC", "secretTag"],
        mappings=[
            MappingRule(
                domain="LB",
                target_field="USUBJID",
                source_form_key="LB",
                source_variable_name="rawSubj",
                business_logic="value.upper()",
            ),
            MappingRule(
                domain="LB",
                target_field="LBTESTCD",
                source_form_key="LB",
                source_variable_name="rawLabCode",
                business_logic="value.upper()",
            ),
        ],
    )
    monkeypatch.setattr(
        rm_mod,
        "resolve_enrichment_pipeline",
        lambda sk: EnrichmentPipeline(cfg) if sk in {"STUDY-LB", "STUDY-AE-SENS"} else None,
    )

    sdk = MagicMock()
    lb_records: list[Any] = [
        Record(
            record_id=2,
            subject_key="orig-2",
            form_id=30,
            record_data={
                "rawSubj": "subj-b",
                "rawLabCode": "hgb",
                "LBDTC": "2026-05-02 08:15:00",
                "BRTHDTC": "1980-04-12",
                "droppedExtra": "ignore-me",
                "auditMeta": {
                    "patientName": "Alice Smith",
                    "secretTag": "TAG-9",  # pragma: allowlist secret
                },
            },
        ),
        {
            "recordId": 1,
            "record_data": {
                "rawSubj": "subj-a",
                "rawLabCode": "wbc",
                "LBDTC": "2026-05-01 07:00:00",
                "BRTHDTC": "1975-11-30",
                "droppedExtra": "ignore-dict",
                "auditMeta": {
                    "patientName": "Bob Jones",
                    "secretTag": "TAG-1",  # pragma: allowlist secret
                },
            },
        },
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(lb_records)
    mapper = RecordMapper(sdk, chunk_size=1)

    assert list(mapper.iter_sdtm_dataframes("STUDY-LB", domain="LB", variable_whitelist=[])) == []

    lb_df = mapper.sdtm_dataframe(
        "STUDY-LB",
        domain="LB",
        variable_whitelist=["USUBJID", "LBTESTCD", "LBDTC", "BRTHDTC", "auditMeta"],
    )
    assert "droppedExtra" not in lb_df.columns
    assert list(lb_df["USUBJID"]) == ["SUBJ-A", "SUBJ-B"]
    assert list(lb_df["LBSEQ"]) == [1, 1]
    assert list(lb_df["LBTESTCD"]) == ["WBC", "HGB"]
    assert list(lb_df["LBDTC"]) == ["2026-05-01T07:00:00", "2026-05-02T08:15:00"]
    assert list(lb_df["BRTHDTC"]) == ["1975", "1980"]
    assert lb_df.iloc[0]["auditMeta"] == {
        "patientName": "***MASKED***",
        "secretTag": "***MASKED***",
    }

    # Sensitive standard DTC and flag variables via custom phi_fields
    ae_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "SUBJ-1",
                "aeTerm": "Rash",
                "aeSerious": True,
                "aeStartDate": "2026-05-01",
            }
        ],
        domain="AE",
        study_key="STUDY-AE-SENS",
    )
    assert ae_df.iloc[0]["AESER"] == "***MASKED***"
    assert ae_df.iloc[0]["AESTDTC"] is None


def test_record_mapper_hydrates_site_interval_and_form_maps(tmp_path) -> None:
    """Req 1: RecordMapper hydrates recordOid, formKey, intervalId/Name, and siteId/Name."""
    import pandas as pd

    from imednet.core.client import Client
    from imednet.integrations.export import _RECORD_META_CONTRACT_TYPES, export_to_csv
    from imednet.models.forms import Form
    from imednet.models.intervals import Interval
    from imednet.models.sites import Site
    from imednet.models.visits import Visit
    from imednet.sdk import ImednetSDK
    from imednet.simulation import SimulationState, SimulationTransport
    from imednet.utils.pandas import import_records_csv

    assert tuple(name for name, _ in _RECORD_META_CONTRACT_TYPES) == (
        "recordId",
        "recordOid",
        "subjectKey",
        "visitId",
        "intervalId",
        "intervalName",
        "formId",
        "formKey",
        "siteId",
        "siteName",
        "recordStatus",
        "dateCreated",
    )

    # Unit check: visitId fallback hydrates intervalId and intervalName when record.interval_id is None
    mock_sdk = MagicMock()
    mock_sdk.get_variables.return_value = [
        Variable(variable_name="V_AGE", label="Age", form_id=301, form_key="")
    ]
    mock_sdk.get_forms.return_value = [Form(form_id=301, form_key="F_DM", form_name="Demographics")]
    mock_sdk.get_sites.return_value = [Site(site_id=101, site_name="SITE-ALPHA")]
    mock_sdk.get_intervals.return_value = [Interval(interval_id=501, interval_name="Baseline")]
    mock_sdk.get_visits.return_value = [
        Visit(visit_id=401, interval_id=501, interval_name="Baseline")
    ]
    mock_sdk.get_records.return_value = [
        Record(
            record_id=601,
            record_oid="REC-601",
            subject_key="SUB1",
            visit_id=401,
            interval_id=None,
            form_id=301,
            form_key="",
            site_id=101,
            record_status="Complete",
            date_created=datetime(2026, 1, 1),
            record_data={"V_AGE": 42},
        )
    ]

    mapper = RecordMapper(mock_sdk)
    df = mapper.dataframe("STUDY", use_labels_as_columns=False)
    row = df.iloc[0]
    assert row["recordOid"] == "REC-601"
    assert row["formKey"] == "F_DM"
    assert row["intervalId"] == 501
    assert row["intervalName"] == "Baseline"
    assert row["siteId"] == 101
    assert row["siteName"] == "SITE-ALPHA"

    # End-to-end symmetric round-trip: export_to_csv -> import_records_csv updates scheduled & unscheduled records
    state = SimulationState()
    transport = SimulationTransport(state=state)
    sdk = ImednetSDK(
        client=Client(
            api_key="sim-key",  # pragma: allowlist secret
            security_key="sim-secret",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=transport,
        )
    )
    # Seed an unscheduled record (no visitId / intervalId) alongside the scheduled seeded records
    sdk.records.create_batch(
        "STUDY",
        [
            {
                "formKey": "F_VS",
                "subjectKey": "SUB1",
                "siteName": "SITE",
                "data": {"V_WEIGHT": 81.5},
            }
        ],
    )
    before_records = state.get_records("STUDY")
    before_count = len(before_records)
    assert before_count == 3  # 2 scheduled + 1 unscheduled

    csv_file = tmp_path / "study_records.csv"
    export_to_csv(sdk, "STUDY", str(csv_file), mask_phi=False)
    assert csv_file.exists()

    # Modify clinical values in exported CSV and re-import via import_records_csv
    exported_df = pd.read_csv(csv_file)
    exported_df.loc[exported_df["recordId"] == 601, "V_AGE"] = 46
    exported_df.loc[exported_df["visitId"].isna(), "V_WEIGHT"] = 82.5
    exported_df.to_csv(csv_file, index=False)

    import_records_csv(sdk, "STUDY", csv_file)

    after_records = state.get_records("STUDY")
    assert len(after_records) == before_count  # zero duplicate records!
    rec_by_id = {r["recordId"]: r for r in after_records}
    assert rec_by_id[601]["recordData"]["V_AGE"] == 46
    unscheduled = [
        r for r in after_records if r.get("visitId") is None and r.get("intervalId") is None
    ]
    assert len(unscheduled) == 1
    assert unscheduled[0]["recordData"]["V_WEIGHT"] == 82.5
