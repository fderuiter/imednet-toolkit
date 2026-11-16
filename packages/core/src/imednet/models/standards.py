"""Data standard profiles and validation models for clinical domains."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from imednet.utils.validators import is_missing_value


class ValidationViolation(BaseModel):
    """Represents a violation of a data standard constraint."""

    field: str
    message: str
    severity: str  # ERROR, WARNING


_CANONICAL_DOMAINS: dict[str, str] = {
    "DM": "DM",
    "DEMOGRAPHICS": "DM",
    "DEMOGRAPHIC": "DM",
    "VS": "VS",
    "VITALS": "VS",
    "VITAL SIGNS": "VS",
    "VITAL_SIGNS": "VS",
    "VITALSIGNS": "VS",
    "AE": "AE",
    "ADVERSE EVENTS": "AE",
    "ADVERSE_EVENTS": "AE",
    "ADVERSE EVENT": "AE",
    "PD": "PD",
    "PROTOCOL DEVIATIONS": "PD",
    "PROTOCOL_DEVIATIONS": "PD",
    "PROTOCOL DEVIATION": "PD",
    "DD": "DD",
    "DEVICE DEFICIENCIES": "DD",
    "DEVICE_DEFICIENCIES": "DD",
    "DEVICE DEFICIENCY": "DD",
}

_STANDARD_SEX_VALUES: list[str] = ["M", "F", "U", "UNDIFFERENTIATED"]
_STANDARD_VITAL_SIGNS_UNITS: list[str] = [
    "mmHg",
    "bpm",
    "beats/min",
    "breaths/min",
    "C",
    "F",
    "Cel",
    "kg",
    "lbs",
    "lb",
    "cm",
    "in",
    "m",
    "%",
]


def _canonical_domain(domain: str) -> str:
    """Normalize a clinical domain identifier to its standard 2-letter code."""
    normalized = domain.strip().upper().replace("-", "_")
    return _CANONICAL_DOMAINS.get(normalized, normalized)


class _DomainDict(dict[str, Any]):
    """Dictionary supporting canonical domain key resolution."""

    def __getitem__(self, key: str) -> Any:
        if key in self:
            return super().__getitem__(key)
        if isinstance(key, str):
            if "." in key:
                domain_part, field_part = key.split(".", 1)
                canonical_domain = _canonical_domain(domain_part)
                for k, v in self.items():
                    if "." in k:
                        kd, kf = k.split(".", 1)
                        if _canonical_domain(kd) == canonical_domain and kf == field_part:
                            return v
            else:
                canonical = _canonical_domain(key)
                for k, v in self.items():
                    if "." not in k and _canonical_domain(k) == canonical:
                        return v
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self else default  # noqa: SIM401

    def __contains__(self, key: object) -> bool:
        if super().__contains__(key):
            return True
        if isinstance(key, str):
            if "." in key:
                domain_part, field_part = key.split(".", 1)
                canonical_domain = _canonical_domain(domain_part)
                for k in self:
                    if "." in k:
                        kd, kf = k.split(".", 1)
                        if _canonical_domain(kd) == canonical_domain and kf == field_part:
                            return True
            else:
                canonical = _canonical_domain(key)
                for k in self:
                    if "." not in k and _canonical_domain(k) == canonical:
                        return True
        return False


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
        self.required_fields = _DomainDict(required_fields) if required_fields else _DomainDict()
        self.recommended_fields = (
            _DomainDict(recommended_fields) if recommended_fields else _DomainDict()
        )
        self.optional_fields = _DomainDict(optional_fields) if optional_fields else _DomainDict()
        self.value_constraints = (
            _DomainDict(value_constraints) if value_constraints else _DomainDict()
        )

    def expected_fields(self, domain: str) -> list[str]:
        """Return the list of expected (required + recommended) fields for a domain."""
        domain_key = _canonical_domain(domain)
        required = self.required_fields.get(domain_key, [])
        recommended = self.recommended_fields.get(domain_key, [])
        return [*required, *recommended]

    def validate(self, domain: str, data: dict[str, Any]) -> list[ValidationViolation]:
        """Validate a data dictionary against the profile for a specific domain.

        Args:
            domain: The domain name (e.g., 'AE', 'PD', 'DM', 'VS').
            data: The data dictionary to validate.

        Returns:
            A list of ValidationViolation objects.
        """
        domain_key = _canonical_domain(domain)
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
                if _canonical_domain(constraint_domain) != domain_key:
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
    """General clinical profile with basic AE, PD, DM, and VS requirements."""

    def __init__(self) -> None:
        """Initialize the general clinical profile."""
        super().__init__(
            profile_name="general",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
            },
            recommended_fields={
                "AE": ["aeDecod", "aeRelationship"],
                "PD": ["dvStatus"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
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
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
            },
            recommended_fields={
                "AE": ["aeActionTaken", "aeOutcome"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
            },
            optional_fields={"DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"]},
            value_constraints={
                "AE.aeSeverity": [1, 2, 3, 4, 5, "1", "2", "3", "4", "5"],
                "DM.sex": _STANDARD_SEX_VALUES,
                "VS.vsUnit": _STANDARD_VITAL_SIGNS_UNITS,
            },
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
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
            },
            recommended_fields={
                "DD": ["ddRelationship", "ddActionTaken"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
            },
            optional_fields={},
            value_constraints={
                "DM.sex": _STANDARD_SEX_VALUES,
                "VS.vsUnit": _STANDARD_VITAL_SIGNS_UNITS,
            },
        )

    def validate(self, domain: str, data: dict[str, Any]) -> list[ValidationViolation]:
        """Validate device-specific constraints for DD and other domains."""
        violations = super().validate(domain=domain, data=data)
        domain_key = _canonical_domain(domain)

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


__all__ = [
    "PROFILE_REGISTRY",
    "DeviceSafetyProfile",
    "DrugSafetyProfile",
    "GeneralClinicalProfile",
    "StandardsProfile",
    "StandardsProfileRegistry",
    "ValidationViolation",
]
