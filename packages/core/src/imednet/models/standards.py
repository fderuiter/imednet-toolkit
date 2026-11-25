"""Data standard profiles and validation models for clinical domains."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from imednet.core.context import is_strict_mode
from imednet.models.base import ImednetBaseModel
from imednet.models.sdtm import (
    _SDTM_RECORDS_SORT,
    _UNSET_PIPELINE,
    SDTM_DOMAIN_SPECS,
    VALID_NAMING_CONVENTIONS,
    _collapse_alnum_key,
    _extract_stream_usubjid_and_id,
    _iter_sorted_sdtm_frames,
    _SdtmNormalizer,
    build_form_sdtm_domain_map,
    extract_record_form_ref,
    filter_forms_by_sdtm_domain,
    format_iso8601_dtc,
    format_variable_name,
    get_sdtm_domain_variables,
    iter_subject_ordered_sdtm_records,
    merge_form_ids_into_record_filter,
    normalize_naming_convention,
    record_matches_sdtm_domain,
    resolve_form_sdtm_domain,
    resolve_sdtm_domain,
    transform_records_to_sdtm,
    transform_records_to_sdtm_rows,
)
from imednet.utils.dates import normalize_iso_date
from imednet.utils.validators import (
    is_boolean_token,
    is_missing_value,
    parse_bool,
)


class ValidationViolation(BaseModel):
    """Represents a violation of a data standard constraint."""

    field: str
    message: str
    severity: str  # ERROR, WARNING


_CANONICAL_DOMAINS: dict[str, str] = {
    "DM": "DM",
    "DEMOGRAPHICS": "DM",
    "DEMOGRAPHIC": "DM",
    "DEMO": "DM",
    "VS": "VS",
    "VITALS": "VS",
    "VITAL SIGNS": "VS",
    "VITAL_SIGNS": "VS",
    "VITALSIGNS": "VS",
    "AE": "AE",
    "ADVERSE EVENTS": "AE",
    "ADVERSE_EVENTS": "AE",
    "ADVERSE EVENT": "AE",
    "ADVERSE_EVENT": "AE",
    "ADVERSEEVENTS": "AE",
    "ADVERSEEVENT": "AE",
    "PD": "PD",
    "DV": "PD",
    "PROTOCOL DEVIATIONS": "PD",
    "PROTOCOL_DEVIATIONS": "PD",
    "PROTOCOL DEVIATION": "PD",
    "PROTOCOL_DEVIATION": "PD",
    "PROTOCOLDEVIATIONS": "PD",
    "PROTOCOLDEVIATION": "PD",
    "DD": "DD",
    "DEVICE DEFICIENCIES": "DD",
    "DEVICE_DEFICIENCIES": "DD",
    "DEVICE DEFICIENCY": "DD",
    "DEVICE_DEFICIENCY": "DD",
    "DEVICEDEFICIENCIES": "DD",
    "DEVICEDEFICIENCY": "DD",
    "CM": "CM",
    "CONMEDS": "CM",
    "CONCOMITANT MEDICATIONS": "CM",
    "CONCOMITANT_MEDICATIONS": "CM",
    "CONCOMITANT MEDICATION": "CM",
    "CONCOMITANT_MEDICATION": "CM",
    "CONCOMITANTMEDICATIONS": "CM",
    "LB": "LB",
    "LAB": "LB",
    "LABS": "LB",
    "LABORATORY": "LB",
    "LABORATORY RESULTS": "LB",
    "LABORATORY_RESULTS": "LB",
    "EX": "EX",
    "EXPOSURE": "EX",
    "EXPOSURES": "EX",
    "MH": "MH",
    "MEDICAL HISTORY": "MH",
    "MEDICAL_HISTORY": "MH",
    "MEDICALHISTORY": "MH",
    "MEDHIST": "MH",
}

_STANDARD_AE_SEVERITY_VALUES: list[Any] = [
    1,
    2,
    3,
    4,
    5,
    "1",
    "2",
    "3",
    "4",
    "5",
    "MILD",
    "MODERATE",
    "SEVERE",
]
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


def _build_standard_field_aliases() -> dict[tuple[str, str], tuple[str, ...]]:
    """Precompute equivalent SDTM/CDASH variable aliases per canonical domain."""
    alias_lookup: dict[tuple[str, str], tuple[str, ...]] = {}
    subj_aliases = ("subjectKey", "subject_key", "USUBJID", "usubjid")
    for spec_dom, spec in SDTM_DOMAIN_SPECS.items():
        dom_codes = (spec_dom, "PD") if spec_dom == "DV" else (spec_dom,)
        for d_code in dom_codes:
            for s_alias in subj_aliases:
                alias_lookup[(d_code, s_alias)] = subj_aliases
            for sdtm_var, aliases in spec["aliases"].items():
                group = tuple(dict.fromkeys([sdtm_var, *aliases, sdtm_var.lower()]))
                for member in group:
                    alias_lookup[(d_code, member)] = group
    return alias_lookup


_STANDARD_FIELD_ALIASES: dict[tuple[str, str], tuple[str, ...]] = _build_standard_field_aliases()


def _get_domain_field_value(domain_key: str, field_name: str, data: Mapping[str, Any]) -> Any:
    """Retrieve *field_name* from *data*, consulting equivalent SDTM/CDASH aliases when needed."""
    direct = data.get(field_name)
    if not is_missing_value(direct):
        return direct
    candidates = _STANDARD_FIELD_ALIASES.get((domain_key, field_name))
    if candidates:
        for cand in candidates:
            if cand != field_name and cand in data:
                cand_val = data.get(cand)
                if not is_missing_value(cand_val):
                    return cand_val
        lower_map = {str(k).lower(): v for k, v in data.items()}
        for cand in candidates:
            cand_lower = cand.lower()
            if cand_lower in lower_map and not is_missing_value(lower_map[cand_lower]):
                return lower_map[cand_lower]
    elif field_name == "subjectKey":
        for subj_k in ("subject_key", "USUBJID", "usubjid"):
            if subj_k in data and not is_missing_value(data.get(subj_k)):
                return data.get(subj_k)
    return direct


class _DomainDict(dict[str, Any]):
    """Dictionary supporting canonical domain key resolution."""

    def __getitem__(self, key: str) -> Any:
        if super().__contains__(key):
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
            if is_missing_value(_get_domain_field_value(domain_key, field_name, data)):
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
            if is_missing_value(_get_domain_field_value(domain_key, field_name, data)):
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

            value = _get_domain_field_value(domain_key, field_name, data)
            if is_missing_value(value):
                continue
            is_allowed = (
                any(isinstance(av, bool) and av is value for av in allowed_values)
                if isinstance(value, bool)
                else (
                    value in allowed_values
                    or (
                        field_name in {"aeSeverity", "AESEV"}
                        and isinstance(value, str)
                        and value.strip().upper() in allowed_values
                    )
                )
            )
            if not is_allowed:
                violations.append(
                    ValidationViolation(
                        field=field_name,
                        message=f"{field_name} must be one of {allowed_values}.",
                        severity="ERROR",
                    )
                )

        return violations


class GeneralClinicalProfile(StandardsProfile):
    """General clinical profile with basic AE, PD, DM, VS, LB, CM, EX, and MH requirements."""

    def __init__(self) -> None:
        """Initialize the general clinical profile."""
        super().__init__(
            profile_name="general",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "CM": ["subjectKey", "cmTerm", "cmStartDate"],
                "EX": ["subjectKey", "exTrt", "exStartDate"],
                "MH": ["subjectKey", "mhTerm", "mhStartDate"],
            },
            recommended_fields={
                "AE": ["aeDecod", "aeRelationship"],
                "PD": ["dvStatus"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "LB": ["lbTestCd", "lbUnit"],
                "CM": ["cmDose", "cmDoseUnit", "cmRoute"],
                "EX": ["exDose", "exDoseUnit", "exRoute"],
                "MH": ["mhCategory", "mhDecod"],
            },
            optional_fields={
                "DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"],
            },
        )


class DrugSafetyProfile(StandardsProfile):
    """Drug safety profile with stricter requirements for AE and safety domains."""

    def __init__(self) -> None:
        """Initialize the drug safety profile."""
        super().__init__(
            profile_name="drug",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity", "aeDecod", "aeRelationship"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "CM": ["subjectKey", "cmTerm", "cmStartDate"],
                "EX": ["subjectKey", "exTrt", "exDose", "exDoseUnit", "exStartDate"],
                "MH": ["subjectKey", "mhTerm", "mhStartDate"],
            },
            recommended_fields={
                "AE": ["aeActionTaken", "aeOutcome"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "LB": ["lbTestCd", "lbUnit"],
                "CM": ["cmDecod", "cmDose", "cmDoseUnit", "cmRoute", "cmIndication"],
                "EX": ["exRoute"],
                "MH": ["mhCategory", "mhDecod"],
            },
            optional_fields={"DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"]},
            value_constraints={
                "AE.aeSeverity": _STANDARD_AE_SEVERITY_VALUES,
                "DM.sex": _STANDARD_SEX_VALUES,
                "VS.vsUnit": _STANDARD_VITAL_SIGNS_UNITS,
            },
        )


class BiologicSafetyProfile(StandardsProfile):
    """Biologic safety profile for biologic and biosimilar clinical trials."""

    def __init__(self) -> None:
        """Initialize the biologic safety profile."""
        super().__init__(
            profile_name="biologic",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity", "aeDecod", "aeRelationship"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "CM": ["subjectKey", "cmTerm", "cmStartDate"],
                "EX": ["subjectKey", "exTrt", "exDose", "exDoseUnit", "exStartDate"],
                "MH": ["subjectKey", "mhTerm", "mhStartDate"],
            },
            recommended_fields={
                "AE": ["aeActionTaken", "aeOutcome", "aeSerious"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "LB": ["lbTestCd", "lbUnit"],
                "CM": ["cmDecod", "cmDose", "cmDoseUnit", "cmRoute", "cmIndication"],
                "EX": ["exRoute"],
                "MH": ["mhCategory", "mhDecod"],
            },
            optional_fields={"DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"]},
            value_constraints={
                "AE.aeSeverity": _STANDARD_AE_SEVERITY_VALUES,
                "DM.sex": _STANDARD_SEX_VALUES,
                "VS.vsUnit": _STANDARD_VITAL_SIGNS_UNITS,
            },
        )


class VaccineSafetyProfile(StandardsProfile):
    """Vaccine safety profile for immunization and reactogenicity clinical trials."""

    def __init__(self) -> None:
        """Initialize the vaccine safety profile."""
        super().__init__(
            profile_name="vaccine",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity", "aeDecod", "aeRelationship"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "CM": ["subjectKey", "cmTerm", "cmStartDate"],
                "EX": ["subjectKey", "exTrt", "exStartDate"],
                "MH": ["subjectKey", "mhTerm", "mhStartDate"],
            },
            recommended_fields={
                "AE": ["aeActionTaken", "aeOutcome", "aeSerious"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "LB": ["lbTestCd", "lbUnit"],
                "CM": ["cmDose", "cmDoseUnit", "cmRoute"],
                "EX": ["exDose", "exDoseUnit", "exRoute"],
                "MH": ["mhCategory", "mhDecod"],
            },
            optional_fields={"DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"]},
            value_constraints={
                "AE.aeSeverity": _STANDARD_AE_SEVERITY_VALUES,
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
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "CM": ["subjectKey", "cmTerm", "cmStartDate"],
                "EX": ["subjectKey", "exTrt", "exStartDate"],
                "MH": ["subjectKey", "mhTerm", "mhStartDate"],
            },
            recommended_fields={
                "DD": ["ddRelationship", "ddActionTaken"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "LB": ["lbTestCd", "lbUnit"],
                "CM": ["cmDose", "cmDoseUnit", "cmRoute"],
                "EX": ["exDose", "exDoseUnit", "exRoute"],
                "MH": ["mhCategory", "mhDecod"],
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

        deficiency_occurred = _get_domain_field_value(domain_key, "ddOccurred", data)
        if deficiency_occurred is True and all(
            is_missing_value(_get_domain_field_value(domain_key, field_name, data))
            for field_name in ("ddTerm", "ddCategory", "ddDate")
        ):
            violations.append(
                ValidationViolation(
                    field="ddOccurred",
                    message="DD record details are required when ddOccurred is true.",
                    severity="ERROR",
                )
            )

        dd_serious = _get_domain_field_value(domain_key, "ddSerious", data)
        is_valid_dd_serious = isinstance(dd_serious, bool) or (
            isinstance(dd_serious, str) and dd_serious.strip().upper() in {"Y", "N"}
        )
        if not is_missing_value(dd_serious) and not is_valid_dd_serious:
            violations.append(
                ValidationViolation(
                    field="ddSerious",
                    message=(
                        "ddSerious must be a boolean or CDISC NY ('Y'/'N') value for device studies."
                    ),
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
PROFILE_REGISTRY.register(BiologicSafetyProfile())
PROFILE_REGISTRY.register(VaccineSafetyProfile())
PROFILE_REGISTRY.register(DeviceSafetyProfile())


# Project reporting vocabulary, not a versioned CDISC CT release. Callers must
# explicitly select it or supply their study-approved terminology snapshot.
NORMALIZATION_TERMINOLOGY: dict[str, set[str]] = {
    # Reporting severity/grade labels
    "aeSeverity": {"MILD", "MODERATE", "SEVERE", "1", "2", "3", "4", "5"},
    "AESEV": {"MILD", "MODERATE", "SEVERE", "1", "2", "3", "4", "5"},
    "dvSeverity": {"MILD", "MODERATE", "SEVERE", "MAJOR", "MINOR", "CRITICAL"},
    "DVSEV": {"MILD", "MODERATE", "SEVERE", "MAJOR", "MINOR", "CRITICAL"},
    "pdSeverity": {"MILD", "MODERATE", "SEVERE", "MAJOR", "MINOR", "CRITICAL"},
    # Reporting relationship labels
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
    # Reporting action labels
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
    # Reporting outcome labels
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

# Compatibility name from the initial proposal; this is not an official CT snapshot.
CDISC_CONTROLLED_TERMINOLOGY = NORMALIZATION_TERMINOLOGY

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
        "brthdat",
        "brthdtc",
        "birthdate",
        "ethnicity",
        "dthdtc",
        "dthfl",
        "dmdtc",
    }
)


def validate_cdash_variable(target_field: str, domain: str) -> tuple[bool, str | None]:
    """Check the local reporting-name syntax and configured domain-prefix policy.

    This heuristic accepts project aliases; it does not establish membership in
    an official CDASH/SDTM version or certify standards conformance.

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
    clean_domain = _canonical_domain(domain)
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
    """Check whether a target passes the local CDASH-style naming heuristic."""
    valid, _ = validate_cdash_variable(target_field, domain)
    return valid


