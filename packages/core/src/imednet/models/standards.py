"""Data standard profiles and validation models for clinical domains."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from imednet.core.context import is_strict_mode
from imednet.models.base import ImednetBaseModel
from imednet.models.sdtm import (
    _MASKED_SENTINEL,
    _SDTM_RECORDS_SORT,
    _UNSET_PIPELINE,
    SDTM_DOMAIN_SPECS,
    VALID_NAMING_CONVENTIONS,
    _collapse_alnum_key,
    _extract_stream_usubjid_and_id,
    _format_usubjid,
    _iter_sorted_sdtm_frames,
    _SdtmNormalizer,
    extract_suppqual_rows,
    format_iso8601_dtc,
    format_variable_name,
    get_sdtm_domain_variables,
    normalize_naming_convention,
    resolve_sdtm_domain,
    transform_records_to_sdtm,
    transform_records_to_sdtm_rows,
    transform_records_to_suppqual,
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
    "CM": "CM",
    "CONCOMITANT MEDICATIONS": "CM",
    "CONCOMITANT_MEDICATIONS": "CM",
    "CONCOMITANT MEDICATION": "CM",
    "CONCOMITANT_MEDICATION": "CM",
    "CONMEDS": "CM",
    "LB": "LB",
    "LAB": "LB",
    "LABS": "LB",
    "LABORATORY": "LB",
    "EX": "EX",
    "EXPOSURE": "EX",
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


_ISO_KNOWN_PREFIX_RE = re.compile(
    r"^(\d{4})"
    r"(?:-(0[1-9]|1[0-2])"
    r"(?:-(0[1-9]|[12]\d|3[01])"
    r"(?:[T ]([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?)?)?)?"
)


def _canonical_domain(domain: str) -> str:
    """Normalize a clinical domain identifier to its standard 2-letter code."""
    normalized = domain.strip().upper().replace("-", "_")
    return _CANONICAL_DOMAINS.get(normalized, normalized)


def _canonicalize_domain_record(
    domain: str,
    data: Mapping[str, Any],
    *,
    normalized_lookups: dict[str, dict[str, str]] | None = None,
    canonical_terms: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Project both uppercase SDTM variables and camelCase/snake_case EDC aliases onto *data*."""
    resolved: dict[str, Any] = dict(data)
    lower_map: dict[str, Any] = {
        str(k).lower(): v for k, v in data.items() if not is_missing_value(v)
    }

    if is_missing_value(resolved.get("subjectKey")):
        for subj_cand in (
            "subject_key",
            "USUBJID",
            "usubjid",
            "SUBJID",
            "subjid",
            "subjectId",
            "subject_id",
        ):
            cand_val = resolved.get(subj_cand)
            if is_missing_value(cand_val):
                cand_val = lower_map.get(subj_cand.lower())
            if not is_missing_value(cand_val):
                resolved["subjectKey"] = cand_val.strip() if isinstance(cand_val, str) else cand_val
                break

    sdtm_domain = resolve_sdtm_domain(domain, fallback="")
    spec = SDTM_DOMAIN_SPECS.get(sdtm_domain)
    if spec is None:
        return resolved

    flag_vars: set[str] = spec.get("flags", set())
    for sdtm_var, aliases in spec["aliases"].items():
        found_val: Any = None
        has_val = False
        for cand in (sdtm_var, *aliases):
            cand_val = resolved.get(cand)
            if is_missing_value(cand_val):
                cand_val = lower_map.get(cand.lower())
            if not is_missing_value(cand_val):
                found_val = cand_val
                has_val = True
                break
        if not has_val:
            continue

        if is_missing_value(resolved.get(sdtm_var)):
            resolved[sdtm_var] = found_val

        for alias in aliases:
            if not is_missing_value(resolved.get(alias)):
                continue
            alias_val = found_val
            if (
                sdtm_var in flag_vars
                and alias != sdtm_var
                and isinstance(alias_val, str)
                and is_boolean_token(alias_val.strip())
            ):
                alias_val = parse_bool(alias_val.strip())
            if (
                normalized_lookups
                and alias in normalized_lookups
                and normalized_lookups[alias]
                and isinstance(alias_val, str)
            ):
                lk = alias_val.strip().upper()
                if lk in normalized_lookups[alias]:
                    alias_val = normalized_lookups[alias][lk]
            elif canonical_terms and alias in canonical_terms and isinstance(alias_val, str):
                lk = alias_val.strip().upper()
                if lk in canonical_terms[alias]:
                    alias_val = canonical_terms[alias][lk]
            resolved[alias] = alias_val

    return resolved


