"""CDISC Dataset-JSON v1.1.0 dataset construction, serialization, and loading."""

from __future__ import annotations

import json
import math
import numbers
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from imednet.models.sdtm import (
    SDTM_DOMAIN_SPECS,
    format_iso8601_dtc,
    resolve_sdtm_domain,
    transform_records_to_sdtm_rows,
)
from imednet.utils.security import global_sensitivity_registry, sanitize_diagnostic_text

from .exchange import (
    MASKED_PHI_SENTINEL,
    DatasetJsonClinicalData,
    DatasetJsonColumn,
    DatasetJsonDataset,
    DatasetJsonDataType,
    DatasetJsonSourceSystem,
    _format_iso_timestamp,
    _get_field,
    _is_reference_form,
    _matches_phi_key,
    sanitize_odm_oid,
)
from .models import DataDictionary, Form, Variable

_BIRTH_COL_RE = re.compile(r"[^a-z0-9]+")
_BIRTH_TOKENS = frozenset({"brthdtc", "brthdat", "birthdate", "dob", "dateofbirth", "birthdt"})

_SDTM_VARIABLE_LABELS: dict[str, str] = {
    "STUDYID": "Study Identifier",
    "DOMAIN": "Domain Abbreviation",
    "USUBJID": "Unique Subject Identifier",
    "SUBJID": "Subject Identifier for the Study",
    "SITEID": "Study Site Identifier",
    "BRTHDTC": "Date/Time of Birth",
    "AGE": "Age",
    "AGEU": "Age Units",
    "SEX": "Sex",
    "RACE": "Race",
    "ETHNIC": "Ethnicity",
    "ARM": "Description of Planned Arm",
    "VISITNUM": "Visit Number",
    "VISIT": "Visit Name",
    "AESEQ": "Sequence Number",
    "AETERM": "Reported Term for the Adverse Event",
    "AEDECOD": "Dictionary-Derived Term",
    "AESEV": "Severity/Intensity",
    "AESER": "Serious Event",
    "AESTDTC": "Start Date/Time of Adverse Event",
    "AEENDTC": "End Date/Time of Adverse Event",
    "AEOUT": "Outcome of Adverse Event",
    "AEREL": "Causality",
    "AEACN": "Action Taken with Study Treatment",
    "CMSEQ": "Sequence Number",
    "CMTRT": "Reported Name of Drug, Med, or Therapy",
    "CMDECOD": "Standardized Medication Name",
    "CMDOSE": "Dose per Administration",
    "CMDOSU": "Dose Units",
    "CMROUTE": "Route of Administration",
    "CMSTDTC": "Start Date/Time of Medication",
    "CMENDTC": "End Date/Time of Medication",
    "CMINDC": "Indication",
    "VSSEQ": "Sequence Number",
    "VSTESTCD": "Vital Signs Test Short Name",
    "VSTEST": "Vital Signs Test Name",
    "VSORRES": "Result or Finding in Original Units",
    "VSORRESU": "Original Units",
    "VSDTC": "Date/Time of Measurements",
    "LBSEQ": "Sequence Number",
    "LBTESTCD": "Lab Test or Examination Short Name",
    "LBTEST": "Lab Test or Examination Name",
    "LBCAT": "Category for Lab Test",
    "LBORRES": "Result or Finding in Original Units",
    "LBORRESU": "Original Units",
    "LBSTRESC": "Character Result/Finding in Std Format",
    "LBSTRESN": "Numeric Result/Finding in Standard Units",
    "LBSTRESU": "Standard Units",
    "LBNRIND": "Reference Range Indicator",
    "LBDTC": "Date/Time of Specimen Collection",
    "DVSEQ": "Sequence Number",
    "DVTERM": "Protocol Deviation Term",
    "DVCAT": "Category for Protocol Deviation",
    "DVSEV": "Severity",
    "DVSTDTC": "Start Date/Time of Deviation",
    "DVSTAT": "Completion Status",
    "DDSEQ": "Sequence Number",
    "DDTERM": "Reported Term for the Device Deficiency",
    "DDCAT": "Category for Device Deficiency",
    "DDSER": "Serious Event",
    "DDSTDTC": "Start Date/Time of Device Deficiency",
    "EXSEQ": "Sequence Number",
    "EXTRT": "Name of Treatment",
    "EXDOSE": "Dose",
    "EXDOSU": "Dose Units",
    "EXDOSFRM": "Dose Form",
    "EXDOSFRQ": "Dosing Frequency per Interval",
    "EXROUTE": "Route of Administration",
    "EXSTDTC": "Start Date/Time of Treatment",
    "EXENDTC": "End Date/Time of Treatment",
    "MHSEQ": "Sequence Number",
    "MHTERM": "Reported Term for the Medical History",
    "MHDECOD": "Dictionary-Derived Term",
    "MHCAT": "Category for Medical History",
    "MHPRESP": "Pre-Specified",
    "MHOCCUR": "Medical History Occurrence",
    "MHSTDTC": "Start Date/Time of Medical History Event",
    "MHENDTC": "End Date/Time of Medical History Event",
    "MHENRF": "End Relative to Reference Period",
    "RECORD_ID": "Source EDC Record Identifier",
}

