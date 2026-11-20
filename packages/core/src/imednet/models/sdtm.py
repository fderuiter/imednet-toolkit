"""Core CDISC SDTM domain transformation, variable naming, and PHI-safe row mapping."""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from imednet.utils.security import (
    SensitivityRegistry,
    global_sensitivity_registry,
    mask_clinical_phi,
)
from imednet.utils.security import (
    _normalize_key as _security_normalize_key,
)

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment, unused-ignore]


def _is_deleted_record(rec: Any) -> bool:
    """Return True if an EDC record or mapping is marked deleted/voided."""
    val = rec.get("deleted") if isinstance(rec, Mapping) else getattr(rec, "deleted", None)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes", "y"}
    return False


def format_iso8601_dtc(val: Any) -> str | None:
    """Convert a timestamp/date value to standard CDISC ISO 8601 DTC string.

    Args:
        val: Date, datetime, string, or numeric value to convert.

    Returns:
        Formatted ISO 8601 string (e.g. '2026-01-15T09:30:00' or '2026-01-15'), or None.
    """
    if val is None or (pd is not None and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        if val_str == "***MASKED***":
            return val_str
        # Check standard date-only pattern YYYY-MM-DD
        if len(val_str) == 10 and val_str[4] == "-" and val_str[7] == "-":
            try:
                d = date.fromisoformat(val_str)
                return d.isoformat()
            except ValueError:
                pass

        # Try fast ISO datetime parsing (handles YYYY-MM-DDTHH:MM:SS, space-separated, fractional seconds, tz)
        try:
            dt = datetime.fromisoformat(val_str)
            return dt.isoformat()
        except ValueError:
            pass

        # Dispatch remaining string formats by delimiter and length
        if "/" in val_str:
            if ":" in val_str and len(val_str) >= 19:
                fmt = None
                if val_str[4] == "/" and val_str[7] == "/":
                    fmt = "%Y/%m/%d %H:%M:%S.%f" if "." in val_str else "%Y/%m/%d %H:%M:%S"
                elif val_str[2] == "/" and val_str[5] == "/":
                    fmt = "%m/%d/%Y %H:%M:%S.%f" if "." in val_str else "%m/%d/%Y %H:%M:%S"
                if fmt:
                    try:
                        return datetime.strptime(val_str, fmt).isoformat()
                    except ValueError:
                        pass
            elif len(val_str) == 10:
                fmt = None
                if val_str[2] == "/" and val_str[5] == "/":
                    fmt = "%m/%d/%Y"
                elif val_str[4] == "/" and val_str[7] == "/":
                    fmt = "%Y/%m/%d"
                if fmt:
                    try:
                        return datetime.strptime(val_str, fmt).strftime("%Y-%m-%d")
                    except ValueError:
                        pass
        elif "-" in val_str:
            if len(val_str) == 10 and val_str[2] == "-" and val_str[5] == "-":
                try:
                    return datetime.strptime(val_str, "%m-%d-%Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
            elif len(val_str) == 11 and val_str[2] == "-" and val_str[6] == "-":
                try:
                    return datetime.strptime(val_str, "%d-%b-%Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
            elif val_str.count("-") == 2:
                parts = val_str.split("-")
                if len(parts) == 3 and parts[0].isdigit() and parts[2].isdigit():
                    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
                        try:
                            return datetime.strptime(val_str, fmt).strftime("%Y-%m-%d")
                        except ValueError:
                            pass

        return val_str
    return str(val)


def _format_sdtm_flag(val: Any) -> str | None:
    """Format boolean flag values into CDISC standard 'Y' or 'N'."""
    if val is None or (pd is not None and pd.isna(val)):
        return None
    if isinstance(val, bool):
        return "Y" if val else "N"
    if isinstance(val, str):
        v = val.strip().upper()
        if v in ("Y", "YES", "TRUE", "1"):
            return "Y"
        if v in ("N", "NO", "FALSE", "0"):
            return "N"
        return val
    return "Y" if bool(val) else "N"


@functools.lru_cache(maxsize=1024)
def _normalize_key(key: str) -> str:
    """Normalize a key name by removing underscores and converting to lowercase."""
    return key.replace("_", "").lower()


SDTM_DOMAIN_SPECS: dict[str, dict[str, Any]] = {
    "AE": {
        "domain": "AE",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "AESEQ",
            "AETERM",
            "AEDECOD",
            "AESEV",
            "AESER",
            "AESTDTC",
            "AEENDTC",
            "AEOUT",
            "AEREL",
            "AEACN",
        ],
        "aliases": {
            "AETERM": ["aeTerm", "ae_term", "AETERM", "aeterm", "ae_name", "term"],
            "AEDECOD": ["aeDecod", "ae_decod", "AEDECOD", "aedecod", "preferred_term", "pt"],
            "AESEV": ["aeSeverity", "ae_severity", "AESEV", "aesev", "severity"],
            "AESER": ["aeSerious", "ae_serious", "AESER", "aeser", "serious"],
            "AESTDTC": [
                "aeStartDate",
                "ae_start_date",
                "AESTDTC",
                "aestdtc",
                "start_date",
                "startDate",
            ],
            "AEENDTC": ["aeEndDate", "ae_end_date", "AEENDTC", "aeendtc", "end_date", "endDate"],
            "AEOUT": ["aeOutcome", "ae_outcome", "AEOUT", "aeout", "outcome"],
            "AEREL": [
                "aeRelationship",
                "ae_relationship",
                "AEREL",
                "aerel",
                "relationship",
                "causality",
            ],
            "AEACN": ["aeActionTaken", "ae_action_taken", "AEACN", "aeacn", "action_taken"],
        },
        "flags": {"AESER"},
        "dtc": {"AESTDTC", "AEENDTC"},
    },
    "DV": {
        "domain": "DV",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "DVSEQ",
            "DVTERM",
            "DVCAT",
            "DVSEV",
            "DVSTDTC",
            "DVSTAT",
        ],
        "aliases": {
            "DVTERM": [
                "dvTerm",
                "dv_term",
                "DVTERM",
                "dvterm",
                "deviation_term",
                "description",
                "term",
            ],
            "DVCAT": ["dvCategory", "dv_category", "DVCAT", "dvcat", "category"],
            "DVSEV": ["dvSeverity", "dv_severity", "DVSEV", "dvsev", "severity"],
            "DVSTDTC": ["dvDate", "dv_date", "DVSTDTC", "dvstdtc", "deviation_date", "date"],
            "DVSTAT": ["dvStatus", "dv_status", "DVSTAT", "dvstat", "status"],
        },
        "flags": set(),
        "dtc": {"DVSTDTC"},
    },
    "DD": {
        "domain": "DD",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "DDSEQ",
            "DDTERM",
            "DDCAT",
            "DDSER",
            "DDSTDTC",
        ],
        "aliases": {
            "DDTERM": ["ddTerm", "dd_term", "DDTERM", "ddterm", "deficiency_term", "term"],
            "DDCAT": ["ddCategory", "dd_category", "DDCAT", "ddcat", "category"],
            "DDSER": ["ddSerious", "dd_serious", "DDSER", "ddser", "DDSPID", "ddspid", "serious"],
            "DDSTDTC": ["ddDate", "dd_date", "DDSTDTC", "ddstdtc", "deficiency_date", "date"],
        },
        "flags": {"DDSER"},
        "dtc": {"DDSTDTC"},
    },
    "CM": {
        "domain": "CM",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "CMSEQ",
            "CMTRT",
            "CMDECOD",
            "CMDOSE",
            "CMDOSU",
            "CMROUTE",
            "CMSTDTC",
            "CMENDTC",
            "CMINDC",
        ],
        "aliases": {
            "CMTRT": [
                "cmTerm",
                "cm_term",
                "CMTRT",
                "cmtrt",
                "medication",
                "medicationName",
                "drug_name",
                "cm_trt",
            ],
            "CMDECOD": ["cmDecod", "cm_decod", "CMDECOD", "cmdecod"],
            "CMDOSE": ["cmDose", "cm_dose", "CMDOSE", "cmdose", "dose"],
            "CMDOSU": ["cmDoseUnit", "cm_dose_unit", "CMDOSU", "cmdosu", "dose_unit", "unit"],
            "CMROUTE": ["cmRoute", "cm_route", "CMROUTE", "cmroute", "route"],
            "CMSTDTC": ["cmStartDate", "cm_start_date", "CMSTDTC", "cmstdtc", "start_date"],
            "CMENDTC": ["cmEndDate", "cm_end_date", "CMENDTC", "cmendtc", "end_date"],
            "CMINDC": ["cmIndication", "cm_indication", "CMINDC", "cmindc", "indication"],
        },
        "flags": set(),
        "dtc": {"CMSTDTC", "CMENDTC"},
    },
    "DM": {
        "domain": "DM",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "SUBJID",
            "BRTHDTC",
            "AGE",
            "AGEU",
            "SEX",
            "RACE",
            "ETHNIC",
            "ARM",
        ],
        "aliases": {
            "SUBJID": ["subjid", "SUBJID", "subject_id", "subjectId", "subjectKey", "subject_key"],
            "BRTHDTC": [
                "brthdat",
                "birthDate",
                "birth_date",
                "BRTHDTC",
                "brthdtc",
                "date_of_birth",
            ],
            "AGE": ["age", "AGE"],
            "AGEU": ["ageu", "age_unit", "AGEU"],
            "SEX": ["sex", "SEX", "gender"],
            "RACE": ["race", "RACE"],
            "ETHNIC": ["ethnic", "ethnicity", "ETHNIC"],
            "ARM": ["arm", "ARM", "treatment_arm", "armcd"],
        },
        "flags": set(),
        "dtc": {"BRTHDTC"},
    },
    "VS": {
        "domain": "VS",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "VSSEQ",
            "VSTESTCD",
            "VSTEST",
            "VSORRES",
            "VSORRESU",
            "VSDTC",
        ],
        "aliases": {
            "VSTESTCD": ["vsTestCd", "vs_test_cd", "VSTESTCD", "vstestcd", "test_cd", "testcd"],
            "VSTEST": ["vsTest", "vs_test", "VSTEST", "vstest", "test_name", "test"],
            "VSORRES": ["vsResult", "vs_result", "VSORRES", "vsorres", "result"],
            "VSORRESU": ["vsUnit", "vs_unit", "VSORRESU", "vsorresu", "unit"],
            "VSDTC": ["vsDate", "vs_date", "VSDTC", "vsdtc", "date"],
        },
        "flags": set(),
        "dtc": {"VSDTC"},
    },
    "LB": {
        "domain": "LB",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "LBSEQ",
            "LBTESTCD",
            "LBTEST",
            "LBCAT",
            "LBORRES",
            "LBORRESU",
            "LBSTRESC",
            "LBSTRESN",
            "LBSTRESU",
            "LBNRIND",
            "LBDTC",
        ],
        "aliases": {
            "LBTESTCD": ["lbTestCd", "lb_test_cd", "LBTESTCD", "lbtestcd", "test_cd", "testcd"],
            "LBTEST": ["lbTest", "lb_test", "LBTEST", "lbtest", "test_name", "test"],
            "LBCAT": ["lbCategory", "lb_category", "LBCAT", "lbcat", "category"],
            "LBORRES": ["lbResult", "lb_result", "LBORRES", "lborres", "result"],
            "LBORRESU": ["lbUnit", "lb_unit", "LBORRESU", "lborresu", "unit"],
            "LBSTRESC": ["lbStdResultChar", "lb_std_result_char", "LBSTRESC", "lbstresc"],
            "LBSTRESN": ["lbStdResultNum", "lb_std_result_num", "LBSTRESN", "lbstresn"],
            "LBSTRESU": ["lbStdUnit", "lb_std_unit", "LBSTRESU", "lbstresu"],
            "LBNRIND": ["lbNormalRangeInd", "lb_normal_range_ind", "LBNRIND", "lbnrind"],
            "LBDTC": ["lbDate", "lb_date", "LBDTC", "lbdtc", "collection_date", "date"],
        },
        "flags": set(),
        "dtc": {"LBDTC"},
    },
    "EX": {
        "domain": "EX",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "EXSEQ",
            "EXTRT",
            "EXDOSE",
            "EXDOSU",
            "EXDOSFRM",
            "EXDOSFRQ",
            "EXROUTE",
            "EXSTDTC",
            "EXENDTC",
        ],
        "aliases": {
            "EXTRT": ["exTrt", "ex_trt", "EXTRT", "extrt", "study_drug", "treatment"],
            "EXDOSE": ["exDose", "ex_dose", "EXDOSE", "exdose", "dose"],
            "EXDOSU": ["exDoseUnit", "ex_dose_unit", "EXDOSU", "exdosu", "dose_unit", "unit"],
            "EXDOSFRM": ["exDoseForm", "ex_dose_form", "EXDOSFRM", "exdosfrm", "dose_form"],
            "EXDOSFRQ": ["exDoseFreq", "ex_dose_freq", "EXDOSFRQ", "exdosfrq", "frequency"],
            "EXROUTE": ["exRoute", "ex_route", "EXROUTE", "exroute", "route"],
            "EXSTDTC": ["exStartDate", "ex_start_date", "EXSTDTC", "exstdtc", "start_date"],
            "EXENDTC": ["exEndDate", "ex_end_date", "EXENDTC", "exendtc", "end_date"],
        },
        "flags": set(),
        "dtc": {"EXSTDTC", "EXENDTC"},
    },
    "MH": {
        "domain": "MH",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "MHSEQ",
            "MHTERM",
            "MHDECOD",
            "MHCAT",
            "MHPRESP",
            "MHOCCUR",
            "MHSTDTC",
            "MHENDTC",
            "MHENRF",
        ],
        "aliases": {
            "MHTERM": ["mhTerm", "mh_term", "MHTERM", "mhterm", "condition", "term"],
            "MHDECOD": ["mhDecod", "mh_decod", "MHDECOD", "mhdecod"],
            "MHCAT": ["mhCategory", "mh_category", "MHCAT", "mhcat", "category"],
            "MHPRESP": ["mhPresp", "mh_presp", "MHPRESP", "mhpresp"],
            "MHOCCUR": ["mhOccur", "mh_occur", "MHOCCUR", "mhoccur"],
            "MHSTDTC": ["mhStartDate", "mh_start_date", "MHSTDTC", "mhstdtc", "start_date"],
            "MHENDTC": ["mhEndDate", "mh_end_date", "MHENDTC", "mhendtc", "end_date"],
            "MHENRF": ["mhEnrf", "mh_enrf", "MHENRF", "mhenrf", "ongoing"],
        },
        "flags": {"MHPRESP", "MHOCCUR"},
        "dtc": {"MHSTDTC", "MHENDTC"},
    },
}