def _is_chronological_dtc_inverted(start_raw: Any, end_raw: Any) -> bool:
    """Return True strictly when *start_raw* represents a later timestamp/date than *end_raw*."""
    if is_missing_value(start_raw) or is_missing_value(end_raw):
        return False
    if _MASKED_SENTINEL in (start_raw, end_raw):
        return False

    start_iso = format_iso8601_dtc(start_raw, mask_phi=False)
    end_iso = format_iso8601_dtc(end_raw, mask_phi=False)
    if not start_iso or not end_iso:
        return False

    if ("T" in start_iso or " " in start_iso) and ("T" in end_iso or " " in end_iso):
        try:
            s_dt = datetime.fromisoformat(
                f"{start_iso[:-1]}+00:00" if start_iso.endswith("Z") else start_iso
            )
            e_dt = datetime.fromisoformat(
                f"{end_iso[:-1]}+00:00" if end_iso.endswith("Z") else end_iso
            )
            if (s_dt.tzinfo is None) == (e_dt.tzinfo is None):
                return s_dt > e_dt
        except ValueError:
            pass

    s_match = _ISO_KNOWN_PREFIX_RE.match(start_iso)
    e_match = _ISO_KNOWN_PREFIX_RE.match(end_iso)
    if not s_match or not e_match:
        return False

    s_parts = tuple(int(g) for g in s_match.groups() if g is not None)
    e_parts = tuple(int(g) for g in e_match.groups() if g is not None)
    if not s_parts or not e_parts:
        return False
    common_len = min(len(s_parts), len(e_parts))
    return s_parts[:common_len] > e_parts[:common_len]


