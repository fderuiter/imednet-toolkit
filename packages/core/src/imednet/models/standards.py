"""Data standard profiles and validation models for clinical domains."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from imednet.utils.validators import is_missing_value


class ValidationViolation(BaseModel):
    """Represents a violation of a data standard constraint."""

    field: str
    message: str
    severity: str  # ERROR, WARNING


class StandardsProfile:
    """A profile defining required, recommended, and optional fields for data domains."""

    def __init__(
        self,
        *,
        profile_name: str,
        required_fields: dict[str, list[str]] | None = None,
        recommended_fields: dict[str, list[str]] | None = None,
        optional_fields: dict[str, list[str]] | None = None,
        value_constraints: dict[str, list[Any]] | None = None,
    ) -> None:
        """Initialize a standards profile.

        Args:
            profile_name: Unique name for the profile.
            required_fields: Mapping of domain to required field names.
            recommended_fields: Mapping of domain to recommended field names.
            optional_fields: Mapping of domain to optional field names.
            value_constraints: Mapping of field or domain.field to allowed values.
        """
        self.profile_name = profile_name
        self.required_fields = required_fields or {}
        self.recommended_fields = recommended_fields or {}
        self.optional_fields = optional_fields or {}
        self.value_constraints = value_constraints or {}

    def expected_fields(self, domain: str) -> list[str]:
        """Return the list of expected (required + recommended) fields for a domain."""
        domain_key = domain.upper()
        return [
            *self.required_fields.get(domain_key, []),
            *self.recommended_fields.get(domain_key, []),
        ]

    def validate(self, domain: str, data: dict[str, Any]) -> list[ValidationViolation]:
        """Validate a data dictionary against the profile for a specific domain.

        Args:
            domain: The domain name (e.g., 'AE', 'PD').
            data: The data dictionary to validate.

        Returns:
            A list of ValidationViolation objects.
        """
        domain_key = domain.upper()
        violations: list[ValidationViolation] = []

        for field_name in self.required_fields.get(domain_key, []):
            if is_missing_value(data.get(field_name)):
                violations.append(
                    ValidationViolation(
                        field=field_name,
                        message=(
                            f"{field_name} is required for {domain_key} in {self.profile_name}."
                        ),
                        severity="ERROR",
                    )
                )

        for field_name in self.recommended_fields.get(domain_key, []):
            if is_missing_value(data.get(field_name)):
                violations.append(
                    ValidationViolation(
                        field=field_name,
                        message=(
                            f"{field_name} is recommended for {domain_key} in {self.profile_name}."
                        ),
                        severity="WARNING",
                    )
                )

        for constraint_key, allowed_values in self.value_constraints.items():
            if "." in constraint_key:
                constraint_domain, field_name = constraint_key.split(".", 1)
                if constraint_domain.upper() != domain_key:
                    continue
            else:
                field_name = constraint_key

            value = data.get(field_name)
            if is_missing_value(value):
                continue
            if value not in allowed_values:
                violations.append(
                    ValidationViolation(
                        field=field_name,
                        message=f"{field_name} must be one of {allowed_values}.",
                        severity="ERROR",
                    )
                )

        return violations


class GeneralClinicalProfile(StandardsProfile):
    """General clinical profile with basic AE and PD requirements."""

    def __init__(self) -> None:
        """Initialize the general clinical profile."""
        super().__init__(
            profile_name="general",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
            },
            recommended_fields={
                "AE": ["aeDecod", "aeRelationship"],
                "PD": ["dvStatus"],
            },
            optional_fields={
                "DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"],
            },
        )


class DrugSafetyProfile(StandardsProfile):
    """Drug safety profile with stricter requirements for AE data."""

    def __init__(self) -> None:
        """Initialize the drug safety profile."""
        super().__init__(
            profile_name="drug",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity", "aeDecod", "aeRelationship"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
            },
            recommended_fields={"AE": ["aeActionTaken", "aeOutcome"]},
            optional_fields={"DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"]},
            value_constraints={"AE.aeSeverity": [1, 2, 3, 4, 5, "1", "2", "3", "4", "5"]},
        )


class DeviceSafetyProfile(StandardsProfile):
    """Device safety profile including device deficiency (DD) requirements."""

    def __init__(self) -> None:
        """Initialize the device safety profile."""
        super().__init__(
            profile_name="device",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"],
            },
            recommended_fields={"DD": ["ddRelationship", "ddActionTaken"]},
            optional_fields={},
        )

    def validate(self, domain: str, data: dict[str, Any]) -> list[ValidationViolation]:
        """Validate device-specific constraints for DD and other domains."""
        violations = super().validate(domain=domain, data=data)
        domain_key = domain.upper()

        if domain_key != "DD":
            return violations

        deficiency_occurred = data.get("ddOccurred")
        if deficiency_occurred is True and all(
            is_missing_value(data.get(field_name))
            for field_name in ("ddTerm", "ddCategory", "ddDate")
        ):
            violations.append(
                ValidationViolation(
                    field="ddOccurred",
                    message="DD record details are required when ddOccurred is true.",
                    severity="ERROR",
                )
            )

        dd_serious = data.get("ddSerious")
        if not is_missing_value(dd_serious) and not isinstance(dd_serious, bool):
            violations.append(
                ValidationViolation(
                    field="ddSerious",
                    message="ddSerious must be a boolean value for device studies.",
                    severity="ERROR",
                )
            )

        return violations


class StandardsProfileRegistry:
    """Registry for managing and retrieving standards profiles."""

    def __init__(self) -> None:
        """Initialize an empty profile registry."""
        self._profiles: dict[str, StandardsProfile] = {}

    def register(self, profile: StandardsProfile) -> None:
        """Register a new standards profile."""
        self._profiles[profile.profile_name] = profile

    def get(self, profile_name: str) -> StandardsProfile:
        """Retrieve a profile by its name."""
        return self._profiles[profile_name]

    def list_profiles(self) -> list[str]:
        """List all registered profile names."""
        return sorted(self._profiles.keys())


PROFILE_REGISTRY = StandardsProfileRegistry()
PROFILE_REGISTRY.register(GeneralClinicalProfile())
PROFILE_REGISTRY.register(DrugSafetyProfile())
PROFILE_REGISTRY.register(DeviceSafetyProfile())


CDISC_CONTROLLED_TERMINOLOGY: dict[str, set[str]] = {
    # Severity / Intensity (C66769)
    "aeSeverity": {"MILD", "MODERATE", "SEVERE", "1", "2", "3", "4", "5"},
    "AESEV": {"MILD", "MODERATE", "SEVERE", "1", "2", "3", "4", "5"},
    "dvSeverity": {"MILD", "MODERATE", "SEVERE", "MAJOR", "MINOR", "CRITICAL"},
    "DVSEV": {"MILD", "MODERATE", "SEVERE", "MAJOR", "MINOR", "CRITICAL"},
    "pdSeverity": {"MILD", "MODERATE", "SEVERE", "MAJOR", "MINOR", "CRITICAL"},
    # Causality / Relationship (C66768)
    "aeRelationship": {
        "RELATED",
        "NOT RELATED",
        "POSSIBLE",
        "PROBABLE",
        "DEFINITE",
        "REMOTE",
        "UNRELATED",
        "POSSIBLY RELATED",
        "PROBABLY RELATED",
        "DEFINITELY RELATED",
        "UNLIKELY RELATED",
        "CONDITIONAL",
        "NOT ASSESSABLE",
        "YES",
        "NO",
    },
    "AEREL": {
        "RELATED",
        "NOT RELATED",
        "POSSIBLE",
        "PROBABLE",
        "DEFINITE",
        "REMOTE",
        "UNRELATED",
        "POSSIBLY RELATED",
        "PROBABLY RELATED",
        "DEFINITELY RELATED",
        "UNLIKELY RELATED",
        "CONDITIONAL",
        "NOT ASSESSABLE",
        "YES",
        "NO",
    },
    "ddRelationship": {"RELATED", "NOT RELATED", "POSSIBLE", "PROBABLE", "REMOTE", "YES", "NO"},
    "DDREL": {"RELATED", "NOT RELATED", "POSSIBLE", "PROBABLE", "REMOTE", "YES", "NO"},
    # Action Taken (C66767)
    "aeActionTaken": {
        "NONE",
        "DOSE REDUCED",
        "DOSE INCREASED",
        "DOSE NOT CHANGED",
        "DRUG INTERRUPTED",
        "DRUG WITHDRAWN",
        "NOT APPLICABLE",
        "UNKNOWN",
    },
    "AEACN": {
        "NONE",
        "DOSE REDUCED",
        "DOSE INCREASED",
        "DOSE NOT CHANGED",
        "DRUG INTERRUPTED",
        "DRUG WITHDRAWN",
        "NOT APPLICABLE",
        "UNKNOWN",
    },
    "ddActionTaken": {
        "REPLACED",
        "REPAIRED",
        "REMOVED",
        "NONE",
        "OTHER",
        "NOT APPLICABLE",
        "UNKNOWN",
    },
    "DDACN": {
        "REPLACED",
        "REPAIRED",
        "REMOVED",
        "NONE",
        "OTHER",
        "NOT APPLICABLE",
        "UNKNOWN",
    },
    # Outcome (C66768)
    "aeOutcome": {
        "RECOVERED/RESOLVED",
        "RECOVERED",
        "RESOLVED",
        "RECOVERING/RESOLVING",
        "RECOVERING",
        "NOT RECOVERED/NOT RESOLVED",
        "NOT RECOVERED",
        "RECOVERED/RESOLVED WITH SEQUELAE",
        "FATAL",
        "UNKNOWN",
    },
    "AEOUT": {
        "RECOVERED/RESOLVED",
        "RECOVERED",
        "RESOLVED",
        "RECOVERING/RESOLVING",
        "RECOVERING",
        "NOT RECOVERED/NOT RESOLVED",
        "NOT RECOVERED",
        "RECOVERED/RESOLVED WITH SEQUELAE",
        "FATAL",
        "UNKNOWN",
    },
    # Seriousness
    "aeSerious": {"Y", "N", "YES", "NO", "TRUE", "FALSE"},
    "AESER": {"Y", "N", "YES", "NO", "TRUE", "FALSE"},
    "ddSerious": {"Y", "N", "YES", "NO", "TRUE", "FALSE"},
    "DDSER": {"Y", "N", "YES", "NO", "TRUE", "FALSE"},
    # Deviation / Deficiency Categories & Status
    "dvCategory": {
        "INCLUSION/EXCLUSION",
        "INFORMED CONSENT",
        "STUDY PROCEDURE",
        "CONCOMITANT MEDICATION",
        "INVESTIGATIONAL PRODUCT",
        "SAFETY REPORTING",
        "VISIT SCHEDULE",
        "OTHER",
        "ELIGIBILITY",
        "CONSENT",
        "PROCEDURE",
    },
    "DVCAT": {
        "INCLUSION/EXCLUSION",
        "INFORMED CONSENT",
        "STUDY PROCEDURE",
        "CONCOMITANT MEDICATION",
        "INVESTIGATIONAL PRODUCT",
        "SAFETY REPORTING",
        "VISIT SCHEDULE",
        "OTHER",
        "ELIGIBILITY",
        "CONSENT",
        "PROCEDURE",
    },
    "pdCategory": {
        "INCLUSION/EXCLUSION",
        "INFORMED CONSENT",
        "STUDY PROCEDURE",
        "CONCOMITANT MEDICATION",
        "INVESTIGATIONAL PRODUCT",
        "SAFETY REPORTING",
        "VISIT SCHEDULE",
        "OTHER",
        "ELIGIBILITY",
        "CONSENT",
        "PROCEDURE",
    },
    "dvStatus": {
        "OPEN",
        "RESOLVED",
        "CLOSED",
        "COMPLETED",
        "PENDING",
        "UNREVIEWED",
        "APPROVED",
        "ESCALATED",
    },
    "DVSTATUS": {
        "OPEN",
        "RESOLVED",
        "CLOSED",
        "COMPLETED",
        "PENDING",
        "UNREVIEWED",
        "APPROVED",
        "ESCALATED",
    },
    "pdStatus": {
        "OPEN",
        "RESOLVED",
        "CLOSED",
        "COMPLETED",
        "PENDING",
        "UNREVIEWED",
        "APPROVED",
        "ESCALATED",
    },
    "ddCategory": {
        "HARDWARE",
        "SOFTWARE",
        "PACKAGING",
        "LABELING",
        "MECHANICAL",
        "ELECTRICAL",
        "BATTERY",
        "OTHER",
    },
    "DDCAT": {
        "HARDWARE",
        "SOFTWARE",
        "PACKAGING",
        "LABELING",
        "MECHANICAL",
        "ELECTRICAL",
        "BATTERY",
        "OTHER",
    },
}

UNIVERSAL_CDASH_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "studyid",
        "study_id",
        "domain",
        "usubjid",
        "subjid",
        "siteid",
        "site_id",
        "visit",
        "visitnum",
        "visit_num",
        "visitid",
        "visit_id",
        "visitdy",
        "epoch",
        "subjectkey",
        "subject_key",
        "recordid",
        "record_id",
    }
)

DOMAIN_PREFIXES: dict[str, set[str]] = {
    "AE": {"ae"},
    "PD": {"dv", "pd"},
    "DV": {"dv", "pd"},
    "DD": {"dd"},
    "DM": {"dm"},
    "CM": {"cm"},
    "EX": {"ex"},
    "LB": {"lb"},
    "MH": {"mh"},
    "QS": {"qs"},
    "VS": {"vs"},
    "PE": {"pe"},
    "DA": {"da"},
    "DS": {"ds"},
    "IE": {"ie"},
    "SU": {"su"},
}

DM_ALLOWED_VARIABLES: frozenset[str] = frozenset(
    {
        "age",
        "ageu",
        "sex",
        "race",
        "ethnic",
        "arm",
        "armcd",
        "actarm",
        "actarmcd",
        "country",
        "brthdtc",
        "dthdtc",
        "dthfl",
        "dmdtc",
    }
)


def validate_cdash_variable(target_field: str, domain: str) -> tuple[bool, str | None]:
    """Validate a target variable against CDASH naming conventions and domain prefixes.

    Args:
        target_field: The field name to validate.
        domain: The clinical domain code (e.g. 'AE', 'PD', 'DD').

    Returns:
        A tuple of (is_valid, error_message_or_none).
    """
    if not target_field or not isinstance(target_field, str):
        return False, "Target field must be a non-empty string"
    if not domain or not isinstance(domain, str):
        return False, "Domain must be a non-empty string"

    clean_target = target_field.strip()
    clean_domain = domain.strip().upper()
    lower_target = clean_target.lower()

    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{1,31}$", clean_target):
        return False, f"Target field '{clean_target}' is not a valid CDASH variable name"

    if lower_target in UNIVERSAL_CDASH_IDENTIFIERS:
        return True, None

    if clean_domain == "DM" and lower_target in DM_ALLOWED_VARIABLES:
        return True, None

    allowed_prefixes = DOMAIN_PREFIXES.get(clean_domain, {clean_domain.lower()})
    has_prefix = any(lower_target.startswith(prefix) for prefix in allowed_prefixes)
    if not has_prefix:
        expected = ", ".join(f"'{p}'" for p in sorted(allowed_prefixes))
        return False, (
            f"Target variable '{clean_target}' does not match CDASH domain prefix for {clean_domain} "
            f"(expected prefix: {expected})"
        )

    return True, None


def is_valid_cdash_variable(target_field: str, domain: str) -> bool:
    """Check whether a target variable conforms to CDASH conventions for a domain."""
    valid, _ = validate_cdash_variable(target_field, domain)
    return valid


__all__ = [
    "CDISC_CONTROLLED_TERMINOLOGY",
    "DM_ALLOWED_VARIABLES",
    "DOMAIN_PREFIXES",
    "PROFILE_REGISTRY",
    "UNIVERSAL_CDASH_IDENTIFIERS",
    "DeviceSafetyProfile",
    "DrugSafetyProfile",
    "GeneralClinicalProfile",
    "StandardsProfile",
    "StandardsProfileRegistry",
    "ValidationViolation",
    "is_valid_cdash_variable",
    "validate_cdash_variable",
]