def resolve_standards_profile(profile: str | StandardsProfile | None) -> StandardsProfile:
    """Resolve a profile name ('general', 'drug', 'biologic', 'vaccine', 'device') or instance."""
    if isinstance(profile, StandardsProfile):
        return profile
    if profile is None:
        return PROFILE_REGISTRY.get("general")
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError(
            "standards_profile must be a StandardsProfile instance or registered profile name"
        )
    key = profile.strip().lower()
    aliases = {
        "drug_safety": "drug",
        "drugsafety": "drug",
        "biologic_safety": "biologic",
        "biologicsafety": "biologic",
        "vaccine_safety": "vaccine",
        "vaccinesafety": "vaccine",
        "device_safety": "device",
        "devicesafety": "device",
        "general_clinical": "general",
        "generalclinical": "general",
    }
    return PROFILE_REGISTRY.get(aliases.get(key, key))


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
    """Normalize explicit terminology/date selections without inferring clinical meaning."""

    def __init__(
        self,
        cdisc_ct: dict[str, set[str]] | None = None,
        *,
        date_fields: Collection[str] = (),
        boolean_fields: Collection[str] = (),
        date_order: Literal["MDY", "DMY"] | None = None,
    ) -> None:
        """Snapshot caller-selected codelists, date fields, and boolean fields.

        No unversioned vocabulary or guessed date/boolean field selection is enabled by
        default. cdisc_ct retains its proposed API name; supplying it does not
        establish CDISC conformance. Study lookups take precedence over codelists.
        """
        if isinstance(date_fields, str):
            raise ValueError("date_fields must be a collection of field names")
        if isinstance(boolean_fields, str):
            raise ValueError("boolean_fields must be a collection of field names")
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
        self._boolean_fields = frozenset(boolean_fields)
        self._date_order = date_order

    def normalize_record(
        self,
        record: dict[str, Any],
        *,
        terminology_lookups: dict[str, dict[str, str]] | None = None,
        normalized_lookups: dict[str, dict[str, str]] | None = None,
        study_key: str | None = None,
    ) -> NormalizationResult:
        """Normalize a single data record."""
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

        if "subjectKey" in normalized_record and isinstance(normalized_record["subjectKey"], str):
            normalized_record["subjectKey"] = normalized_record["subjectKey"].strip()
        if "subject_key" in normalized_record and isinstance(normalized_record["subject_key"], str):
            normalized_record["subject_key"] = normalized_record["subject_key"].strip()

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
            canonical_map = self._canonical_terms[field_name]
            if isinstance(raw_value, bool):
                bool_key = "TRUE" if raw_value else "FALSE"
                ny_key = "Y" if raw_value else "N"
                if bool_key in canonical_map or ny_key in canonical_map:
                    return raw_value, None
            canonical = (
                canonical_map.get(raw_value.strip().upper()) if isinstance(raw_value, str) else None
            )
            if canonical is not None:
                return canonical, None
            return raw_value, f"Field {field_name} is outside the configured terminology"

        if field_name in self._date_fields:
            return normalize_iso_date(raw_value, date_order=self._date_order), None

        if field_name in self._boolean_fields and isinstance(raw_value, str):
            stripped_value = raw_value.strip()
            if stripped_value and is_boolean_token(stripped_value):
                return parse_bool(stripped_value), None

        return raw_value, None


