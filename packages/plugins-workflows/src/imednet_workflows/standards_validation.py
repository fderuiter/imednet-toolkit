"""Workflow for validating data records against clinical data standards."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from imednet.spi.models import StandardsProfile, ValidationViolation
from imednet.spi.utils import is_boolean_token, is_missing_value, parse_bool


class NormalizationResult(BaseModel):
    """Result of a record normalization operation."""

    normalized_record: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


def _normalize_lookups(
    terminology_lookups: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """Normalize terminology lookup keys for case-insensitive matching."""
    if not terminology_lookups:
        return {}
    return {
        field_name: {str(key).strip().upper(): value for key, value in lookup.items()}
        for field_name, lookup in terminology_lookups.items()
    }


class CategoricalNormalizer:
    """Normalizes categorical values in data records using terminology lookups."""

    def normalize_record(
        self,
        record: dict[str, Any],
        *,
        terminology_lookups: dict[str, dict[str, str]] | None = None,
        normalized_lookups: dict[str, dict[str, str]] | None = None,
    ) -> NormalizationResult:
        """Normalize a single data record.

        Args:
            record: The raw data record as a dictionary.
            terminology_lookups: Optional mapping of field names to their terminology maps.
            normalized_lookups: Optional precomputed normalized terminology lookups.

        Returns:
            A NormalizationResult containing the normalized record and any warnings.
        """
        normalized_record: dict[str, Any] = {}
        warnings: list[str] = []
        lookups = (
            normalized_lookups
            if normalized_lookups is not None
            else self._normalize_lookups(terminology_lookups)
        )

        subject_key = record.get("subjectKey") or record.get("subject_key")
        for field_name, raw_value in record.items():
            normalized_value, warning_message = self._normalize_value(
                field_name=field_name,
                raw_value=raw_value,
                normalized_lookups=lookups,
                subject_key=subject_key,
            )
            normalized_record[field_name] = normalized_value
            if warning_message is not None:
                warnings.append(warning_message)

        return NormalizationResult(normalized_record=normalized_record, warnings=warnings)

    def normalize_lookups(
        self, terminology_lookups: dict[str, dict[str, str]] | None = None
    ) -> dict[str, dict[str, str]]:
        """Normalize terminology lookup keys for case-insensitive matching."""
        return _normalize_lookups(terminology_lookups)

    def _normalize_lookups(
        self, terminology_lookups: dict[str, dict[str, str]] | None = None
    ) -> dict[str, dict[str, str]]:
        """Internal helper to normalize terminology lookup keys for case-insensitive matching."""
        return self.normalize_lookups(terminology_lookups)

    def _normalize_value(
        self,
        *,
        field_name: str,
        raw_value: Any,
        normalized_lookups: dict[str, dict[str, str]],
        subject_key: Any,
    ) -> tuple[Any, str | None]:
        """Internal helper to normalize a single field value."""
        lookup = normalized_lookups.get(field_name)
        if lookup and isinstance(raw_value, str):
            lookup_key = raw_value.strip().upper()
            if lookup_key in lookup:
                return lookup[lookup_key], None
            subject_prefix = (
                f"Subject {subject_key} " if isinstance(subject_key, str) and subject_key else ""
            )
            return raw_value, f"{subject_prefix}record {field_name} is unmapped"

        if isinstance(raw_value, str):
            stripped_value = raw_value.strip()
            if stripped_value and is_boolean_token(stripped_value):
                return parse_bool(stripped_value), None

        return raw_value, None


class StandardsReadinessReport(BaseModel):
    """Comprehensive report on the readiness of data records against clinical standards."""

    score: float
    successfully_validated_fields: int
    total_expected_fields: int
    warnings: list[str] = Field(default_factory=list)
    violations: list[ValidationViolation] = Field(default_factory=list)


class StandardsReadinessValidator:
    """Validates and scores data records against a clinical standards profile."""

    def __init__(
        self, profile: StandardsProfile, normalizer: CategoricalNormalizer | None = None
    ) -> None:
        """Initialize the validator.

        Args:
            profile: The standards profile to validate against.
            normalizer: Optional custom normalizer.
        """
        self._profile = profile
        self._normalizer = normalizer or CategoricalNormalizer()

    def _normalize_lookups(
        self, terminology_lookups: dict[str, dict[str, str]] | None = None
    ) -> dict[str, dict[str, str]]:
        """Internal helper to normalize terminology lookup keys for case-insensitive matching."""
        if hasattr(self._normalizer, "normalize_lookups"):
            return self._normalizer.normalize_lookups(terminology_lookups)
        return _normalize_lookups(terminology_lookups)

    def score_records(
        self,
        *,
        records_by_domain: dict[str, list[dict[str, Any]]],
        terminology_lookups: dict[str, dict[str, str]] | None = None,
    ) -> StandardsReadinessReport:
        """Validate and score multiple domains of records.

        Args:
            records_by_domain: Mapping of domain names to lists of records.
            terminology_lookups: Optional terminology lookups for normalization.

        Returns:
            A StandardsReadinessReport containing scores and violations.
        """
        lookups = terminology_lookups or {}
        warnings: list[str] = []
        violations: list[ValidationViolation] = []
        successfully_validated_fields = 0
        total_expected_fields = 0
        normalized_lookups = self._normalize_lookups(lookups)

        for domain, records in records_by_domain.items():
            expected_fields = self._profile.expected_fields(domain)
            for record in records:
                normalization_result = self._normalizer.normalize_record(
                    record, normalized_lookups=normalized_lookups
                )
                warnings.extend(normalization_result.warnings)

                record_violations = self._profile.validate(
                    domain=domain, data=normalization_result.normalized_record
                )
                violations.extend(record_violations)

                total_expected_fields += len(expected_fields)
                error_fields = {
                    violation.field
                    for violation in record_violations
                    if violation.severity.upper() == "ERROR"
                }
                for field_name in expected_fields:
                    value = normalization_result.normalized_record.get(field_name)
                    if field_name in error_fields or is_missing_value(value):
                        continue
                    successfully_validated_fields += 1

        score = (
            100.0
            if total_expected_fields == 0
            else (successfully_validated_fields / total_expected_fields) * 100
        )
        return StandardsReadinessReport(
            score=score,
            successfully_validated_fields=successfully_validated_fields,
            total_expected_fields=total_expected_fields,
            warnings=warnings,
            violations=violations,
        )


__all__ = [
    "CategoricalNormalizer",
    "NormalizationResult",
    "StandardsReadinessReport",
    "StandardsReadinessValidator",
]
