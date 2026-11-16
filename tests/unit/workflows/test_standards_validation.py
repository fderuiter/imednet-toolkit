"""Unit tests for standards validation."""

from typing import Any

import pytest

from imednet.models.standards import (
    NORMALIZATION_TERMINOLOGY,
    DrugSafetyProfile,
    GeneralClinicalProfile,
)
from imednet_workflows.standards_validation import (
    CategoricalNormalizer,
    NormalizationResult,
    StandardsReadinessValidator,
)


def test_categorical_normalizer_translates_lookup_values_and_yes_no_booleans() -> None:
    """Test that categorical normalizer translates lookup values and yes no booleans."""
    normalizer = CategoricalNormalizer()
    result = normalizer.normalize_record(
        {
            "subjectKey": "S004",
            "AE_SEV": "2",
            "aeSerious": "YES",
        },
        terminology_lookups={"AE_SEV": {"1": "MILD", "2": "MODERATE", "3": "SEVERE"}},
    )

    assert result.normalized_record["AE_SEV"] == "MODERATE"
    assert result.normalized_record["aeSerious"] is True
    assert result.warnings == []


def test_standards_readiness_validator_scores_records() -> None:
    """Test that standards readiness validator scores records."""
    validator = StandardsReadinessValidator(profile=DrugSafetyProfile())
    report = validator.score_records(
        records_by_domain={
            "AE": [
                {
                    "subjectKey": "S001",
                    "aeTerm": "Headache",
                    "aeSeverity": "3",
                    "aeDecod": "HEADACHE",
                    "aeRelationship": "RELATED",
                    "aeActionTaken": "NONE",
                    "aeOutcome": "RECOVERED",
                },
                {
                    "subjectKey": "S004",
                    "aeTerm": "Nausea",
                    "aeSeverity": "9",
                    "aeDecod": "",
                    "aeRelationship": "",
                },
            ]
        },
        terminology_lookups={"aeSeverity": {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5"}},
    )

    assert report.total_expected_fields == 14
    assert report.successfully_validated_fields == 9
    assert report.score == pytest.approx((9 / 14) * 100)
    assert "Subject S004 record aeSeverity is unmapped" in report.warnings
    assert {(violation.field, violation.severity) for violation in report.violations} == {
        ("aeActionTaken", "WARNING"),
        ("aeDecod", "ERROR"),
        ("aeOutcome", "WARNING"),
        ("aeRelationship", "ERROR"),
        ("aeSeverity", "ERROR"),
    }


def test_categorical_normalizer_precomputed_lookups_match_legacy_lookups() -> None:
    """Test that precomputed lookups produce identical results to legacy terminology_lookups."""
    normalizer = CategoricalNormalizer()
    record = {
        "subjectKey": "S005",
        "AE_SEV": " 2 ",
        "aeSerious": "YES",
        "aeStatus": "UNKNOWN",
    }
    raw_lookups = {
        "AE_SEV": {" 1 ": "MILD", " 2 ": "MODERATE", " 3 ": "SEVERE"},
        "aeStatus": {"RESOLVED": "COMPLETED"},
    }

    legacy_result = normalizer.normalize_record(record, terminology_lookups=raw_lookups)

    precomputed = normalizer._normalize_lookups(raw_lookups)
    precomputed_result = normalizer.normalize_record(record, normalized_lookups=precomputed)

    assert precomputed_result.normalized_record == legacy_result.normalized_record
    assert precomputed_result.warnings == legacy_result.warnings
    assert precomputed_result.normalized_record["AE_SEV"] == "MODERATE"
    assert precomputed_result.normalized_record["aeSerious"] is True
    assert "Subject S005 record aeStatus is unmapped" in precomputed_result.warnings


def test_categorical_normalizer_handles_none_and_empty_lookups() -> None:
    """Test that normalizer handles None and empty lookups gracefully."""
    normalizer = CategoricalNormalizer()
    record = {"subjectKey": "S006", "aeSerious": "NO", "rawField": "ABC"}

    assert normalizer._normalize_lookups(None) == {}
    assert normalizer._normalize_lookups({}) == {}

    result_none = normalizer.normalize_record(record, terminology_lookups=None)
    assert result_none.normalized_record["aeSerious"] is False
    assert result_none.normalized_record["rawField"] == "ABC"
    assert result_none.warnings == []

    result_empty = normalizer.normalize_record(record, terminology_lookups={})
    assert result_empty.normalized_record == result_none.normalized_record

    result_omitted = normalizer.normalize_record(record)
    assert result_omitted.normalized_record == result_none.normalized_record


def test_categorical_normalizer_case_and_whitespace_normalization() -> None:
    """Test case insensitivity and whitespace handling with precomputed lookups."""
    normalizer = CategoricalNormalizer()
    lookups = {"fieldA": {" low ": "L", "Medium": "M", "HIGH": "H"}}
    precomputed = normalizer._normalize_lookups(lookups)

    assert precomputed == {"fieldA": {"LOW": "L", "MEDIUM": "M", "HIGH": "H"}}

    record = {"fieldA": "  low  "}
    res = normalizer.normalize_record(record, normalized_lookups=precomputed)
    assert res.normalized_record["fieldA"] == "L"


def test_standards_readiness_validator_without_terminology_lookups() -> None:
    """Test that standards readiness validator works without terminology lookups."""
    validator = StandardsReadinessValidator(profile=DrugSafetyProfile())
    records = {
        "AE": [
            {
                "subjectKey": "S001",
                "aeTerm": "Headache",
                "aeSeverity": "MODERATE",
                "aeDecod": "HEADACHE",
                "aeRelationship": "RELATED",
                "aeActionTaken": "NONE",
                "aeOutcome": "RECOVERED",
            }
        ]
    }

    report_none = validator.score_records(records_by_domain=records, terminology_lookups=None)
    report_empty = validator.score_records(records_by_domain=records, terminology_lookups={})

    assert report_none.score == report_empty.score
    assert report_none.successfully_validated_fields == report_empty.successfully_validated_fields
    assert report_none.violations == report_empty.violations
    assert report_none.warnings == report_empty.warnings


def test_standards_readiness_validator_matches_direct_normalizer_pipeline() -> None:
    """Test that validator pipeline produces identical normalized records and warnings as normalizer."""
    normalizer = CategoricalNormalizer()
    validator = StandardsReadinessValidator(profile=DrugSafetyProfile(), normalizer=normalizer)

    lookups = {"aeSeverity": {"1": "MILD", "2": "MODERATE", "3": "SEVERE"}}
    records = [
        {
            "subjectKey": "S001",
            "aeTerm": "Headache",
            "aeSeverity": "2",
            "aeDecod": "HEADACHE",
            "aeRelationship": "RELATED",
            "aeActionTaken": "NONE",
            "aeOutcome": "RECOVERED",
        },
        {
            "subjectKey": "S002",
            "aeTerm": "Fever",
            "aeSeverity": "9",
            "aeDecod": "",
            "aeRelationship": "",
        },
    ]

    report = validator.score_records(
        records_by_domain={"AE": records},
        terminology_lookups=lookups,
    )

    precomputed = normalizer._normalize_lookups(lookups)
    direct_warnings = []
    for record in records:
        direct_result = normalizer.normalize_record(record, normalized_lookups=precomputed)
        direct_warnings.extend(direct_result.warnings)

    assert report.warnings == direct_warnings


def test_standards_readiness_validator_custom_normalizer_without_normalize_lookups() -> None:
    """Test validator when custom normalizer lacks normalize_lookups."""

    class LegacyCustomNormalizer:
        def normalize_record(
            self,
            record: dict[str, Any],
            *,
            terminology_lookups: dict[str, dict[str, str]] | None = None,
            normalized_lookups: dict[str, dict[str, str]] | None = None,
        ) -> NormalizationResult:
            return NormalizationResult(normalized_record=record, warnings=[])

    validator = StandardsReadinessValidator(
        profile=DrugSafetyProfile(),
        normalizer=LegacyCustomNormalizer(),  # type: ignore[arg-type]
    )
    report = validator.score_records(
        records_by_domain={"AE": [{"subjectKey": "S001"}]},
        terminology_lookups={"aeSeverity": {"1": "MILD"}},
    )
    assert report.successfully_validated_fields == 1
    assert report.total_expected_fields == 7


def test_standards_readiness_validator_scores_demographics_records() -> None:
    """Test that validator scores Demographics records and detects missing required/recommended fields."""
    validator = StandardsReadinessValidator(profile=GeneralClinicalProfile())
    report = validator.score_records(
        records_by_domain={
            "DM": [
                {
                    "subjectKey": "S001",
                    "sex": "M",
                    "birthDate": "1990-01-01",
                    "age": 34,
                    "race": "White",
                    "ethnicity": "Not Hispanic or Latino",
                },
                {
                    "subjectKey": "S002",
                    # missing sex (required) and birthDate, age (recommended)
                    "race": "Asian",
                    "ethnicity": "Not Hispanic or Latino",
                },
            ]
        }
    )

    # 6 expected fields per DM record in general profile = 12 total expected
    assert report.total_expected_fields == 12
    # Record 1: all 6 valid. Record 2: subjectKey, race, ethnicity valid = 3 valid. Total = 9
    assert report.successfully_validated_fields == 9
    assert report.score == pytest.approx((9 / 12) * 100)
    assert {(v.field, v.severity) for v in report.violations} == {
        ("sex", "ERROR"),
        ("birthDate", "WARNING"),
        ("age", "WARNING"),
    }


def test_standards_readiness_validator_scores_vital_signs_records() -> None:
    """Test that validator scores Vital Signs records and detects missing attributes."""
    validator = StandardsReadinessValidator(profile=GeneralClinicalProfile())
    report = validator.score_records(
        records_by_domain={
            "VS": [
                {
                    "subjectKey": "S001",
                    "vsTest": "Systolic Blood Pressure",
                    "vsTestCd": "SYSBP",
                    "vsResult": 120,
                    "vsUnit": "mmHg",
                    "vsDate": "2024-01-01T10:00:00",
                },
                {
                    "subjectKey": "S002",
                    "vsTest": "Pulse",
                    # missing vsResult, vsDate (required), missing vsTestCd, vsUnit (recommended)
                },
            ]
        }
    )

    # 6 expected fields per VS record (4 required + 2 recommended) = 12 total expected
    assert report.total_expected_fields == 12
    # Record 1: 6 valid. Record 2: subjectKey, vsTest valid = 2 valid. Total = 8
    assert report.successfully_validated_fields == 8
    assert report.score == pytest.approx((8 / 12) * 100)
    assert {(v.field, v.severity) for v in report.violations} == {
        ("vsResult", "ERROR"),
        ("vsDate", "ERROR"),
        ("vsTestCd", "WARNING"),
        ("vsUnit", "WARNING"),
    }


def test_standards_readiness_validator_scores_combined_clinical_domains() -> None:
    """Test that validator scores combined datasets with AE, DM, and VS records."""
    validator = StandardsReadinessValidator(profile=GeneralClinicalProfile())
    records = {
        "AE": [
            {
                "subjectKey": "S001",
                "aeTerm": "Headache",
                "aeSeverity": "MILD",
                "aeDecod": "HEADACHE",
                "aeRelationship": "NOT RELATED",
            }
        ],
        "DM": [
            {
                "subjectKey": "S001",
                "sex": "F",
                "birthDate": "1985-06-20",
                "age": 39,
                "race": "Black",
                "ethnicity": "Not Hispanic or Latino",
            }
        ],
        "VS": [
            {
                "subjectKey": "S001",
                "vsTest": "Heart Rate",
                "vsTestCd": "HR",
                "vsResult": 72,
                "vsUnit": "bpm",
                "vsDate": "2024-01-01T10:00:00",
            }
        ],
    }

    report = validator.score_records(records_by_domain=records)
    # AE: 5 expected (3 req + 2 rec)
    # DM: 6 expected (2 req + 4 rec)
    # VS: 6 expected (4 req + 2 rec)
    # Total expected = 17, all valid = 17
    assert report.total_expected_fields == 17
    assert report.successfully_validated_fields == 17
    assert report.score == 100.0
    assert report.violations == []
    assert report.warnings == []


def test_standards_readiness_validator_handles_domain_aliases() -> None:
    """Test that validator handles domain aliases such as Demographics and Vital Signs."""
    validator = StandardsReadinessValidator(profile=GeneralClinicalProfile())

    report_canonical = validator.score_records(
        records_by_domain={
            "DM": [{"subjectKey": "S001", "sex": "M"}],
            "VS": [
                {"subjectKey": "S001", "vsTest": "SYSBP", "vsResult": 120, "vsDate": "2024-01-01"}
            ],
        }
    )

    report_aliased = validator.score_records(
        records_by_domain={
            "Demographics": [{"subjectKey": "S001", "sex": "M"}],
            "Vital Signs": [
                {"subjectKey": "S001", "vsTest": "SYSBP", "vsResult": 120, "vsDate": "2024-01-01"}
            ],
        }
    )

    assert report_canonical.score == report_aliased.score
    assert (
        report_canonical.successfully_validated_fields
        == report_aliased.successfully_validated_fields
    )
    assert report_canonical.total_expected_fields == report_aliased.total_expected_fields


def test_standards_readiness_validator_terminology_normalization_and_warnings() -> None:
    """Test that categorical normalizer maps terminology and flags unmapped values as warnings."""
    validator = StandardsReadinessValidator(profile=DrugSafetyProfile())
    lookups = {
        "sex": {"MALE": "M", "FEMALE": "F"},
        "vsTestCd": {"BLOOD_PRESSURE": "SYSBP"},
        "vsUnit": {"MILLIMETERS_OF_MERCURY": "mmHg"},
    }

    records = {
        "DM": [
            {
                "subjectKey": "S001",
                "sex": "MALE",
                "birthDate": "1990-01-01",
                "age": 34,
                "race": "White",
                "ethnicity": "Not Hispanic",
            },
            {
                "subjectKey": "S002",
                "sex": "UNKNOWN_OTHER",  # unmapped
                "birthDate": "1992-01-01",
                "age": 32,
                "race": "White",
                "ethnicity": "Not Hispanic",
            },
        ],
        "VS": [
            {
                "subjectKey": "S001",
                "vsTest": "Blood Pressure",
                "vsTestCd": "BLOOD_PRESSURE",  # mapped to SYSBP
                "vsResult": 120,
                "vsUnit": "MILLIMETERS_OF_MERCURY",  # mapped to mmHg
                "vsDate": "2024-01-01T00:00:00",
            },
            {
                "subjectKey": "S002",
                "vsTest": "Blood Pressure",
                "vsTestCd": "UNMAPPED_CD",  # unmapped
                "vsResult": 120,
                "vsUnit": "LIGHT_YEARS",  # unmapped and invalid constraint
                "vsDate": "2024-01-01T00:00:00",
            },
        ],
    }

    report = validator.score_records(records_by_domain=records, terminology_lookups=lookups)

    assert "Subject S002 record sex is unmapped" in report.warnings
    assert "Subject S002 record vsTestCd is unmapped" in report.warnings
    assert "Subject S002 record vsUnit is unmapped" in report.warnings
    # S002 has sex and vsUnit constraint error violations
    error_fields = {v.field for v in report.violations if v.severity == "ERROR"}
    assert "sex" in error_fields
    assert "vsUnit" in error_fields


def test_categorical_normalizer_normalizes_cdisc_ct() -> None:
    """Test that CategoricalNormalizer normalizes valid CDISC CT values case-insensitively."""
    normalizer = CategoricalNormalizer(
        NORMALIZATION_TERMINOLOGY,
        date_fields={
            "aeDate",
            "date_ymd",
            "date_mdy",
            "date_cdisc",
            "date_compact",
            "datetime_str",
        },
    )
    result = normalizer.normalize_record(
        {
            "subjectKey": "  S001  ",
            "aeSeverity": "mild",
            "aeRelationship": "related",
            "aeActionTaken": "none",
            "aeOutcome": "recovered",
            "aeDate": "2024/03/15",
        }
    )

    assert result.normalized_record["subjectKey"] == "S001"
    assert result.normalized_record["aeSeverity"] == "MILD"
    assert result.normalized_record["aeRelationship"] == "RELATED"
    assert result.normalized_record["aeActionTaken"] == "NONE"
    assert result.normalized_record["aeOutcome"] == "RECOVERED"
    assert result.normalized_record["aeDate"] == "2024-03-15"
    assert result.warnings == []


def test_categorical_normalizer_flags_unmapped_cdisc_ct_value() -> None:
    """Test that values not in CDISC CT generate detailed warnings."""
    normalizer = CategoricalNormalizer(
        NORMALIZATION_TERMINOLOGY,
        date_fields={
            "aeDate",
            "date_ymd",
            "date_mdy",
            "date_cdisc",
            "date_compact",
            "datetime_str",
        },
    )
    result = normalizer.normalize_record(
        {
            "subjectKey": "S002",
            "aeSeverity": "EXTREME_PAIN",
            "aeRelationship": "MAYBE_RELATED",
        }
    )

    assert result.normalized_record["aeSeverity"] == "EXTREME_PAIN"
    assert any(
        "Field aeSeverity is outside the configured terminology" in w for w in result.warnings
    )
    assert any(
        "Field aeRelationship is outside the configured terminology" in w for w in result.warnings
    )


def test_categorical_normalizer_canonicalizes_usubjid_with_study_key() -> None:
    """Test canonical unique subject identifier formatting across records."""
    normalizer = CategoricalNormalizer(
        NORMALIZATION_TERMINOLOGY,
        date_fields={
            "aeDate",
            "date_ymd",
            "date_mdy",
            "date_cdisc",
            "date_compact",
            "datetime_str",
        },
    )
    result = normalizer.normalize_record(
        {
            "studyKey": "STUDY-001",
            "subjectKey": "  SUBJ-123  ",
            "USUBJID": "",
        },
        study_key="STUDY-001",
    )

    assert result.normalized_record["subjectKey"] == "SUBJ-123"
    assert result.normalized_record["USUBJID"] == "STUDY-001-SUBJ-123"


def test_categorical_normalizer_transforms_various_date_formats() -> None:
    """Test that various date formats are transformed into ISO 8601 strings."""
    normalizer = CategoricalNormalizer(
        NORMALIZATION_TERMINOLOGY,
        date_fields={
            "aeDate",
            "date_ymd",
            "date_mdy",
            "date_cdisc",
            "date_compact",
            "datetime_str",
        },
    )
    record = {
        "date_ymd": "2024/03/15",
        "date_mdy": "03/15/2024",
        "date_cdisc": "15-MAR-2024",
        "date_compact": "20240315",
        "datetime_str": "2024-03-15 12:30:00",
        "non_date": "Headache",
    }
    result = normalizer.normalize_record(record)
    assert result.normalized_record["date_ymd"] == "2024-03-15"
    assert result.normalized_record["date_mdy"] == "2024-03-15"
    assert result.normalized_record["date_cdisc"] == "2024-03-15"
    assert result.normalized_record["date_compact"] == "2024-03-15"
    assert result.normalized_record["datetime_str"] == "2024-03-15T12:30:00"
    assert result.normalized_record["non_date"] == "Headache"


def test_standards_readiness_validator_records_cdisc_ct_violations() -> None:
    """Test that readiness report records CDISC CT violations and calculates accurate score."""
    validator = StandardsReadinessValidator(
        profile=DrugSafetyProfile(), normalizer=CategoricalNormalizer(NORMALIZATION_TERMINOLOGY)
    )
    report = validator.score_records(
        records_by_domain={
            "AE": [
                {
                    "subjectKey": "S001",
                    "aeTerm": "Headache",
                    "aeSeverity": "3",
                    "aeDecod": "HEADACHE",
                    "aeRelationship": "NOT_A_VALID_RELATIONSHIP",  # CDISC CT violation
                    "aeActionTaken": "NONE",
                    "aeOutcome": "RECOVERED",
                }
            ]
        }
    )

    # 7 expected fields, 1 error violation -> 6 successfully validated fields
    assert report.total_expected_fields == 7
    assert report.successfully_validated_fields == 6
    assert report.score == pytest.approx((6 / 7) * 100)
    assert any(
        v.field == "aeRelationship" and "outside the configured terminology" in v.message
        for v in report.violations
    )


@pytest.mark.performance
def test_normalization_performance_under_two_milliseconds_per_record() -> None:
    """Verify that record normalization completes in under 2 milliseconds per record."""
    import time

    normalizer = CategoricalNormalizer(
        NORMALIZATION_TERMINOLOGY,
        date_fields={
            "aeDate",
            "date_ymd",
            "date_mdy",
            "date_cdisc",
            "date_compact",
            "datetime_str",
        },
    )
    record = {
        "studyKey": "STUDY-PERF",
        "subjectKey": "  SUBJ-001  ",
        "aeTerm": "Headache",
        "aeSeverity": "mild",
        "aeRelationship": "related",
        "aeActionTaken": "none",
        "aeOutcome": "recovered",
        "aeDate": "15-MAR-2024",
    }

    start = time.perf_counter()
    iterations = 500
    for _ in range(iterations):
        normalizer.normalize_record(record)
    elapsed_ms = (time.perf_counter() - start) * 1000
    ms_per_record = elapsed_ms / iterations

    # Constraint: under 2 ms per record
    assert ms_per_record < 2.0