def _coerce_float_or_default(v: Any) -> float:
    """Coerce numeric inputs to float, defaulting to 0.0 on drift unless strict mode is active."""
    if is_missing_value(v):
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        if is_strict_mode():
            raise
        return 0.0


class StandardsReadinessReport(ImednetBaseModel):
    """Comprehensive report on the readiness of data records against clinical standards."""

    score: float
    successfully_validated_fields: int
    total_expected_fields: int
    warnings: list[str] = Field(default_factory=list)
    violations: list[ValidationViolation] = Field(default_factory=list)
    profile: str = "general"
    checked_records: int = 0
    domain_scores: dict[str, float] = Field(default_factory=dict)

    @field_validator("score", mode="before")
    @classmethod
    def _normalize_score(cls, v: Any) -> float:
        return _coerce_float_or_default(v)

    @field_validator("domain_scores", mode="before")
    @classmethod
    def _normalize_domain_scores(cls, v: Any) -> dict[str, float]:
        if not isinstance(v, dict):
            return {}
        return {str(k): _coerce_float_or_default(val) for k, val in v.items()}

    @property
    def is_ready(self) -> bool:
        """Return True when score reaches 100, all domains are ready, and no ERROR violations exist."""
        return (
            self.score >= 100.0
            and all(s >= 100.0 for s in self.domain_scores.values())
            and not any(v.severity.upper() == "ERROR" for v in self.violations)
        )

    @property
    def checked_fields(self) -> int:
        """Alias for ``total_expected_fields`` used by CLI and export readiness reports."""
        return self.total_expected_fields


