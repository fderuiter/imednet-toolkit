"""Core CDISC SDTM domain transformation, variable naming, and PHI-safe row mapping."""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from imednet.utils.security import (
    SensitivityRegistry,
    global_sensitivity_registry,
    mask_clinical_phi,
)

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment, unused-ignore]

_MASKED_SENTINEL = "***MASKED***"
_SDTM_RECORDS_SORT = "subjectKey,asc;recordId,asc"
_ISO_YEAR_RE = re.compile(r"^\d{4}$")
_ISO_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_PARTIAL_UNKNOWN_YEAR_RE = re.compile(r"^(\d{4})-(?:UN|UNK|-)", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")
_UNSET_PIPELINE: Any = object()


def _is_deleted_record(rec: Any) -> bool:
    """Return True if an EDC record or mapping is marked deleted/voided."""
    if isinstance(rec, BaseModel):
        rec_ns = rec.__dict__
        if "deleted" in rec_ns:
            val = rec_ns["deleted"]
        elif hasattr(type(rec), "deleted"):
            val = getattr(rec, "deleted", None)
        else:
            return False
    elif isinstance(rec, Mapping):
        val = rec.get("deleted")
    else:
        val = getattr(rec, "deleted", None)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes", "y"}
    return False


@functools.lru_cache(maxsize=2048)
def _parse_dtc_string(val_str: str) -> tuple[str, int | None]:
    """Parse a non-empty date/datetime string into (formatted_dtc, year_or_none)."""
    # Check standard date-only pattern YYYY-MM-DD
    if len(val_str) == 10 and val_str[4] == "-" and val_str[7] == "-":
        try:
            d = date.fromisoformat(val_str)
            return d.isoformat(), d.year
        except ValueError:
            pass

    # Try fast ISO datetime parsing (handles YYYY-MM-DDTHH:MM:SS, space-separated, fractional seconds, tz)
    try:
        dt = datetime.fromisoformat(val_str)
        return dt.isoformat(), dt.year
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
                    parsed_dt = datetime.strptime(val_str, fmt)
                    return parsed_dt.isoformat(), parsed_dt.year
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
                    parsed_dt = datetime.strptime(val_str, fmt)
                    return parsed_dt.strftime("%Y-%m-%d"), parsed_dt.year
                except ValueError:
                    pass
    elif "-" in val_str:
        if len(val_str) == 10 and val_str[2] == "-" and val_str[5] == "-":
            try:
                parsed_dt = datetime.strptime(val_str, "%m-%d-%Y")
                return parsed_dt.strftime("%Y-%m-%d"), parsed_dt.year
            except ValueError:
                pass
        elif len(val_str) == 11 and val_str[2] == "-" and val_str[6] == "-":
            try:
                parsed_dt = datetime.strptime(val_str, "%d-%b-%Y")
                return parsed_dt.strftime("%Y-%m-%d"), parsed_dt.year
            except ValueError:
                pass
        elif val_str.count("-") == 2:
            parts = val_str.split("-")
            if len(parts) == 3 and parts[0].isdigit() and parts[2].isdigit():
                for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
                    try:
                        parsed_dt = datetime.strptime(val_str, fmt)
                        return parsed_dt.strftime("%Y-%m-%d"), parsed_dt.year
                    except ValueError:
                        pass

    if _ISO_YEAR_RE.match(val_str):
        return val_str, int(val_str)
    ym_match = _ISO_YEAR_MONTH_RE.match(val_str)
    if ym_match:
        return val_str, int(ym_match.group(1))
    unk_match = _PARTIAL_UNKNOWN_YEAR_RE.match(val_str)
    if unk_match:
        return val_str, int(unk_match.group(1))

    return val_str, None


def format_iso8601_dtc(
    val: Any,
    *,
    mask_phi: bool = False,
    redact_to_year: bool = False,
) -> str | None:
    """Convert a timestamp/date value to standard CDISC ISO 8601 DTC string.

    Args:
        val: Date, datetime, string, or numeric value to convert.
        mask_phi: When True, redact parsed dates to a four-digit ISO 8601 year or None.
        redact_to_year: Alias for ``mask_phi`` to emit only ``YYYY`` or None.

    Returns:
        Formatted ISO 8601 string (e.g. '2026-01-15T09:30:00', '2026-01-15', or '1985'),
        or None when missing or redacted without a valid year.
    """
    should_redact = mask_phi or redact_to_year
    if val is None:
        return None
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str or val_str == _MASKED_SENTINEL:
            return None
        formatted, parsed_year = _parse_dtc_string(val_str)
        if should_redact:
            return f"{parsed_year:04d}" if parsed_year is not None else None
        return formatted
    if isinstance(val, bool):
        return None if should_redact else str(val)
    if isinstance(val, datetime):
        return f"{val.year:04d}" if should_redact else val.isoformat()
    if isinstance(val, date):
        return f"{val.year:04d}" if should_redact else val.isoformat()
    if isinstance(val, int):
        if should_redact:
            return f"{val:04d}" if 1000 <= val <= 9999 else None
        return str(val)
    if isinstance(val, float) and math.isnan(val):
        return None
    if not isinstance(val, (list, tuple, dict, set)) and pd is not None and bool(pd.isna(val)):
        return None
    return None if should_redact else str(val)


def _format_sdtm_flag(val: Any) -> str | None:
    """Format boolean flag values into CDISC standard 'Y' or 'N'."""
    if val is None:
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
    if pd is not None and pd.isna(val):
        return None
    return "Y" if bool(val) else "N"


@functools.lru_cache(maxsize=1024)
def _normalize_key(key: str) -> str:
    """Normalize a key name by removing underscores and converting to lowercase."""
    return key.replace("_", "").lower()


@functools.lru_cache(maxsize=1024)
def _collapse_alnum_key(key: str) -> str:
    """Collapse non-alphanumeric delimiters and lowercase a field key."""
    return _NON_ALNUM_RE.sub("", key).lower()


def _is_birth_dtc_key(key: str) -> bool:
    """Return True when *key* represents a CDISC birth-date DTC/DAT variable."""
    return _collapse_alnum_key(key).endswith(("brthdtc", "brthdat"))


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
        "primary_dtc": "AESTDTC",
        "topic_vars": ("AETERM",),
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
        "primary_dtc": "DVSTDTC",
        "topic_vars": ("DVTERM",),
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
        "primary_dtc": "DDSTDTC",
        "topic_vars": ("DDTERM",),
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
        "primary_dtc": "CMSTDTC",
        "topic_vars": ("CMTRT",),
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
                "dob",
                "DOB",
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
        "primary_dtc": "BRTHDTC",
        "topic_vars": ("SUBJID",),
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
        "primary_dtc": "VSDTC",
        "topic_vars": ("VSTESTCD", "VSTEST"),
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
        "primary_dtc": "LBDTC",
        "topic_vars": ("LBTESTCD", "LBTEST"),
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
        "primary_dtc": "EXSTDTC",
        "topic_vars": ("EXTRT",),
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
        "primary_dtc": "MHSTDTC",
        "topic_vars": ("MHTERM",),
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


_SDTM_DOMAIN_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


def _match_known_sdtm_domain(text: str | None) -> str:
    """Return canonical 2-letter SDTM domain if *text* matches a known domain or EDC form alias."""
    if not text or not isinstance(text, str) or not text.strip():
        return ""
    upper = text.strip().upper()
    compact = _SDTM_DOMAIN_NON_ALNUM_RE.sub("", upper)
    if not compact:
        return ""
    if compact in _DOMAIN_NAME_ALIASES:
        return _DOMAIN_NAME_ALIASES[compact]
    if compact in SDTM_DOMAIN_SPECS:
        return compact
    tokens = [tok for tok in _SDTM_DOMAIN_NON_ALNUM_RE.split(upper) if tok and tok != "F"]
    for tok in tokens:
        if tok in _DOMAIN_NAME_ALIASES:
            return _DOMAIN_NAME_ALIASES[tok]
        if tok in SDTM_DOMAIN_SPECS:
            return tok
        stripped_digits = tok.rstrip("0123456789")
        if stripped_digits and stripped_digits in _DOMAIN_NAME_ALIASES:
            return _DOMAIN_NAME_ALIASES[stripped_digits]
        if stripped_digits and stripped_digits in SDTM_DOMAIN_SPECS:
            return stripped_digits
    return ""


def resolve_sdtm_domain(domain: str | None, *, fallback: str = "AE") -> str:
    """Canonicalize an SDTM domain code or form/domain alias (e.g. 'PD' -> 'DV')."""
    if not domain or not isinstance(domain, str) or not domain.strip():
        return fallback
    known = _match_known_sdtm_domain(domain)
    if known:
        return known
    upper = domain.strip().upper()
    compact = _SDTM_DOMAIN_NON_ALNUM_RE.sub("", upper)
    if not compact:
        return fallback
    sanitized = compact.lstrip("0123456789")
    return sanitized if sanitized else fallback


def _extract_study_config_form_domains(
    *,
    study_config: Any | None = None,
    mappings: Iterable[Any] | None = None,
    study_key: str | None = None,
) -> dict[str, str]:
    """Extract normalized ``source_form_key -> SDTM domain`` lookup from ``StudyConfiguration``."""
    resolved_cfg = study_config
    if resolved_cfg is None and mappings is None and study_key:
        from imednet.integrations.enrichment import resolve_study_configuration

        resolved_cfg = resolve_study_configuration(study_key)

    raw_mappings: Iterable[Any] = ()
    if mappings is not None:
        raw_mappings = mappings
    elif resolved_cfg is not None:
        raw_mappings = getattr(resolved_cfg, "mappings", None) or ()

    lookup: dict[str, str] = {}
    for rule in raw_mappings:
        if isinstance(rule, Mapping):
            raw_fk = rule.get("source_form_key") or rule.get("sourceFormKey")
            raw_dom = rule.get("domain") or rule.get("DOMAIN")
        else:
            raw_fk = getattr(rule, "source_form_key", None) or getattr(rule, "sourceFormKey", None)
            raw_dom = getattr(rule, "domain", None) or getattr(rule, "DOMAIN", None)
        if not raw_fk or not raw_dom:
            continue
        fk_str = str(raw_fk).strip()
        dom_str = resolve_sdtm_domain(str(raw_dom), fallback="")
        if not fk_str or not dom_str:
            continue
        lookup.setdefault(fk_str, dom_str)
        lookup.setdefault(fk_str.upper(), dom_str)
        lookup.setdefault(fk_str.lower(), dom_str)
        collapsed = _collapse_alnum_key(fk_str)
        if collapsed:
            lookup.setdefault(f"__alnum__:{collapsed}", dom_str)
    return lookup


def _lookup_configured_form_domain(
    candidate: Any,
    cfg_lookup: Mapping[str, str],
) -> str:
    """Resolve *candidate* against pre-extracted ``StudyConfiguration`` form mappings."""
    if not candidate or not cfg_lookup:
        return ""
    text = str(candidate).strip()
    if not text:
        return ""
    for probe in (text, text.upper(), text.lower(), f"__alnum__:{_collapse_alnum_key(text)}"):
        if probe in cfg_lookup:
            return cfg_lookup[probe]
    return ""


def _coerce_form_id_int(raw_fid: Any) -> int | None:
    if isinstance(raw_fid, bool) or raw_fid is None:
        return None
    if isinstance(raw_fid, int):
        return raw_fid if raw_fid > 0 else None
    if isinstance(raw_fid, str):
        s = raw_fid.strip()
        if s and s.isdigit():
            parsed = int(s)
            return parsed if parsed > 0 else None
    return None


def extract_record_form_ref(rec: Any) -> tuple[int | None, str | None]:
    """Extract ``(form_id, form_key)`` from a ``Record`` model or dictionary."""
    if isinstance(rec, Mapping):
        raw_fid = rec.get("formId") if "formId" in rec else rec.get("form_id")
        raw_fk = rec.get("formKey") or rec.get("form_key")
    else:
        raw_fid = getattr(rec, "form_id", None)
        if raw_fid is None:
            raw_fid = getattr(rec, "formId", None)
        raw_fk = getattr(rec, "form_key", None) or getattr(rec, "formKey", None)
    fid = _coerce_form_id_int(raw_fid)
    fk = (
        str(raw_fk).strip()
        if isinstance(raw_fk, str)
        else (str(raw_fk).strip() if raw_fk else None)
    )
    return fid, fk or None


def resolve_form_sdtm_domain(
    form: Any,
    *,
    study_config: Any | None = None,
    mappings: Iterable[Any] | None = None,
    study_key: str | None = None,
    form_domain_map: Mapping[Any, str] | None = None,
    fallback: str = "AE",
    _cfg_lookup: Mapping[str, str] | None = None,
) -> str:
    """Resolve the canonical SDTM domain for a ``Form``, mapping dict, or form key/name string."""
    if form is None:
        return fallback

    fid: int | None = None
    fkey: str | None = None
    fname: str | None = None
    explicit_dom: str | None = None

    if isinstance(form, str):
        fkey = form.strip() or None
    elif isinstance(form, Mapping):
        raw_fid = form.get("formId") if "formId" in form else form.get("form_id")
        fid = _coerce_form_id_int(raw_fid)
        raw_fk = form.get("formKey") or form.get("form_key")
        fkey = str(raw_fk).strip() if raw_fk else None
        raw_fn = form.get("formName") or form.get("form_name")
        fname = str(raw_fn).strip() if raw_fn else None
        raw_dom = form.get("domain") or form.get("DOMAIN")
        explicit_dom = str(raw_dom).strip() if raw_dom else None
    else:
        raw_fid = getattr(form, "form_id", None)
        if raw_fid is None:
            raw_fid = getattr(form, "formId", None)
        fid = _coerce_form_id_int(raw_fid)
        raw_fk = getattr(form, "form_key", None) or getattr(form, "formKey", None)
        fkey = str(raw_fk).strip() if raw_fk else None
        raw_fn = getattr(form, "form_name", None) or getattr(form, "formName", None)
        fname = str(raw_fn).strip() if raw_fn else None
        raw_dom = getattr(form, "domain", None) or getattr(form, "DOMAIN", None)
        explicit_dom = str(raw_dom).strip() if isinstance(raw_dom, str) and raw_dom else None

    if explicit_dom:
        resolved_explicit = resolve_sdtm_domain(explicit_dom, fallback="")
        if resolved_explicit:
            return resolved_explicit

    if form_domain_map:
        if fid is not None:
            for cand_fid in (fid, str(fid)):
                if cand_fid in form_domain_map:
                    mapped = resolve_sdtm_domain(form_domain_map[cand_fid], fallback="")
                    if mapped:
                        return mapped
        if fkey:
            for cand_k in (fkey, fkey.upper(), fkey.lower()):
                if cand_k in form_domain_map:
                    mapped = resolve_sdtm_domain(form_domain_map[cand_k], fallback="")
                    if mapped:
                        return mapped

    cfg_lookup = (
        _cfg_lookup
        if _cfg_lookup is not None
        else _extract_study_config_form_domains(
            study_config=study_config,
            mappings=mappings,
            study_key=study_key,
        )
    )
    if cfg_lookup:
        for cand in (fkey, fname):
            cfg_dom = _lookup_configured_form_domain(cand, cfg_lookup)
            if cfg_dom:
                return cfg_dom

    for cand in (fkey, fname):
        known = _match_known_sdtm_domain(cand)
        if known:
            return known

    return fallback


def build_form_sdtm_domain_map(
    forms: Iterable[Any] | None = None,
    *,
    study_config: Any | None = None,
    mappings: Iterable[Any] | None = None,
    study_key: str | None = None,
) -> dict[Any, str]:
    """Build a ``{form_id / form_key: SDTM_DOMAIN}`` map from ``Form`` metadata & ``StudyConfiguration``."""
    cfg_lookup = _extract_study_config_form_domains(
        study_config=study_config,
        mappings=mappings,
        study_key=study_key,
    )
    mapping: dict[Any, str] = {
        k: v for k, v in cfg_lookup.items() if not k.startswith("__alnum__:")
    }
    if forms is None:
        return mapping

    for f in forms:
        if isinstance(f, Mapping):
            raw_fid = f.get("formId") if "formId" in f else f.get("form_id")
            raw_fk = f.get("formKey") or f.get("form_key")
        else:
            raw_fid = getattr(f, "form_id", None)
            if raw_fid is None:
                raw_fid = getattr(f, "formId", None)
            raw_fk = getattr(f, "form_key", None) or getattr(f, "formKey", None)

        dom = resolve_form_sdtm_domain(f, _cfg_lookup=cfg_lookup, fallback="")
        if not dom:
            continue
        coerced_fid = _coerce_form_id_int(raw_fid)
        if coerced_fid is not None:
            mapping[coerced_fid] = dom
        if raw_fk:
            fk_str = str(raw_fk).strip()
            if fk_str:
                mapping[fk_str] = dom
                mapping[fk_str.upper()] = dom
                mapping[fk_str.lower()] = dom
    return mapping


def filter_forms_by_sdtm_domain(
    forms: Iterable[Any],
    domain: str | None,
    *,
    study_config: Any | None = None,
    mappings: Iterable[Any] | None = None,
    study_key: str | None = None,
) -> list[Any]:
    """Return forms whose resolved SDTM domain matches ``domain``."""
    if domain is None or not str(domain).strip():
        return list(forms)
    target = resolve_sdtm_domain(domain, fallback="")
    if not target:
        return []
    cfg_lookup = _extract_study_config_form_domains(
        study_config=study_config,
        mappings=mappings,
        study_key=study_key,
    )
    matched: list[Any] = []
    for f in forms:
        dom = resolve_form_sdtm_domain(f, _cfg_lookup=cfg_lookup, fallback="")
        if not dom:
            raw_fk = (
                f.get("formKey") or f.get("form_key")
                if isinstance(f, Mapping)
                else getattr(f, "form_key", None) or getattr(f, "formKey", None)
            )
            if raw_fk:
                dom = resolve_sdtm_domain(str(raw_fk), fallback="")
        if dom == target:
            matched.append(f)
    return matched


def record_matches_sdtm_domain(
    rec: Any,
    domain: str,
    form_domain_map: Mapping[Any, str] | None = None,
    *,
    study_config: Any | None = None,
    mappings: Iterable[Any] | None = None,
    study_key: str | None = None,
) -> bool:
    """Return True when ``rec`` belongs to ``domain`` (via Form map, ``StudyConfiguration``, or key)."""
    target = resolve_sdtm_domain(domain, fallback="")
    if not target:
        return True

    if isinstance(rec, Mapping):
        raw_dom = rec.get("DOMAIN") or rec.get("domain")
    else:
        raw_dom = getattr(rec, "domain", None) or getattr(rec, "DOMAIN", None)
    if isinstance(raw_dom, str) and raw_dom.strip():
        return resolve_sdtm_domain(raw_dom, fallback="") == target

    fid, fk = extract_record_form_ref(rec)
    if form_domain_map:
        if fid is not None:
            for cand_fid in (fid, str(fid)):
                if cand_fid in form_domain_map:
                    return resolve_sdtm_domain(form_domain_map[cand_fid], fallback="") == target
        if fk:
            mapped_fk = (
                form_domain_map.get(fk)
                or form_domain_map.get(fk.upper())
                or form_domain_map.get(fk.lower())
            )
            if mapped_fk:
                return mapped_fk == target

    if fk and (study_config is not None or mappings is not None or study_key is not None):
        cfg_lookup = _extract_study_config_form_domains(
            study_config=study_config,
            mappings=mappings,
            study_key=study_key,
        )
        cfg_dom = _lookup_configured_form_domain(fk, cfg_lookup)
        if cfg_dom:
            return cfg_dom == target

    if fk:
        known_fk = _match_known_sdtm_domain(fk)
        if known_fk:
            return known_fk == target
        return resolve_sdtm_domain(fk, fallback="") == target

    if form_domain_map and fid is not None:
        has_int_keys = any(isinstance(k, int) and not isinstance(k, bool) for k in form_domain_map)
        if has_int_keys:
            return False
    return True


def merge_form_ids_into_record_filter(
    record_filter: Any,
    form_ids: Sequence[Any] | None,
) -> Any:
    """Merge domain-matched ``formId`` values into an ``sdk.records.list`` filter."""
    if form_ids is None:
        return record_filter
    clean_ids: list[int] = []
    seen_ids: set[int] = set()
    for item in form_ids:
        coerced = _coerce_form_id_int(item)
        if coerced is None:
            if isinstance(item, Mapping):
                coerced = _coerce_form_id_int(
                    item.get("formId") if "formId" in item else item.get("form_id")
                )
            else:
                coerced = _coerce_form_id_int(
                    getattr(item, "form_id", None)
                    if getattr(item, "form_id", None) is not None
                    else getattr(item, "formId", None)
                )
        if coerced is not None and coerced not in seen_ids:
            seen_ids.add(coerced)
            clean_ids.append(coerced)

    if not clean_ids and len(form_ids) > 0:
        return record_filter

    form_val: Any = clean_ids[0] if len(clean_ids) == 1 else list(clean_ids)
    if record_filter is None:
        return {"formId": form_val}
    if isinstance(record_filter, str):
        return {"formId": form_val, "filter": record_filter}
    if isinstance(record_filter, Mapping):
        merged = dict(record_filter)
        existing = merged.get("formId")
        if existing is None and "form_id" in merged:
            existing = merged.pop("form_id")
        if existing is None:
            merged["formId"] = form_val
            return merged
        existing_int = _coerce_form_id_int(existing)
        if existing_int is not None:
            existing_set = {existing_int}
        elif isinstance(existing, Iterable) and not isinstance(existing, (str, bytes)):
            existing_set = {cid for x in existing if (cid := _coerce_form_id_int(x)) is not None}
        else:
            existing_set = None
        if existing_set is not None:
            intersected = [fid for fid in clean_ids if fid in existing_set]
            merged["formId"] = intersected[0] if len(intersected) == 1 else intersected
        else:
            merged["formId"] = form_val
        return merged
    if isinstance(record_filter, Sequence):
        return {"formId": form_val, "filter": ";".join(str(x) for x in record_filter)}
    return {"formId": form_val}


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
        "subject_key",
        "subject_id",
        "subject_oid",
        "study_key",
        "study_id",
        "STUDYID",
        "DOMAIN",
        "record_id",
        "record_oid",
        "record_type",
        "record_status",
        "parent_record_id",
        "program_key",
        "form_id",
        "form_key",
        "form_name",
        "interval_id",
        "interval_name",
        "site_id",
        "site_name",
        "visit_id",
        "deleted",
        "is_deleted",
        "date_created",
        "date_modified",
        "last_updated",
        "keywords",
        "record_data",
    }
)

