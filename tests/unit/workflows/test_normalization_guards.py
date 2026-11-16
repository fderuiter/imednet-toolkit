"""Data-preservation and explicit configuration boundaries for normalization."""

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from imednet.models.standards import DrugSafetyProfile
from imednet.models.study_config import MappingRule
from imednet.utils.dates import normalize_iso_date
from imednet_workflows.standards_validation import (
    CategoricalNormalizer,
    StandardsReadinessValidator,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024/3/15", "2024-03-15"),
        ("03/15/2024", "2024-03-15"),
        ("15/03/2024", "2024-03-15"),
        ("15-mar-2024", "2024-03-15"),
        ("20240315", "2024-03-15"),
        ("2024-03-15 10:30:01", "2024-03-15T10:30:01"),
        ("2024-03-15 10:30:01.123456789+05:30", "2024-03-15T10:30:01.123456789+05:30"),
        ("2024-03-15T10:30:01Z", "2024-03-15T10:30:01Z"),
        ("2024-03", "2024-03"),
        ("2024", "2024"),
        ("2024-UNK-15", "2024-UNK-15"),
        ("03/04/2024", "03/04/2024"),
        ("2024-02-30T12:00:00", "2024-02-30T12:00:00"),
        ("2024-01-01T25:00:00", "2024-01-01T25:00:00"),
        ("2024-01-01T12:00:00+05:99", "2024-01-01T12:00:00+05:99"),
        ("2023-02-29", "2023-02-29"),
        ("00/15/2024", "00/15/2024"),
        ("31-xyz-2024", "31-xyz-2024"),
        ("31-Feb-2024", "31-Feb-2024"),
        ("20240231", "20240231"),
        (None, None),
        ("  ", "  "),
        (False, False),
        (0, 0),
        (date(2024, 3, 15), "2024-03-15"),
        (
            datetime(2024, 3, 15, 10, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            "2024-03-15T10:00:00+05:30",
        ),
    ],
)
def test_explicit_date_normalization_preserves_precision_and_unknowns(value, expected):
    assert normalize_iso_date(value) == expected


def test_ambiguous_dates_require_explicit_order():
    assert normalize_iso_date("03/04/2024", date_order="MDY") == "2024-03-04"
    assert normalize_iso_date("03/04/2024", date_order="DMY") == "2024-04-03"
    with pytest.raises(ValueError, match="date_order"):
        normalize_iso_date("03/04/2024", date_order="invalid")


def test_default_normalizer_preserves_date_like_ids_and_existing_global_identity():
    record = {
        "subjectKey": "20240315",
        "USUBJID": "global-009",
        "studyKey": "MixedStudy",
        "comment": "2024/03/15",
        "aeSeverity": "mild",
    }
    normalized = (
        CategoricalNormalizer().normalize_record(record, study_key="OTHER").normalized_record
    )
    assert normalized == record
    assert record["USUBJID"] == "global-009"
    assert CategoricalNormalizer().normalize_record(
        {"USUBJID": "NO", "subjectKey": "YES"}
    ).normalized_record == {"USUBJID": "NO", "subjectKey": "YES"}


def test_date_fields_are_selected_explicitly_and_unknowns_retained():
    record = {
        "date": "03/04/2024",
        "identifier": "20240315",
        "text": "2024/03/15",
        "partial": "2024-03",
        "unknown": "NO",
    }
    normalizer = CategoricalNormalizer(date_fields={"date", "partial", "unknown"}, date_order="DMY")
    result = normalizer.normalize_record(record).normalized_record
    assert result == {**record, "date": "2024-04-03"}
    assert record["date"] == "03/04/2024"
    with pytest.raises(ValueError, match="collection"):
        CategoricalNormalizer(date_fields="date")
    with pytest.raises(ValueError, match="date_order"):
        CategoricalNormalizer(date_order="invalid")


