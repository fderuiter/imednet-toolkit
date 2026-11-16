"""Unit tests for record mapper."""

import tracemalloc
from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from faker import Faker
from pydantic import BaseModel, ValidationError

from imednet.models.records import Record
from imednet.models.reporting import AdverseEvent, DeviceDeficiency, ProtocolDeviation
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
        if kwargs.get("variableNames"):
            assert kwargs == {
                "study_key": "STUDY",
                "variableNames": ["VAR1"],
                "formIds": [10],
            }
            return [variables[0]]
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
            "variableNames": ["VAR1"],
            "formIds": [10],
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
        if kwargs.get("variableNames"):
            assert kwargs == {
                "study_key": "STUDY",
                "record_data_filter": None,
                "variableNames": ["VAR1"],
                "formIds": [10],
            }
            return [record]
        assert kwargs == {
            "study_key": "STUDY",
            "record_data_filter": None,
        }
        return [record]

    sdk.get_records.side_effect = rec_list

    mapper = RecordMapper(sdk)
    df = mapper.dataframe("STUDY", variable_whitelist=["VAR1"], form_whitelist=[10])

    sdk.get_variables.assert_called_once_with(
        study_key="STUDY",
        variableNames=["VAR1"],
        formIds=[10],
    )
    sdk.get_records.assert_called_once_with(
        study_key="STUDY",
        record_data_filter=None,
        variableNames=["VAR1"],
        formIds=[10],
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
    # None or empty
    assert format_iso8601_dtc(None) is None
    assert format_iso8601_dtc("") is None


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