_DOMAIN_NAME_ALIASES: dict[str, str] = {
    "AE": "AE",
    "ADVERSEEVENTS": "AE",
    "ADVERSEEVENT": "AE",
    "PD": "DV",
    "DV": "DV",
    "PROTOCOLDEVIATIONS": "DV",
    "PROTOCOLDEVIATION": "DV",
    "DD": "DD",
    "DEVICEDEFICIENCIES": "DD",
    "DEVICEDEFICIENCY": "DD",
    "CM": "CM",
    "CONCOMITANTMEDICATIONS": "CM",
    "CONMEDS": "CM",
    "DM": "DM",
    "DEMOGRAPHICS": "DM",
    "DEMO": "DM",
    "VS": "VS",
    "VITALSIGNS": "VS",
    "VITALS": "VS",
    "LB": "LB",
    "LAB": "LB",
    "LABS": "LB",
    "LABORATORY": "LB",
    "EX": "EX",
    "EXPOSURE": "EX",
    "MH": "MH",
    "MEDICALHISTORY": "MH",
    "MEDHIST": "MH",
}

_GENERIC_ALIASES_EXCLUDED_FROM_GLOBAL_SDTM: frozenset[str] = frozenset(
    {
        "term",
        "severity",
        "serious",
        "start_date",
        "startdate",
        "end_date",
        "enddate",
        "outcome",
        "relationship",
        "causality",
        "action_taken",
        "category",
        "date",
        "status",
        "description",
        "unit",
        "dose",
        "dose_unit",
        "doseunit",
        "dose_form",
        "doseform",
        "frequency",
        "route",
        "indication",
        "test",
        "test_name",
        "testname",
        "test_cd",
        "testcd",
        "result",
        "collection_date",
        "collectiondate",
        "treatment",
        "study_drug",
        "studydrug",
        "condition",
        "ongoing",
    }
)