def _collect_chronological_dtc_violations(
    domain: str,
    raw_record: Mapping[str, Any],
    canonical_record: Mapping[str, Any],
) -> list[ValidationViolation]:
    """Validate ``--STDTC <= --ENDTC`` pairs across SDTM and camelCase/snake_case fields."""
    violations: list[ValidationViolation] = []
    checked_pairs: set[tuple[str, str]] = set()
    sdtm_dom = resolve_sdtm_domain(domain, fallback="")

    for key in list(canonical_record.keys()):
        key_upper = str(key).upper()
        if key_upper.endswith("STDTC") and len(key_upper) > 5:
            prefix = key_upper[:-5]
            start_var = f"{prefix}STDTC"
            end_var = f"{prefix}ENDTC"
            if (start_var, end_var) in checked_pairs:
                continue
            start_val = canonical_record.get(key)
            end_val = canonical_record.get(end_var)
            if is_missing_value(end_val):
                for k_cand, v_cand in canonical_record.items():
                    if str(k_cand).upper() == end_var and not is_missing_value(v_cand):
                        end_val = v_cand
                        break
            checked_pairs.add((start_var, end_var))
            if _is_chronological_dtc_inverted(start_val, end_val):
                violations.append(
                    ValidationViolation(
                        field=end_var,
                        message=(
                            f"Chronological order violation: {start_var} must be less than or "
                            f"equal to {end_var}."
                        ),
                        severity="ERROR",
                    )
                )

    lower_raw = {str(k).lower(): (str(k), v) for k, v in raw_record.items()}
    for low_k, (orig_k, start_val) in lower_raw.items():
        end_lookup: str | None = None
        if low_k.endswith("startdate"):
            prefix_low = low_k[:-9]
            end_lookup = f"{prefix_low}enddate"
        elif low_k.endswith("_start_date"):
            prefix_low = low_k[:-11]
            end_lookup = f"{prefix_low}_end_date"
        elif low_k == "start_date":
            end_lookup = "end_date"
        if end_lookup is None or end_lookup not in lower_raw:
            continue
        end_orig_k, end_val = lower_raw[end_lookup]
        inferred_prefix = sdtm_dom if len(sdtm_dom) == 2 else ""
        if (
            inferred_prefix
            and (f"{inferred_prefix}STDTC", f"{inferred_prefix}ENDTC") in checked_pairs
        ):
            continue
        pair_id = (orig_k.upper(), end_orig_k.upper())
        if pair_id in checked_pairs:
            continue
        checked_pairs.add(pair_id)
        target_field = f"{inferred_prefix}ENDTC" if inferred_prefix else end_orig_k
        start_label = f"{inferred_prefix}STDTC" if inferred_prefix else orig_k
        if _is_chronological_dtc_inverted(start_val, end_val):
            violations.append(
                ValidationViolation(
                    field=target_field,
                    message=(
                        f"Chronological order violation: {start_label} must be less than or "
                        f"equal to {target_field}."
                    ),
                    severity="ERROR",
                )
            )

    return violations


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

    def _resolve_profile_domain(self, domain: str) -> str:
        """Resolve canonical domain key, mapping 'DV' to 'PD' when 'DV' is not explicitly defined."""
        domain_key = _canonical_domain(domain)
        if (
            domain_key == "DV"
            and "DV" not in self.required_fields
            and "DV" not in self.recommended_fields
            and ("PD" in self.required_fields or "PD" in self.recommended_fields)
        ):
            return "PD"
        return domain_key

    def expected_fields(self, domain: str) -> list[str]:
        """Return the list of expected (required + recommended) fields for a domain."""
        domain_key = self._resolve_profile_domain(domain)
        required = self.required_fields.get(domain_key, [])
        recommended = self.recommended_fields.get(domain_key, [])
        return [*required, *recommended]

    def validate(self, domain: str, data: dict[str, Any]) -> list[ValidationViolation]:
        """Validate a data dictionary against the profile for a specific domain.

        Args:
            domain: The domain name (e.g., 'AE', 'PD', 'DM', 'VS', 'CM', 'LB', 'EX', 'MH').
            data: The data dictionary to validate.

        Returns:
            A list of ValidationViolation objects.
        """
        domain_key = self._resolve_profile_domain(domain)
        canonical_data = _canonicalize_domain_record(domain_key, data)
        violations: list[ValidationViolation] = []

        for field_name in self.required_fields.get(domain_key, []):
            if is_missing_value(canonical_data.get(field_name)):
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
            if is_missing_value(canonical_data.get(field_name)):
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
                if self._resolve_profile_domain(constraint_domain) != domain_key:
                    continue
            else:
                field_name = constraint_key

            value = canonical_data.get(field_name)
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
    """General clinical profile with core CDISC domain requirements."""

    def __init__(self) -> None:
        """Initialize the general clinical profile."""
        super().__init__(
            profile_name="general",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
                "CM": ["subjectKey", "cmTerm"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "EX": ["subjectKey", "exTrt", "exDose", "exStartDate"],
                "MH": ["subjectKey", "mhTerm"],
            },
            recommended_fields={
                "AE": ["aeDecod", "aeRelationship"],
                "PD": ["dvStatus"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "CM": ["cmDose", "cmDoseUnit", "cmRoute", "cmStartDate"],
                "LB": ["lbTestCd", "lbUnit"],
                "EX": ["exDoseUnit", "exRoute"],
                "MH": ["mhCategory", "mhStartDate"],
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
                "CM": ["subjectKey", "cmTerm"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "EX": ["subjectKey", "exTrt", "exDose", "exStartDate"],
                "MH": ["subjectKey", "mhTerm"],
            },
            recommended_fields={
                "AE": ["aeActionTaken", "aeOutcome"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "CM": ["cmDose", "cmDoseUnit", "cmRoute", "cmStartDate"],
                "LB": ["lbTestCd", "lbUnit"],
                "EX": ["exDoseUnit", "exRoute"],
                "MH": ["mhCategory", "mhStartDate"],
            },
            optional_fields={"DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"]},
            value_constraints={
                "AE.aeSeverity": _STANDARD_AE_SEVERITY_VALUES,
                "DM.sex": _STANDARD_SEX_VALUES,
                "VS.vsUnit": _STANDARD_VITAL_SIGNS_UNITS,
            },
        )


class BiologicSafetyProfile(StandardsProfile):
    """Biologic safety profile with parenteral administration and safety readiness rules."""

    def __init__(self) -> None:
        """Initialize the biologic safety profile."""
        super().__init__(
            profile_name="biologic",
            required_fields={
                "AE": [
                    "subjectKey",
                    "aeTerm",
                    "aeSeverity",
                    "aeDecod",
                    "aeRelationship",
                    "aeSerious",
                ],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DM": ["subjectKey", "sex"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
                "CM": ["subjectKey", "cmTerm"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "EX": ["subjectKey", "exTrt", "exDose", "exRoute", "exStartDate"],
                "MH": ["subjectKey", "mhTerm"],
            },
            recommended_fields={
                "AE": ["aeActionTaken", "aeOutcome", "aeStartDate"],
                "PD": ["dvStatus"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "CM": ["cmDose", "cmDoseUnit", "cmRoute", "cmStartDate"],
                "LB": ["lbTestCd", "lbUnit", "lbCategory"],
                "EX": ["exDoseUnit", "exDoseForm"],
                "MH": ["mhDecod", "mhCategory", "mhStartDate"],
            },
            optional_fields={"DD": ["subjectKey", "ddTerm", "ddCategory", "ddDate", "ddSerious"]},
            value_constraints={
                "AE.aeSeverity": _STANDARD_AE_SEVERITY_VALUES,
                "DM.sex": _STANDARD_SEX_VALUES,
                "VS.vsUnit": _STANDARD_VITAL_SIGNS_UNITS,
            },
        )


class VaccineSafetyProfile(StandardsProfile):
    """Vaccine safety profile with immunization exposure and reactogenicity rules."""

    def __init__(self) -> None:
        """Initialize the vaccine safety profile."""
        super().__init__(
            profile_name="vaccine",
            required_fields={
                "AE": ["subjectKey", "aeTerm", "aeSeverity", "aeSerious", "aeStartDate"],
                "PD": ["subjectKey", "dvTerm", "dvCategory", "dvSeverity", "dvDate"],
                "DM": ["subjectKey", "sex", "age"],
                "VS": ["subjectKey", "vsTest", "vsResult", "vsDate"],
                "CM": ["subjectKey", "cmTerm"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "EX": ["subjectKey", "exTrt", "exDose", "exStartDate"],
                "MH": ["subjectKey", "mhTerm"],
            },
            recommended_fields={
                "AE": ["aeDecod", "aeRelationship", "aeOutcome"],
                "PD": ["dvStatus"],
                "DM": ["birthDate", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "CM": ["cmDose", "cmDoseUnit", "cmRoute", "cmStartDate", "cmIndication"],
                "LB": ["lbTestCd", "lbUnit"],
                "EX": ["exDoseUnit", "exRoute"],
                "MH": ["mhCategory", "mhStartDate"],
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
                "CM": ["subjectKey", "cmTerm"],
                "LB": ["subjectKey", "lbTest", "lbResult", "lbDate"],
                "EX": ["subjectKey", "exTrt", "exDose", "exStartDate"],
                "MH": ["subjectKey", "mhTerm"],
            },
            recommended_fields={
                "DD": ["ddRelationship", "ddActionTaken"],
                "DM": ["birthDate", "age", "race", "ethnicity"],
                "VS": ["vsTestCd", "vsUnit"],
                "CM": ["cmDose", "cmDoseUnit", "cmRoute", "cmStartDate"],
                "LB": ["lbTestCd", "lbUnit"],
                "EX": ["exDoseUnit", "exRoute"],
                "MH": ["mhCategory", "mhStartDate"],
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
        domain_key = self._resolve_profile_domain(domain)

        if domain_key != "DD":
            return violations

        canonical_data = _canonicalize_domain_record(domain_key, data)
        deficiency_occurred = canonical_data.get("ddOccurred")
        if deficiency_occurred is True and all(
            is_missing_value(canonical_data.get(field_name))
            for field_name in ("ddTerm", "ddCategory", "ddDate")
        ):
            violations.append(
                ValidationViolation(
                    field="ddOccurred",
                    message="DD record details are required when ddOccurred is true.",
                    severity="ERROR",
                )
            )

        dd_serious = canonical_data.get("ddSerious")
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
    """Resolve a profile name ('drug', 'device', 'general') or StandardsProfile instance."""
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


_NON_BOOLEAN_CDISC_FIELDS: frozenset[str] = frozenset(
    {
        "sex",
        "gender",
        "vsorresu",
        "vsunit",
        "vs_unit",
        "lborresu",
        "lbunit",
        "lb_unit",
        "lbstresu",
        "lbstdunit",
        "lb_std_unit",
        "cmdosu",
        "cmdoseunit",
        "cm_dose_unit",
        "exdosu",
        "exdoseunit",
        "ex_dose_unit",
        "ageu",
        "age_unit",
        "unit",
        "aesev",
        "aeseverity",
        "ae_severity",
        "dvsev",
        "dvseverity",
        "dv_severity",
        "pdseverity",
        "severity",
        "vsorres",
        "vsresult",
        "vs_result",
        "lborres",
        "lbresult",
        "lb_result",
        "lbstresc",
        "lbstresn",
        "result",
        "cmdose",
        "cm_dose",
        "exdose",
        "ex_dose",
        "dose",
        "age",
        "arm",
        "armcd",
        "actarm",
        "actarmcd",
        "subjid",
        "subjectid",
        "subject_id",
        "siteid",
        "site_id",
        "sitekey",
        "site_key",
        "vstestcd",
        "vs_test_cd",
        "lbtestcd",
        "lb_test_cd",
        "lbnrind",
        "lbnormalrangeind",
        "lb_normal_range_ind",
        "studyid",
        "study_id",
        "domain",
    }
)


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

        lower_field = field_name.lower()
        if (
            isinstance(raw_value, str)
            and lower_field not in _NON_BOOLEAN_CDISC_FIELDS
            and not lower_field.endswith("seq")
        ):
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
        """Return True when score reaches 100 and no ERROR-severity violations exist."""
        return self.score >= 100.0 and not any(
            v.severity.upper() == "ERROR" for v in self.violations
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

        canonical_terms: dict[str, dict[str, str]] = getattr(
            self._normalizer, "_canonical_terms", {}
        )

        for domain, records in records_by_domain.items():
            effective_domain = self._profile._resolve_profile_domain(domain)
            expected_fields = self._profile.expected_fields(effective_domain)
            scoring_fields = expected_fields or list(
                self._profile.optional_fields.get(effective_domain, [])
            )
            is_dm_domain = _canonical_domain(effective_domain) == "DM"
            seen_dm_subjects: set[str] = set()
            dom_valid_fields = 0
            dom_expected_fields = 0
            dom_violations: list[ValidationViolation] = []

            if records and not scoring_fields:
                checked_records += len(records)
                unprofiled_violation = ValidationViolation(
                    field="DOMAIN",
                    message=(
                        f"Domain '{domain}' has no defined profile variables in "
                        f"'{self._profile.profile_name}'."
                    ),
                    severity="ERROR",
                )
                violations.append(unprofiled_violation)
                total_expected_fields += len(records)
                domain_scores[domain] = 0.0
                continue

            for record in records:
                checked_records += 1
                normalization_result = self._normalizer.normalize_record(
                    record, normalized_lookups=normalized_lookups
                )
                warnings.extend(normalization_result.warnings)

                canonical_record = _canonicalize_domain_record(
                    effective_domain,
                    normalization_result.normalized_record,
                    normalized_lookups=normalized_lookups,
                    canonical_terms=canonical_terms,
                )

                record_violations = list(
                    self._profile.validate(domain=effective_domain, data=canonical_record)
                )

                existing_error_fields = {
                    violation.field
                    for violation in record_violations
                    if violation.severity.upper() == "ERROR"
                }
                for field_name in scoring_fields:
                    val = canonical_record.get(field_name)
                    if is_missing_value(val):
                        continue
                    if (
                        field_name not in existing_error_fields
                        and field_name in accepted_terms
                        and not normalized_lookups.get(field_name)
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

                structural_violations = _collect_chronological_dtc_violations(
                    effective_domain,
                    record,
                    canonical_record,
                )
                if is_dm_domain:
                    raw_dm_subj = (
                        canonical_record.get("USUBJID")
                        or canonical_record.get("usubjid")
                        or canonical_record.get("subjectKey")
                        or canonical_record.get("subject_key")
                        or canonical_record.get("SUBJID")
                        or canonical_record.get("subjid")
                    )
                    if not is_missing_value(raw_dm_subj):
                        dm_subj = str(raw_dm_subj).strip()
                        study_id = (
                            canonical_record.get("STUDYID")
                            or canonical_record.get("studyKey")
                            or canonical_record.get("study_key")
                            or ""
                        )
                        if study_id and dm_subj != _MASKED_SENTINEL:
                            dm_subj = _format_usubjid(dm_subj, str(study_id))
                        if dm_subj and dm_subj != _MASKED_SENTINEL:
                            if dm_subj in seen_dm_subjects:
                                structural_violations.append(
                                    ValidationViolation(
                                        field="USUBJID",
                                        message=(
                                            "Duplicate USUBJID detected in DM domain; "
                                            "DM requires one record per subject."
                                        ),
                                        severity="ERROR",
                                    )
                                )
                            else:
                                seen_dm_subjects.add(dm_subj)

                record_violations.extend(structural_violations)
                dom_violations.extend(record_violations)
                violations.extend(record_violations)

                structural_error_count = sum(
                    1 for v in structural_violations if v.severity.upper() == "ERROR"
                )
                total_expected_fields += len(scoring_fields) + structural_error_count
                dom_expected_fields += len(scoring_fields) + structural_error_count
                error_fields = {
                    violation.field
                    for violation in record_violations
                    if violation.severity.upper() == "ERROR"
                }
                for field_name in scoring_fields:
                    value = canonical_record.get(field_name)
                    if field_name in error_fields or is_missing_value(value):
                        continue
                    successfully_validated_fields += 1
                    dom_valid_fields += 1

            if (
                records
                and dom_valid_fields == 0
                and not any(v.severity.upper() == "ERROR" for v in dom_violations)
            ):
                violations.append(
                    ValidationViolation(
                        field="DOMAIN",
                        message=(
                            f"Domain '{domain}' batch matched zero profile variables in "
                            f"'{self._profile.profile_name}'."
                        ),
                        severity="ERROR",
                    )
                )

            domain_scores[domain] = (
                0.0
                if dom_expected_fields == 0
                else (dom_valid_fields / dom_expected_fields) * 100.0
            )

        score = (
            0.0
            if total_expected_fields == 0
            else (successfully_validated_fields / total_expected_fields) * 100
        )
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
    "extract_suppqual_rows",
    "format_iso8601_dtc",
    "format_variable_name",
    "get_sdtm_domain_variables",
    "is_valid_cdash_variable",
    "normalize_naming_convention",
    "resolve_sdtm_domain",
    "resolve_standards_profile",
    "transform_records_to_sdtm",
    "transform_records_to_sdtm_rows",
    "transform_records_to_suppqual",
    "validate_cdash_variable",
]
