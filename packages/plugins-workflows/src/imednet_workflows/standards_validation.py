"""Workflow for validating data records against clinical data standards."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Literal

from pydantic import BaseModel, Field

from imednet.spi.models import (
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


def _matches_boolean_terminology(val: bool, terms: Collection[str]) -> bool:
    """Return True when a native boolean aligns with boolean tokens in a codelist."""
    expected_tokens = {"Y", "YES", "TRUE", "1"} if val else {"N", "NO", "FALSE", "0"}
    return bool(expected_tokens.intersection(terms))


class CategoricalNormalizer:
    """Normalize explicit terminology/date selections without inferring clinical meaning."""

    def __init__(
        self,
        cdisc_ct: dict[str, set[str]] | None = None,
        *,
        date_fields: Collection[str] = (),
        date_order: Literal["MDY", "DMY"] | None = None,
    ) -> None:
        """Snapshot caller-selected codelists and date fields.

        No unversioned vocabulary or guessed date-field selection is enabled by
        default. cdisc_ct retains its proposed API name; supplying it does not
        establish CDISC conformance. Study lookups take precedence over codelists.
        """
        if isinstance(date_fields, str):
            raise ValueError("date_fields must be a collection of field names")
        if date_order not in (None, "MDY", "DMY"):
            raise ValueError("date_order must be MDY, DMY or None")
        self.cdisc_ct = {field: set(terms) for field, terms in (cdisc_ct or {}).items()}
        self._canonical_terms: dict[str, dict[str, str]] = {}
        for field, terms in self.cdisc_ct.items():
            canonical: dict[str, str] = {}
            for term in sorted(terms):
                key = term.strip().upper()
                if not key or key in canonical:
                    raise ValueError("Terminology contains empty or case-colliding terms")
                canonical[key] = term.strip()
            self._canonical_terms[field] = canonical
        self._date_fields = frozenset(date_fields)
        self._date_order = date_order

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
            study_key: Explicit opt-in to deriving an empty USUBJID placeholder.

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

        subject_key = record.get("subjectKey", record.get("subject_key"))
        canonical_subject = subject_key.strip() if isinstance(subject_key, str) else subject_key
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

        # Derive only an explicitly requested empty identifier. Existing global
        # identity and caller-supplied study casing must never be rewritten.
        if (
            isinstance(study_key, str)
            and study_key.strip()
            and isinstance(canonical_subject, str)
            and canonical_subject
        ):
            for key in ("USUBJID", "usubjid"):
                if key in record and (record[key] is None or record[key] == ""):
                    normalized_record[key] = f"{study_key.strip()}-{canonical_subject}"

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
        if field_name in {
            "USUBJID",
            "usubjid",
            "subjectKey",
            "subject_key",
            "studyKey",
            "study_key",
        }:
            return raw_value, None
        lookup = normalized_lookups.get(field_name)
        if lookup and isinstance(raw_value, str):
            lookup_key = raw_value.strip().upper()
            if lookup_key in lookup:
                return lookup[lookup_key], None
            subject_prefix = (
                f"Subject {subject_key} " if isinstance(subject_key, str) and subject_key else ""
            )
            return raw_value, f"{subject_prefix}record {field_name} is unmapped"

        if field_name in self._canonical_terms and not is_missing_value(raw_value):
            if isinstance(raw_value, bool) and _matches_boolean_terminology(
                raw_value, self._canonical_terms[field_name]
            ):
                return raw_value, None
            canonical = (
                self._canonical_terms[field_name].get(raw_value.strip().upper())
                if isinstance(raw_value, str)
                else None
            )
            if canonical is not None:
                return canonical, None
            return raw_value, f"Field {field_name} is outside the configured terminology"

        if field_name in self._date_fields:
            return normalize_iso_date(raw_value, date_order=self._date_order), None

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
        accepted_terms = {
            field: {term.strip().upper() for term in terms}
            for field, terms in getattr(self._normalizer, "cdisc_ct", {}).items()
        }

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

                # Apply only caller-selected terminology, without duplicating profile errors.
                existing_error_fields = {
                    violation.field
                    for violation in record_violations
                    if violation.severity.upper() == "ERROR"
                }
                for field_name in expected_fields:
                    val = normalization_result.normalized_record.get(field_name)
                    if is_missing_value(val):
                        continue
                    is_accepted_bool = isinstance(val, bool) and _matches_boolean_terminology(
                        val, accepted_terms.get(field_name, set())
                    )
                    if (
                        field_name not in existing_error_fields
                        and field_name in accepted_terms
                        and not normalized_lookups.get(field_name)
                        and not is_accepted_bool
                        and (
                            not isinstance(val, str)
                            or val.strip().upper() not in accepted_terms[field_name]
                        )
                    ):
                        record_violations.append(
                            ValidationViolation(
                                field=field_name,
                                message=f"Field {field_name} is outside the configured terminology.",
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