class StandardsReadinessValidator:
    """Validates and scores data records against a clinical standards profile."""

    def __init__(
        self, profile: StandardsProfile | str, normalizer: CategoricalNormalizer | None = None
    ) -> None:
        """Initialize the validator."""
        self._profile = resolve_standards_profile(profile)
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
        """Validate and score multiple domains of records."""
        lookups = terminology_lookups or {}
        warnings: list[str] = []
        violations: list[ValidationViolation] = []
        successfully_validated_fields = 0
        total_expected_fields = 0
        checked_records = 0
        domain_scores: dict[str, float] = {}
        normalized_lookups = self._normalize_lookups(lookups)
        accepted_terms = {
            field: {term.strip().upper() for term in terms}
            for field, terms in getattr(self._normalizer, "cdisc_ct", {}).items()
        }

        has_unconfigured_domain = False
        for domain, records in records_by_domain.items():
            effective_domain = (
                "PD"
                if domain.strip().upper() == "DV" and not self._profile.expected_fields("DV")
                else domain
            )
            domain_key = _canonical_domain(effective_domain)
            expected_fields = self._profile.expected_fields(effective_domain)
            if not expected_fields:
                has_unconfigured_domain = True
                checked_records += len(records)
                domain_scores[domain] = 0.0
                for record in records:
                    normalization_result = self._normalizer.normalize_record(
                        record, normalized_lookups=normalized_lookups
                    )
                    warnings.extend(normalization_result.warnings)
                unconfigured_msg = (
                    f"Domain '{domain}' has no configured standards rules in "
                    f"profile '{self._profile.profile_name}'."
                )
                warnings.append(f"{unconfigured_msg} Readiness score is 0.0.")
                violations.append(
                    ValidationViolation(
                        field=domain,
                        message=unconfigured_msg,
                        severity="ERROR",
                    )
                )
                continue
            if not records:
                domain_scores[domain] = 0.0
                violations.append(
                    ValidationViolation(
                        field=domain,
                        message=f"No records found for domain '{domain}'.",
                        severity="ERROR",
                    )
                )
                continue
            dom_valid_fields = 0
            dom_expected_fields = 0
            for record in records:
                checked_records += 1
                normalization_result = self._normalizer.normalize_record(
                    record, normalized_lookups=normalized_lookups
                )
                warnings.extend(normalization_result.warnings)

                record_violations = list(
                    self._profile.validate(
                        domain=effective_domain, data=normalization_result.normalized_record
                    )
                )

                existing_error_fields = {
                    violation.field
                    for violation in record_violations
                    if violation.severity.upper() == "ERROR"
                }
                for field_name in expected_fields:
                    val = _get_domain_field_value(
                        domain_key,
                        field_name,
                        normalization_result.normalized_record,
                    )
                    if is_missing_value(val):
                        continue
                    if (
                        field_name not in existing_error_fields
                        and field_name in accepted_terms
                        and not normalized_lookups.get(field_name)
                    ):
                        term_set = accepted_terms[field_name]
                        in_terminology = (
                            (
                                ("TRUE" if val else "FALSE") in term_set
                                or ("Y" if val else "N") in term_set
                            )
                            if isinstance(val, bool)
                            else (isinstance(val, str) and val.strip().upper() in term_set)
                        )
                        if not in_terminology:
                            record_violations.append(
                                ValidationViolation(
                                    field=field_name,
                                    message=(
                                        f"Field {field_name} is outside the configured terminology."
                                    ),
                                    severity="ERROR",
                                )
                            )

                violations.extend(record_violations)

                total_expected_fields += len(expected_fields)
                dom_expected_fields += len(expected_fields)
                error_fields = {
                    violation.field
                    for violation in record_violations
                    if violation.severity.upper() == "ERROR"
                }
                for field_name in expected_fields:
                    value = _get_domain_field_value(
                        domain_key,
                        field_name,
                        normalization_result.normalized_record,
                    )
                    if field_name in error_fields or is_missing_value(value):
                        continue
                    successfully_validated_fields += 1
                    dom_valid_fields += 1

            domain_scores[domain] = (
                0.0
                if dom_expected_fields == 0
                else (dom_valid_fields / dom_expected_fields) * 100.0
            )

        if checked_records == 0 or total_expected_fields == 0 or has_unconfigured_domain:
            score = 0.0
            warnings.append(
                "No study records or expected standard fields were evaluated; readiness score is 0.0."
            )
            if not any(v.severity.upper() == "ERROR" for v in violations):
                violations.append(
                    ValidationViolation(
                        field="study",
                        message="No study records or expected standard fields were evaluated.",
                        severity="ERROR",
                    )
                )
        else:
            score = (successfully_validated_fields / total_expected_fields) * 100.0
        return StandardsReadinessReport(
            score=score,
            successfully_validated_fields=successfully_validated_fields,
            total_expected_fields=total_expected_fields,
            warnings=warnings,
            violations=violations,
            profile=self._profile.profile_name,
            checked_records=checked_records,
            domain_scores=domain_scores,
        )


