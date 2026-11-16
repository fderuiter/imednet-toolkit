"""Unit tests for standards validation."""

from typing import Any

import pytest

from imednet.models.standards import DrugSafetyProfile
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
