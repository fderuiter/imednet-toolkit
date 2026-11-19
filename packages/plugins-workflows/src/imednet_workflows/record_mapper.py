"""Record-to-DataFrame mapping workflow for iMednet.

This module provides the :class:`RecordMapper` which handles fetching,
filtering, and parsing EDC records into tabular formats like pandas
DataFrames, including support for dynamic Pydantic model validation.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable, Iterator
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

from .cached_loader import CachedRecordsLoader
from .chunked_pipeline import DEFAULT_CHUNK_SIZE, ChunkedRecordPipeline, iter_chunks

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

# Setup basic logging
logger = logging.getLogger(__name__)


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


def transform_records_to_sdtm(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
) -> pd.DataFrame:
    """Transform clinical records into a normalized CDISC SDTM domain DataFrame.

    Maps subject keys to USUBJID, study keys to STUDYID, sequences records per
    subject, and converts event timestamps to standard ISO 8601 DTC string formats.

    Args:
        records: An iterable of Record models, Pydantic models (such as AdverseEvent),
            or raw dictionaries.
        domain: CDISC SDTM domain code (e.g. 'AE', 'DV', 'DD', 'CM', 'DM', 'VS').
        study_key: Unique study identifier to populate STUDYID.
        include_deleted: When True, include voided (deleted=True) records.

    Returns:
        A pandas DataFrame with standardized domain variables.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for SDTM transformations. Install with "
            "'pip install \"imednet[pandas]\"'."
        )

    domain_clean = domain.strip().upper()
    if domain_clean == "PD":
        domain_clean = "DV"

    spec = SDTM_DOMAIN_SPECS.get(domain_clean)
    std_vars = spec["variables"] if spec else ["STUDYID", "DOMAIN", "USUBJID", f"{domain_clean}SEQ"]
    alias_map = spec["aliases"] if spec else {}
    flag_vars = spec["flags"] if spec else set()
    dtc_vars = spec["dtc"] if spec else set()

    rows: list[dict[str, Any]] = []
    seq_counters: dict[str, int] = {}

    for rec in records:
        if not include_deleted and is_record_deleted(rec):
            continue
        model_field_to_serialized: dict[str, str] = {}
        all_field_aliases: dict[str, set[str]] = {}
        base_consumed: frozenset[str] | set[str] = _BASE_SDTM_CONSUMED_KEYS

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
            rec_dict = getattr(rec, "__dict__", {})

        if "record_data" in rec_dict and isinstance(rec_dict["record_data"], dict):
            nested_data = rec_dict.pop("record_data")
            for k, v in nested_data.items():
                if k not in rec_dict:
                    rec_dict[k] = v

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
            or getattr(rec, "subject_key", None)
            or getattr(rec, "subjectKey", None)
            or ""
        )
        seq_counters[usubjid] = seq_counters.get(usubjid, 0) + 1
        seq_val = seq_counters[usubjid]

        row: dict[str, Any] = {
            "STUDYID": study_key,
            "DOMAIN": domain_clean,
            "USUBJID": usubjid,
            f"{domain_clean}SEQ": seq_val,
        }

        consumed_keys: set[str] = set(base_consumed)

        lower_rec_dict = {k.lower(): (k, v) for k, v in rec_dict.items()}

        for var_name in std_vars:
            if var_name in ("STUDYID", "DOMAIN", "USUBJID", f"{domain_clean}SEQ"):
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

    if not rows:
        return pd.DataFrame(columns=std_vars)

    df = pd.DataFrame(rows)
    cols = [c for c in std_vars if c in df.columns]
    extra_cols = [c for c in df.columns if c not in std_vars]
    df = df[cols + extra_cols]
    return df


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
        include_deleted: bool = False,
    ) -> None:
        """Initialize with an :class:`ImednetSDK` instance.

        ``loader`` enables cache-backed streaming for large-study workflows.
        ``chunk_size`` controls the maximum number of records parsed into each
        yielded batch when using chunked mapping helpers.
        ``include_deleted`` controls whether voided (``deleted=True``) records
        are retained (defaults to ``False``).
        """
        self.sdk = sdk
        self._loader = loader
        self._pipeline = ChunkedRecordPipeline(chunk_size=chunk_size)
        self._include_deleted = include_deleted
        self._last_variables: list[VariableModel] = []

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

    def _stream_sdk_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
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

        use_streaming_endpoint = self._can_stream_via_records_endpoint()
        records_ep = getattr(self.sdk, "records", None)
        fetch_fn: Any = (
            records_ep.list
            if use_streaming_endpoint and records_ep is not None
            else self.sdk.get_records
        )

        try:
            if form_id_list is not None:
                for fid in form_id_list:
                    for rec in fetch_fn(
                        study_key=study_key,
                        record_data_filter=record_data_filter,
                        **filters,
                        formId=fid,
                    ):
                        if not effective_include_deleted and is_record_deleted(rec):
                            continue
                        yield rec
                return

            for rec in fetch_fn(
                study_key=study_key,
                record_data_filter=record_data_filter,
                **filters,
            ):
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
        return list(
            self._stream_sdk_records(
                study_key,
                visit_key=visit_key,
                extra_filters=extra_filters,
                record_data_filter=record_data_filter,
                include_deleted=include_deleted,
            )
        )

    def _iter_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
    ) -> Iterable[RecordModel]:
        """Iterate lazily over records for a study, using cache if available.

        Args:
            study_key: The study identifier.
            visit_key: Optional visit filter.
            extra_filters: Optional additional filter criteria.
            record_data_filter: Optional clinical variable payload filter.
            include_deleted: Optional override for voided-record inclusion.

        Returns:
            An iterable of Record models.
        """
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        raw_filters = dict(extra_filters) if extra_filters else {}
        form_id_list, _ = self._extract_form_ids(raw_filters)
        form_ids: set[Any] | None = set(form_id_list) if form_id_list is not None else None

        loader = self._loader
        if loader is not None:
            sync_method = getattr(loader, "sync_records", None)
            iter_method = getattr(type(loader), "iter_cached_records", None)
            if callable(sync_method) and callable(iter_method):
                sync_method(study_key)
                return self._filter_records(
                    iter_method(loader, study_key, chunk_size=self._pipeline.chunk_size),
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
    ) -> dict[str, Any]:
        """Parse a single record using the dynamic Pydantic model.

        Args:
            rec: The Record model to parse.
            record_model: The dynamic Pydantic model for record data.

        Returns:
            A dictionary containing both metadata and parsed record data.
        """
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
        data = rec.record_data if isinstance(rec.record_data, dict) else {}
        parsed = record_model(**data).model_dump(by_alias=False)
        return {**meta, **parsed}

    def _parse_records(
        self, records: Iterable[RecordModel], record_model: type[BaseModel]
    ) -> tuple[list[dict[str, Any]], int]:
        """Parse raw records into row dictionaries and count failures."""
        rows: list[dict[str, Any]] = []
        errors = 0
        for chunk_rows, chunk_errors in self._iter_parsed_rows(records, record_model):
            rows.extend(chunk_rows)
            errors += chunk_errors
        return rows, errors

    def _iter_parsed_rows(
        self,
        records: Iterable[RecordModel],
        record_model: type[BaseModel],
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
                    rows.append(self._parse_record(rec, record_model))
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
    ) -> pd.DataFrame:
        """Create the output DataFrame from parsed rows."""
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        meta_cols = [
            "recordId",
            "subjectKey",
            "visitId",
            "formId",
            "recordStatus",
            "dateCreated",
        ]

        for key in variable_keys:
            if key not in df.columns:
                df[key] = pd.NA

        df = df[meta_cols + variable_keys]
        if use_labels:
            rename_map = {key: label_map.get(key, key) for key in variable_keys}
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
                include_deleted=include_deleted,
            )
            return

        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.dataframe. Install "
                "with 'pip install \"imednet[pandas]\"'."
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
        ):
            errors += chunk_errors
            df = self._build_dataframe(rows, variable_keys, label_map, use_labels_as_columns)
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
        include_deleted: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Generate a nested JSON tree structure up to three levels deep.

        Outputs a Subject -> Visit -> Form tree of records. Maps variables
        using form-scoped models to prevent namespace collisions. Relational
        identifiers such as parent_record_id are populated dynamically based
        on hierarchy context.
        """
        from imednet_workflows.study_structure import get_study_structure

        study_struct = get_study_structure(self.sdk, study_key)

        # 1. Build form-scoped models
        form_models: dict[int, type[BaseModel]] = {}
        form_label_maps: dict[int, dict[str, str]] = {}

        for interval in study_struct.intervals:
            for form in interval.forms:
                if form_whitelist and form.form_id not in form_whitelist:
                    continue
                if form.form_id in form_models:
                    continue
                var_keys = [
                    v.variable_name
                    for v in (form.variables or [])
                    if not variable_whitelist or v.variable_name in variable_whitelist
                ]
                label_map = {v.variable_name: v.label for v in (form.variables or [])}
                form_models[form.form_id] = self._build_record_model(var_keys, label_map)  # type: ignore
                form_label_maps[form.form_id] = label_map  # type: ignore

        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = form_whitelist

        # 2. Fetch and iterate records
        records = self._iter_records(
            study_key,
            visit_key,
            extra_filters=extra_filters or None,
            include_deleted=include_deleted,
        )

        tree: dict[str, dict[str, Any]] = {}

        for rec in records:
            if rec.form_id not in form_models:
                continue

            model = form_models[rec.form_id]
            label_map = form_label_maps[rec.form_id]  # type: ignore

            try:
                parsed_data = self._parse_record(rec, model)
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

            if use_labels_as_keys:
                remapped: dict[str, Any] = {}
                for k, v in parsed_data.items():
                    lbl = label_map.get(k)
                    remapped[lbl if lbl is not None else k] = v
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
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Transform an iterable of records into a normalized SDTM domain DataFrame."""
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        return transform_records_to_sdtm(
            records,
            domain=domain,
            study_key=study_key,
            include_deleted=effective_include_deleted,
        )

    def sdtm_dataframe(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Return a :class:`pandas.DataFrame` transformed into a CDISC SDTM domain dataset."""
        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.sdtm_dataframe. Install "
                "with 'pip install \"imednet[pandas]\"'."
            )
        frames = list(
            self.iter_sdtm_dataframes(
                study_key=study_key,
                domain=domain,
                visit_key=visit_key,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                include_deleted=include_deleted,
            )
        )
        if not frames:
            return transform_records_to_sdtm([], domain=domain, study_key=study_key)
        return pd.concat(frames, ignore_index=True)

    def iter_sdtm_dataframes(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        include_deleted: bool | None = None,
    ) -> Iterator[pd.DataFrame]:
        """Yield chunked pandas DataFrames transformed into an SDTM domain dataset."""
        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.iter_sdtm_dataframes. Install "
                "with 'pip install \"imednet[pandas]\"'."
            )
        if variable_whitelist is not None and len(variable_whitelist) == 0:
            return
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        allowed_vars = set(variable_whitelist) if variable_whitelist is not None else None
        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = form_whitelist

        records = self._iter_records(
            study_key,
            visit_key=visit_key,
            extra_filters=extra_filters or None,
            include_deleted=effective_include_deleted,
        )
        for chunk in iter_chunks(records, chunk_size=self._pipeline.chunk_size):
            records_batch: list[Any] = chunk
            if allowed_vars is not None:
                projected_chunk: list[Any] = []
                for rec in chunk:
                    rec_data = getattr(rec, "record_data", None)
                    if hasattr(rec, "model_copy") and isinstance(rec_data, dict):
                        projected_chunk.append(
                            rec.model_copy(
                                update={
                                    "record_data": {
                                        k: v for k, v in rec_data.items() if k in allowed_vars
                                    }
                                }
                            )
                        )
                    else:
                        projected_chunk.append(rec)
                records_batch = projected_chunk
            df = transform_records_to_sdtm(
                records_batch,
                domain=domain,
                study_key=study_key,
                include_deleted=effective_include_deleted,
            )
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
                include_deleted=include_deleted,
            )

        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.dataframe. Install "
                "with 'pip install \"imednet[pandas]\"'."
            )
        frames = list(
            self.iter_dataframes(
                study_key,
                visit_key=visit_key,
                use_labels_as_columns=use_labels_as_columns,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
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