_METADATA_SDTM_KEYS: dict[str, str] = {
    "study_key": "STUDYID",
    "study_id": "STUDYID",
    "studyid": "STUDYID",
    "subject_key": "USUBJID",
    "usubjid": "USUBJID",
    "site_key": "SITEID",
    "site_id": "SITEID",
    "siteid": "SITEID",
    "visit_id": "VISITNUM",
    "visitnum": "VISITNUM",
    "visit_key": "VISIT",
    "domain": "DOMAIN",
}

_GLOBAL_UNAMBIGUOUS_SDTM_ALIASES: dict[str, str] = {}
for _dom_code, _dom_spec in SDTM_DOMAIN_SPECS.items():
    for _sdtm_var, _aliases in _dom_spec["aliases"].items():
        if _dom_code == "DM" and _sdtm_var == "SUBJID":
            continue
        for _alias in _aliases:
            _norm_alias = _normalize_key(_alias)
            if (
                _norm_alias
                and _norm_alias not in _GENERIC_ALIASES_EXCLUDED_FROM_GLOBAL_SDTM
                and _alias.lower() not in _GENERIC_ALIASES_EXCLUDED_FROM_GLOBAL_SDTM
            ):
                _GLOBAL_UNAMBIGUOUS_SDTM_ALIASES.setdefault(_norm_alias, _sdtm_var)

