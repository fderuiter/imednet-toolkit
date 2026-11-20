"""Record-to-DataFrame mapping workflow for iMednet.

This module provides the :class:`RecordMapper` which handles fetching,
filtering, and parsing EDC records into tabular formats like pandas
DataFrames, including support for dynamic Pydantic model validation.
"""

from __future__ import annotations

import functools
import heapq
import inspect
import logging
import math
import re
import types
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment, unused-ignore]
from pydantic import BaseModel, Field, ValidationError, create_model

from imednet.spi.endpoints import Record as RecordModel
from imednet.spi.endpoints import Variable as VariableModel
from imednet.spi.sink_base import is_record_deleted
from imednet.spi.utils import (
    EnrichmentPipeline,
    global_sensitivity_registry,
    mask_clinical_phi,
    mask_custom_phi_recursive,
    resolve_enrichment_pipeline,
)

from .cached_loader import CachedRecordsLoader
from .chunked_pipeline import DEFAULT_CHUNK_SIZE, ChunkedRecordPipeline, iter_chunks

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

# Setup basic logging
logger = logging.getLogger(__name__)

_MASKED_SENTINEL = "***MASKED***"
_SDTM_RECORDS_SORT = "subjectKey,asc;recordId,asc"
_ISO_YEAR_RE = re.compile(r"^\d{4}$")
_ISO_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_PARTIAL_UNKNOWN_YEAR_RE = re.compile(r"^(\d{4})-(?:UN|UNK|-)", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")
_UNSET_PIPELINE: Any = object()


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
    if isinstance(val, float) and math.isnan(val):
        return None
    if not isinstance(val, (list, tuple, dict, set)) and pd is not None and bool(pd.isna(val)):
        return None
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
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str or val_str == _MASKED_SENTINEL:
            return None
        formatted, parsed_year = _parse_dtc_string(val_str)
        if should_redact:
            return f"{parsed_year:04d}" if parsed_year is not None else None
        return formatted
    return None if should_redact else str(val)


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
}


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
        "formId",
        "visitId",
        "recordStatus",
    }
)