_BASE_SDTM_EXACT_CONSUMED_KEYS: frozenset[str] = _BASE_SDTM_CONSUMED_KEYS | frozenset(
    {
        "usubjid",
        "subjectKey",
        "subjectId",
        "subjectOid",
        "studyKey",
        "domain",
        "recordId",
        "recordOid",
        "recordType",
        "recordStatus",
        "parentRecordId",
        "programKey",
        "formId",
        "formKey",
        "formName",
        "intervalId",
        "intervalName",
        "siteId",
        "siteName",
        "visitId",
        "isDeleted",
        "dateCreated",
        "dateModified",
        "lastUpdated",
        "recordData",
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
    for initial_k in _BASE_SDTM_EXACT_CONSUMED_KEYS:
        if initial_k in model_field_to_serialized:
            serialized_k = model_field_to_serialized[initial_k]
            consumed.add(serialized_k)
            consumed.update(all_field_aliases.get(serialized_k, set()))
        if initial_k.lower() in model_field_to_serialized:
            serialized_k = model_field_to_serialized[initial_k.lower()]
            consumed.add(serialized_k)
            consumed.update(all_field_aliases.get(serialized_k, set()))
    return frozenset(consumed)


@functools.lru_cache(maxsize=512)
def _expand_model_var_consumed(
    model_cls: type[BaseModel],
    cand_names: tuple[str, ...],
) -> frozenset[str]:
    """Precompute serialized and alias keys consumed for a matched SDTM variable on *model_cls*."""
    model_field_to_serialized, all_field_aliases = _get_model_field_aliases(model_cls)
    extra: set[str] = set()
    for cand in cand_names:
        cand_lower = cand.lower()
        if cand in model_field_to_serialized:
            serialized_cand = model_field_to_serialized[cand]
            extra.add(serialized_cand)
            extra.update(all_field_aliases.get(serialized_cand, ()))
        if cand_lower in model_field_to_serialized:
            serialized_cand = model_field_to_serialized[cand_lower]
            extra.add(serialized_cand)
            extra.update(all_field_aliases.get(serialized_cand, ()))
        extra.update(all_field_aliases.get(cand, ()))
        extra.update(all_field_aliases.get(cand_lower, ()))
    return frozenset(extra)


@functools.lru_cache(maxsize=512)
def _get_model_var_serialized_set(
    model_cls: type[BaseModel],
    lower_cands: tuple[str, ...],
) -> frozenset[str]:
    """Precompute serialized field keys on *model_cls* matching a variable or its aliases."""
    model_field_to_serialized, _ = _get_model_field_aliases(model_cls)
    return frozenset(
        model_field_to_serialized[lc] for lc in lower_cands if lc in model_field_to_serialized
    )


def _mask_value_recursive(
    val: Any,
    *,
    registry: SensitivityRegistry,
    collapsed_phi_keys: frozenset[str],
) -> Any:
    """Recursively mask nested dict/list/tuple values against SensitivityRegistry and custom phi_keys."""
    if isinstance(val, dict):
        masked_dict: dict[str, Any] = {}
        for k, v in val.items():
            key_str = str(k)
            if (
                bool(collapsed_phi_keys)
                and not registry.is_exempt(key_str)
                and _collapse_alnum_key(key_str) in collapsed_phi_keys
            ) or registry.is_sensitive(key_str):
                masked_dict[k] = _MASKED_SENTINEL
            else:
                masked_dict[k] = _mask_value_recursive(
                    v,
                    registry=registry,
                    collapsed_phi_keys=collapsed_phi_keys,
                )
        return masked_dict
    if isinstance(val, list):
        return [
            _mask_value_recursive(
                item,
                registry=registry,
                collapsed_phi_keys=collapsed_phi_keys,
            )
            for item in val
        ]
    if isinstance(val, tuple):
        return tuple(
            _mask_value_recursive(
                item,
                registry=registry,
                collapsed_phi_keys=collapsed_phi_keys,
            )
            for item in val
        )
    return mask_clinical_phi(val, registry=registry)


def _record_id_sort_key(raw_id: Any) -> tuple[int, int, str]:
    """Build a deterministic comparable sort key for numeric, string, or missing record IDs."""
    if raw_id is None or raw_id == "":
        return (2, 0, "")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        return (0, raw_id, "")
    if isinstance(raw_id, str):
        stripped = raw_id.strip()
        if not stripped:
            return (2, 0, "")
        try:
            return (0, int(stripped), stripped)
        except ValueError:
            return (1, 0, stripped)
    return (1, 0, str(raw_id))


def _dtc_sort_key(canonical_dtc: str | None) -> tuple[int, str]:
    """Place present canonical ISO 8601 timestamps before missing values."""
    if canonical_dtc:
        return (0, canonical_dtc)
    return (1, "")


def _single_topic_sort_entry(v: Any) -> tuple[int, str]:
    """Build a comparable sort tuple for a single domain topic variable value."""
    if v is None or (isinstance(v, str) and not v):
        return (1, "")
    return (0, str(v))


def _topic_sort_key(topic_values: tuple[Any, ...]) -> tuple[tuple[int, str], ...]:
    """Build comparable sort tuples for domain topic variable values."""
    return tuple(map(_single_topic_sort_entry, topic_values))


def _extract_stream_usubjid_and_id(rec: Any) -> tuple[str, tuple[int, int, str]]:
    """Extract (usubjid, record_id_sort_key) from a raw Record, model, or dict for stream merging."""
    rec_data = getattr(rec, "record_data", None)
    usubjid: Any = None
    raw_id: Any = getattr(rec, "record_id", None)
    if raw_id is None:
        raw_id = getattr(rec, "recordId", None)

    if isinstance(rec_data, dict):
        usubjid = rec_data.get("USUBJID") or rec_data.get("usubjid")
    if isinstance(rec, dict):
        rd = rec.get("record_data") or rec.get("recordData")
        if isinstance(rd, dict) and not usubjid:
            usubjid = rd.get("USUBJID") or rd.get("usubjid")
        if not usubjid:
            usubjid = rec.get("USUBJID") or rec.get("subjectKey") or rec.get("subject_key")
        if raw_id is None:
            raw_id = rec.get("recordId") if "recordId" in rec else rec.get("record_id")
    if not usubjid:
        usubjid = getattr(rec, "subject_key", None) or getattr(rec, "subjectKey", None) or ""
    if raw_id is None:
        raw_id = getattr(rec, "record_oid", None) or (
            rec.get("recordOid") if isinstance(rec, dict) else None
        )
    return str(usubjid), _record_id_sort_key(raw_id)


def _canonical_record_id_key(raw_id: Any) -> str | None:
    """Normalize a record identifier to a canonical string key for sidecar lineage."""
    if raw_id is None or isinstance(raw_id, bool):
        return None
    text = str(raw_id).strip()
    return text if text else None


def _normalize_positive_visitnum(val: Any) -> int | None:
    """Coerce positive integer visit identifiers to ``int``; normalize ``<= 0`` or invalid to ``None``."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val > 0 else None
    if isinstance(val, float):
        if val.is_integer():
            ival = int(val)
            return ival if ival > 0 else None
        return None
    if isinstance(val, str):
        stripped = val.strip()
        if not stripped:
            return None
        try:
            ival = int(stripped)
            return ival if ival > 0 else None
        except ValueError:
            return None
    return None


@dataclass
class _PreparedSdtmRow:
    """Normalized SDTM row prior to final per-subject sequence number assignment."""

    usubjid: str
    sort_key: tuple[tuple[int, str], tuple[tuple[int, str], ...], tuple[int, int, str], int]
    row: dict[str, Any]
    record_id: Any = None
    record_id_key: str | None = None


class _SdtmNormalizer:
    """Reusable per-domain normalizer encapsulating SDTM specs, enrichment, and PHI redaction."""

    def __init__(
        self,
        *,
        domain: str,
        study_key: str,
        include_deleted: bool,
        mask_phi: bool,
        pipeline: Any | None = None,
        phi_keys: Iterable[str] | None = None,
        registry: SensitivityRegistry | None = None,
        preserve_record_id: bool = False,
    ) -> None:
        domain_clean = resolve_sdtm_domain(domain)
        self.domain_clean = domain_clean
        self.study_key = study_key
        self.include_deleted = include_deleted
        self.mask_phi = mask_phi
        self.preserve_record_id = bool(preserve_record_id)
        self.pipeline = pipeline
        self.registry: SensitivityRegistry = (
            global_sensitivity_registry if registry is None else registry
        )
        self.include_seq = domain_clean != "DM"
        self.seq_var = f"{domain_clean}SEQ"

        spec = SDTM_DOMAIN_SPECS.get(domain_clean)
        if spec:
            self.std_vars: list[str] = list(spec["variables"])
            self.alias_map: dict[str, list[str]] = spec["aliases"]
            self.flag_vars: set[str] = spec["flags"]
            self.dtc_vars: set[str] = spec["dtc"]
            self.primary_dtc: str = spec["primary_dtc"]
            self.topic_vars: tuple[str, ...] = spec["topic_vars"]
        else:
            self.std_vars = (
                ["STUDYID", "DOMAIN", "USUBJID", self.seq_var]
                if self.include_seq
                else ["STUDYID", "DOMAIN", "USUBJID"]
            )
            self.alias_map = {}
            self.flag_vars = set()
            self.dtc_vars = set()
            self.primary_dtc = f"{domain_clean}STDTC"
            self.topic_vars = (
                f"{domain_clean}TERM",
                f"{domain_clean}TRT",
                f"{domain_clean}TESTCD",
                f"{domain_clean}TEST",
                "SUBJID",
            )

        self.has_enrichment_transforms = bool(
            pipeline is not None
            and (
                getattr(pipeline, "terminology", None)
                or getattr(pipeline, "all_mappings", None)
                or getattr(pipeline, "mappings", None)
            )
        )
        self.terminology: dict[str, dict[str, str]] = (
            getattr(pipeline, "terminology", {}) if pipeline is not None else {}
        )
        merged_phi_keys: set[str] = set()
        if pipeline is not None and getattr(pipeline, "phi_keys", None):
            merged_phi_keys.update(k for k in pipeline.phi_keys if k)
        if phi_keys is not None:
            merged_phi_keys.update(k for k in phi_keys if k)
        self.pipeline_phi_keys: set[str] = merged_phi_keys
        self.collapsed_phi_keys: frozenset[str] = frozenset(
            _collapse_alnum_key(pk) for pk in self.pipeline_phi_keys
        )
        self._exempt_cache: dict[str, bool] = {}
        self._sens_cache: dict[str, bool] = {}
        self.sensitive_mapped_targets: frozenset[str] = frozenset()
        sensitive_targets: set[str] = set()
        remaps_usubjid = False
        all_rules = getattr(pipeline, "all_mappings", None) if pipeline is not None else None
        if all_rules is None and pipeline is not None:
            mappings_dict = getattr(pipeline, "mappings", None)
            if isinstance(mappings_dict, Mapping):
                all_rules = list(mappings_dict.values())
        if all_rules:
            for rule in all_rules:
                rule_dom = resolve_sdtm_domain(getattr(rule, "domain", None), fallback="")
                if rule_dom and rule_dom != domain_clean:
                    continue
                src_var = getattr(rule, "source_variable_name", "") or ""
                if rule.target_field and rule.target_field.strip().upper() == "USUBJID":
                    remaps_usubjid = True
                if (
                    rule.target_field
                    and src_var
                    and not self._is_exempt_key(rule.target_field)
                    and self._is_sensitive_key(src_var)
                ):
                    sensitive_targets.add(_collapse_alnum_key(rule.target_field))
        self.sensitive_mapped_targets = frozenset(sensitive_targets)
        self._sens_cache.clear()
        self.remaps_usubjid = remaps_usubjid
        self._usubjid_is_sensitive = self._is_sensitive_key("USUBJID")
        self._subject_key_is_sensitive = self._is_sensitive_key(
            "subject_key"
        ) or self._is_sensitive_key("subjectKey")
        self._topic_set = frozenset(self.topic_vars)
        self._compiled_std_vars: tuple[
            tuple[
                str,
                str,
                tuple[tuple[str, str], ...],
                tuple[str, ...],
                frozenset[str],
                tuple[str, ...],
                bool,
                bool,
                bool,
                bool,
                bool,
                bool,
            ],
            ...,
        ] = tuple(
            (
                vname,
                vname.lower(),
                tuple((c, c.lower()) for c in self.alias_map.get(vname, [])),
                tuple(self.alias_map.get(vname, [])),
                frozenset((vname.lower(), *(c.lower() for c in self.alias_map.get(vname, [])))),
                (vname.lower(), *(c.lower() for c in self.alias_map.get(vname, []))),
                vname in self.dtc_vars or vname.endswith("DTC"),
                _is_birth_dtc_key(vname),
                vname in self.flag_vars or vname.endswith("SET"),
                self._is_exempt_key(vname),
                self._is_sensitive_key(vname),
                vname in self._topic_set,
            )
            for vname in self.std_vars
            if vname not in ("STUDYID", "DOMAIN", "USUBJID", self.seq_var)
        )

    def _is_exempt_key(self, key: str) -> bool:
        """Cached exemption lookup for *key*."""
        res = self._exempt_cache.get(key)
        if res is None:
            res = self.registry.is_exempt(key)
            self._exempt_cache[key] = res
        return res

    def _is_sensitive_key(self, key: str) -> bool:
        """Return True when *key* is sensitive via active registry, study config, or mapping."""
        res = self._sens_cache.get(key)
        if res is not None:
            return res
        if self._is_exempt_key(key):
            self._sens_cache[key] = False
            return False
        if self.registry.is_sensitive(key):
            self._sens_cache[key] = True
            return True
        if self.collapsed_phi_keys or self.sensitive_mapped_targets:
            collapsed = _collapse_alnum_key(key)
            matched = (
                collapsed in self.collapsed_phi_keys or collapsed in self.sensitive_mapped_targets
            )
            self._sens_cache[key] = matched
            return matched
        self._sens_cache[key] = False
        return False

    def _apply_var_terminology(self, var_name: str, val: Any) -> Any:
        """Apply domain-qualified or variable-keyed StudyConfiguration terminology lookups."""
        if not self.terminology or val is None or isinstance(val, (dict, list, tuple)):
            return val
        str_val = str(val)
        domain_qual = f"{self.domain_clean}.{var_name}"
        if domain_qual in self.terminology and str_val in self.terminology[domain_qual]:
            return self.terminology[domain_qual][str_val]
        if var_name in self.terminology and str_val in self.terminology[var_name]:
            return self.terminology[var_name][str_val]
        return val

    def _unpack_record_payload(
        self,
        rec: Any,
    ) -> tuple[
        dict[str, Any],
        type[BaseModel] | None,
        dict[str, str],
        dict[str, set[str]],
        frozenset[str] | set[str],
        Any,
        Any,
        Any,
    ]:
        """Extract record dictionary, model metadata, and envelope identifiers."""
        model_cls: type[BaseModel] | None = None
        model_field_to_serialized: dict[str, str] = {}
        all_field_aliases: dict[str, set[str]] = {}
        base_consumed: frozenset[str] | set[str] = _BASE_SDTM_CONSUMED_KEYS
        raw_record_id: Any = None
        raw_visit_id: Any = None
        subj_fallback: Any = None

        if isinstance(rec, BaseModel):
            rec_ns = rec.__dict__
            raw_record_id = rec_ns.get("record_id")
            if raw_record_id is None:
                raw_record_id = rec_ns.get("recordId")
            raw_visit_id = rec_ns.get("visit_id")
            if raw_visit_id is None:
                raw_visit_id = rec_ns.get("visitId")
            subj_fallback = rec_ns.get("subject_key") or rec_ns.get("subjectKey")
            direct_rd = rec_ns.get("record_data")
            if direct_rd is not None:
                rec_dict = dict(direct_rd) if isinstance(direct_rd, Mapping) else {}
                if subj_fallback:
                    rec_dict.setdefault("subject_key", subj_fallback)
                    rec_dict.setdefault("subjectKey", subj_fallback)
            else:
                rec_dict = rec.model_dump(by_alias=True)
                model_cls = type(rec)
                model_field_to_serialized, all_field_aliases = _get_model_field_aliases(model_cls)
                base_consumed = _get_model_consumed_keys(model_cls)
        elif isinstance(rec, Mapping):
            rec_dict = dict(rec)
        else:
            raw_record_id = getattr(rec, "record_id", None)
            if raw_record_id is None:
                raw_record_id = getattr(rec, "recordId", None)
            raw_visit_id = getattr(rec, "visit_id", None)
            if raw_visit_id is None:
                raw_visit_id = getattr(rec, "visitId", None)
            subj_fallback = getattr(rec, "subject_key", None) or getattr(rec, "subjectKey", None)
            direct_rd = getattr(rec, "record_data", None)
            if direct_rd is None:
                direct_rd = getattr(rec, "recordData", None)
            if isinstance(direct_rd, Mapping):
                rec_dict = dict(direct_rd)
                if subj_fallback:
                    rec_dict.setdefault("subject_key", subj_fallback)
                    rec_dict.setdefault("subjectKey", subj_fallback)
            else:
                rec_dict = dict(getattr(rec, "__dict__", {}))

        for nested_key in ("record_data", "recordData"):
            if nested_key in rec_dict and isinstance(rec_dict[nested_key], Mapping):
                nested_data = dict(rec_dict.pop(nested_key))
                for k, v in nested_data.items():
                    if k not in rec_dict:
                        rec_dict[k] = v

        if raw_record_id is None:
            raw_record_id = self._resolve_fallback_record_id(rec, rec_dict)

        if raw_visit_id is None:
            for vk in ("VISITNUM", "visitnum", "visitId", "visit_id"):
                if vk in rec_dict and rec_dict[vk] is not None:
                    raw_visit_id = rec_dict[vk]
                    break

        return (
            rec_dict,
            model_cls,
            model_field_to_serialized,
            all_field_aliases,
            base_consumed,
            raw_record_id,
            raw_visit_id,
            subj_fallback,
        )

    @staticmethod
    def _resolve_fallback_record_id(rec: Any, rec_dict: dict[str, Any]) -> Any:
        """Resolve record identifier from unpacked dictionary or object attributes."""
        for key in ("recordId", "record_id", "RECORD_ID"):
            if key in rec_dict:
                return rec_dict.get(key)
        if isinstance(rec, BaseModel):
            return (
                rec.__dict__.get("record_oid")
                or rec.__dict__.get("recordOid")
                or rec_dict.get("recordOid")
                or rec_dict.get("record_oid")
            )
        if isinstance(rec, Mapping):
            return rec_dict.get("recordOid") or rec_dict.get("record_oid")
        return (
            getattr(rec, "record_oid", None)
            or getattr(rec, "recordOid", None)
            or rec_dict.get("recordOid")
            or rec_dict.get("record_oid")
        )

    @staticmethod
    def _resolve_candidate_value(
        var_name: str,
        var_lower: str,
        cand_pairs: tuple[tuple[str, str], ...],
        rec_dict: dict[str, Any],
        lower_rec_dict: dict[str, tuple[str, Any]],
        model_field_to_serialized: dict[str, str],
    ) -> tuple[str | None, Any]:
        """Match an SDTM variable or one of its aliases in *rec_dict*."""
        if var_name in rec_dict:
            return var_name, rec_dict[var_name]
        if var_lower in lower_rec_dict:
            return lower_rec_dict[var_lower]
        if (
            var_name in model_field_to_serialized
            and model_field_to_serialized[var_name] in rec_dict
        ):
            fk = model_field_to_serialized[var_name]
            return fk, rec_dict[fk]
        if (
            var_lower in model_field_to_serialized
            and model_field_to_serialized[var_lower] in rec_dict
        ):
            fk = model_field_to_serialized[var_lower]
            return fk, rec_dict[fk]
        for cand, cand_lower in cand_pairs:
            if cand in rec_dict:
                return cand, rec_dict[cand]
            if cand_lower in lower_rec_dict:
                return lower_rec_dict[cand_lower]
            if cand in model_field_to_serialized and model_field_to_serialized[cand] in rec_dict:
                fk = model_field_to_serialized[cand]
                return fk, rec_dict[fk]
            if (
                cand_lower in model_field_to_serialized
                and model_field_to_serialized[cand_lower] in rec_dict
            ):
                fk = model_field_to_serialized[cand_lower]
                return fk, rec_dict[fk]
        return None, None

    def _populate_std_vars(
        self,
        row: dict[str, Any],
        rec_dict: dict[str, Any],
        lower_rec_dict: dict[str, tuple[str, Any]],
        consumed_keys: set[str],
        unmasked_topic_map: dict[str, Any],
        model_cls: type[BaseModel] | None,
        model_field_to_serialized: dict[str, str],
        all_field_aliases: dict[str, set[str]],
    ) -> str | None:
        """Populate domain-standard SDTM variables on *row* and return primary sort DTC."""
        canonical_dtc_for_sort: str | None = None
        serialized_sets_fn = _get_model_var_serialized_set
        for spec_entry in self._compiled_std_vars:
            (
                var_name,
                var_lower,
                cand_pairs,
                cand_names,
                all_lower_set,
                all_lower_tuple,
                is_dtc,
                is_birth_dtc,
                is_flag,
                is_var_exempt,
                is_var_sensitive,
                is_topic,
            ) = spec_entry
            found_key: str | None = None
            val: Any = None
            if not all_lower_set.isdisjoint(lower_rec_dict) or (
                model_cls is not None
                and not serialized_sets_fn(model_cls, all_lower_tuple).isdisjoint(rec_dict)
            ):
                found_key, val = self._resolve_candidate_value(
                    var_name,
                    var_lower,
                    cand_pairs,
                    rec_dict,
                    lower_rec_dict,
                    model_field_to_serialized,
                )
                if found_key:
                    consumed_keys.add(found_key)
                    consumed_keys.update(cand_names)
                    if model_cls is not None:
                        consumed_keys.update(all_field_aliases.get(found_key, ()))
                        consumed_keys.update(_expand_model_var_consumed(model_cls, cand_names))

            if self.terminology:
                val = self._apply_var_terminology(var_name, val)
            if is_topic:
                unmasked_topic_map[var_name] = val

            should_mask_var = (
                self.mask_phi
                and not is_var_exempt
                and (
                    is_var_sensitive
                    or (found_key is not None and self._is_sensitive_key(found_key))
                )
            )
            if is_dtc:
                full_dtc = format_iso8601_dtc(val, mask_phi=False)
                if var_name == self.primary_dtc or (
                    canonical_dtc_for_sort is None and full_dtc is not None
                ):
                    canonical_dtc_for_sort = full_dtc
                if self.mask_phi and is_birth_dtc:
                    row[var_name] = format_iso8601_dtc(val, mask_phi=True)
                elif should_mask_var:
                    row[var_name] = None
                else:
                    row[var_name] = full_dtc
            elif is_flag:
                formatted_flag = _format_sdtm_flag(val)
                row[var_name] = (
                    _MASKED_SENTINEL
                    if (should_mask_var and formatted_flag is not None)
                    else formatted_flag
                )
            elif should_mask_var:
                row[var_name] = (
                    _MASKED_SENTINEL if (val is not None or found_key is not None) else None
                )
            else:
                row[var_name] = val
        return canonical_dtc_for_sort

    def _populate_extra_vars(
        self,
        row: dict[str, Any],
        rec_dict: dict[str, Any],
        consumed_keys: set[str],
        unmasked_topic_map: dict[str, Any],
        canonical_dtc_for_sort: str | None,
    ) -> str | None:
        """Preserve non-envelope extra fields on *row* with PHI masking and DTC formatting."""
        normalized_consumed = set(map(_normalize_key, consumed_keys))
        for k, raw_v in rec_dict.items():
            if (
                k in _BASE_SDTM_EXACT_CONSUMED_KEYS
                or k in consumed_keys
                or _normalize_key(k) in normalized_consumed
            ):
                continue
            extra_val = self._apply_var_terminology(k, raw_v) if self.terminology else raw_v
            if k in self._topic_set and k not in unmasked_topic_map:
                unmasked_topic_map[k] = extra_val
            if self.mask_phi and not self._is_exempt_key(k):
                if _is_birth_dtc_key(k):
                    if canonical_dtc_for_sort is None:
                        canonical_dtc_for_sort = format_iso8601_dtc(extra_val, mask_phi=False)
                    row[k] = format_iso8601_dtc(extra_val, mask_phi=True)
                    continue
                if self._is_sensitive_key(k):
                    row[k] = _MASKED_SENTINEL
                    continue
                if isinstance(extra_val, (dict, list, tuple)):
                    extra_val = _mask_value_recursive(
                        extra_val,
                        registry=self.registry,
                        collapsed_phi_keys=self.collapsed_phi_keys,
                    )
            k_lower = k.lower()
            if "date" in k_lower or "time" in k_lower or "dtc" in k_lower:
                formatted_extra_dtc = format_iso8601_dtc(extra_val, mask_phi=False)
                k_upper = k.upper()
                if k_upper == self.primary_dtc or (
                    canonical_dtc_for_sort is None
                    and k_upper.endswith("DTC")
                    and formatted_extra_dtc is not None
                ):
                    canonical_dtc_for_sort = formatted_extra_dtc
                row[k] = formatted_extra_dtc
            else:
                row[k] = extra_val
        return canonical_dtc_for_sort

    def prepare_record(self, rec: Any, orig_idx: int) -> _PreparedSdtmRow | None:
        """Normalize a single clinical record and compute its Subject-group sort key."""
        if not self.include_deleted and _is_deleted_record(rec):
            return None

        (
            rec_dict,
            model_cls,
            model_field_to_serialized,
            all_field_aliases,
            base_consumed,
            raw_record_id,
            raw_visit_id,
            subj_fallback,
        ) = self._unpack_record_payload(rec)

        if self.pipeline is not None and (
            self.has_enrichment_transforms or not hasattr(self.pipeline, "terminology")
        ):
            _, rec_form_key = extract_record_form_ref(rec)
            try:
                rec_dict = self.pipeline.process(
                    rec_dict,
                    mask_phi=False,
                    domain=self.domain_clean,
                    form_key=rec_form_key,
                )
            except TypeError:
                rec_dict = self.pipeline.process(rec_dict, mask_phi=False)

        explicit_usubjid = rec_dict.get("USUBJID") or rec_dict.get("usubjid")
        usubjid_raw = (
            explicit_usubjid
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
            or subj_fallback
            or ""
        )
        usubjid = str(usubjid_raw)
        if (
            self.mask_phi
            and usubjid
            and (
                self._usubjid_is_sensitive
                or (not explicit_usubjid and self._subject_key_is_sensitive)
            )
        ):
            usubjid = _MASKED_SENTINEL

        row: dict[str, Any] = {
            "STUDYID": self.study_key,
            "DOMAIN": self.domain_clean,
            "USUBJID": usubjid,
        }
        if self.include_seq:
            row[self.seq_var] = 0

        consumed_keys: set[str] = set(base_consumed)
        consumed_keys.update(("RECORD_ID", "VISITNUM", "visitnum"))
        if not self.include_seq:
            consumed_keys.update(("DMSEQ", "dmseq"))

        lower_rec_dict = {k.lower(): (k, v) for k, v in rec_dict.items()}
        unmasked_topic_map: dict[str, Any] = {}
        canonical_dtc_for_sort = self._populate_std_vars(
            row,
            rec_dict,
            lower_rec_dict,
            consumed_keys,
            unmasked_topic_map,
            model_cls,
            model_field_to_serialized,
            all_field_aliases,
        )
        canonical_dtc_for_sort = self._populate_extra_vars(
            row,
            rec_dict,
            consumed_keys,
            unmasked_topic_map,
            canonical_dtc_for_sort,
        )

        if self.domain_clean != "DM":
            explicit_vnum = (
                rec_dict.get("VISITNUM") if "VISITNUM" in rec_dict else rec_dict.get("visitnum")
            )
            norm_vnum = _normalize_positive_visitnum(
                explicit_vnum if explicit_vnum is not None else raw_visit_id
            )
            if norm_vnum is not None:
                row["VISITNUM"] = norm_vnum

        if self.preserve_record_id and raw_record_id is not None:
            row["RECORD_ID"] = raw_record_id

        record_id_key = (
            str(raw_record_id).strip()
            if raw_record_id is not None
            and not isinstance(raw_record_id, bool)
            and str(raw_record_id).strip()
            else None
        )
        topic_vals = tuple(map(unmasked_topic_map.get, self.topic_vars))
        sort_key = (
            _dtc_sort_key(canonical_dtc_for_sort),
            _topic_sort_key(topic_vals),
            _record_id_sort_key(raw_record_id),
            orig_idx,
        )
        return _PreparedSdtmRow(
            usubjid=usubjid,
            sort_key=sort_key,
            row=row,
            record_id=raw_record_id,
            record_id_key=record_id_key,
        )

    def finalize_subject_group(
        self,
        group: list[_PreparedSdtmRow],
        seq_counters: dict[str, int],
        record_seqs: dict[str, int] | None = None,
        *,
        seq_overrides: Mapping[Any, int] | None = None,
        lineage_out: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Sort a Subject's normalized rows and stamp sequential <DOM>SEQ values."""
        group.sort(key=lambda item: item.sort_key)
        overrides_has_scoped = _mapping_has_scoped_seq_keys(seq_overrides)
        record_seqs_has_scoped = _mapping_has_scoped_seq_keys(record_seqs)

        if self.include_seq and (seq_overrides or record_seqs):
            for item in group:
                rk = item.record_id_key or _canonical_record_id_key(item.record_id)
                if rk is not None:
                    ov_val = _lookup_scoped_seq_value(
                        seq_overrides,
                        domain=self.domain_clean,
                        usubjid=item.usubjid,
                        rec_id_key=rk,
                        has_scoped_keys=overrides_has_scoped,
                    )
                    if ov_val is not None:
                        seq_counters[item.usubjid] = max(
                            seq_counters.get(item.usubjid, 0),
                            ov_val,
                        )
                    rs_val = _lookup_scoped_seq_value(
                        record_seqs,
                        domain=self.domain_clean,
                        usubjid=item.usubjid,
                        rec_id_key=rk,
                        has_scoped_keys=record_seqs_has_scoped or overrides_has_scoped,
                    )
                    if rs_val is not None:
                        seq_counters[item.usubjid] = max(
                            seq_counters.get(item.usubjid, 0),
                            rs_val,
                        )
        rows: list[dict[str, Any]] = []
        for item in group:
            rec_id_key = item.record_id_key or _canonical_record_id_key(item.record_id)
            assigned_seq: int | None = None
            if self.include_seq:
                override_seq: int | None = None
                if rec_id_key is not None:
                    override_seq = _lookup_scoped_seq_value(
                        seq_overrides,
                        domain=self.domain_clean,
                        usubjid=item.usubjid,
                        rec_id_key=rec_id_key,
                        has_scoped_keys=overrides_has_scoped,
                    )
                    if override_seq is None:
                        override_seq = _lookup_scoped_seq_value(
                            record_seqs,
                            domain=self.domain_clean,
                            usubjid=item.usubjid,
                            rec_id_key=rec_id_key,
                            has_scoped_keys=record_seqs_has_scoped or overrides_has_scoped,
                        )

                if override_seq is not None:
                    assigned_seq = override_seq
                    seq_counters[item.usubjid] = max(
                        seq_counters.get(item.usubjid, 0),
                        assigned_seq,
                    )
                else:
                    assigned_seq = seq_counters.get(item.usubjid, 0) + 1
                    seq_counters[item.usubjid] = assigned_seq

                if record_seqs is not None and rec_id_key is not None:
                    scoped_rs_key = format_scoped_seq_override_key(
                        self.domain_clean, item.usubjid, rec_id_key
                    )
                    record_seqs[scoped_rs_key] = assigned_seq
                    record_seqs_has_scoped = True
                item.row[self.seq_var] = assigned_seq
            if lineage_out is not None and rec_id_key is not None:
                lineage_out[rec_id_key] = {
                    "study_id": self.study_key,
                    "domain": self.domain_clean,
                    "usubjid": item.usubjid,
                    "seq_col": self.seq_var if self.include_seq else None,
                    "seq_val": assigned_seq if self.include_seq else None,
                }
            rows.append(item.row)
        return rows

    def rows_to_dataframe(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        """Build a column-ordered SDTM DataFrame from finalized rows."""
        if not rows:
            empty_cols = (
                [*self.std_vars, "RECORD_ID"] if self.preserve_record_id else list(self.std_vars)
            )
            empty_df = pd.DataFrame(columns=empty_cols)
            empty_df.attrs["imednet_sdtm_redacted"] = bool(self.mask_phi)
            return empty_df
        df = pd.DataFrame(rows)
        cols = [c for c in self.std_vars if c in df.columns]
        extra_cols = [c for c in df.columns if c not in self.std_vars]
        out = df[cols + extra_cols]
        out.attrs["imednet_sdtm_redacted"] = bool(self.mask_phi)
        return out


def format_scoped_seq_override_key(domain: str, usubjid: str, record_id: str) -> str:
    """Format a canonical ``(domain, usubjid, record_id)`` key for ``seq_overrides``."""
    return f"{domain.strip().upper()}:{usubjid}:{record_id}"


def _mapping_has_scoped_seq_keys(mapping: Mapping[Any, int] | None) -> bool:
    """Return True when ``mapping`` contains tuple or ``domain:usubjid:record_id`` keys."""
    if not mapping:
        return False
    keys_iter = dict.keys(mapping) if isinstance(mapping, dict) else mapping.keys()
    return any(
        (isinstance(k, tuple) and len(k) == 3) or (isinstance(k, str) and k.count(":") >= 2)
        for k in keys_iter
    )


def _lookup_scoped_seq_value(
    mapping: Mapping[Any, int] | None,
    *,
    domain: str,
    usubjid: str,
    rec_id_key: str,
    has_scoped_keys: bool,
) -> int | None:
    """Look up a sequence override scoped by ``(domain, usubjid, record_id)``."""
    if not mapping:
        return None
    dom_clean = domain.strip().upper()
    tuple_key = (dom_clean, usubjid, rec_id_key)
    scoped_str_key = format_scoped_seq_override_key(dom_clean, usubjid, rec_id_key)

    if isinstance(mapping, dict):
        if dict.__contains__(mapping, tuple_key):
            return int(dict.__getitem__(mapping, tuple_key))
        if dict.__contains__(mapping, scoped_str_key):
            return int(dict.__getitem__(mapping, scoped_str_key))
        if not has_scoped_keys and dict.__contains__(mapping, rec_id_key):
            return int(dict.__getitem__(mapping, rec_id_key))
        return None

    if tuple_key in mapping:
        return int(mapping[tuple_key])
    if scoped_str_key in mapping:
        return int(mapping[scoped_str_key])
    if not has_scoped_keys and rec_id_key in mapping:
        return int(mapping[rec_id_key])
    return None


def _emit_subject_group_frames(
    emit_buffer: list[dict[str, Any]],
    subj_rows: list[dict[str, Any]],
    *,
    chunk_size: int,
    normalizer: _SdtmNormalizer,
) -> Iterator[pd.DataFrame]:
    """Append finalized rows for one Subject and yield chunk-bounded DataFrames without splitting Subjects unnecessarily."""
    if emit_buffer and len(emit_buffer) + len(subj_rows) > chunk_size:
        yield normalizer.rows_to_dataframe(list(emit_buffer))
        emit_buffer.clear()
    emit_buffer.extend(subj_rows)
    while len(emit_buffer) >= chunk_size:
        batch = emit_buffer[:chunk_size]
        del emit_buffer[:chunk_size]
        yield normalizer.rows_to_dataframe(batch)


def _iter_sorted_sdtm_frames(
    records: Iterable[Any],
    normalizer: _SdtmNormalizer,
    *,
    chunk_size: int,
    seq_counters: dict[str, int],
    source_is_ordered: bool = False,
    seq_overrides: Mapping[Any, int] | None = None,
    lineage_out: dict[str, dict[str, Any]] | None = None,
) -> Iterator[pd.DataFrame]:
    """Yield bounded SDTM DataFrames grouped by USUBJID with continuous sequence counters."""
    effective_ordered = bool(source_is_ordered and not normalizer.remaps_usubjid)
    if not effective_ordered:
        grouped: dict[str, list[_PreparedSdtmRow]] = {}
        for idx, rec in enumerate(records):
            prepared = normalizer.prepare_record(rec, idx)
            if prepared is not None:
                grouped.setdefault(prepared.usubjid, []).append(prepared)
        emit_rows: list[dict[str, Any]] = []
        for usubjid in sorted(grouped):
            subj_rows = normalizer.finalize_subject_group(
                grouped[usubjid],
                seq_counters,
                seq_overrides=seq_overrides,
                lineage_out=lineage_out,
            )
            yield from _emit_subject_group_frames(
                emit_rows,
                subj_rows,
                chunk_size=chunk_size,
                normalizer=normalizer,
            )
        if emit_rows:
            yield normalizer.rows_to_dataframe(emit_rows)
        return

    current_group: list[_PreparedSdtmRow] = []
    last_usubjid: str | None = None
    emit_buffer: list[dict[str, Any]] = []

    for orig_idx, rec in enumerate(records):
        prepared = normalizer.prepare_record(rec, orig_idx)
        if prepared is None:
            continue

        if last_usubjid is None:
            last_usubjid = prepared.usubjid
            current_group = [prepared]
            continue

        if prepared.usubjid == last_usubjid:
            current_group.append(prepared)
            continue

        if prepared.usubjid < last_usubjid:
            raise ValueError(
                f"Non-monotonic USUBJID order in SDTM stream with source_is_ordered=True: "
                f"{prepared.usubjid!r} arrived after {last_usubjid!r}."
            )

        subj_rows = normalizer.finalize_subject_group(
            current_group,
            seq_counters,
            seq_overrides=seq_overrides,
            lineage_out=lineage_out,
        )
        current_group = [prepared]
        last_usubjid = prepared.usubjid
        yield from _emit_subject_group_frames(
            emit_buffer,
            subj_rows,
            chunk_size=chunk_size,
            normalizer=normalizer,
        )

    if current_group:
        subj_rows = normalizer.finalize_subject_group(
            current_group,
            seq_counters,
            seq_overrides=seq_overrides,
            lineage_out=lineage_out,
        )
        yield from _emit_subject_group_frames(
            emit_buffer,
            subj_rows,
            chunk_size=chunk_size,
            normalizer=normalizer,
        )
    if emit_buffer:
        yield normalizer.rows_to_dataframe(emit_buffer)


def _resolve_sdtm_pipeline(
    study_key: str,
    pipeline: Any = _UNSET_PIPELINE,
    enrichment_pipeline: Any = _UNSET_PIPELINE,
) -> Any | None:
    """Resolve an EnrichmentPipeline from explicit args or StudyConfiguration."""
    if enrichment_pipeline is not _UNSET_PIPELINE:
        return enrichment_pipeline
    if pipeline is not _UNSET_PIPELINE:
        return pipeline
    from imednet.integrations.enrichment import resolve_enrichment_pipeline

    return resolve_enrichment_pipeline(study_key)


def iter_subject_ordered_sdtm_records(
    records: Iterable[Any],
    *,
    study_key: str,
    sdtm_domain: str | None = None,
    form_domain_map: Mapping[Any, str] | None = None,
    study_config: Any | None = None,
    pipeline: Any = _UNSET_PIPELINE,
    mask_phi: bool = True,
    source_is_ordered: bool = False,
) -> Iterator[Any]:
    """Yield active Study Records grouped by Subject and ordered chronologically per SDTM domain.

    When ``source_is_ordered`` is ``True`` (upstream pagination uses ``_SDTM_RECORDS_SORT``
    and ``StudyConfiguration`` does not remap ``USUBJID``), buffers at most one Subject's
    Records in memory at a time so streaming ``batch_size=1`` / ``chunk_size=1`` exports
    assign identical ``<DOM>SEQ`` values to full-batch runs.
    """
    resolved_pipeline = _resolve_sdtm_pipeline(study_key, pipeline)
    resolved_cfg = study_config
    if resolved_cfg is None and resolved_pipeline is not None:
        resolved_cfg = getattr(resolved_pipeline, "config", None)
    cfg_lookup = _extract_study_config_form_domains(
        study_config=resolved_cfg,
        study_key=study_key if resolved_cfg is None else None,
    )
    target_dom = (
        resolve_sdtm_domain(sdtm_domain, fallback="")
        if isinstance(sdtm_domain, str) and sdtm_domain.strip()
        else ""
    )

    normalizers: dict[str, _SdtmNormalizer] = {}

    def _get_normalizer(dom: str) -> _SdtmNormalizer:
        norm = normalizers.get(dom)
        if norm is None:
            norm = _SdtmNormalizer(
                domain=dom,
                study_key=study_key,
                include_deleted=True,
                mask_phi=mask_phi,
                pipeline=resolved_pipeline,
            )
            normalizers[dom] = norm
        return norm

    remaps_usubjid = False
    all_rules = (
        getattr(resolved_pipeline, "all_mappings", None) if resolved_pipeline is not None else None
    )
    if all_rules is None and resolved_pipeline is not None:
        mappings_dict = getattr(resolved_pipeline, "mappings", None)
        if isinstance(mappings_dict, Mapping):
            all_rules = list(mappings_dict.values())
    if all_rules:
        for rule in all_rules:
            if (
                getattr(rule, "target_field", None)
                and str(rule.target_field).strip().upper() == "USUBJID"
            ):
                remaps_usubjid = True
                break

    effective_ordered = bool(source_is_ordered and not remaps_usubjid)

    def _resolve_rec_domain(rec: Any) -> str:
        if target_dom:
            return target_dom
        dom = resolve_form_sdtm_domain(
            rec,
            form_domain_map=form_domain_map,
            _cfg_lookup=cfg_lookup,
            fallback="",
        )
        if dom:
            return dom
        _, fk = extract_record_form_ref(rec)
        return resolve_sdtm_domain(fk, fallback="AE")

    def _prepare_entry(rec: Any, idx: int) -> tuple[str, str, tuple[Any, ...], int, Any]:
        dom = _resolve_rec_domain(rec)
        norm = _get_normalizer(dom)
        prepared = norm.prepare_record(rec, idx)
        if prepared is not None:
            return (prepared.usubjid, dom, prepared.sort_key, idx, rec)
        usubj, rec_id_sk = _extract_stream_usubjid_and_id(rec)
        fallback_sk = ((1, ""), (), rec_id_sk, idx)
        return (usubj, dom, fallback_sk, idx, rec)

    def _sort_subject_entries(
        entries: list[tuple[str, str, tuple[Any, ...], int, Any]],
    ) -> Iterator[Any]:
        if len(entries) <= 1:
            for item in entries:
                yield item[4]
            return
        # Group per domain within Subject so each domain's records arrive in exact sort_key order
        by_dom: dict[str, list[tuple[str, str, tuple[Any, ...], int, Any]]] = {}
        dom_order: list[str] = []
        for entry in entries:
            d = entry[1]
            if d not in by_dom:
                by_dom[d] = []
                dom_order.append(d)
            by_dom[d].append(entry)
        if len(dom_order) == 1:
            single = by_dom[dom_order[0]]
            single.sort(key=lambda e: e[2])
            for item in single:
                yield item[4]
            return
        for d in dom_order:
            d_items = by_dom[d]
            d_items.sort(key=lambda e: e[2])
            for item in d_items:
                yield item[4]

    if not effective_ordered or isinstance(records, (list, tuple)):
        grouped_all: dict[str, list[tuple[str, str, tuple[Any, ...], int, Any]]] = {}
        for idx, rec in enumerate(records):
            entry = _prepare_entry(rec, idx)
            grouped_all.setdefault(entry[0], []).append(entry)
        for usubjid in sorted(grouped_all):
            yield from _sort_subject_entries(grouped_all[usubjid])
        return

    rec_iter = iter(records)
    current_usubjid: str | None = None
    current_entries: list[tuple[str, str, tuple[Any, ...], int, Any]] = []
    yielded_any = False
    idx = 0

    for rec in rec_iter:
        entry = _prepare_entry(rec, idx)
        idx += 1
        usubjid = entry[0]

        if current_usubjid is None:
            current_usubjid = usubjid
            current_entries = [entry]
            continue

        if usubjid == current_usubjid:
            current_entries.append(entry)
            continue

        if usubjid < current_usubjid:
            if not yielded_any:
                # Stream was not actually ordered by Subject: drain and sort all Subjects safely.
                fallback_groups: dict[str, list[tuple[str, str, tuple[Any, ...], int, Any]]] = {
                    current_usubjid: current_entries,
                }
                fallback_groups.setdefault(usubjid, []).append(entry)
                for tail_rec in rec_iter:
                    tail_entry = _prepare_entry(tail_rec, idx)
                    idx += 1
                    fallback_groups.setdefault(tail_entry[0], []).append(tail_entry)
                for subj_key in sorted(fallback_groups):
                    yield from _sort_subject_entries(fallback_groups[subj_key])
                return
            raise ValueError(
                f"Non-monotonic USUBJID order in SDTM stream with source_is_ordered=True: "
                f"{usubjid!r} arrived after {current_usubjid!r}."
            )

        finished = current_entries
        current_usubjid = usubjid
        current_entries = [entry]
        yielded_any = True
        yield from _sort_subject_entries(finished)

    if current_entries:
        yield from _sort_subject_entries(current_entries)


def get_sdtm_domain_variables(domain: str) -> list[str]:
    """Return standard uppercase SDTM variable list for ``domain``."""
    domain_clean = resolve_sdtm_domain(domain)
    spec = SDTM_DOMAIN_SPECS.get(domain_clean)
    if spec is not None:
        return list(spec["variables"])
    if domain_clean == "DM":
        return ["STUDYID", "DOMAIN", "USUBJID"]
    return ["STUDYID", "DOMAIN", "USUBJID", f"{domain_clean}SEQ"]


def transform_records_to_sdtm_rows(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
    mask_phi: bool = True,
    seq_counters: dict[str, int] | None = None,
    record_seqs: dict[str, int] | None = None,
    preserve_record_id: bool = False,
    seq_overrides: Mapping[str, int] | None = None,
    lineage_out: dict[str, dict[str, Any]] | None = None,
    phi_keys: Iterable[str] | None = None,
    registry: SensitivityRegistry | None = None,
    pipeline: Any = _UNSET_PIPELINE,
    enrichment_pipeline: Any = _UNSET_PIPELINE,
) -> list[dict[str, Any]]:
    """Transform clinical records into normalized CDISC SDTM row dictionaries.

    Applies ``SensitivityRegistry`` and custom ``phi_keys`` / ``EnrichmentPipeline``
    masking *before* constructing ``USUBJID`` or mapping SDTM domain variables,
    redacts masked ``BRTHDTC`` values to 4-digit ISO 8601 years or ``None``, sorts
    records per subject by ``(--DTC, topic_vars, recordId)``, omits ``DMSEQ`` on
    ``DM``, and maintains per-``USUBJID`` sequence counters across streaming batches
    when ``seq_counters`` is supplied.
    """
    resolved_pipeline = _resolve_sdtm_pipeline(study_key, pipeline, enrichment_pipeline)
    normalizer = _SdtmNormalizer(
        domain=domain,
        study_key=study_key,
        include_deleted=include_deleted,
        mask_phi=mask_phi,
        pipeline=resolved_pipeline,
        phi_keys=phi_keys,
        registry=registry,
        preserve_record_id=preserve_record_id,
    )
    counters = seq_counters if seq_counters is not None else {}

    grouped: dict[str, list[_PreparedSdtmRow]] = {}
    for idx, rec in enumerate(records):
        prepared = normalizer.prepare_record(rec, idx)
        if prepared is not None:
            grouped.setdefault(prepared.usubjid, []).append(prepared)

    rows: list[dict[str, Any]] = []
    for usubjid in sorted(grouped):
        rows.extend(
            normalizer.finalize_subject_group(
                grouped[usubjid],
                counters,
                record_seqs=record_seqs,
                seq_overrides=seq_overrides,
                lineage_out=lineage_out,
            )
        )
    return rows


def transform_records_to_sdtm(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
    mask_phi: bool = True,
    seq_counters: dict[str, int] | None = None,
    record_seqs: dict[str, int] | None = None,
    preserve_record_id: bool = False,
    seq_overrides: Mapping[str, int] | None = None,
    lineage_out: dict[str, dict[str, Any]] | None = None,
    phi_keys: Iterable[str] | None = None,
    registry: SensitivityRegistry | None = None,
    pipeline: Any = _UNSET_PIPELINE,
    enrichment_pipeline: Any = _UNSET_PIPELINE,
) -> pd.DataFrame:
    """Transform clinical records into a normalized CDISC SDTM domain DataFrame.

    Maps subject keys to USUBJID, study keys to STUDYID, sorts records per
    subject by canonical ``--DTC`` timestamp, domain topic variable, and record
    identifier, sequences records per subject (omitting ``DMSEQ`` on ``DM``),
    enforces PHI redaction and ``StudyConfiguration`` rules, and converts event
    timestamps to standard ISO 8601 DTC string formats.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for SDTM transformations. Install with "
            "'pip install \"imednet[export]\"'."
        )

    resolved_pipeline = _resolve_sdtm_pipeline(study_key, pipeline, enrichment_pipeline)
    normalizer = _SdtmNormalizer(
        domain=domain,
        study_key=study_key,
        include_deleted=include_deleted,
        mask_phi=mask_phi,
        pipeline=resolved_pipeline,
        phi_keys=phi_keys,
        registry=registry,
        preserve_record_id=preserve_record_id,
    )
    counters = seq_counters if seq_counters is not None else {}

    grouped: dict[str, list[_PreparedSdtmRow]] = {}
    for idx, rec in enumerate(records):
        prepared = normalizer.prepare_record(rec, idx)
        if prepared is not None:
            grouped.setdefault(prepared.usubjid, []).append(prepared)

    rows: list[dict[str, Any]] = []
    for usubjid in sorted(grouped):
        rows.extend(
            normalizer.finalize_subject_group(
                grouped[usubjid],
                counters,
                record_seqs=record_seqs,
                seq_overrides=seq_overrides,
                lineage_out=lineage_out,
            )
        )

    return normalizer.rows_to_dataframe(rows)


__all__ = [
    "SDTM_DOMAIN_SPECS",
    "VALID_NAMING_CONVENTIONS",
    "_SDTM_RECORDS_SORT",
    "_UNSET_PIPELINE",
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
    "iter_subject_ordered_sdtm_records",
    "merge_form_ids_into_record_filter",
    "normalize_naming_convention",
    "record_matches_sdtm_domain",
    "resolve_form_sdtm_domain",
    "resolve_sdtm_domain",
    "transform_records_to_sdtm",
    "transform_records_to_sdtm_rows",
]