VALID_NAMING_CONVENTIONS: frozenset[str] = frozenset({"snake_case", "camelCase", "sdtm"})

_VAR_FIRST_CAP_RE = re.compile(r"(.)([A-Z][a-z]+)")
_VAR_ALL_CAP_RE = re.compile(r"([a-z0-9])([A-Z])")
_VAR_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


@functools.lru_cache(maxsize=1024)
def _to_snake_identifier(name: str) -> str:
    """Convert camelCase, PascalCase, or separated variable names into snake_case."""
    cleaned = _VAR_NON_ALNUM_RE.sub("_", name).strip("_")
    if not cleaned:
        return ""
    s1 = _VAR_FIRST_CAP_RE.sub(r"\1_\2", cleaned)
    s2 = _VAR_ALL_CAP_RE.sub(r"\1_\2", s1)
    return re.sub(r"_+", "_", s2).lower()


def normalize_naming_convention(convention: str | None) -> str | None:
    """Validate and canonicalize a SinkConfig naming_convention value."""
    if convention is None:
        return None
    if not isinstance(convention, str):
        raise ValueError("naming_convention must be 'snake_case', 'camelCase', 'sdtm', or None")
    cleaned = convention.strip()
    if not cleaned:
        return None
    lower = cleaned.lower()
    if lower in {"snake_case", "snake"}:
        return "snake_case"
    if lower in {"camelcase", "camel_case", "camel"}:
        return "camelCase"
    if lower == "sdtm":
        return "sdtm"
    raise ValueError(
        f"Unsupported naming_convention '{convention}'. "
        "Expected one of: 'snake_case', 'camelCase', 'sdtm'."
    )


