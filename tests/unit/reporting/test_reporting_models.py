"""Unit tests for reporting models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from imednet.models.reporting import (
    AdverseEvent,
    Demographics,
    DeviceDeficiency,
    ProtocolDeviation,
    VitalSign,
    VitalSigns,
)


def test_adverse_event_parses_alias_input_and_coerces_types() -> None:
    """Test that adverse event parses alias input and coerces types."""
    model = AdverseEvent.model_validate(
        {
            "subjectKey": 101,
            "aeTerm": "  Headache  ",
            "aeSeverity": "MILD",
            "aeSerious": "1",
            "aeStartDate": "2024-01-01T10:00:00+00:00",
            "aeEndDate": "2024-01-02T10:00:00+00:00",
        }
    )

    assert model.subject_key == "101"
    assert model.ae_term == "Headache"
    assert model.ae_serious is True
    assert model.ae_start_date == datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert model.model_dump(by_alias=True)["subjectKey"] == "101"


def test_protocol_deviation_applies_defaults_and_parses_datetime_timestamp() -> None:
    """Test that protocol deviation applies defaults and parses datetime timestamp."""
    model = ProtocolDeviation.model_validate(
        {
            "subjectKey": 77,
            "dvTerm": "Missed visit",
            "dvCategory": "PROCEDURE",
            "dvSeverity": "MAJOR",
            "dvDate": 1705309200,
        }
    )

    assert model.subject_key == "77"
    assert model.dv_status == "Unreviewed"
    assert model.dv_date == datetime(2024, 1, 15, 9, 0, tzinfo=timezone.utc)


def test_device_deficiency_parses_valid_input() -> None:
    """Test that device deficiency parses valid input."""
    model = DeviceDeficiency.model_validate(
        {
            "subjectKey": "SUBJ-001",
            "ddTerm": "Battery failed",
            "ddCategory": "HARDWARE",
            "ddDate": "2024-03-15T12:30:00+00:00",
            "ddSerious": "false",
        }
    )

    assert model.dd_serious is False
    assert model.dd_date == datetime(2024, 3, 15, 12, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (AdverseEvent, {"aeTerm": "Headache", "aeSeverity": "MILD"}),
        (
            ProtocolDeviation,
            {
                "subjectKey": "SUBJ-1",
                "dvCategory": "PROCEDURE",
                "dvSeverity": "MAJOR",
                "dvDate": "2024-01-15T09:00:00+00:00",
            },
        ),
        (
            DeviceDeficiency,
            {
                "subjectKey": "SUBJ-1",
                "ddTerm": "Battery failed",
                "ddDate": "2024-01-15T09:00:00+00:00",
            },
        ),
    ],
)
def test_reporting_models_reject_missing_required_fields(model_cls: type, payload: dict) -> None:
    """Test that reporting models reject missing required fields."""
    with pytest.raises(ValidationError):
        model_cls.model_validate(payload)


def test_protocol_deviation_rejects_invalid_datetime() -> None:
    """Test that protocol deviation rejects invalid datetime."""
    with pytest.raises(ValidationError):
        ProtocolDeviation.model_validate(
            {
                "subjectKey": "SUBJ-1",
                "dvTerm": "Missed visit",
                "dvCategory": "PROCEDURE",
                "dvSeverity": "MAJOR",
                "dvDate": "not-a-datetime",
            }
        )


def test_demographics_parses_alias_input_and_coerces_types() -> None:
    """Test that Demographics parses alias input, coerces types, and applies defaults."""
    model = Demographics.model_validate(
        {
            "subjectKey": 201,
            "sex": "  M  ",
            "birthDate": "1990-05-15T00:00:00+00:00",
            "age": "34",
            "race": "  White  ",
            "ethnicity": "Not Hispanic or Latino",
        }
    )

    assert model.subject_key == "201"
    assert model.sex == "M"
    assert model.birth_date == datetime(1990, 5, 15, 0, 0, tzinfo=timezone.utc)
    assert model.age == 34
    assert model.race == "White"
    assert model.ethnicity == "Not Hispanic or Latino"
    dumped = model.model_dump(by_alias=True)
    assert dumped["subjectKey"] == "201"
    assert dumped["birthDate"] == datetime(1990, 5, 15, 0, 0, tzinfo=timezone.utc)


def test_demographics_defaults_optional_fields_to_none() -> None:
    """Test that Demographics allows optional baseline fields to default to None."""
    model = Demographics.model_validate(
        {
            "subjectKey": "SUBJ-002",
            "sex": "F",
        }
    )

    assert model.subject_key == "SUBJ-002"
    assert model.sex == "F"
    assert model.birth_date is None
    assert model.age is None
    assert model.race is None
    assert model.ethnicity is None


@pytest.mark.parametrize(
    "payload",
    [
        {"sex": "M"},  # missing subjectKey
        {"subjectKey": "SUBJ-001"},  # missing sex
    ],
)
def test_demographics_rejects_missing_required_fields(payload: dict) -> None:
    """Test that Demographics rejects payloads missing mandatory identifiers."""
    with pytest.raises(ValidationError):
        Demographics.model_validate(payload)


def test_demographics_rejects_invalid_datetime() -> None:
    """Test that Demographics rejects invalid birthDate format."""
    with pytest.raises(ValidationError):
        Demographics.model_validate(
            {
                "subjectKey": "SUBJ-1",
                "sex": "M",
                "birthDate": "invalid-date",
            }
        )


def test_vital_signs_parses_alias_input_and_coerces_types() -> None:
    """Test that VitalSigns parses alias input, coerces types, and supports VitalSign alias."""
    model = VitalSigns.model_validate(
        {
            "subjectKey": 301,
            "vsTest": "  Systolic Blood Pressure  ",
            "vsTestCd": "SYSBP",
            "vsResult": "120.5",
            "vsUnit": "mmHg",
            "vsDate": "2024-06-01T08:30:00+00:00",
        }
    )

    assert model.subject_key == "301"
    assert model.vs_test == "Systolic Blood Pressure"
    assert model.vs_test_cd == "SYSBP"
    assert model.vs_result == 120.5
    assert model.vs_unit == "mmHg"
    assert model.vs_date == datetime(2024, 6, 1, 8, 30, tzinfo=timezone.utc)
    assert VitalSign is VitalSigns


def test_vital_signs_allows_optional_fields_to_default_to_none() -> None:
    """Test that VitalSigns defaults optional vsTestCd and vsUnit to None."""
    model = VitalSigns.model_validate(
        {
            "subjectKey": "SUBJ-003",
            "vsTest": "Heart Rate",
            "vsResult": 72,
            "vsDate": 1717230600,
        }
    )

    assert model.subject_key == "SUBJ-003"
    assert model.vs_test == "Heart Rate"
    assert model.vs_test_cd is None
    assert model.vs_unit is None
    assert model.vs_date == datetime(2024, 6, 1, 8, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "payload",
    [
        {"vsTest": "SYSBP", "vsResult": 120, "vsDate": "2024-06-01T08:30:00+00:00"},
        {"subjectKey": "S1", "vsResult": 120, "vsDate": "2024-06-01T08:30:00+00:00"},
        {"subjectKey": "S1", "vsTest": "SYSBP", "vsDate": "2024-06-01T08:30:00+00:00"},
        {"subjectKey": "S1", "vsTest": "SYSBP", "vsResult": 120},
    ],
)
def test_vital_signs_rejects_missing_required_fields(payload: dict) -> None:
    """Test that VitalSigns rejects payloads missing required attributes."""
    with pytest.raises(ValidationError):
        VitalSigns.model_validate(payload)


def test_vital_signs_rejects_invalid_datetime() -> None:
    """Test that VitalSigns rejects invalid vsDate format."""
    with pytest.raises(ValidationError):
        VitalSigns.model_validate(
            {
                "subjectKey": "SUBJ-1",
                "vsTest": "Pulse",
                "vsResult": 75,
                "vsDate": "not-a-datetime",
            }
        )