def test_usubjid_derivation_is_explicit_and_preserves_case():
    normalizer = CategoricalNormalizer()
    raw = {"studyKey": "MixedStudy", "subjectKey": "SUB-1", "USUBJID": ""}
    assert normalizer.normalize_record(raw).normalized_record == raw
    assert (
        normalizer.normalize_record(raw, study_key="MixedStudy").normalized_record["USUBJID"]
        == "MixedStudy-SUB-1"
    )
    assert (
        "USUBJID"
        not in normalizer.normalize_record({"subjectKey": "SUB-1"}, study_key="S").normalized_record
    )


def test_terminology_is_snapshotted_case_insensitive_and_does_not_echo_payloads():
    source = {"answer": {"Yes", "No"}}
    normalizer = CategoricalNormalizer(source)
    source["answer"].add("Maybe")
    assert normalizer.normalize_record({"answer": "yes"}).normalized_record["answer"] == "Yes"
    result = normalizer.normalize_record(
        {"answer": "private-clinical-value", "subjectKey": "private-subject"}
    )
    assert result.normalized_record["answer"] == "private-clinical-value"
    assert result.warnings == ["Field answer is outside the configured terminology"]
    with pytest.raises(ValueError, match="case-colliding"):
        CategoricalNormalizer({"answer": {"Yes", "YES"}})
    with pytest.raises(ValueError, match="empty"):
        CategoricalNormalizer({"answer": {" "}})


def test_lookup_mapping_takes_precedence_in_readiness_scoring():
    validator = StandardsReadinessValidator(
        DrugSafetyProfile(), CategoricalNormalizer({"aeSeverity": {"MILD"}})
    )
    record = {
        "subjectKey": "S",
        "aeTerm": "Headache",
        "aeSeverity": "raw",
        "aeDecod": "Headache",
        "aeRelationship": "RELATED",
        "aeActionTaken": "NONE",
        "aeOutcome": "RECOVERED",
    }
    report = validator.score_records(
        records_by_domain={"AE": [record]}, terminology_lookups={"aeSeverity": {"raw": "3"}}
    )
    assert not any(v.field == "aeSeverity" for v in report.violations)


def test_sdk_strictness_does_not_enable_cdash_name_validation(monkeypatch):
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "true")
    monkeypatch.delenv("IMEDNET_STRICT_CDASH", raising=False)
    raw = dict(
        domain="AE",
        targetField="study_specific_field",
        sourceFormKey="FORM",
        sourceVariableName="RAW",
    )
    assert MappingRule.model_validate(raw).target_field == "study_specific_field"
    with pytest.raises(ValidationError, match="prefix"):
        MappingRule.model_validate({**raw, "strictCdash": True})


def test_empty_lookup_does_not_disable_configured_codelist():
    validator = StandardsReadinessValidator(
        DrugSafetyProfile(), CategoricalNormalizer({"aeSeverity": {"MILD"}})
    )
    report = validator.score_records(
        records_by_domain={"AE": [{"subjectKey": "S", "aeTerm": "A", "aeSeverity": "3"}]},
        terminology_lookups={"aeSeverity": {}},
    )
    assert sum(v.field == "aeSeverity" and v.severity == "ERROR" for v in report.violations) == 1


def test_terminology_errors_are_counted_once_and_exclude_values():
    validator = StandardsReadinessValidator(
        DrugSafetyProfile(), CategoricalNormalizer({"aeRelationship": {"Related"}})
    )
    report = validator.score_records(
        records_by_domain={
            "AE": [
                {
                    "subjectKey": "private-subject",
                    "aeTerm": "A",
                    "aeSeverity": "3",
                    "aeRelationship": "private-value",
                }
            ]
        }
    )
    matches = [v for v in report.violations if v.field == "aeRelationship"]
    assert len(matches) == 1 and matches[0].severity == "ERROR"
    assert "private-value" not in matches[0].message
    assert "private-subject" not in " ".join(report.warnings)


@pytest.mark.parametrize("field", ["subjectKey", "birthDate", "age", "sex", "race", "ethnicity"])
def test_strict_names_accept_current_demographics_reporting_fields(field):
    assert (
        MappingRule(
            domain="Demographics",
            targetField=field,
            sourceFormKey="FORM",
            sourceVariableName="RAW",
            strictCdash=True,
        ).target_field
        == field
    )