_SDTM_INTEGER_VARS = frozenset(
    {
        "AGE",
        "VISITNUM",
        "RECORD_ID",
        "AESEQ",
        "CMSEQ",
        "VSSEQ",
        "LBSEQ",
        "DVSEQ",
        "DDSEQ",
        "EXSEQ",
        "MHSEQ",
    }
)
_SDTM_FLOAT_VARS = frozenset({"LBSTRESN", "CMDOSE", "EXDOSE"})


def _is_birth_column(name: str) -> bool:
    cleaned = _BIRTH_COL_RE.sub("", name.lower())
    return bool(cleaned) and (cleaned in _BIRTH_TOKENS or cleaned.endswith("brthdtc"))


def _is_missing(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return True
    type_name = type(val).__name__
    return type_name in {"NAType", "NaTType"}


def _map_variable_datatype(
    var: Variable | None, col_name: str, *, as_sdtm: bool
) -> DatasetJsonDataType:
    if as_sdtm:
        upper = col_name.upper()
        if upper in _SDTM_INTEGER_VARS or (upper.endswith("SEQ") and upper != "DMSEQ"):
            return "integer"
        if upper in _SDTM_FLOAT_VARS:
            return "float"
        return "string"

    if var is not None:
        vt = (var.variableType or "").strip().lower()
        if vt == "integer":
            return "integer"
        if vt in {"float", "decimal", "numeric"}:
            return "float"
        if vt == "boolean":
            return "boolean"
        if vt == "date":
            return "date"
        if vt == "datetime":
            return "datetime"
        if vt == "time":
            return "time"

    low = col_name.lower()
    if low in {
        "record_id",
        "recordid",
        "form_id",
        "formid",
        "visit_id",
        "visitid",
        "site_id",
        "siteid",
    }:
        return "integer"
    return "string"


def _coerce_cell_for_datatype(
    raw_val: Any,
    data_type: DatasetJsonDataType,
) -> str | int | float | bool | None:
    """Coerce a non-missing Python scalar to match a Dataset-JSON ``dataType``."""
    if _is_missing(raw_val):
        return None
    if isinstance(raw_val, str) and raw_val == MASKED_PHI_SENTINEL:
        return MASKED_PHI_SENTINEL

    if data_type == "integer":
        if isinstance(raw_val, bool):
            return int(raw_val)
        if isinstance(raw_val, numbers.Integral):
            return int(raw_val)
        if isinstance(raw_val, numbers.Real):
            f_val = float(raw_val)
            if math.isnan(f_val) or math.isinf(f_val):
                return None
            if f_val.is_integer():
                return int(f_val)
            return round(f_val)
        if isinstance(raw_val, str):
            s = raw_val.strip()
            if not s:
                return None
            try:
                return int(s)
            except ValueError:
                try:
                    f_parsed = float(s)
                    if f_parsed.is_integer():
                        return int(f_parsed)
                except ValueError:
                    pass
                return s
        return str(raw_val)

    if data_type in {"float", "double", "decimal"}:
        if isinstance(raw_val, bool):
            return float(int(raw_val))
        if isinstance(raw_val, numbers.Real):
            f_val = float(raw_val)
            return None if (math.isnan(f_val) or math.isinf(f_val)) else f_val
        if isinstance(raw_val, str):
            s = raw_val.strip()
            if not s:
                return None
            try:
                f_parsed = float(s)
                return None if (math.isnan(f_parsed) or math.isinf(f_parsed)) else f_parsed
            except ValueError:
                return s
        return str(raw_val)

    if data_type == "boolean":
        if isinstance(raw_val, bool):
            return raw_val
        if isinstance(raw_val, str):
            low = raw_val.strip().lower()
            if low in {"true", "1", "yes", "y"}:
                return True
            if low in {"false", "0", "no", "n"}:
                return False
            if not low:
                return None
        if isinstance(raw_val, numbers.Real):
            return bool(raw_val)
        return bool(raw_val)

    if isinstance(raw_val, datetime):
        return _format_iso_timestamp(raw_val)
    if isinstance(raw_val, date):
        return raw_val.isoformat()
    if isinstance(raw_val, (Mapping, list, tuple)):
        return json.dumps(raw_val, sort_keys=True, ensure_ascii=False)
    if isinstance(raw_val, numbers.Integral) and not isinstance(raw_val, bool):
        return str(int(raw_val))
    if isinstance(raw_val, numbers.Real) and not isinstance(raw_val, bool):
        f_val = float(raw_val)
        if math.isnan(f_val) or math.isinf(f_val):
            return None
        return str(int(f_val)) if f_val.is_integer() else str(f_val)
    return str(raw_val)


def _mask_dataset_cell(
    col_name: str,
    raw_val: Any,
    *,
    mask_phi: bool,
    as_sdtm: bool,
    custom_phi_keys: set[str] | None,
) -> Any:
    if _is_missing(raw_val):
        return None
    if not mask_phi:
        return raw_val
    if (
        as_sdtm
        and _is_birth_column(col_name)
        and not global_sensitivity_registry.is_exempt(col_name)
    ):
        return format_iso8601_dtc(raw_val, mask_phi=True)
    if _matches_phi_key(col_name, custom_phi_keys):
        return MASKED_PHI_SENTINEL
    if isinstance(raw_val, str):
        return sanitize_diagnostic_text(raw_val)
    return raw_val


def _unpack_flat_record(rec: Any) -> dict[str, Any]:
    """Flatten an iMednet ``Record`` model or dictionary into a clean CDASH row dict."""
    if isinstance(rec, Mapping):
        raw_data = rec.get("record_data") if "record_data" in rec else rec.get("recordData")
        if isinstance(raw_data, Mapping):
            row: dict[str, Any] = {}
            for k in (
                "study_key",
                "studyKey",
                "subject_key",
                "subjectKey",
                "site_id",
                "siteId",
                "visit_id",
                "visitId",
                "form_id",
                "formId",
                "form_key",
                "formKey",
                "record_id",
                "recordId",
            ):
                if k in rec and rec[k] is not None:
                    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", k).lower()
                    row.setdefault(snake, rec[k])
            for pk, pv in raw_data.items():
                if not isinstance(pv, (Mapping, list)):
                    row[str(pk)] = pv
                else:
                    row[str(pk)] = json.dumps(pv, sort_keys=True, ensure_ascii=False)
            return row
        return {str(k): v for k, v in rec.items() if k not in {"visits", "forms"}}

    row_obj: dict[str, Any] = {}
    for attr in (
        "study_key",
        "subject_key",
        "site_id",
        "visit_id",
        "form_id",
        "form_key",
        "record_id",
    ):
        val = _get_field(rec, attr)
        if val is not None:
            row_obj[attr] = val
    rec_data = _get_field(rec, "record_data", "recordData")
    if isinstance(rec_data, Mapping):
        for pk, pv in rec_data.items():
            row_obj[str(pk)] = pv
    return row_obj


def build_dataset_json_dataset(
    study_oid_or_rows: str | Sequence[Any],
    rows: Sequence[Any] | None = None,
    *,
    study_key: str | None = None,
    study_oid: str | None = None,
    dataset_name: str | None = None,
    name: str | None = None,
    dataset_label: str | None = None,
    label: str | None = None,
    item_group_oid: str | None = None,
    form: Form | None = None,
    datadict: DataDictionary | None = None,
    data_dictionary: DataDictionary | None = None,
    as_sdtm: bool = False,
    sdtm_domain: str | None = None,
    is_reference_data: bool = False,
    metadata_version_oid: str = "MDV.001",
    file_oid: str | None = None,
    creation_date_time: str | datetime | None = None,
    originator: str | None = None,
    source_system_version: str = "1.2.0",
    mask_phi: bool = True,
    custom_phi_keys: set[str] | None = None,
    use_labels_as_names: bool = False,
) -> DatasetJsonDataset:
    """Construct a deterministic CDISC Dataset-JSON v1.1.0 ``DatasetJsonDataset``."""
    if isinstance(study_oid_or_rows, str):
        resolved_study_oid = study_oid_or_rows
        raw_rows: Sequence[Any] = rows if rows is not None else ()
    else:
        raw_rows = study_oid_or_rows
        resolved_study_oid = study_oid or study_key or "STUDY_001"

    resolved_dd = data_dictionary if data_dictionary is not None else datadict
    resolved_ds_name = dataset_name or name or sdtm_domain or "RECORDS"
    resolved_ds_label = dataset_label if dataset_label is not None else label
    if form is None and resolved_dd is not None:
        form = next(
            (f for f in resolved_dd.forms if f.formKey.upper() == resolved_ds_name.strip().upper()),
            None,
        )

    normalized_rows: list[Mapping[str, Any]] = [
        r
        if (isinstance(r, Mapping) and "record_data" not in r and "recordData" not in r)
        else _unpack_flat_record(r)
        for r in raw_rows
    ]
    rows = normalized_rows
    created_iso = _format_iso_timestamp(creation_date_time, fallback="1970-01-01T00:00:00Z")
    clean_name = re.sub(r"[^A-Za-z0-9_]+", "_", resolved_ds_name.strip()).strip("_") or "RECORDS"
    resolved_ig_oid = item_group_oid or (
        f"IG.{clean_name.upper()}" if as_sdtm else f"IG_{clean_name}"
    )
    resolved_label = (
        resolved_ds_label
        if resolved_ds_label is not None
        else (form.formName if (form is not None and form.formName) else clean_name)
    )

    # Determine ordered column keys
    var_by_name: dict[str, Variable] = {}
    if form is not None:
        for v in form.variables:
            var_by_name[v.variableName] = v

    ordered_cols: list[str] = []
    seen_cols: set[str] = set()

    def _add_col(c: str) -> None:
        if c and c not in seen_cols:
            seen_cols.add(c)
            ordered_cols.append(c)

    if as_sdtm:
        dom = resolve_sdtm_domain(sdtm_domain or clean_name, fallback=clean_name.upper())
        spec = SDTM_DOMAIN_SPECS.get(dom)
        if spec is not None:
            for sv in spec["variables"]:
                _add_col(str(sv))
        else:
            for base_sv in ("STUDYID", "DOMAIN", "USUBJID"):
                _add_col(base_sv)
        for row in rows:
            for rk in row:
                _add_col(str(rk))
    else:
        envelope_order = (
            "study_key",
            "subject_key",
            "site_id",
            "visit_id",
            "form_id",
            "form_key",
            "record_id",
        )
        present_keys = {str(k) for r in rows for k in r}
        for ek in envelope_order:
            if ek in present_keys:
                _add_col(ek)
        if form is not None:
            for v in form.variables:
                _add_col(v.variableName)
        for rk in sorted(present_keys):
            if rk not in {"visits", "forms"}:
                _add_col(rk)
        if not ordered_cols:
            for ek in ("study_key", "subject_key", "record_id"):
                _add_col(ek)

    # Mask cells first so sensitive numeric columns masked with '***MASKED***' promote dataType cleanly
    masked_matrix: list[list[Any]] = []
    col_saw_masked_str: set[str] = set()
    for row in rows:
        masked_row: list[Any] = []
        for col_key in ordered_cols:
            raw_v = row.get(col_key)
            mv = _mask_dataset_cell(
                col_key,
                raw_v,
                mask_phi=mask_phi,
                as_sdtm=as_sdtm,
                custom_phi_keys=custom_phi_keys,
            )
            if mv == MASKED_PHI_SENTINEL:
                col_saw_masked_str.add(col_key)
            masked_row.append(mv)
        masked_matrix.append(masked_row)

    # Build DatasetJsonColumn definitions
    columns: list[DatasetJsonColumn] = []
    key_seq_counter = 1
    for col_idx, col_key in enumerate(ordered_cols):
        var_meta = var_by_name.get(col_key)
        inferred_dtype = _map_variable_datatype(var_meta, col_key, as_sdtm=as_sdtm)
        if col_key in col_saw_masked_str or (
            mask_phi
            and not (as_sdtm and _is_birth_column(col_key))
            and _matches_phi_key(col_key, custom_phi_keys)
        ):
            inferred_dtype = "string"

        # Verify actual non-null values can coerce cleanly; if non-numeric string present in numeric column, widen to string
        if inferred_dtype in {"integer", "float", "double", "decimal"}:
            for m_row in masked_matrix:
                cell_cand = m_row[col_idx]
                coerced = _coerce_cell_for_datatype(cell_cand, inferred_dtype)
                if isinstance(coerced, str):
                    inferred_dtype = "string"
                    break

        if as_sdtm:
            col_label = _SDTM_VARIABLE_LABELS.get(col_key.upper(), col_key)
            item_oid = f"IT.{clean_name.upper()}.{col_key}"
        elif var_meta is not None:
            col_label = var_meta.label or col_key
            item_oid = f"IT.{clean_name}.{col_key}"
        else:
            col_label = col_key.replace("_", " ").title()
            item_oid = f"IT.{clean_name}.{col_key}"

        key_seq: int | None = None
        upper_ck = col_key.upper()
        if (
            as_sdtm
            and (
                upper_ck in {"STUDYID", "USUBJID"}
                or (upper_ck.endswith("SEQ") and upper_ck != "DMSEQ")
            )
        ) or (not as_sdtm and col_key in {"study_key", "subject_key", "record_id"}):
            key_seq = key_seq_counter
            key_seq_counter += 1

        col_len: int | None = None
        if var_meta is not None:
            raw_len = var_meta.extraAttributes.get("length")
            if isinstance(raw_len, int) and raw_len >= 1:
                col_len = raw_len
            elif isinstance(raw_len, str) and raw_len.isdigit() and int(raw_len) >= 1:
                col_len = int(raw_len)

        rendered_col_name = (
            var_meta.label if (use_labels_as_names and var_meta and var_meta.label) else col_key
        ) or col_key

        columns.append(
            DatasetJsonColumn(
                itemOID=sanitize_odm_oid("", item_oid),
                name=rendered_col_name,
                label=col_label,
                dataType=inferred_dtype,
                length=col_len,
                displayFormat=var_meta.format if var_meta and var_meta.format else None,
                keySequence=key_seq,
            )
        )

    # Coerce final positional tuples
    coerced_rows: list[tuple[str | int | float | bool | None, ...]] = []
    for m_row in masked_matrix:
        tuple_cells = tuple(
            _coerce_cell_for_datatype(m_row[c_i], columns[c_i].data_type)
            for c_i in range(len(columns))
        )
        coerced_rows.append(tuple_cells)

    return DatasetJsonDataset(
        datasetJSONCreationDateTime=created_iso,
        datasetJSONVersion="1.1.0",
        fileOID=file_oid or f"DSJ.{resolved_study_oid}.{clean_name}",
        originator=originator,
        sourceSystem=DatasetJsonSourceSystem(
            name="imednet-toolkit",
            version=source_system_version,
        ),
        studyOID=None if is_reference_data else resolved_study_oid,
        metaDataVersionOID=metadata_version_oid,
        itemGroupOID=resolved_ig_oid,
        isReferenceData=True if is_reference_data else None,
        records=len(coerced_rows),
        name=clean_name.upper() if as_sdtm else clean_name,
        label=resolved_label,
        columns=tuple(columns),
        rows=tuple(coerced_rows),
    )


def build_dataset_json_clinical_data(
    study_key_or_records: str | Iterable[Any],
    records: Iterable[Any] | None = None,
    *,
    study_key: str | None = None,
    data_dictionary: DataDictionary | None = None,
    datadict: DataDictionary | None = None,
    as_sdtm: bool = False,
    sdtm_domain: str | None = None,
    metadata_version_oid: str = "MDV.001",
    creation_date_time: str | datetime | None = None,
    mask_phi: bool = True,
    include_deleted: bool = False,
    custom_phi_keys: set[str] | None = None,
) -> DatasetJsonClinicalData:
    """Build per-Form (CDASH) or per-Domain (SDTM) Dataset-JSON v1.1 ``itemGroupData`` datasets."""
    resolved_dd = data_dictionary if data_dictionary is not None else datadict
    if isinstance(study_key_or_records, str):
        resolved_study_key = study_key_or_records
        raw_records: Iterable[Any] = records if records is not None else ()
    else:
        raw_records = study_key_or_records
        resolved_study_key = (
            study_key
            or (resolved_dd.metadata.studyKey if resolved_dd is not None else None)
            or "STUDY_001"
        )
    study_key = resolved_study_key
    data_dictionary = resolved_dd

    created_iso = _format_iso_timestamp(creation_date_time, fallback="1970-01-01T00:00:00Z")
    active_records = []
    for rec in raw_records:
        del_flag = _get_field(rec, "deleted")
        is_del = (
            bool(del_flag)
            if isinstance(del_flag, bool)
            else (
                str(del_flag).strip().lower() in {"true", "1", "yes"}
                if del_flag is not None
                else False
            )
        )
        if is_del and not include_deleted:
            continue
        active_records.append(rec)

    forms_by_key: dict[str, Form] = {}
    forms_by_id: dict[int, Form] = {}
    if data_dictionary is not None:
        for f in data_dictionary.forms:
            forms_by_key[f.formKey] = f
            if isinstance(f.formId, int):
                forms_by_id[f.formId] = f

    item_groups: dict[str, DatasetJsonDataset] = {}
    reference_groups: dict[str, DatasetJsonDataset] = {}

    if as_sdtm:
        form_dom_map: dict[Any, str] = {}
        if data_dictionary is not None:
            for f in data_dictionary.forms:
                dom_cand = str(f.extraAttributes.get("domain") or f.formKey)
                resolved_dom = resolve_sdtm_domain(dom_cand, fallback=f.formKey.upper())
                form_dom_map[f.formKey] = resolved_dom
                if isinstance(f.formId, int):
                    form_dom_map[f.formId] = resolved_dom

        grouped_by_dom: dict[str, list[Any]] = {}
        if sdtm_domain:
            target_dom = resolve_sdtm_domain(sdtm_domain)
            grouped_by_dom[target_dom] = active_records
        else:
            for rec in active_records:
                fk = _get_field(rec, "form_key", "formKey")
                fid = _get_field(rec, "form_id", "formId")
                dom_raw = (
                    _get_field(rec, "DOMAIN", "domain")
                    or (form_dom_map.get(fk) if fk is not None else None)
                    or (form_dom_map.get(fid) if fid is not None else None)
                    or (str(fk) if fk else "AE")
                )
                dom_code = resolve_sdtm_domain(str(dom_raw))
                grouped_by_dom.setdefault(dom_code, []).append(rec)

        for dom_code in sorted(grouped_by_dom.keys()):
            dom_records = grouped_by_dom[dom_code]
            sdtm_rows = transform_records_to_sdtm_rows(
                dom_records,
                domain=dom_code,
                study_key=study_key,
                mask_phi=mask_phi,
                include_deleted=include_deleted,
            )
            ds = build_dataset_json_dataset(
                study_key,
                sdtm_rows,
                dataset_name=dom_code,
                dataset_label=f"{dom_code} Domain",
                item_group_oid=f"IG.{dom_code}",
                as_sdtm=True,
                sdtm_domain=dom_code,
                metadata_version_oid=metadata_version_oid,
                creation_date_time=created_iso,
                mask_phi=mask_phi,
                custom_phi_keys=custom_phi_keys,
            )
            item_groups[ds.item_group_oid] = ds

        return DatasetJsonClinicalData(
            studyOID=study_key,
            metaDataVersionOID=metadata_version_oid,
            itemGroupData=tuple(item_groups.values()),
            referenceData=tuple(reference_groups.values()),
        )

    # CDASH / Form-grouped Dataset-JSON v1.1 datasets
    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    for f_key in forms_by_key:
        grouped_rows[f_key] = []

    for rec in active_records:
        fk_raw = _get_field(rec, "form_key", "formKey")
        fid_raw = _get_field(rec, "form_id", "formId")
        matched_form: Form | None = None
        if fk_raw not in (None, "") and str(fk_raw) in forms_by_key:
            matched_form = forms_by_key[str(fk_raw)]
        elif isinstance(fid_raw, int) and fid_raw in forms_by_id:
            matched_form = forms_by_id[fid_raw]

        resolved_fk = (
            matched_form.formKey
            if matched_form is not None
            else (
                str(fk_raw).strip()
                if fk_raw not in (None, "")
                else (f"FORM_{fid_raw}" if fid_raw not in (None, "") else "RECORDS")
            )
        )
        row_dict = _unpack_flat_record(rec)
        row_dict.setdefault("study_key", study_key)
        grouped_rows.setdefault(resolved_fk, []).append(row_dict)

    for fk in sorted(grouped_rows.keys()):
        form_obj = forms_by_key.get(fk)
        rows_for_form = grouped_rows[fk]
        if not rows_for_form and form_obj is None:
            continue
        is_ref = _is_reference_form(form_obj) if form_obj is not None else False
        ds = build_dataset_json_dataset(
            study_key,
            rows_for_form,
            dataset_name=fk,
            dataset_label=form_obj.formName if (form_obj and form_obj.formName) else fk,
            item_group_oid=f"IG_{fk}",
            form=form_obj,
            as_sdtm=False,
            is_reference_data=is_ref,
            metadata_version_oid=metadata_version_oid,
            creation_date_time=created_iso,
            mask_phi=mask_phi,
            custom_phi_keys=custom_phi_keys,
        )
        if is_ref:
            reference_groups[ds.item_group_oid] = ds
        item_groups[ds.item_group_oid] = ds

    return DatasetJsonClinicalData(
        studyOID=study_key,
        metaDataVersionOID=metadata_version_oid,
        itemGroupData=tuple(item_groups.values()),
        referenceData=tuple(reference_groups.values()),
    )


def load_dataset_json(source: str | Path | Mapping[str, Any]) -> DatasetJsonDataset:
    """Parse and validate a CDISC Dataset-JSON v1.1.0 document into ``DatasetJsonDataset``."""
    if isinstance(source, Mapping):
        raw = dict(source)
    elif isinstance(source, Path):
        raw = json.loads(source.read_text(encoding="utf-8"))
    elif isinstance(source, str):
        stripped = source.strip()
        if stripped.startswith("{"):
            raw = json.loads(stripped)
        else:
            raw = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        raise TypeError(f"Unsupported Dataset-JSON source type: {type(source)}")
    return DatasetJsonDataset.model_validate(raw)


def dataset_json_to_rows(dataset: DatasetJsonDataset) -> list[dict[str, Any]]:
    """Convert positional ``rows`` in a ``DatasetJsonDataset`` back into named row dicts."""
    col_names = [col.name for col in dataset.columns]
    return [dict(zip(col_names, row, strict=False)) for row in dataset.rows]


__all__ = [
    "build_dataset_json_clinical_data",
    "build_dataset_json_dataset",
    "dataset_json_to_rows",
    "load_dataset_json",
]