def resolve_sdtm_domain(domain: str | None, *, fallback: str = "AE") -> str:
    """Canonicalize an SDTM domain code or form/domain alias (e.g. 'PD' -> 'DV')."""
    if not domain or not isinstance(domain, str) or not domain.strip():
        return fallback
    cleaned = domain.strip()
    compact = "".join(ch for ch in cleaned.upper() if ch.isalnum())
    if compact in _DOMAIN_NAME_ALIASES:
        return _DOMAIN_NAME_ALIASES[compact]
    # Prefix heuristics for common EDC form keys (e.g. 'F_AE', 'AE_FORM', 'VS_VITALS')
    upper = cleaned.upper()
    tokens = [tok for tok in upper.replace("-", "_").split("_") if tok and tok != "F"]
    for tok in tokens:
        if tok in _DOMAIN_NAME_ALIASES:
            return _DOMAIN_NAME_ALIASES[tok]
    return upper


def format_variable_name(
    name: str,
    convention: str | None,
    *,
    domain: str | None = None,
    is_metadata: bool = False,
) -> str:
    """Format a metadata or clinical variable key using ``convention``.

    Supported conventions: ``None`` (unchanged), ``"snake_case"``, ``"camelCase"``, ``"sdtm"``.
    """
    canonical_conv = normalize_naming_convention(convention)
    if canonical_conv is None or not name:
        return name

    snake = _to_snake_identifier(name)
    if not snake:
        return name

    if canonical_conv == "snake_case":
        return snake

    if canonical_conv == "camelCase":
        parts = [p for p in snake.split("_") if p]
        if not parts:
            return name
        return parts[0] + "".join(part.capitalize() for part in parts[1:])

    # canonical_conv == "sdtm"
    if is_metadata:
        if snake in _METADATA_SDTM_KEYS:
            return _METADATA_SDTM_KEYS[snake]
        return snake.upper()
    if snake in _METADATA_SDTM_KEYS and name in {
        "studyKey",
        "study_key",
        "STUDYID",
        "subjectKey",
        "subject_key",
        "USUBJID",
    }:
        return _METADATA_SDTM_KEYS[snake]
    compact_norm = _normalize_key(name)
    if domain:
        dom = resolve_sdtm_domain(domain)
        spec = SDTM_DOMAIN_SPECS.get(dom)
        if spec is not None:
            for sdtm_var, aliases in spec["aliases"].items():
                if name == sdtm_var or any(_normalize_key(a) == compact_norm for a in aliases):
                    return str(sdtm_var)
    if compact_norm in _GLOBAL_UNAMBIGUOUS_SDTM_ALIASES:
        return _GLOBAL_UNAMBIGUOUS_SDTM_ALIASES[compact_norm]
    return re.sub(r"[^A-Za-z0-9]", "", snake).upper()


_BASE_SDTM_CONSUMED_KEYS: frozenset[str] = frozenset(
    {
        "USUBJID",
        "subjectKey",
        "subject_key",
        "study_key",
        "studyKey",
        "STUDYID",
        "DOMAIN",
        "domain",
        "recordId",
        "record_id",
        "formId",
        "form_id",
        "visitId",
        "visit_id",
        "recordStatus",
        "record_status",
    }
)