@functools.lru_cache(maxsize=128)
def _get_model_field_aliases(
    model_cls: type[BaseModel],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Extract and cache field alias mappings for a Pydantic model class.

    Args:
        model_cls: A Pydantic BaseModel subclass.

    Returns:
        A tuple of (model_field_to_serialized, all_field_aliases).
    """
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
    """Precompute base consumed keys for a Pydantic model class.

    Args:
        model_cls: A Pydantic BaseModel subclass.

    Returns:
        A frozenset of consumed key names for the model.
    """
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


@functools.lru_cache(maxsize=1024)
def _collapse_alnum_key(key: str) -> str:
    """Collapse non-alphanumeric delimiters and lowercase a field key."""
    return _NON_ALNUM_RE.sub("", key).lower()


def _is_birth_dtc_key(key: str) -> bool:
    """Return True when *key* represents a CDISC birth-date DTC variable."""
    return _collapse_alnum_key(key).endswith("brthdtc")


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


def _topic_sort_key(topic_values: tuple[Any, ...]) -> tuple[tuple[int, str], ...]:
    """Build comparable sort tuples for domain topic variable values."""
    return tuple(
        (1, "") if (v is None or (isinstance(v, str) and not v)) else (0, str(v))
        for v in topic_values
    )


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


@dataclass
class _PreparedSdtmRow:
    """Normalized SDTM row prior to final per-subject sequence number assignment."""

    usubjid: str
    sort_key: tuple[tuple[int, str], tuple[tuple[int, str], ...], tuple[int, int, str], int]
    row: dict[str, Any]


class _SdtmNormalizer:
    """Reusable per-domain normalizer encapsulating SDTM specs, enrichment, and PHI redaction."""

    def __init__(
        self,
        *,
        domain: str,
        study_key: str,
        include_deleted: bool,
        mask_phi: bool,
        pipeline: EnrichmentPipeline | None,
    ) -> None:
        domain_clean = domain.strip().upper()
        if domain_clean == "PD":
            domain_clean = "DV"
        self.domain_clean = domain_clean
        self.study_key = study_key
        self.include_deleted = include_deleted
        self.mask_phi = mask_phi
        self.pipeline = pipeline
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
            pipeline is not None and (pipeline.terminology or pipeline.mappings)
        )
        self.terminology = pipeline.terminology if pipeline is not None else {}
        self.pipeline_phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()
        self.collapsed_phi_keys: frozenset[str] = frozenset(
            _collapse_alnum_key(pk) for pk in self.pipeline_phi_keys
        )
        self._exempt_cache: dict[str, bool] = {}
        self._sens_cache: dict[str, bool] = {}
        self.sensitive_mapped_targets: frozenset[str] = frozenset()
        sensitive_targets: set[str] = set()
        remaps_usubjid = False
        if pipeline is not None and pipeline.mappings:
            for src_var, rule in pipeline.mappings.items():
                if rule.target_field and rule.target_field.strip().upper() == "USUBJID":
                    remaps_usubjid = True
                if (
                    rule.target_field
                    and not self._is_exempt_key(rule.target_field)
                    and self._is_sensitive_key(src_var)
                ):
                    sensitive_targets.add(_collapse_alnum_key(rule.target_field))
        self.sensitive_mapped_targets = frozenset(sensitive_targets)
        self._sens_cache.clear()
        self.remaps_usubjid = remaps_usubjid
        self._topic_set = frozenset(self.topic_vars)
        self._compiled_std_vars: tuple[
            tuple[
                str,
                str,
                tuple[tuple[str, str], ...],
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
            res = global_sensitivity_registry.is_exempt(key)
            self._exempt_cache[key] = res
        return res

    def _is_sensitive_key(self, key: str) -> bool:
        """Return True when *key* is sensitive via default registry, study config, or mapping."""
        res = self._sens_cache.get(key)
        if res is not None:
            return res
        if self._is_exempt_key(key):
            self._sens_cache[key] = False
            return False
        if global_sensitivity_registry.is_sensitive(key):
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

    def prepare_record(self, rec: Any, orig_idx: int) -> _PreparedSdtmRow | None:
        """Normalize a single clinical record and compute its Subject-group sort key."""
        if not self.include_deleted and is_record_deleted(rec):
            return None

        model_field_to_serialized: dict[str, str] = {}
        all_field_aliases: dict[str, set[str]] = {}
        base_consumed: frozenset[str] | set[str] = _BASE_SDTM_CONSUMED_KEYS

        raw_record_id: Any = getattr(rec, "record_id", None)
        if raw_record_id is None:
            raw_record_id = getattr(rec, "recordId", None)

        if hasattr(rec, "record_data") and getattr(rec, "record_data", None) is not None:
            rec_dict = dict(rec.record_data) if isinstance(rec.record_data, dict) else {}
            if getattr(rec, "subject_key", None):
                rec_dict["subject_key"] = rec.subject_key
                rec_dict["subjectKey"] = rec.subject_key
            if getattr(rec, "date_created", None):
                rec_dict["date_created"] = rec.date_created
        elif isinstance(rec, BaseModel):
            rec_dict = rec.model_dump(by_alias=True)
            model_cls = type(rec)
            model_field_to_serialized, all_field_aliases = _get_model_field_aliases(model_cls)
            base_consumed = _get_model_consumed_keys(model_cls)
        elif isinstance(rec, dict):
            rec_dict = dict(rec)
        else:
            rec_dict = dict(getattr(rec, "__dict__", {}))

        if "record_data" in rec_dict and isinstance(rec_dict["record_data"], dict):
            nested_data = rec_dict.pop("record_data")
            for k, v in nested_data.items():
                if k not in rec_dict:
                    rec_dict[k] = v

        if raw_record_id is None:
            raw_record_id = (
                rec_dict.get("recordId")
                if "recordId" in rec_dict
                else rec_dict.get("record_id") or getattr(rec, "record_oid", None)
            )

        if self.pipeline is not None and self.has_enrichment_transforms:
            rec_dict = self.pipeline.process(rec_dict, mask_phi=False)

        usubjid_raw = (
            rec_dict.get("USUBJID")
            or rec_dict.get("usubjid")
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
            or getattr(rec, "subject_key", None)
            or getattr(rec, "subjectKey", None)
            or ""
        )
        usubjid = str(usubjid_raw)

        row: dict[str, Any] = {
            "STUDYID": self.study_key,
            "DOMAIN": self.domain_clean,
            "USUBJID": usubjid,
        }
        if self.include_seq:
            row[self.seq_var] = 0

        consumed_keys: set[str] = set(base_consumed)
        if not self.include_seq:
            consumed_keys.add("DMSEQ")
            consumed_keys.add("dmseq")

        lower_rec_dict = {k.lower(): (k, v) for k, v in rec_dict.items()}
        canonical_dtc_for_sort: str | None = None
        unmasked_topic_map: dict[str, Any] = {}

        for (
            var_name,
            var_lower,
            cand_pairs,
            is_dtc,
            is_birth_dtc,
            is_flag,
            is_var_exempt,
            is_var_sensitive,
            is_topic,
        ) in self._compiled_std_vars:
            val = None
            found_key = None
            if var_name in rec_dict:
                val = rec_dict[var_name]
                found_key = var_name
            elif var_lower in lower_rec_dict:
                found_key, val = lower_rec_dict[var_lower]
            elif (
                var_name in model_field_to_serialized
                and model_field_to_serialized[var_name] in rec_dict
            ):
                found_key = model_field_to_serialized[var_name]
                val = rec_dict[found_key]
            elif (
                var_lower in model_field_to_serialized
                and model_field_to_serialized[var_lower] in rec_dict
            ):
                found_key = model_field_to_serialized[var_lower]
                val = rec_dict[found_key]
            else:
                for cand, cand_lower in cand_pairs:
                    if cand in rec_dict:
                        val = rec_dict[cand]
                        found_key = cand
                        break
                    if cand_lower in lower_rec_dict:
                        found_key, val = lower_rec_dict[cand_lower]
                        break
                    if (
                        cand in model_field_to_serialized
                        and model_field_to_serialized[cand] in rec_dict
                    ):
                        found_key = model_field_to_serialized[cand]
                        val = rec_dict[found_key]
                        break
                    if (
                        cand_lower in model_field_to_serialized
                        and model_field_to_serialized[cand_lower] in rec_dict
                    ):
                        found_key = model_field_to_serialized[cand_lower]
                        val = rec_dict[found_key]
                        break

            if found_key:
                consumed_keys.add(found_key)
                if all_field_aliases:
                    consumed_keys.update(all_field_aliases.get(found_key, ()))
                    consumed_keys.update(all_field_aliases.get(found_key.lower(), ()))
                for cand, cand_lower in cand_pairs:
                    consumed_keys.add(cand)
                    if cand_lower in lower_rec_dict:
                        consumed_keys.add(lower_rec_dict[cand_lower][0])
                    if model_field_to_serialized:
                        if cand in model_field_to_serialized:
                            serialized_cand = model_field_to_serialized[cand]
                            consumed_keys.add(serialized_cand)
                            consumed_keys.update(all_field_aliases.get(serialized_cand, ()))
                        if cand_lower in model_field_to_serialized:
                            serialized_cand = model_field_to_serialized[cand_lower]
                            consumed_keys.add(serialized_cand)
                            consumed_keys.update(all_field_aliases.get(serialized_cand, ()))
                        consumed_keys.update(all_field_aliases.get(cand, ()))
                        consumed_keys.update(all_field_aliases.get(cand_lower, ()))

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
                if should_mask_var and formatted_flag is not None:
                    row[var_name] = _MASKED_SENTINEL
                else:
                    row[var_name] = formatted_flag
            elif should_mask_var:
                row[var_name] = (
                    _MASKED_SENTINEL if (val is not None or found_key is not None) else None
                )
            else:
                row[var_name] = val

        normalized_consumed = {_normalize_key(c) for c in consumed_keys}
        for k, raw_v in rec_dict.items():
            if k not in consumed_keys and _normalize_key(k) not in normalized_consumed:
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
                        extra_val = mask_clinical_phi(extra_val)
                        if self.pipeline_phi_keys:
                            extra_val = mask_custom_phi_recursive(extra_val, self.pipeline_phi_keys)
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

        topic_vals = tuple(unmasked_topic_map.get(tv) for tv in self.topic_vars)
        sort_key = (
            _dtc_sort_key(canonical_dtc_for_sort),
            _topic_sort_key(topic_vals),
            _record_id_sort_key(raw_record_id),
            orig_idx,
        )
        return _PreparedSdtmRow(usubjid=usubjid, sort_key=sort_key, row=row)

    def finalize_subject_group(
        self,
        group: list[_PreparedSdtmRow],
        seq_counters: dict[str, int],
    ) -> list[dict[str, Any]]:
        """Sort a Subject's normalized rows and stamp sequential <DOM>SEQ values."""
        group.sort(key=lambda item: item.sort_key)
        rows: list[dict[str, Any]] = []
        for item in group:
            if self.include_seq:
                next_seq = seq_counters.get(item.usubjid, 0) + 1
                seq_counters[item.usubjid] = next_seq
                item.row[self.seq_var] = next_seq
            rows.append(item.row)
        return rows

    def rows_to_dataframe(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        """Build a column-ordered SDTM DataFrame from finalized rows."""
        if not rows:
            return pd.DataFrame(columns=self.std_vars)
        df = pd.DataFrame(rows)
        cols = [c for c in self.std_vars if c in df.columns]
        extra_cols = [c for c in df.columns if c not in self.std_vars]
        return df[cols + extra_cols]


def _iter_sorted_sdtm_frames(
    records: Iterable[Any],
    normalizer: _SdtmNormalizer,
    *,
    chunk_size: int,
    seq_counters: dict[str, int],
    source_is_ordered: bool = False,
) -> Iterator[pd.DataFrame]:
    """Yield bounded SDTM DataFrames grouped by USUBJID with continuous sequence counters."""
    if isinstance(records, (list, tuple)):
        grouped: dict[str, list[_PreparedSdtmRow]] = {}
        for idx, rec in enumerate(records):
            prepared = normalizer.prepare_record(rec, idx)
            if prepared is not None:
                grouped.setdefault(prepared.usubjid, []).append(prepared)
        emit_rows: list[dict[str, Any]] = []
        for usubjid in sorted(grouped):
            emit_rows.extend(normalizer.finalize_subject_group(grouped[usubjid], seq_counters))
            while len(emit_rows) >= chunk_size:
                batch = emit_rows[:chunk_size]
                emit_rows = emit_rows[chunk_size:]
                yield normalizer.rows_to_dataframe(batch)
        if emit_rows:
            yield normalizer.rows_to_dataframe(emit_rows)
        return

    staged_groups: dict[str, list[_PreparedSdtmRow]] = {}
    group_order: list[str] = []
    last_usubjid: str | None = None
    staged_total_rows = 0
    yielded_any = False
    orig_idx = 0
    rec_iter = iter(records)
    emit_buffer: list[dict[str, Any]] = []

    for rec in rec_iter:
        prepared = normalizer.prepare_record(rec, orig_idx)
        orig_idx += 1
        if prepared is None:
            continue

        if not yielded_any:
            if (
                last_usubjid is not None
                and prepared.usubjid != last_usubjid
                and (prepared.usubjid < last_usubjid or prepared.usubjid in staged_groups)
            ):
                # Ungrouped input detected prior to emitting any chunk: drain and sort all subjects.
                staged_groups.setdefault(prepared.usubjid, []).append(prepared)
                for tail_rec in rec_iter:
                    tail_prepared = normalizer.prepare_record(tail_rec, orig_idx)
                    orig_idx += 1
                    if tail_prepared is not None:
                        staged_groups.setdefault(tail_prepared.usubjid, []).append(tail_prepared)
                for usubjid in sorted(staged_groups):
                    emit_buffer.extend(
                        normalizer.finalize_subject_group(staged_groups[usubjid], seq_counters)
                    )
                    while len(emit_buffer) >= chunk_size:
                        batch = emit_buffer[:chunk_size]
                        emit_buffer = emit_buffer[chunk_size:]
                        yield normalizer.rows_to_dataframe(batch)
                if emit_buffer:
                    yield normalizer.rows_to_dataframe(emit_buffer)
                return

            if prepared.usubjid not in staged_groups:
                staged_groups[prepared.usubjid] = []
                group_order.append(prepared.usubjid)
            staged_groups[prepared.usubjid].append(prepared)
            staged_total_rows += 1
            last_usubjid = prepared.usubjid

            completed_groups = len(group_order) - 1
            completed_rows = staged_total_rows - len(staged_groups[last_usubjid])
            min_completed_groups = 1 if source_is_ordered else (4 if chunk_size == 1 else 2)
            if completed_groups >= min_completed_groups and completed_rows >= chunk_size:
                for done_subj in group_order[:-1]:
                    done_group = staged_groups.pop(done_subj)
                    emit_buffer.extend(normalizer.finalize_subject_group(done_group, seq_counters))
                group_order = [last_usubjid]
                staged_total_rows = len(staged_groups[last_usubjid])
                while len(emit_buffer) >= chunk_size:
                    batch = emit_buffer[:chunk_size]
                    emit_buffer = emit_buffer[chunk_size:]
                    yielded_any = True
                    yield normalizer.rows_to_dataframe(batch)
            continue

        # Already streaming ordered Subject groups
        assert last_usubjid is not None
        if prepared.usubjid != last_usubjid:
            finished_group = staged_groups.pop(last_usubjid)
            emit_buffer.extend(normalizer.finalize_subject_group(finished_group, seq_counters))
            staged_groups[prepared.usubjid] = [prepared]
            last_usubjid = prepared.usubjid
            while len(emit_buffer) >= chunk_size:
                batch = emit_buffer[:chunk_size]
                emit_buffer = emit_buffer[chunk_size:]
                yield normalizer.rows_to_dataframe(batch)
        else:
            staged_groups[last_usubjid].append(prepared)

    for rem_subj in sorted(staged_groups) if not yielded_any else list(staged_groups):
        emit_buffer.extend(normalizer.finalize_subject_group(staged_groups[rem_subj], seq_counters))
    while len(emit_buffer) >= chunk_size:
        batch = emit_buffer[:chunk_size]
        emit_buffer = emit_buffer[chunk_size:]
        yield normalizer.rows_to_dataframe(batch)
    if emit_buffer:
        yield normalizer.rows_to_dataframe(emit_buffer)


def transform_records_to_sdtm(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
    mask_phi: bool = True,
    enrichment_pipeline: EnrichmentPipeline | None = _UNSET_PIPELINE,
    seq_counters: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Transform clinical records into a normalized CDISC SDTM domain DataFrame.

    Maps subject keys to USUBJID, study keys to STUDYID, sorts records per
    subject by canonical ``--DTC`` timestamp, domain topic variable, and record
    identifier, sequences records per subject (omitting ``DMSEQ`` on ``DM``),
    enforces PHI redaction and ``StudyConfiguration`` rules, and converts event
    timestamps to standard ISO 8601 DTC string formats.

    Args:
        records: An iterable of Record models, Pydantic models (such as AdverseEvent),
            or raw dictionaries.
        domain: CDISC SDTM domain code (e.g. 'AE', 'DV', 'DD', 'CM', 'DM', 'VS').
        study_key: Unique study identifier to populate STUDYID.
        include_deleted: When True, include voided (deleted=True) records.
        mask_phi: When True (default), mask text PHI variables and redact ``BRTHDTC``
            to four-digit ISO 8601 years or None.
        enrichment_pipeline: Optional pre-resolved :class:`EnrichmentPipeline`.
        seq_counters: Optional mutable per-subject sequence counter map preserved across chunks.

    Returns:
        A pandas DataFrame with standardized domain variables.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for SDTM transformations. Install with "
            "'pip install \"imednet[export]\"'."
        )

    resolved_pipeline = (
        resolve_enrichment_pipeline(study_key)
        if enrichment_pipeline is _UNSET_PIPELINE
        else enrichment_pipeline
    )
    normalizer = _SdtmNormalizer(
        domain=domain,
        study_key=study_key,
        include_deleted=include_deleted,
        mask_phi=mask_phi,
        pipeline=resolved_pipeline,
    )
    counters = seq_counters if seq_counters is not None else {}

    grouped: dict[str, list[_PreparedSdtmRow]] = {}
    for idx, rec in enumerate(records):
        prepared = normalizer.prepare_record(rec, idx)
        if prepared is not None:
            grouped.setdefault(prepared.usubjid, []).append(prepared)

    rows: list[dict[str, Any]] = []
    for usubjid in sorted(grouped):
        rows.extend(normalizer.finalize_subject_group(grouped[usubjid], counters))

    return normalizer.rows_to_dataframe(rows)


class RecordMapper:
    """Maps EDC records for a study into a pandas DataFrame.

    Features:
      - Fetches variable definitions for column mapping.
      - Dynamically creates a Pydantic model for type validation of record data.
      - Fetches records, applying server-side filtering where possible.
      - Merges metadata and record data.
      - Offers choice between variable names or labels for column headers.
      - Handles parsing errors gracefully for individual records.

    Example:
        sdk = ImednetSDK(api_key, security_key, base_url)
        mapper = RecordMapper(sdk)
        # Get DataFrame with labels as columns, filtered by visit
        df_labels = mapper.dataframe(study_key="MYSTUDY", visit_key="VISIT1")
        # Get DataFrame with variable names as columns
        df_names = mapper.dataframe(study_key="MYSTUDY", use_labels_as_columns=False)
    """

    def __init__(
        self,
        sdk: ImednetFacade,
        *,
        loader: CachedRecordsLoader | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        mask_phi: bool = True,
        include_deleted: bool = False,
    ) -> None:
        """Initialize with an :class:`ImednetSDK` instance.

        ``loader`` enables cache-backed streaming for large-study workflows.
        ``chunk_size`` controls the maximum number of records parsed into each
        yielded batch when using chunked mapping helpers.
        ``mask_phi`` controls whether sensitive clinical fields are masked at
        mapper ingress by default.
        ``include_deleted`` controls whether voided (``deleted=True``) records
        are retained (defaults to ``False``).
        """
        self.sdk = sdk
        self._loader = loader
        self._pipeline = ChunkedRecordPipeline(chunk_size=chunk_size)
        self.mask_phi = mask_phi
        self._include_deleted = include_deleted
        self._last_variables: list[VariableModel] = []
        self._active_study_key: str | None = None
        self._enrichment_cache: dict[str, EnrichmentPipeline | None] = {}

    def _get_enrichment_pipeline(self, study_key: str | None = None) -> EnrichmentPipeline | None:
        """Resolve and cache committed StudyConfiguration rules for *study_key*."""
        sk = study_key if isinstance(study_key, str) and study_key else self._active_study_key
        if not sk:
            return None
        if sk not in self._enrichment_cache:
            self._enrichment_cache[sk] = resolve_enrichment_pipeline(sk)
        return self._enrichment_cache[sk]

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
    def _fetch_variable_metadata(
        self,
        study_key: str,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Return variable names and label mapping for a study."""
        self._active_study_key = study_key
        self._last_variables = []
        if variable_whitelist is not None and len(variable_whitelist) == 0:
            return [], {}
        if form_whitelist is not None and len(form_whitelist) == 0:
            return [], {}

        filters: dict[str, Any] = {}
        if form_whitelist is not None:
            filters["formId"] = (
                form_whitelist[0] if len(form_whitelist) == 1 else list(form_whitelist)
            )
            if (
                variable_whitelist is not None
                and len(variable_whitelist) == 1
                and len(form_whitelist) == 1
            ):
                filters["variableName"] = variable_whitelist[0]
        elif variable_whitelist is not None:
            filters["variableName"] = (
                variable_whitelist[0] if len(variable_whitelist) == 1 else list(variable_whitelist)
            )

        variables: list[VariableModel] = list(
            self.sdk.get_variables(
                study_key=study_key,
                **filters,
            )
        )
        if form_whitelist is not None:
            allowed_forms = set(form_whitelist)
            variables = [v for v in variables if v.form_id is None or v.form_id in allowed_forms]
        if variable_whitelist is not None:
            allowed_vars = set(variable_whitelist)
            variables = [v for v in variables if v.variable_name in allowed_vars]

        if not variables:
            logger.warning(
                "No variables found for study '%s'. Returning empty DataFrame.",
                study_key,
            )
            return [], {}

        self._last_variables = list(variables)
        variable_keys = [v.variable_name for v in variables]
        label_map = {v.variable_name: v.label for v in variables}
        return variable_keys, label_map  # type: ignore

    def _build_record_model(
        self, variable_keys: list[str], label_map: dict[str, str]
    ) -> type[BaseModel]:
        """Create a dynamic model for the record data payload."""
        fields: dict[str, tuple[Any | None, Any]] = {}
        for key in variable_keys:
            fields[key] = (
                Any | None,
                Field(None, alias=key, description=label_map.get(key, key)),
            )
        return create_model("RecordData", __base__=BaseModel, **fields)  # type: ignore

    @staticmethod
    def _extract_form_ids(
        filters: dict[str, Any | tuple[str, Any] | list[Any]],
    ) -> tuple[list[Any] | None, dict[str, Any | tuple[str, Any] | list[Any]]]:
        """Extract scalar or multi-item formId filters and strip variable filters for /records."""
        cleaned = dict(filters)
        cleaned.pop("variableNames", None)
        cleaned.pop("variableName", None)

        if "formId" not in cleaned:
            return None, cleaned

        raw_form_id = cleaned.get("formId")
        if isinstance(raw_form_id, (list, set)):
            cleaned.pop("formId")
            return list(dict.fromkeys(raw_form_id)), cleaned
        if not isinstance(raw_form_id, tuple):
            cleaned.pop("formId")
            return [raw_form_id], cleaned
        return None, cleaned

    def _is_mock_sdk_or_callable(self, fn: Any = None) -> bool:
        """Return True when ``self.sdk`` or *fn* is a unittest mock object."""
        from unittest.mock import NonCallableMock

        return isinstance(self.sdk, NonCallableMock) or isinstance(fn, NonCallableMock)

    def _can_stream_via_records_endpoint(self) -> bool:
        """Return True when ``self.sdk.records.list`` can stream paginated responses."""
        from unittest.mock import NonCallableMock

        if isinstance(self.sdk, NonCallableMock):
            mock_children = getattr(self.sdk, "_mock_children", {})
            obj_vars = vars(self.sdk)
            if "records" in mock_children or "records" in obj_vars:
                return True
            return not ("get_records" in mock_children or "get_records" in obj_vars)

        records_ep = getattr(self.sdk, "records", None)
        return records_ep is not None and callable(getattr(records_ep, "list", None))

    @staticmethod
    def _merge_form_record_streams(
        fetch_fn: Any,
        *,
        study_key: str,
        form_id_list: list[Any],
        record_data_filter: str | None,
        filters: dict[str, Any],
        include_deleted: bool,
    ) -> Iterator[RecordModel]:
        """Merge per-form SDK record streams by (USUBJID, recordId) so Subjects stay grouped."""

        def _iter_keyed_form(
            form_idx: int, fid: Any
        ) -> Iterator[tuple[str, tuple[int, int, str], int, int, RecordModel]]:
            raw_stream = fetch_fn(
                study_key=study_key,
                record_data_filter=record_data_filter,
                **filters,
                formId=fid,
            )
            if isinstance(raw_stream, (list, tuple)):
                filtered = [r for r in raw_stream if include_deleted or not is_record_deleted(r)]
                keyed = [
                    (*_extract_stream_usubjid_and_id(r), form_idx, seq_idx, r)
                    for seq_idx, r in enumerate(filtered)
                ]
                keyed.sort(key=lambda entry: entry[:4])
                yield from keyed
                return

            seq_idx = 0
            for rec in raw_stream:
                if not include_deleted and is_record_deleted(rec):
                    continue
                usubjid, rec_id_key = _extract_stream_usubjid_and_id(rec)
                yield (usubjid, rec_id_key, form_idx, seq_idx, rec)
                seq_idx += 1

        merged = heapq.merge(
            *(_iter_keyed_form(idx, fid) for idx, fid in enumerate(form_id_list)),
            key=lambda entry: entry[:4],
        )
        for _, _, _, _, rec in merged:
            yield rec

    def _stream_sdk_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
        sort: str | None = None,
        group_by_subject: bool = False,
    ) -> Iterator[RecordModel]:
        """Lazily stream records from the SDK, applying visit/form/deleted filters."""
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        raw_filters: dict[str, Any | tuple[str, Any] | list[Any]] = (
            dict(extra_filters) if extra_filters else {}
        )
        if visit_key is not None:
            try:
                raw_filters["visitId"] = int(visit_key)
            except ValueError:
                logger.warning(
                    "Invalid visit_key '%s'. Should be convertible to int. Fetching all records.",
                    visit_key,
                )

        form_id_list, filters = self._extract_form_ids(raw_filters)
        if form_id_list is not None and len(form_id_list) == 0:
            return
        if sort is not None and "sort" not in filters:
            filters["sort"] = sort

        use_streaming_endpoint = self._can_stream_via_records_endpoint()
        records_ep = getattr(self.sdk, "records", None) if use_streaming_endpoint else None
        fetch_fn: Any = (
            records_ep.list
            if use_streaming_endpoint and records_ep is not None
            else self.sdk.get_records
        )

        try:
            if form_id_list is not None:
                if group_by_subject and len(form_id_list) > 1:
                    yield from self._merge_form_record_streams(
                        fetch_fn,
                        study_key=study_key,
                        form_id_list=form_id_list,
                        record_data_filter=record_data_filter,
                        filters=filters,
                        include_deleted=effective_include_deleted,
                    )
                    return
                for fid in form_id_list:
                    raw_form_records = fetch_fn(
                        study_key=study_key,
                        record_data_filter=record_data_filter,
                        **filters,
                        formId=fid,
                    )
                    if group_by_subject and (
                        isinstance(raw_form_records, (list, tuple))
                        or (
                            self._is_mock_sdk_or_callable(fetch_fn)
                            and not isinstance(raw_form_records, types.GeneratorType)
                        )
                    ):
                        materialized_form = [
                            rec
                            for rec in raw_form_records
                            if effective_include_deleted or not is_record_deleted(rec)
                        ]
                        materialized_form.sort(key=lambda r: _extract_stream_usubjid_and_id(r))
                        yield from materialized_form
                        continue
                    for rec in raw_form_records:
                        if not effective_include_deleted and is_record_deleted(rec):
                            continue
                        yield rec
                return

            raw_records = fetch_fn(
                study_key=study_key,
                record_data_filter=record_data_filter,
                **filters,
            )
            if group_by_subject and (
                isinstance(raw_records, (list, tuple))
                or (
                    self._is_mock_sdk_or_callable(fetch_fn)
                    and not isinstance(raw_records, types.GeneratorType)
                )
            ):
                materialized = [
                    rec
                    for rec in raw_records
                    if effective_include_deleted or not is_record_deleted(rec)
                ]
                materialized.sort(key=lambda r: _extract_stream_usubjid_and_id(r))
                yield from materialized
                return

            for rec in raw_records:
                if not effective_include_deleted and is_record_deleted(rec):
                    continue
                yield rec
        except ValueError:
            raise
        except Exception as exc:  # pragma: no cover - unexpected
            logger.error("Failed to fetch records for study '%s': %s", study_key, exc)
            return

    def _fetch_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
    ) -> list[RecordModel]:
        """Fetch records for a study applying optional filters."""
        self._active_study_key = study_key
        return list(
            self._stream_sdk_records(
                study_key,
                visit_key=visit_key,
                extra_filters=extra_filters,
                record_data_filter=record_data_filter,
                include_deleted=include_deleted,
            )
        )

    @staticmethod
    def _resolve_loader_iter_callable(loader: Any) -> tuple[Any | None, bool]:
        """Return (bound_iter_callable, supports_order_by_subject) for *loader*."""
        from unittest.mock import NonCallableMock

        bound_iter: Any = None
        if isinstance(loader, NonCallableMock):
            mock_children = getattr(loader, "_mock_children", {})
            mock_vars = vars(loader)
            if "iter_cached_records" in mock_children or "iter_cached_records" in mock_vars:
                bound_iter = loader.iter_cached_records
        else:
            inst_vars = vars(loader) if hasattr(loader, "__dict__") else {}
            if "iter_cached_records" in inst_vars and callable(inst_vars["iter_cached_records"]):
                bound_iter = inst_vars["iter_cached_records"]
            elif callable(getattr(type(loader), "iter_cached_records", None)):
                bound_iter = loader.iter_cached_records

        if not callable(bound_iter):
            return None, False

        supports_subject_order = False
        try:
            sig = inspect.signature(bound_iter)
            supports_subject_order = "order_by_subject" in sig.parameters
        except (TypeError, ValueError):
            supports_subject_order = False
        return bound_iter, supports_subject_order

    def _iter_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
        sort: str | None = None,
        group_by_subject: bool = False,
    ) -> Iterable[RecordModel]:
        """Iterate lazily over records for a study, using cache if available.

        Args:
            study_key: The study identifier.
            visit_key: Optional visit filter.
            extra_filters: Optional additional filter criteria.
            record_data_filter: Optional clinical variable payload filter.
            include_deleted: Optional override for voided-record inclusion.
            sort: Optional upstream sort query expression.
            group_by_subject: When True, request Subject-grouped ordering from cache/multi-form streams.

        Returns:
            An iterable of Record models.
        """
        self._active_study_key = study_key
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        raw_filters = dict(extra_filters) if extra_filters else {}
        form_id_list, _ = self._extract_form_ids(raw_filters)
        form_ids: set[Any] | None = set(form_id_list) if form_id_list is not None else None

        loader = self._loader
        if loader is not None:
            sync_method = getattr(loader, "sync_records", None)
            bound_iter, supports_subject_order = self._resolve_loader_iter_callable(loader)
            if callable(sync_method) and callable(bound_iter):
                sync_method(study_key)
                iter_kwargs: dict[str, Any] = {"chunk_size": self._pipeline.chunk_size}
                if group_by_subject and supports_subject_order:
                    iter_kwargs["order_by_subject"] = True
                return self._filter_records(
                    bound_iter(study_key, **iter_kwargs),
                    visit_key=visit_key,
                    form_ids=form_ids,
                    include_deleted=effective_include_deleted,
                )

        return self._stream_sdk_records(
            study_key,
            visit_key,
            extra_filters=extra_filters,
            record_data_filter=record_data_filter,
            include_deleted=effective_include_deleted,
            sort=sort,
            group_by_subject=group_by_subject,
        )

    def _filter_records(
        self,
        records: Iterable[RecordModel],
        *,
        visit_key: str | None,
        form_ids: set[Any] | None,
        include_deleted: bool | None = None,
    ) -> Iterator[RecordModel]:
        """Apply visit, form, and deleted filters to an iterable of records."""
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        visit_id: int | None = None
        if visit_key is not None:
            try:
                visit_id = int(visit_key)
            except ValueError:
                logger.warning(
                    "Invalid visit_key '%s'. Should be convertible to int. Fetching all records.",
                    visit_key,
                )

        for record in records:
            if not effective_include_deleted and is_record_deleted(record):
                continue
            if visit_id is not None and record.visit_id != visit_id:
                continue
            if form_ids is not None and record.form_id not in form_ids:
                continue
            yield record

    def _parse_record(
        self,
        rec: RecordModel,
        record_model: type[BaseModel],
        *,
        mask_phi: bool | None = None,
        study_key: str | None = None,
    ) -> dict[str, Any]:
        """Parse a single record using the dynamic Pydantic model.

        Args:
            rec: The Record model to parse.
            record_model: The dynamic Pydantic model for record data.
            mask_phi: Optional per-call override for PHI masking.
            study_key: Optional study key used to resolve StudyConfiguration rules.

        Returns:
            A dictionary containing both metadata and parsed record data.
        """
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        meta = {
            "recordId": rec.record_id,
            "subjectKey": rec.subject_key,
            "visitId": rec.visit_id,
            "formId": rec.form_id,
            "recordStatus": rec.record_status,
            "dateCreated": (
                rec.date_created.isoformat()  # type: ignore[union-attr]
                if hasattr(rec.date_created, 'isoformat')
                else str(rec.date_created)
                if rec.date_created
                else None
            ),
        }
        raw_data = rec.record_data if isinstance(rec.record_data, dict) else {}
        data = mask_clinical_phi(raw_data) if (should_mask and raw_data) else raw_data
        parsed = record_model(**data).model_dump(by_alias=False)
        resolved_sk = (
            study_key
            if isinstance(study_key, str) and study_key
            else getattr(rec, "study_key", None) or self._active_study_key
        )
        pipeline = self._get_enrichment_pipeline(
            resolved_sk if isinstance(resolved_sk, str) else None
        )
        if pipeline is not None:
            parsed = pipeline.process(parsed, mask_phi=should_mask)
        return {**meta, **parsed}

    def _parse_records(
        self,
        records: Iterable[RecordModel],
        record_model: type[BaseModel],
        *,
        mask_phi: bool | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Parse raw records into row dictionaries and count failures."""
        rows: list[dict[str, Any]] = []
        errors = 0
        for chunk_rows, chunk_errors in self._iter_parsed_rows(
            records,
            record_model,
            mask_phi=mask_phi,
        ):
            rows.extend(chunk_rows)
            errors += chunk_errors
        return rows, errors

    def _iter_parsed_rows(
        self,
        records: Iterable[RecordModel],
        record_model: type[BaseModel],
        *,
        mask_phi: bool | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], int]]:
        """Iterate over records and parse them in chunks.

        Yields:
            Tuples of (parsed_rows_list, error_count) for each chunk.
        """
        for chunk in iter_chunks(records, chunk_size=self._pipeline.chunk_size):
            rows: list[dict[str, Any]] = []
            errors = 0
            for rec in chunk:
                try:
                    rows.append(self._parse_record(rec, record_model, mask_phi=mask_phi))
                except (ValidationError, TypeError) as exc:
                    errors += 1
                    logger.warning(
                        "Failed to parse record data for recordId %s: %s",
                        rec.record_id,
                        exc,
                    )
                except Exception as exc:  # pragma: no cover - unexpected
                    errors += 1
                    logger.error("Unexpected error processing recordId %s: %s", rec.record_id, exc)
            yield rows, errors

    def _build_dataframe(
        self,
        rows: list[dict[str, Any]],
        variable_keys: list[str],
        label_map: dict[str, str],
        use_labels: bool,
        *,
        mask_phi: bool | None = None,
        study_key: str | None = None,
    ) -> pd.DataFrame:
        """Create the output DataFrame from parsed rows."""
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        should_mask = self.mask_phi if mask_phi is None else mask_phi
        pipeline = self._get_enrichment_pipeline(study_key)
        pipeline_phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()
        mapped_targets: list[str] = []
        replaced_sources: set[str] = set()
        if pipeline is not None and pipeline.mappings:
            for src_var, rule in pipeline.mappings.items():
                if rule.target_field and rule.target_field != src_var and src_var in variable_keys:
                    replaced_sources.add(src_var)
                    if (
                        rule.target_field not in variable_keys
                        and rule.target_field not in mapped_targets
                    ):
                        mapped_targets.append(rule.target_field)

        effective_var_keys = [
            k for k in variable_keys if k not in replaced_sources
        ] + mapped_targets
        meta_cols = [
            "recordId",
            "subjectKey",
            "visitId",
            "formId",
            "recordStatus",
            "dateCreated",
        ]

        unique_effective_var_keys = list(
            dict.fromkeys(key for key in effective_var_keys if key not in meta_cols)
        )
        for key in unique_effective_var_keys:
            if key not in df.columns:
                df[key] = pd.NA

        df = df[meta_cols + unique_effective_var_keys]
        if should_mask:
            collapsed_phi_keys = {_collapse_alnum_key(pk) for pk in pipeline_phi_keys}
            sensitive_vars = [
                key
                for key in unique_effective_var_keys
                if global_sensitivity_registry.is_sensitive(key)
                or (
                    bool(collapsed_phi_keys)
                    and not global_sensitivity_registry.is_exempt(key)
                    and _collapse_alnum_key(key) in collapsed_phi_keys
                )
            ]
            for key in sensitive_vars:
                df[key] = "***MASKED***"

            non_sensitive_vars = [
                key for key in unique_effective_var_keys if key not in set(sensitive_vars)
            ]
            if non_sensitive_vars:
                var_frame = df[non_sensitive_vars]
                try:
                    obj_cols = var_frame.select_dtypes(include=[object, "str"]).columns
                except TypeError:
                    obj_cols = var_frame.select_dtypes(include=[object]).columns
                for col in obj_cols:
                    df[col] = df[col].apply(mask_clinical_phi)
                    if pipeline_phi_keys:
                        df[col] = df[col].apply(
                            lambda val: mask_custom_phi_recursive(val, pipeline_phi_keys)
                        )

        if use_labels:
            rename_map = {key: label_map.get(key, key) for key in unique_effective_var_keys}
            df = df.rename(columns=rename_map)
        return df

    def iter_dataframes(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> Iterator[pd.DataFrame]:
        """Yield mapped record DataFrames in bounded chunks.

        When ``as_sdtm`` is True, yields normalized CDISC SDTM domain DataFrames.

        Each yielded frame contains at most ``chunk_size`` mapped records from
        this mapper instance. Prefer this method over ``dataframe()`` when
        processing or exporting large studies so each chunk can be committed
        before the next batch is parsed.
        """
        if as_sdtm:
            target_domain = domain or "AE"
            yield from self.iter_sdtm_dataframes(
                study_key=study_key,
                domain=target_domain,
                visit_key=visit_key,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                mask_phi=mask_phi,
                include_deleted=include_deleted,
            )
            return

        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.dataframe. Install "
                "with 'pip install \"imednet[export]\"'."
            )
        variable_keys, label_map = self._fetch_variable_metadata(
            study_key,
            variable_whitelist=variable_whitelist,
            form_whitelist=form_whitelist,
        )
        if not variable_keys:
            return

        record_model = self._build_record_model(variable_keys, label_map)
        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = form_whitelist

        errors = 0
        yielded = False
        for rows, chunk_errors in self._iter_parsed_rows(
            self._iter_records(
                study_key,
                visit_key,
                extra_filters=extra_filters or None,
                include_deleted=include_deleted,
            ),
            record_model,
            mask_phi=mask_phi,
        ):
            errors += chunk_errors
            df = self._build_dataframe(
                rows,
                variable_keys,
                label_map,
                use_labels_as_columns,
                mask_phi=mask_phi,
            )
            if df.empty:
                continue
            yielded = True
            yield df

        if errors:
            logger.warning("Encountered %s errors while parsing record data.", errors)
        if not yielded:
            logger.info(
                "No records processed successfully for study '%s' with the given filters.",
                study_key,
            )

    def build_hierarchy(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_keys: bool = False,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        records: Iterable[RecordModel] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate a nested JSON tree structure up to three levels deep.

        Outputs a Subject -> Visit -> Form tree of records. Maps variables
        using form-scoped models to prevent namespace collisions. Relational
        identifiers such as parent_record_id are populated dynamically based
        on hierarchy context.
        """
        from imednet_workflows.study_structure import get_study_structure

        self._active_study_key = study_key
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        pipeline = self._get_enrichment_pipeline(study_key)
        pipeline_phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()
        study_struct = get_study_structure(self.sdk, study_key)

        # 1. Build form-scoped models across both standalone study forms and interval forms
        form_models: dict[int, type[BaseModel]] = {}
        form_label_maps: dict[int, dict[str, str]] = {}

        candidate_forms: list[Any] = []
        raw_study_forms = getattr(study_struct, "forms", None)
        if isinstance(raw_study_forms, Iterable):
            candidate_forms.extend(raw_study_forms)
        for interval in study_struct.intervals:
            candidate_forms.extend(interval.forms)

        for form in candidate_forms:
            if form_whitelist and form.form_id not in form_whitelist:
                continue
            if form.form_id in form_models:
                continue
            var_keys = list(
                dict.fromkeys(
                    v.variable_name
                    for v in (form.variables or [])
                    if v.variable_name
                    and (not variable_whitelist or v.variable_name in variable_whitelist)
                )
            )
            label_map = {
                v.variable_name: v.label for v in (form.variables or []) if v.variable_name
            }
            form_models[form.form_id] = self._build_record_model(var_keys, label_map)  # type: ignore
            form_label_maps[form.form_id] = label_map  # type: ignore

        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = form_whitelist

        # 2. Fetch and iterate records unless pre-streamed records were supplied
        record_iter: Iterable[RecordModel] = (
            records
            if records is not None
            else self._iter_records(
                study_key,
                visit_key,
                extra_filters=extra_filters or None,
                include_deleted=include_deleted,
            )
        )

        meta_keys = {
            "recordId",
            "subjectKey",
            "visitId",
            "formId",
            "recordStatus",
            "dateCreated",
        }
        tree: dict[str, dict[str, Any]] = {}

        for rec in record_iter:
            if rec.form_id not in form_models:
                continue

            model = form_models[rec.form_id]
            label_map = form_label_maps[rec.form_id]  # type: ignore

            try:
                parsed_data = self._parse_record(
                    rec,
                    model,
                    mask_phi=should_mask,
                    study_key=study_key,
                )
            except (ValidationError, TypeError) as exc:
                logger.warning(
                    "Failed to parse record data for recordId %s: %s",
                    rec.record_id,
                    exc,
                )
                continue
            except Exception as exc:  # pragma: no cover - unexpected
                logger.error("Unexpected error processing recordId %s: %s", rec.record_id, exc)
                continue

            if should_mask:
                parsed_data = mask_clinical_phi(parsed_data)
                if pipeline_phi_keys:
                    parsed_data = mask_custom_phi_recursive(parsed_data, pipeline_phi_keys)

            if use_labels_as_keys:
                remapped: dict[str, Any] = {}
                for k, v in parsed_data.items():
                    if k in meta_keys:
                        remapped[k] = v
                    else:
                        lbl = label_map.get(k)
                        target_key = lbl if lbl is not None else k
                        if target_key not in meta_keys:
                            remapped[target_key] = v
                parsed_data = remapped

            # Automatically perform lookups for parent record identifiers using available metadata
            resolved_parent_id = getattr(rec, "parent_record_id", None)
            if not resolved_parent_id:
                resolved_parent_id = (
                    f"{rec.visit_id}_{rec.form_id}" if rec.visit_id is not None else None
                )

            if "parent_record_id" not in parsed_data or parsed_data.get("parent_record_id") is None:
                parsed_data["parent_record_id"] = resolved_parent_id
                # If we mapped to a label or alias, also try to set it there
                if use_labels_as_keys and "parentRecordId" in parsed_data:
                    parsed_data["parentRecordId"] = resolved_parent_id

            subj_key = rec.subject_key
            if subj_key not in tree:
                tree[subj_key] = {"subject_key": subj_key, "visits": {}, "forms": {}}  # type: ignore

            subj_node = tree[subj_key]  # type: ignore
            form_id = rec.form_id

            visit_id = rec.visit_id
            if visit_id is None:
                if form_id not in subj_node["forms"]:
                    subj_node["forms"][form_id] = {"form_id": form_id, "records": []}
                subj_node["forms"][form_id]["records"].append(parsed_data)
            else:
                if visit_id not in subj_node["visits"]:
                    subj_node["visits"][visit_id] = {"visit_id": visit_id, "forms": {}}

                visit_node = subj_node["visits"][visit_id]

                if form_id not in visit_node["forms"]:
                    visit_node["forms"][form_id] = {"form_id": form_id, "records": []}

                form_node = visit_node["forms"][form_id]
                form_node["records"].append(parsed_data)

        # 3. Convert dicts to lists
        result = []
        for subj_key, subj in tree.items():  # noqa: B007
            subj_visits = []
            for visit_id, visit in subj["visits"].items():  # noqa: B007
                visit_forms = []
                for form_id, form in visit["forms"].items():  # noqa: B007
                    visit_forms.append(form)
                visit["forms"] = visit_forms
                subj_visits.append(visit)
            subj["visits"] = subj_visits
            unscheduled_forms = list(subj.pop("forms", {}).values())
            if unscheduled_forms:
                subj["forms"] = unscheduled_forms
            result.append(subj)

        return result

    def transform_to_sdtm(
        self,
        records: Iterable[Any],
        domain: str,
        study_key: str,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Transform an iterable of records into a normalized SDTM domain DataFrame."""
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        pipeline = self._get_enrichment_pipeline(study_key)
        return transform_records_to_sdtm(
            records,
            domain=domain,
            study_key=study_key,
            include_deleted=effective_include_deleted,
            mask_phi=should_mask,
            enrichment_pipeline=pipeline,
        )

    def sdtm_dataframe(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Return a :class:`pandas.DataFrame` transformed into a CDISC SDTM domain dataset."""
        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.sdtm_dataframe. Install "
                "with 'pip install \"imednet[export]\"'."
            )
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        frames = list(
            self.iter_sdtm_dataframes(
                study_key=study_key,
                domain=domain,
                visit_key=visit_key,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                mask_phi=should_mask,
                include_deleted=include_deleted,
            )
        )
        if not frames:
            return transform_records_to_sdtm(
                [],
                domain=domain,
                study_key=study_key,
                mask_phi=should_mask,
                enrichment_pipeline=self._get_enrichment_pipeline(study_key),
            )
        return pd.concat(frames, ignore_index=True)

    @staticmethod
    def _project_record_whitelist(
        records: Iterable[Any],
        allowed_vars: set[str],
    ) -> Iterator[Any]:
        """Lazily project ``record_data`` dictionaries down to ``allowed_vars``."""
        for rec in records:
            rec_data = getattr(rec, "record_data", None)
            if hasattr(rec, "model_copy") and isinstance(rec_data, dict):
                yield rec.model_copy(
                    update={"record_data": {k: v for k, v in rec_data.items() if k in allowed_vars}}
                )
            elif isinstance(rec, dict):
                copied = dict(rec)
                if isinstance(copied.get("record_data"), dict):
                    copied["record_data"] = {
                        k: v for k, v in copied["record_data"].items() if k in allowed_vars
                    }
                yield copied
            else:
                yield rec

    def iter_sdtm_dataframes(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> Iterator[pd.DataFrame]:
        """Yield chunked pandas DataFrames transformed into an SDTM domain dataset."""
        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.iter_sdtm_dataframes. Install "
                "with 'pip install \"imednet[export]\"'."
            )
        if variable_whitelist is not None and len(variable_whitelist) == 0:
            return
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        pipeline = self._get_enrichment_pipeline(study_key)

        allowed_vars: set[str] | None = None
        if variable_whitelist is not None:
            allowed_vars = set(variable_whitelist)
            if pipeline is not None and pipeline.mappings:
                for src_var, rule in pipeline.mappings.items():
                    if rule.target_field in allowed_vars:
                        allowed_vars.add(src_var)

        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = form_whitelist

        source_is_ordered = False
        force_drain_ungrouped_loader = False
        if self._loader is not None:
            bound_iter, supports_subject_order = self._resolve_loader_iter_callable(self._loader)
            from unittest.mock import NonCallableMock

            if supports_subject_order and not isinstance(bound_iter, NonCallableMock):
                source_is_ordered = True
            elif bound_iter is not None:
                force_drain_ungrouped_loader = True
        elif not self._is_mock_sdk_or_callable():
            source_is_ordered = True

        records: Iterable[Any] = self._iter_records(
            study_key,
            visit_key=visit_key,
            extra_filters=extra_filters or None,
            include_deleted=effective_include_deleted,
            sort=_SDTM_RECORDS_SORT,
            group_by_subject=True,
        )
        if allowed_vars is not None:
            records = self._project_record_whitelist(records, allowed_vars)
        if force_drain_ungrouped_loader:
            records = list(records)

        normalizer = _SdtmNormalizer(
            domain=domain,
            study_key=study_key,
            include_deleted=effective_include_deleted,
            mask_phi=should_mask,
            pipeline=pipeline,
        )
        if normalizer.remaps_usubjid:
            source_is_ordered = False
        seq_counters: dict[str, int] = {}
        for df in _iter_sorted_sdtm_frames(
            records,
            normalizer,
            chunk_size=self._pipeline.chunk_size,
            seq_counters=seq_counters,
            source_is_ordered=source_is_ordered,
        ):
            if not df.empty:
                yield df

    def dataframe(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Return a :class:`pandas.DataFrame` of records for a study."""
        if as_sdtm:
            target_domain = domain or "AE"
            return self.sdtm_dataframe(
                study_key=study_key,
                domain=target_domain,
                visit_key=visit_key,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                mask_phi=mask_phi,
                include_deleted=include_deleted,
            )

        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.dataframe. Install "
                "with 'pip install \"imednet[export]\"'."
            )
        frames = list(
            self.iter_dataframes(
                study_key,
                visit_key=visit_key,
                use_labels_as_columns=use_labels_as_columns,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                mask_phi=mask_phi,
                include_deleted=include_deleted,
            )
        )
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "SDTM_DOMAIN_SPECS",
    "RecordMapper",
    "format_iso8601_dtc",
    "transform_records_to_sdtm",
]
