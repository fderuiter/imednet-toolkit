"""Workflow for validating data records against clinical data standards."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from imednet.spi.models import (
    CDISC_CONTROLLED_TERMINOLOGY,
    StandardsProfile,
    ValidationViolation,
)
from imednet.spi.utils import (
    is_boolean_token,
    is_missing_value,
    normalize_iso_date,
    parse_bool,
)


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
    """Normalizes categorical values in data records using terminology lookups and CDISC CT."""

    def __init__(self, cdisc_ct: dict[str, set[str]] | None = None) -> None:
        """Initialize with optional custom CDISC Controlled Terminology codelists."""
        self.cdisc_ct: dict[str, set[str]] = (
            cdisc_ct if cdisc_ct is not None else CDISC_CONTROLLED_TERMINOLOGY
        )

    def normalize_record(
        self,
        record: dict[str, Any],
        *,
        terminology_lookups: dict[str, dict[str, str]] | None = None,
        normalized_lookups: dict[str, dict[str, str]] | None = None,
        study_key: str | None = None,
    ) -> NormalizationResult:
        """Normalize a single data record.

        Args:
            record: The raw data record as a dictionary.
            terminology_lookups: Optional mapping of field names to their terminology maps.
            normalized_lookups: Optional precomputed normalized terminology lookups.
            study_key: Optional study key for generating canonical USUBJID.

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

        subject_key = (
            record.get("subjectKey")
            or record.get("subject_key")
            or record.get("USUBJID")
            or record.get("usubjid")
        )
        canonical_subject = subject_key.strip() if isinstance(subject_key, str) else subject_key
        study = study_key or record.get("studyKey") or record.get("study_key")

        for field_name, raw_value in record.items():
            normalized_value, warning_message = self._normalize_value(
                field_name=field_name,
                raw_value=raw_value,
                normalized_lookups=lookups,
                subject_key=canonical_subject,
            )
            normalized_record[field_name] = normalized_value
            if warning_message is not None:
                warnings.append(warning_message)

        # Canonicalize subject identifier
        if "subjectKey" in normalized_record and isinstance(normalized_record["subjectKey"], str):
            normalized_record["subjectKey"] = normalized_record["subjectKey"].strip()
        if "subject_key" in normalized_record and isinstance(normalized_record["subject_key"], str):
            normalized_record["subject_key"] = normalized_record["subject_key"].strip()

        # Format canonical unique subject identifier (USUBJID) if study is known
        if study and isinstance(canonical_subject, str) and canonical_subject:
            clean_study = str(study).strip().upper()
            clean_subj = canonical_subject.strip()
            if not clean_subj.upper().startswith(f"{clean_study}-"):
                canonical_usubjid = f"{clean_study}-{clean_subj}"
            else:
                canonical_usubjid = clean_subj
            if "USUBJID" in record or "usubjid" in record:
                target_key = "USUBJID" if "USUBJID" in record else "usubjid"
                normalized_record[target_key] = canonical_usubjid

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

        # Check CDISC Controlled Terminology if no user lookup was provided for this field
        if field_name in self.cdisc_ct and isinstance(raw_value, str) and raw_value.strip():
            stripped_val = raw_value.strip()
            upper_val = stripped_val.upper()
            codelist = self.cdisc_ct[field_name]
            if upper_val in codelist:
                canonical_term = upper_val
                for term in codelist:
                    if str(term).upper() == upper_val:
                        canonical_term = str(term)
                        break
                return canonical_term, None

            subject_prefix = (
                f"Subject {subject_key} " if isinstance(subject_key, str) and subject_key else ""
            )
            return (
                raw_value,
                f"{subject_prefix}record {field_name} value '{raw_value}' does not match CDISC Controlled Terminology",
            )

        # Transform valid date inputs into ISO 8601 strings
        normalized_date = normalize_iso_date(raw_value)
        if normalized_date != raw_value:
            return normalized_date, None

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

                record_violations = list(
                    self._profile.validate(
                        domain=domain, data=normalization_result.normalized_record
                    )
                )

                # Check for CDISC Controlled Terminology violations not already reported by profile
                existing_error_fields = {
                    violation.field
                    for violation in record_violations
                    if violation.severity.upper() == "ERROR"
                }
                cdisc_ct = getattr(self._normalizer, "cdisc_ct", CDISC_CONTROLLED_TERMINOLOGY)
                for field_name in expected_fields:
                    val = normalization_result.normalized_record.get(field_name)
                    if is_missing_value(val):
                        continue
                    if field_name not in existing_error_fields and field_name in cdisc_ct:
                        codelist = cdisc_ct[field_name]
                        if (
                            isinstance(val, str)
                            and val.strip()
                            and val.strip().upper() not in codelist
                        ):
                            record_violations.append(
                                ValidationViolation(
                                    field=field_name,
                                    message=(
                                        f"{field_name} value '{val}' does not match CDISC "
                                        f"Controlled Terminology."
                                    ),
                                    severity="ERROR",
                                )
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