@functools.lru_cache(maxsize=128)
def _get_model_field_aliases(
    model_cls: type[BaseModel],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Extract and cache field alias mappings for a Pydantic model class."""
    model_field_to_serialized: dict[str, str] = {}
    all_field_aliases: dict[str, set[str]] = {}

    for fname, finfo in getattr(model_cls, "model_fields", {}).items():
        serialized_key = fname
        if (
            isinstance(getattr(finfo, "serialization_alias", None), str)
            and finfo.serialization_alias
        ):
            serialized_key = finfo.serialization_alias
        elif isinstance(getattr(finfo, "alias", None), str) and finfo.alias:
            serialized_key = finfo.alias

        field_aliases = {fname, serialized_key}
        if isinstance(getattr(finfo, "alias", None), str) and finfo.alias:
            field_aliases.add(finfo.alias)
        if (
            isinstance(getattr(finfo, "serialization_alias", None), str)
            and finfo.serialization_alias
        ):
            field_aliases.add(finfo.serialization_alias)
        if isinstance(getattr(finfo, "validation_alias", None), str) and finfo.validation_alias:
            field_aliases.add(finfo.validation_alias)

        for alias in field_aliases:
            model_field_to_serialized[alias] = serialized_key
            model_field_to_serialized.setdefault(alias.lower(), serialized_key)
            all_field_aliases.setdefault(alias, set()).update(field_aliases)
            all_field_aliases.setdefault(alias.lower(), set()).update(field_aliases)

    return model_field_to_serialized, all_field_aliases


@functools.lru_cache(maxsize=128)
def _get_model_consumed_keys(model_cls: type[BaseModel]) -> frozenset[str]:
    """Precompute base consumed keys for a Pydantic model class."""
    model_field_to_serialized, all_field_aliases = _get_model_field_aliases(model_cls)
    consumed = set(_BASE_SDTM_CONSUMED_KEYS)
    for initial_k in list(consumed):
        if initial_k in model_field_to_serialized:
            serialized_k = model_field_to_serialized[initial_k]
            consumed.add(serialized_k)
            consumed.update(all_field_aliases.get(serialized_k, set()))
        if initial_k.lower() in model_field_to_serialized:
            serialized_k = model_field_to_serialized[initial_k.lower()]
            consumed.add(serialized_k)
            consumed.update(all_field_aliases.get(serialized_k, set()))
    return frozenset(consumed)


def _mask_value_recursive(
    val: Any,
    *,
    registry: SensitivityRegistry,
    normalized_phi_keys: frozenset[str],
) -> Any:
    """Recursively mask nested dict/list values against SensitivityRegistry and custom phi_keys."""
    if isinstance(val, dict):
        masked_dict: dict[str, Any] = {}
        for k, v in val.items():
            norm_k = _security_normalize_key(k)
            if (
                norm_k in normalized_phi_keys and not registry.is_exempt(k)
            ) or registry.is_sensitive(k):
                masked_dict[k] = "***MASKED***"
            else:
                masked_dict[k] = _mask_value_recursive(
                    v,
                    registry=registry,
                    normalized_phi_keys=normalized_phi_keys,
                )
        return masked_dict
    if isinstance(val, list):
        return [
            _mask_value_recursive(
                item,
                registry=registry,
                normalized_phi_keys=normalized_phi_keys,
            )
            for item in val
        ]
    if isinstance(val, tuple):
        return tuple(
            _mask_value_recursive(
                item,
                registry=registry,
                normalized_phi_keys=normalized_phi_keys,
            )
            for item in val
        )
    return mask_clinical_phi(val, registry=registry)


def get_sdtm_domain_variables(domain: str) -> list[str]:
    """Return standard uppercase SDTM variable list for ``domain``."""
    domain_clean = resolve_sdtm_domain(domain)
    spec = SDTM_DOMAIN_SPECS.get(domain_clean)
    if spec is not None:
        return list(spec["variables"])
    return ["STUDYID", "DOMAIN", "USUBJID", f"{domain_clean}SEQ"]


def transform_records_to_sdtm_rows(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
    mask_phi: bool = True,
    seq_counters: dict[str, int] | None = None,
    phi_keys: Iterable[str] | None = None,
    registry: SensitivityRegistry | None = None,
    pipeline: Any | None = None,
) -> list[dict[str, Any]]:
    """Transform clinical records into normalized CDISC SDTM row dictionaries.

    Applies ``SensitivityRegistry`` and custom ``phi_keys`` / ``EnrichmentPipeline``
    masking *before* constructing ``USUBJID`` or mapping SDTM domain variables,
    and maintains per-``USUBJID`` sequence counters across streaming batches when
    ``seq_counters`` is supplied.
    """
    domain_clean = resolve_sdtm_domain(domain)
    spec = SDTM_DOMAIN_SPECS.get(domain_clean)
    std_vars = (
        list(spec["variables"]) if spec else ["STUDYID", "DOMAIN", "USUBJID", f"{domain_clean}SEQ"]
    )
    alias_map: dict[str, list[str]] = spec["aliases"] if spec else {}
    flag_vars: set[str] = spec["flags"] if spec else set()
    dtc_vars: set[str] = spec["dtc"] if spec else set()
    seq_col = f"{domain_clean}SEQ"
    include_seq_col = seq_col in std_vars

    active_counters = seq_counters if seq_counters is not None else {}
    active_registry = global_sensitivity_registry if registry is None else registry
    normalized_phi_keys = (
        frozenset(_security_normalize_key(k) for k in phi_keys if k)
        if phi_keys is not None
        else frozenset()
    )
    key_sensitive_cache: dict[str, bool] = {}

    def _is_key_sensitive(key: str) -> bool:
        cached = key_sensitive_cache.get(key)
        if cached is not None:
            return cached
        norm = _security_normalize_key(key)
        res = (
            norm in normalized_phi_keys and not active_registry.is_exempt(key)
        ) or active_registry.is_sensitive(key)
        key_sensitive_cache[key] = res
        return res

    rows: list[dict[str, Any]] = []

    for rec in records:
        if not include_deleted and _is_deleted_record(rec):
            continue
        model_field_to_serialized: dict[str, str] = {}
        all_field_aliases: dict[str, set[str]] = {}
        base_consumed: frozenset[str] | set[str] = _BASE_SDTM_CONSUMED_KEYS

        raw_subject_attr: Any = None
        if hasattr(rec, "record_data") and getattr(rec, "record_data", None) is not None:
            rec_dict = dict(rec.record_data) if isinstance(rec.record_data, dict) else {}
            if pipeline is not None and rec_dict:
                rec_dict = pipeline.process(rec_dict, mask_phi=mask_phi)
            raw_subject_attr = getattr(rec, "subject_key", None)
            if raw_subject_attr:
                rec_dict["subject_key"] = raw_subject_attr
                rec_dict["subjectKey"] = raw_subject_attr
            if getattr(rec, "date_created", None):
                rec_dict["date_created"] = rec.date_created
        elif isinstance(rec, BaseModel):
            rec_dict = rec.model_dump(by_alias=True)
            model_cls = type(rec)
            model_field_to_serialized, all_field_aliases = _get_model_field_aliases(model_cls)
            base_consumed = _get_model_consumed_keys(model_cls)
            raw_subject_attr = getattr(rec, "subject_key", None) or getattr(rec, "subjectKey", None)
            if pipeline is not None and rec_dict:
                rec_dict = pipeline.process(rec_dict, mask_phi=mask_phi)
        elif isinstance(rec, dict):
            rec_dict = dict(rec)
            if "record_data" in rec_dict and isinstance(rec_dict["record_data"], dict):
                nested_data = dict(rec_dict.pop("record_data"))
                if pipeline is not None and nested_data:
                    nested_data = pipeline.process(nested_data, mask_phi=mask_phi)
                for k, v in nested_data.items():
                    if k not in rec_dict:
                        rec_dict[k] = v
            elif pipeline is not None and rec_dict:
                rec_dict = pipeline.process(rec_dict, mask_phi=mask_phi)
        else:
            rec_dict = dict(getattr(rec, "__dict__", {}))
            raw_subject_attr = getattr(rec, "subject_key", None) or getattr(rec, "subjectKey", None)
            if "record_data" in rec_dict and isinstance(rec_dict["record_data"], dict):
                nested_data = dict(rec_dict.pop("record_data"))
                if pipeline is not None and nested_data:
                    nested_data = pipeline.process(nested_data, mask_phi=mask_phi)
                for k, v in nested_data.items():
                    if k not in rec_dict:
                        rec_dict[k] = v

        # Enforce SensitivityRegistry + custom PHI masking BEFORE building USUBJID or SDTM outputs
        if mask_phi:
            masked_rec: dict[str, Any] = {}
            for k, v in rec_dict.items():
                if _is_key_sensitive(k):
                    masked_rec[k] = "***MASKED***"
                elif isinstance(v, (dict, list, tuple)):
                    masked_rec[k] = _mask_value_recursive(
                        v,
                        registry=active_registry,
                        normalized_phi_keys=normalized_phi_keys,
                    )
                else:
                    masked_rec[k] = v
            rec_dict = masked_rec
            if raw_subject_attr is not None and (
                _is_key_sensitive("subject_key") or _is_key_sensitive("subjectKey")
            ):
                raw_subject_attr = "***MASKED***"

        usubjid = (
            rec_dict.get("USUBJID")
            or rec_dict.get("subjectKey")
            or rec_dict.get("subject_key")
            or (
                rec_dict.get(model_field_to_serialized.get("subject_key", ""))
                if model_field_to_serialized
                else None
            )
            or (
                rec_dict.get(model_field_to_serialized.get("subjectKey", ""))
                if model_field_to_serialized
                else None
            )
            or raw_subject_attr
            or ""
        )
        if mask_phi and _is_key_sensitive("USUBJID") and usubjid:
            usubjid = "***MASKED***"

        active_counters[usubjid] = active_counters.get(usubjid, 0) + 1
        seq_val = active_counters[usubjid]

        row: dict[str, Any] = {
            "STUDYID": study_key,
            "DOMAIN": domain_clean,
            "USUBJID": usubjid,
        }
        if include_seq_col:
            row[seq_col] = seq_val

        consumed_keys: set[str] = set(base_consumed)
        lower_rec_dict = {k.lower(): (k, v) for k, v in rec_dict.items()}

        for var_name in std_vars:
            if var_name in ("STUDYID", "DOMAIN", "USUBJID", seq_col):
                continue

            val = None
            found_key = None
            if var_name in rec_dict:
                val = rec_dict[var_name]
                found_key = var_name
            elif var_name.lower() in lower_rec_dict:
                found_key, val = lower_rec_dict[var_name.lower()]
            elif (
                var_name in model_field_to_serialized
                and model_field_to_serialized[var_name] in rec_dict
            ):
                found_key = model_field_to_serialized[var_name]
                val = rec_dict[found_key]
            elif (
                var_name.lower() in model_field_to_serialized
                and model_field_to_serialized[var_name.lower()] in rec_dict
            ):
                found_key = model_field_to_serialized[var_name.lower()]
                val = rec_dict[found_key]
            else:
                candidates = alias_map.get(var_name, [])
                for cand in candidates:
                    if cand in rec_dict:
                        val = rec_dict[cand]
                        found_key = cand
                        break
                    if cand.lower() in lower_rec_dict:
                        found_key, val = lower_rec_dict[cand.lower()]
                        break
                    if (
                        cand in model_field_to_serialized
                        and model_field_to_serialized[cand] in rec_dict
                    ):
                        found_key = model_field_to_serialized[cand]
                        val = rec_dict[found_key]
                        break
                    if (
                        cand.lower() in model_field_to_serialized
                        and model_field_to_serialized[cand.lower()] in rec_dict
                    ):
                        found_key = model_field_to_serialized[cand.lower()]
                        val = rec_dict[found_key]
                        break

            if found_key:
                consumed_keys.add(found_key)
                consumed_keys.update(all_field_aliases.get(found_key, set()))
                consumed_keys.update(all_field_aliases.get(found_key.lower(), set()))
                for cand in alias_map.get(var_name, []):
                    consumed_keys.add(cand)
                    if cand in rec_dict:
                        consumed_keys.add(cand)
                    if cand.lower() in lower_rec_dict:
                        consumed_keys.add(lower_rec_dict[cand.lower()][0])
                    if cand in model_field_to_serialized:
                        serialized_cand = model_field_to_serialized[cand]
                        consumed_keys.add(serialized_cand)
                        consumed_keys.update(all_field_aliases.get(serialized_cand, set()))
                    if cand.lower() in model_field_to_serialized:
                        serialized_cand = model_field_to_serialized[cand.lower()]
                        consumed_keys.add(serialized_cand)
                        consumed_keys.update(all_field_aliases.get(serialized_cand, set()))
                    consumed_keys.update(all_field_aliases.get(cand, set()))
                    consumed_keys.update(all_field_aliases.get(cand.lower(), set()))

            if var_name in dtc_vars or var_name.endswith("DTC"):
                row[var_name] = format_iso8601_dtc(val)
            elif var_name in flag_vars or var_name.endswith("SET"):
                row[var_name] = _format_sdtm_flag(val)
            else:
                row[var_name] = val

        normalized_consumed = {_normalize_key(c) for c in consumed_keys}
        for k, v in rec_dict.items():
            if k not in consumed_keys and _normalize_key(k) not in normalized_consumed:
                if any(x in k.lower() for x in ("date", "time", "dtc")):
                    row[k] = format_iso8601_dtc(v)
                else:
                    row[k] = v

        rows.append(row)

    return rows


def transform_records_to_sdtm(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
    mask_phi: bool = True,
    seq_counters: dict[str, int] | None = None,
    phi_keys: Iterable[str] | None = None,
    registry: SensitivityRegistry | None = None,
    pipeline: Any | None = None,
) -> pd.DataFrame:
    """Transform clinical records into a normalized CDISC SDTM domain DataFrame.

    Maps subject keys to USUBJID, study keys to STUDYID, sequences records per
    subject across batches when ``seq_counters`` is passed, masks PHI via
    ``SensitivityRegistry`` before building ``USUBJID`` or SDTM variables when
    ``mask_phi=True``, and converts event timestamps to ISO 8601 DTC strings.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for SDTM transformations. Install with "
            "'pip install \"imednet[export]\"'."
        )

    std_vars = get_sdtm_domain_variables(domain)
    rows = transform_records_to_sdtm_rows(
        records,
        domain=domain,
        study_key=study_key,
        include_deleted=include_deleted,
        mask_phi=mask_phi,
        seq_counters=seq_counters,
        phi_keys=phi_keys,
        registry=registry,
        pipeline=pipeline,
    )
    if not rows:
        return pd.DataFrame(columns=std_vars)

    df = pd.DataFrame(rows)
    cols = [c for c in std_vars if c in df.columns]
    extra_cols = [c for c in df.columns if c not in std_vars]
    return df[cols + extra_cols]


__all__ = [
    "SDTM_DOMAIN_SPECS",
    "VALID_NAMING_CONVENTIONS",
    "_format_sdtm_flag",
    "_get_model_consumed_keys",
    "_get_model_field_aliases",
    "_normalize_key",
    "format_iso8601_dtc",
    "format_variable_name",
    "get_sdtm_domain_variables",
    "normalize_naming_convention",
    "resolve_sdtm_domain",
    "transform_records_to_sdtm",
    "transform_records_to_sdtm_rows",
]