__all__ = [
    "CDISC_CONTROLLED_TERMINOLOGY",
    "DM_ALLOWED_VARIABLES",
    "DOMAIN_PREFIXES",
    "NORMALIZATION_TERMINOLOGY",
    "PROFILE_REGISTRY",
    "SDTM_DOMAIN_SPECS",
    "UNIVERSAL_CDASH_IDENTIFIERS",
    "VALID_NAMING_CONVENTIONS",
    "_SDTM_RECORDS_SORT",
    "_UNSET_PIPELINE",
    "BiologicSafetyProfile",
    "CategoricalNormalizer",
    "DeviceSafetyProfile",
    "DrugSafetyProfile",
    "GeneralClinicalProfile",
    "NormalizationResult",
    "StandardsProfile",
    "StandardsProfileRegistry",
    "StandardsReadinessReport",
    "StandardsReadinessValidator",
    "VaccineSafetyProfile",
    "ValidationViolation",
    "_SdtmNormalizer",
    "_collapse_alnum_key",
    "_extract_stream_usubjid_and_id",
    "_iter_sorted_sdtm_frames",
    "build_form_sdtm_domain_map",
    "extract_record_form_ref",
    "filter_forms_by_sdtm_domain",
    "format_iso8601_dtc",
    "format_variable_name",
    "get_sdtm_domain_variables",
    "is_valid_cdash_variable",
    "iter_subject_ordered_sdtm_records",
    "merge_form_ids_into_record_filter",
    "normalize_naming_convention",
    "record_matches_sdtm_domain",
    "resolve_form_sdtm_domain",
    "resolve_sdtm_domain",
    "resolve_standards_profile",
    "transform_records_to_sdtm",
    "transform_records_to_sdtm_rows",
    "validate_cdash_variable",
]
