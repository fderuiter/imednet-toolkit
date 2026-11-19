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
from pydantic import BaseModel, Field, create_model

from imednet.spi.sink_base import is_async_sdk, is_record_deleted
from imednet.spi.tabular_mapper import BaseTabularRecordMapper

from .cached_loader import CachedRecordsLoader
from .chunked_pipeline import DEFAULT_CHUNK_SIZE, ChunkedRecordPipeline, iter_chunks

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade
    from imednet.spi.models import StudyStructure

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


class RecordMapper(BaseTabularRecordMapper):
    """Maps EDC records for a study into a pandas DataFrame or CDISC SDTM domain.

    Extends :class:`BaseTabularRecordMapper` with optional ``CachedRecordsLoader``
    integration and CDISC SDTM domain transformations.
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
        """Initialize with a synchronous :class:`ImednetSDK` instance."""
        if is_async_sdk(sdk):
            raise TypeError(
                "RecordMapper is synchronous-only and cannot be used with AsyncImednetSDK. "
                "Instantiate synchronous ImednetSDK instead."
            )
        self._pipeline = ChunkedRecordPipeline(chunk_size=chunk_size)
        super().__init__(
            sdk,
            chunk_size=self._pipeline.chunk_size,
            mask_phi=mask_phi,
            include_deleted=include_deleted,
        )
        self._loader = loader

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

    def _get_study_structure(self, study_key: str) -> StudyStructure:
        """Retrieve study structural metadata via :func:`get_study_structure`."""
        from imednet_workflows.study_structure import get_study_structure

        return get_study_structure(self.sdk, study_key)

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
        yield from self._iter_tabular_dataframes(
            study_key,
            visit_key=visit_key,
            use_labels_as_columns=use_labels_as_columns,
            variable_whitelist=variable_whitelist,
            form_whitelist=form_whitelist,
            mask_phi=mask_phi,
            include_deleted=include_deleted,
        )

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
