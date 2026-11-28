"""Canonical JSON and CDISC ODM XML emission for iMednet Data Dictionary."""

from __future__ import annotations

import copy
import json
import re

# Serialization only: this module never parses XML; defusedxml has no Element builder.
import xml.etree.ElementTree as ET  # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from imednet.utils.security import (
    SensitivityRegistry,
    build_study_sensitivity_registry,
    sanitize_diagnostic_text,
)
from imednet.utils.tabular import MASKED_SENTINEL

from .exchange import (
    OdmAdminData,
    OdmAnnotation,
    OdmArchive,
    OdmAuditRecord,
    OdmClinicalData,
    OdmFormData,
    OdmItemData,
    OdmItemGroupData,
    OdmReferenceData,
    OdmSubjectData,
)
from .models import BusinessLogicRule, Choice, DataDictionary, Form, Variable
from .normalize import (
    normalize_form_id,
    normalize_measurement_units,
    normalize_variable_bounds,
    normalize_variable_type,
)

ODM_NS = "http://www.cdisc.org/ns/odm/v1.3"
IMEDNET_NS = "http://www.imednet.com/ns/odm/metadata"

_ROUNDTRIP_VENDOR_VARIABLE_TYPES: frozenset[str] = frozenset(
    {"Calculation", "Slider", "File", "Descriptive"}
)
_PRESERVED_ODM_DATA_TYPES: frozenset[str] = frozenset(
    {
        "partialDate",
        "incompleteDate",
        "partialDatetime",
        "incompleteDatetime",
        "partialTime",
        "incompleteTime",
        "durationDatetime",
        "intervalDatetime",
        "URI",
    }
)


def _sort_key_int_then_str(num: int | None, text: str | None) -> tuple[int, int, str]:
    """Helper for stable sorting where None numbers sort last."""
    is_none = 1 if num is None else 0
    val = num if num is not None else 0
    return (is_none, val, text or "")


def sort_data_dictionary(datadict: DataDictionary) -> DataDictionary:
    """Return a deep copy of DataDictionary with deterministic ordering.

    Ordering guarantees:
    - Forms sorted by formKey.
    - Variables on each form sorted by sequence, then variableName.
    - Choices on each variable sorted by position, then choiceValue.
    - Business logic rules sorted by sequence, then id.
    """
    sorted_dict = copy.deepcopy(datadict)

    # 1. Sort forms by formKey
    sorted_dict.forms.sort(key=lambda f: f.formKey or "")

    for form in sorted_dict.forms:
        # 2. Sort variables
        form.variables.sort(key=lambda v: _sort_key_int_then_str(v.sequence, v.variableName))
        # 3. Sort choices on each variable
        for var in form.variables:
            var.choices.sort(key=lambda c: _sort_key_int_then_str(c.position, c.choiceValue))
        # 4. Sort business logic rules on each form
        form.businessLogic.sort(
            key=lambda r: _sort_key_int_then_str(r.sequence, str(r.id) if r.id is not None else "")
        )

    # 5. Sort unassigned business logic rules
    sorted_dict.unassignedBusinessLogic.sort(
        key=lambda r: _sort_key_int_then_str(r.sequence, str(r.id) if r.id is not None else "")
    )

    return sorted_dict


def _clean_extra_attributes(data: Any) -> Any:
    """Recursively strip empty extraAttributes dictionaries to keep JSON minimal."""
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for k, v in data.items():
            if k == "extraAttributes" and isinstance(v, dict) and not v:
                continue
            cleaned[k] = _clean_extra_attributes(v)
        return cleaned
    if isinstance(data, list):
        return [_clean_extra_attributes(x) for x in data]
    return data


def to_json(
    datadict: DataDictionary,
    *,
    indent: int = 2,
    exclude_generated_at: bool = False,
) -> str:
    """Serialize DataDictionary to canonical, deterministic JSON.

    Args:
        datadict: DataDictionary instance.
        indent: Indentation level (default: 2 spaces).
        exclude_generated_at: If True, omit metadata.generatedAt for deterministic diffs/tests.

    Returns:
        Canonical JSON string encoded with UTF-8 representation.
    """
    sorted_dict = sort_data_dictionary(datadict)
    data = sorted_dict.model_dump(mode="json", exclude_none=True)

    if exclude_generated_at and "metadata" in data and "generatedAt" in data["metadata"]:
        del data["metadata"]["generatedAt"]

    cleaned = _clean_extra_attributes(data)
    return json.dumps(cleaned, indent=indent, ensure_ascii=False) + "\n"


def to_file(
    datadict: DataDictionary,
    file_path: Path | str,
    *,
    indent: int = 2,
    exclude_generated_at: bool = False,
) -> None:
    """Write canonical DataDictionary JSON to a UTF-8 encoded file.

    Args:
        datadict: DataDictionary instance.
        file_path: Destination file path.
        indent: Indentation level.
        exclude_generated_at: If True, omit metadata.generatedAt.
    """
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    json_str = to_json(datadict, indent=indent, exclude_generated_at=exclude_generated_at)
    path.write_text(json_str, encoding="utf-8")


def _map_to_odm_datatype(
    var_type: str | None,
    *,
    precision: str | None = None,
    has_choices: bool = False,
    extra_attributes: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Map canonical iMednet variableType to CDISC ODM DataType and ControlType."""
    if isinstance(extra_attributes, dict):
        odm_dt = extra_attributes.get("odmDataType")
        if isinstance(odm_dt, str) and odm_dt.strip() in _PRESERVED_ODM_DATA_TYPES:
            return odm_dt.strip(), None
    if isinstance(var_type, str) and var_type.strip() in _PRESERVED_ODM_DATA_TYPES:
        return var_type.strip(), None

    vt_norm = normalize_variable_type(var_type, precision=precision)
    if not vt_norm:
        return "text", ("RadioButton" if has_choices else None)
    if vt_norm in _ROUNDTRIP_VENDOR_VARIABLE_TYPES:
        return "text", None
    if vt_norm == "Integer":
        return "integer", None
    if vt_norm == "Float":
        return "float", None
    if vt_norm == "Date":
        return "date", None
    if vt_norm == "DateTime":
        return "datetime", None
    if vt_norm == "Time":
        return "time", None
    if vt_norm == "Boolean":
        return "boolean", None
    if vt_norm == "Radio":
        return "text", "RadioButton"
    if vt_norm == "Single Select":
        return "text", "SingleSelect"
    if vt_norm == "Multi Select":
        return "text", "MultiSelect"
    if vt_norm == "Text":
        return "text", ("Text" if has_choices else None)
    return "text", ("RadioButton" if has_choices else None)


def _extract_sdtm_attr(obj: Any, key: str) -> str | None:
    """Extract and validate a non-empty SDTM attribute from a form or variable model."""
    if obj is None:
        return None
    val: Any = getattr(obj, key, None)
    if val is None and hasattr(obj, "get"):
        val = obj.get(key)
    if val is None:
        extra = getattr(obj, "extraAttributes", None)
        if isinstance(extra, dict):
            val = extra.get(key)
    if val is not None:
        cleaned = str(val).strip()
        if cleaned:
            return cleaned
    return None


def _freeze_value(value: Any) -> Any:
    """Convert nested dicts and lists into immutable hashable structures for signatures."""
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze_value(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze_value(v) for v in value)
    return value


def _choices_signature(choices: list[Choice]) -> tuple[Any, ...]:
    """Compute an immutable signature for a sorted Choice collection."""
    sorted_choices = sorted(
        choices, key=lambda c: _sort_key_int_then_str(c.position, c.choiceValue)
    )
    return tuple(
        (
            c.choiceValue,
            c.choiceText,
            c.position if c.position is not None else idx,
            _freeze_value(c.extraAttributes),
            _freeze_value(c.model_extra or {}),
        )
        for idx, c in enumerate(sorted_choices, 1)
    )


def _codelist_signature(var: Variable) -> tuple[str, tuple[Any, ...]] | None:
    """Compute an equivalence signature for an ODM CodeList, or None when unchoiced."""
    if not var.choices:
        return None
    datatype, _ = _map_to_odm_datatype(
        var.variableType,
        precision=var.precision,
        has_choices=True,
        extra_attributes=var.extraAttributes,
    )
    cl_dtype = datatype if datatype in ("integer", "float", "text") else "text"
    return (cl_dtype, _choices_signature(var.choices))


def _item_def_signature(var: Variable) -> Any:
    """Compute an equivalence signature for an ODM ItemDef + CodeList definition."""
    choices_sig = _choices_signature(var.choices)
    excluded_keys = {
        "Domain",
        "SASDatasetName",
        "Purpose",
        "methodOid",
        "collectionExceptionConditionOid",
        "Role",
        "role",
        "RoleCodeListOID",
        "roleCodeListOid",
        "itemOID",
    }
    merged_extra = {k: v for k, v in (var.model_extra or {}).items() if k not in excluded_keys}
    for k, v in var.extraAttributes.items():
        if k not in excluded_keys:
            merged_extra[k] = v
    sdtm_sig = tuple(
        _extract_sdtm_attr(var, key) for key in ("SDSVarName", "SASFieldName", "Origin", "Comment")
    )
    return (
        var.label,
        normalize_variable_type(var.variableType, precision=var.precision) or var.variableType,
        _map_to_odm_datatype(
            var.variableType,
            precision=var.precision,
            has_choices=bool(var.choices),
            extra_attributes=var.extraAttributes,
        ),
        var.format,
        var.precision,
        sdtm_sig,
        _freeze_value(normalize_measurement_units(var.extraAttributes)),
        _freeze_value(normalize_variable_bounds(var.extraAttributes)),
        choices_sig,
        _freeze_value(merged_extra),
    )


def _default_unwrapped_name(item_oid: str, form_key: str, ig_oid: str | None = None) -> str:
    """Return the variableName that ODM import infers from item_oid without VariableName."""
    raw_oid = item_oid.strip()
    prefixes: list[str] = []
    if form_key:
        prefixes.extend([f"IT.{form_key}.", f"{form_key}."])
        for f_lead in ("F.", "F_", "FORM.", "FORM_"):
            if form_key.startswith(f_lead) and len(form_key) > len(f_lead):
                f_stem = form_key[len(f_lead) :]
                prefixes.extend([f"IT.{f_stem}.", f"{f_stem}."])
    eff_ig = ig_oid if ig_oid else f"IG_{form_key}"
    if eff_ig:
        prefixes.append(f"{eff_ig}.")
        for ig_lead in ("IG.", "IG_"):
            if eff_ig.startswith(ig_lead) and len(eff_ig) > len(ig_lead):
                ig_stem = eff_ig[len(ig_lead) :]
                prefixes.extend([f"IT.{ig_stem}.", f"{ig_stem}."])
    for prefix in prefixes:
        if raw_oid.startswith(prefix):
            suffix = raw_oid[len(prefix) :].strip()
            if suffix:
                return suffix
    return raw_oid


def _allocate_unscoped_oid(var_name: str, used_oids: set[str]) -> str:
    """Allocate a globally unique unprefixed OID for a non-colliding variable definition."""
    base_var = var_name.strip() or "ITEM"
    if base_var not in used_oids:
        used_oids.add(base_var)
        return base_var
    counter = 2
    while True:
        fallback = f"IT.{base_var}__{counter}"
        if fallback not in used_oids:
            used_oids.add(fallback)
            return fallback
        counter += 1


def _allocate_scoped_oid(
    form_key: str,
    var_name: str,
    used_oids: set[str],
    reserved_raw_names: set[str],
) -> str:
    """Allocate a globally unique form-scoped OID without colliding with raw or prior OIDs."""
    base_var = var_name.strip() or "ITEM"
    prefix = f"IT.{form_key}.{base_var}" if form_key else f"IT.{base_var}"
    if prefix not in used_oids and prefix not in reserved_raw_names:
        used_oids.add(prefix)
        return prefix

    counter = 2
    while True:
        fallback = f"{prefix}__{counter}"
        if fallback not in used_oids and fallback not in reserved_raw_names:
            used_oids.add(fallback)
            return fallback
        counter += 1


def _allocate_codelist_oid(prefix: str, used_oids: set[str]) -> str:
    """Allocate a globally unique ODM CodeList OID from a candidate prefix."""
    if prefix not in used_oids:
        used_oids.add(prefix)
        return prefix
    counter = 2
    while True:
        fallback = f"{prefix}__{counter}"
        if fallback not in used_oids:
            used_oids.add(fallback)
            return fallback
        counter += 1


def _allocate_codelist_oids(
    sorted_forms: list[Form],
    sorted_vars_by_form: list[list[Variable]],
) -> tuple[dict[tuple[int, int], str], list[tuple[str, str, str, list[Choice]]]]:
    """Allocate shared CL_{variableName} or form-qualified CL.{formKey}.{variableName} OIDs."""
    cl_sigs_by_var_name: dict[str, set[Any]] = {}
    cl_sig_by_occ: dict[tuple[int, int], tuple[str, tuple[Any, ...]] | None] = {}
    for f_idx, sorted_vars in enumerate(sorted_vars_by_form):
        for v_idx, var in enumerate(sorted_vars):
            cl_sig = _codelist_signature(var)
            cl_sig_by_occ[(f_idx, v_idx)] = cl_sig
            cl_sigs_by_var_name.setdefault(var.variableName, set()).add(cl_sig)

    divergent_cl_names = {v_name for v_name, sigs in cl_sigs_by_var_name.items() if len(sigs) > 1}
    used_cl_oids: set[str] = set()
    unscoped_cl_by_name: dict[str, str] = {}
    scoped_cl_by_group: dict[tuple[str, str, Any], str] = {}

    for f_idx, sorted_vars in enumerate(sorted_vars_by_form):
        for v_idx, var in enumerate(sorted_vars):
            if (
                cl_sig_by_occ[(f_idx, v_idx)] is not None
                and var.variableName not in divergent_cl_names
                and var.variableName not in unscoped_cl_by_name
            ):
                base_var = var.variableName.strip() or "ITEM"
                unscoped_cl_by_name[var.variableName] = _allocate_codelist_oid(
                    f"CL_{base_var}",
                    used_cl_oids,
                )

    cl_oid_by_occ: dict[tuple[int, int], str] = {}
    emitted_codelists: set[str] = set()
    codelist_elements: list[tuple[str, str, str, list[Choice]]] = []
    for f_idx, form in enumerate(sorted_forms):
        for v_idx, var in enumerate(sorted_vars_by_form[f_idx]):
            cl_sig = cl_sig_by_occ[(f_idx, v_idx)]
            if cl_sig is None:
                continue
            cl_dtype, _ = cl_sig
            base_var = var.variableName.strip() or "ITEM"
            if var.variableName in divergent_cl_names:
                group_key = (form.formKey, var.variableName, cl_sig)
                cl_oid = scoped_cl_by_group.get(group_key)
                if cl_oid is None:
                    prefix = f"CL.{form.formKey}.{base_var}" if form.formKey else f"CL.{base_var}"
                    cl_oid = _allocate_codelist_oid(prefix, used_cl_oids)
                    scoped_cl_by_group[group_key] = cl_oid
                cl_name = (
                    f"{form.formKey}.{base_var} CodeList"
                    if form.formKey
                    else f"{base_var} CodeList"
                )
            else:
                cl_oid = unscoped_cl_by_name[var.variableName]
                cl_name = f"{base_var} CodeList"

            cl_oid_by_occ[(f_idx, v_idx)] = cl_oid
            if cl_oid not in emitted_codelists:
                emitted_codelists.add(cl_oid)
                codelist_elements.append((cl_oid, cl_name, cl_dtype, var.choices))

    return cl_oid_by_occ, codelist_elements


def _allocate_item_oids(
    sorted_forms: list[Form],
) -> tuple[list[list[Variable]], dict[tuple[int, int], str], dict[str, list[str]]]:
    """Allocate globally unique ODM ItemDef OIDs across unscoped and form-scoped variables."""
    sorted_vars_by_form: list[list[Variable]] = [
        sorted(f.variables, key=lambda v: _sort_key_int_then_str(v.sequence, v.variableName))
        for f in sorted_forms
    ]

    sigs_by_var_name: dict[str, set[Any]] = {}
    sig_by_occ: dict[tuple[int, int], Any] = {}
    reserved_raw_names: set[str] = set()
    for f_idx, sorted_vars in enumerate(sorted_vars_by_form):
        for v_idx, var in enumerate(sorted_vars):
            sig = _item_def_signature(var)
            sig_by_occ[(f_idx, v_idx)] = sig
            sigs_by_var_name.setdefault(var.variableName, set()).add(sig)
            if stripped_name := var.variableName.strip():
                reserved_raw_names.add(stripped_name)

    scoped_var_names = {v_name for v_name, sigs in sigs_by_var_name.items() if len(sigs) > 1}
    used_item_oids: set[str] = set()
    preserved_oid_by_occ: dict[tuple[int, int], str] = {}
    preserved_sig_by_oid: dict[str, Any] = {}
    for f_idx, sorted_vars in enumerate(sorted_vars_by_form):
        for v_idx, var in enumerate(sorted_vars):
            raw_pres = var.extraAttributes.get("itemOID")
            pres_oid = raw_pres.strip() if isinstance(raw_pres, str) else ""
            if pres_oid:
                sig = sig_by_occ[(f_idx, v_idx)]
                existing_sig = preserved_sig_by_oid.get(pres_oid)
                if existing_sig is None or existing_sig == sig:
                    preserved_sig_by_oid[pres_oid] = sig
                    preserved_oid_by_occ[(f_idx, v_idx)] = pres_oid
                    used_item_oids.add(pres_oid)

    unscoped_oid_by_name: dict[str, str] = {}
    scoped_oid_by_group: dict[tuple[str, str, Any], str] = {}

    # Pass 1: Reserve unprefixed OIDs for variables with a single consistent definition
    for f_idx, sorted_vars in enumerate(sorted_vars_by_form):
        for v_idx, var in enumerate(sorted_vars):
            if (f_idx, v_idx) in preserved_oid_by_occ:
                continue
            if (
                var.variableName not in scoped_var_names
                and var.variableName not in unscoped_oid_by_name
            ):
                unscoped_oid_by_name[var.variableName] = _allocate_unscoped_oid(
                    var.variableName,
                    used_item_oids,
                )

    # Pass 2: Allocate collision-free form-scoped OIDs for variables with distinct definitions
    oid_by_occ: dict[tuple[int, int], str] = {}
    referencing_forms_by_oid: dict[str, list[str]] = {}
    for f_idx, form in enumerate(sorted_forms):
        sorted_vars = sorted_vars_by_form[f_idx]
        for v_idx, var in enumerate(sorted_vars):
            if (f_idx, v_idx) in preserved_oid_by_occ:
                item_oid = preserved_oid_by_occ[(f_idx, v_idx)]
            elif var.variableName in scoped_var_names:
                group_key = (form.formKey, var.variableName, sig_by_occ[(f_idx, v_idx)])
                cached_scoped = scoped_oid_by_group.get(group_key)
                if cached_scoped is None:
                    cached_scoped = _allocate_scoped_oid(
                        form.formKey,
                        var.variableName,
                        used_item_oids,
                        reserved_raw_names,
                    )
                    scoped_oid_by_group[group_key] = cached_scoped
                item_oid = cached_scoped
            else:
                item_oid = unscoped_oid_by_name[var.variableName]

            oid_by_occ[(f_idx, v_idx)] = item_oid
            ref_forms = referencing_forms_by_oid.setdefault(item_oid, [])
            if form.formKey not in ref_forms:
                ref_forms.append(form.formKey)

    return sorted_vars_by_form, oid_by_occ, referencing_forms_by_oid


def _collect_study_measurement_units(
    datadict: DataDictionary,
    sorted_forms: list[Form],
) -> list[dict[str, str]]:
    """Collect deduplicated MeasurementUnit definitions from Study and Variable metadata."""
    units_by_oid: dict[str, dict[str, str]] = {}
    ordered_units: list[dict[str, str]] = []

    def _register(unit_dict: dict[str, str]) -> None:
        oid = unit_dict["oid"]
        if oid not in units_by_oid:
            units_by_oid[oid] = unit_dict
            ordered_units.append(unit_dict)

    for u in normalize_measurement_units(datadict.metadata.extraAttributes):
        _register(u)

    for form in sorted_forms:
        sorted_vars = sorted(
            form.variables, key=lambda v: _sort_key_int_then_str(v.sequence, v.variableName)
        )
        for var in sorted_vars:
            for u in normalize_measurement_units(var.extraAttributes):
                _register(u)
            for rc in normalize_variable_bounds(var.extraAttributes):
                if rc_mu := rc.get("measurementUnitOid"):
                    rc_oid = str(rc_mu).strip()
                    if rc_oid:
                        _register({"oid": rc_oid, "name": rc_oid, "symbol": rc_oid})

    return ordered_units


def _emit_protocol_and_visits(mdv_elem: ET.Element, extra_meta: dict[str, Any]) -> None:
    """Emit Protocol and StudyEventDef elements into MetaDataVersion if present."""
    proto = extra_meta.get("protocol")
    if isinstance(proto, dict):
        proto_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}Protocol")
        if proto_desc := proto.get("description"):
            desc_elem = ET.SubElement(proto_elem, f"{{{ODM_NS}}}Description")
            tt_desc = ET.SubElement(desc_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"})
            tt_desc.text = str(proto_desc)
        se_refs = proto.get("studyEventRefs")
        if isinstance(se_refs, list):
            for idx, se_ref in enumerate(se_refs, 1):
                if not isinstance(se_ref, dict):
                    continue
                se_oid = str(se_ref.get("studyEventOid") or "").strip()
                if not se_oid:
                    continue
                ref_attribs: dict[str, str] = {
                    "StudyEventOID": se_oid,
                    "OrderNumber": str(se_ref.get("orderNumber", idx)),
                    "Mandatory": str(se_ref.get("mandatory") or "No"),
                }
                if cond_oid := se_ref.get("collectionExceptionConditionOid"):
                    ref_attribs["CollectionExceptionConditionOID"] = str(cond_oid)
                ET.SubElement(proto_elem, f"{{{ODM_NS}}}StudyEventRef", ref_attribs)

    visits = extra_meta.get("visits")
    if isinstance(visits, list):
        for visit in visits:
            if not isinstance(visit, dict):
                continue
            v_oid = str(visit.get("oid") or "").strip()
            if not v_oid:
                continue
            se_attribs: dict[str, str] = {
                "OID": v_oid,
                "Name": str(visit.get("name") or v_oid),
                "Repeating": str(visit.get("repeating") or "No"),
                "Type": str(visit.get("type") or "Scheduled"),
            }
            if v_cat := visit.get("category"):
                se_attribs["Category"] = str(v_cat)
            se_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}StudyEventDef", se_attribs)
            if v_desc := visit.get("description"):
                desc_elem = ET.SubElement(se_elem, f"{{{ODM_NS}}}Description")
                tt_desc = ET.SubElement(
                    desc_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"}
                )
                tt_desc.text = str(v_desc)
            v_forms = visit.get("forms")
            if isinstance(v_forms, list):
                for f_idx, vf in enumerate(v_forms, 1):
                    if not isinstance(vf, dict):
                        continue
                    f_oid = str(vf.get("formOid") or "").strip()
                    if not f_oid:
                        continue
                    fr_attribs: dict[str, str] = {
                        "FormOID": f_oid,
                        "OrderNumber": str(vf.get("orderNumber", f_idx)),
                        "Mandatory": str(vf.get("mandatory") or "No"),
                    }
                    if f_cond := vf.get("collectionExceptionConditionOid"):
                        fr_attribs["CollectionExceptionConditionOID"] = str(f_cond)
                    ET.SubElement(se_elem, f"{{{ODM_NS}}}FormRef", fr_attribs)


def _emit_native_odm_rules(
    mdv_elem: ET.Element,
    scoped_rules: list[tuple[str | None, BusinessLogicRule]],
    target_tag: str,
    seen_oids: set[tuple[str, str]],
    used_cond_oids: set[str],
) -> None:
    """Emit native ODM ConditionDef or MethodDef elements for ``target_tag``."""
    for _, rule in scoped_rules:
        logic = rule.logic if isinstance(rule.logic, dict) else {}
        odm_elem_type = logic.get("odmElement") or rule.extraAttributes.get("odmElement")
        if odm_elem_type != target_tag or rule.id is None:
            continue
        r_oid = str(rule.id).strip()
        if not r_oid or (target_tag, r_oid) in seen_oids:
            continue
        seen_oids.add((target_tag, r_oid))
        if target_tag == "ConditionDef":
            used_cond_oids.add(r_oid)

        attribs: dict[str, str] = {
            "OID": r_oid,
            "Name": str(rule.name or r_oid),
        }
        if target_tag == "MethodDef":
            m_type = (
                logic.get("methodType") or rule.extraAttributes.get("methodType") or "Computation"
            )
            attribs["Type"] = str(m_type)

        rule_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}{target_tag}", attribs)
        desc = logic.get("description") or rule.extraAttributes.get("description")
        if desc:
            d_elem = ET.SubElement(rule_elem, f"{{{ODM_NS}}}Description")
            tt_elem = ET.SubElement(d_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"})
            tt_elem.text = str(desc)

        exprs = logic.get("formalExpressions")
        if isinstance(exprs, list):
            for expr in exprs:
                if not isinstance(expr, dict):
                    continue
                fe_attribs: dict[str, str] = {}
                if ctx := expr.get("context"):
                    fe_attribs["Context"] = str(ctx)
                fe_elem = ET.SubElement(rule_elem, f"{{{ODM_NS}}}FormalExpression", fe_attribs)
                if body := expr.get("expression"):
                    fe_elem.text = str(body)

        aliases = rule.extraAttributes.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if not isinstance(alias, dict):
                    continue
                alias_attribs: dict[str, str] = {}
                if a_ctx := alias.get("context"):
                    alias_attribs["Context"] = str(a_ctx)
                if a_name := alias.get("name"):
                    alias_attribs["Name"] = str(a_name)
                if alias_attribs:
                    ET.SubElement(rule_elem, f"{{{ODM_NS}}}Alias", alias_attribs)


def _emit_formal_rules(
    mdv_elem: ET.Element,
    scoped_rules: list[tuple[str | None, BusinessLogicRule]],
) -> None:
    """Emit ODM ConditionDef (all sources) followed by MethodDef elements."""
    seen_oids: set[tuple[str, str]] = set()
    used_cond_oids: set[str] = set()

    # Pass 1: Standard ODM ConditionDef rules
    _emit_native_odm_rules(mdv_elem, scoped_rules, "ConditionDef", seen_oids, used_cond_oids)

    # Pass 2: iMednet / CSV business logic rules (Form, Workflow, Query, Approval, Required, etc.)
    auto_idx = 1
    for container_form, rule in scoped_rules:
        logic = rule.logic if isinstance(rule.logic, dict) else {}
        odm_elem_type = logic.get("odmElement") or rule.extraAttributes.get("odmElement")
        if odm_elem_type in ("ConditionDef", "MethodDef"):
            continue

        candidate_oid = str(rule.id).strip() if rule.id is not None else ""
        if not candidate_oid or candidate_oid in used_cond_oids:
            while f"COND.IMEDNET.{auto_idx}" in used_cond_oids:
                auto_idx += 1
            cond_oid = f"COND.IMEDNET.{auto_idx}"
            auto_idx += 1
        else:
            cond_oid = candidate_oid
        used_cond_oids.add(cond_oid)

        cond_attribs: dict[str, str] = {
            "OID": cond_oid,
            "Name": str(rule.name or cond_oid),
            f"{{{IMEDNET_NS}}}RuleSource": "imednet",
        }
        if rule.id is not None:
            cond_attribs[f"{{{IMEDNET_NS}}}RuleId"] = str(rule.id)
            cond_attribs[f"{{{IMEDNET_NS}}}RuleIdType"] = (
                "int" if isinstance(rule.id, int) and not isinstance(rule.id, bool) else "str"
            )
        if rule.name is not None:
            cond_attribs[f"{{{IMEDNET_NS}}}RuleName"] = rule.name
        if rule.type is not None:
            cond_attribs[f"{{{IMEDNET_NS}}}RuleType"] = rule.type
        if rule.status is not None:
            cond_attribs[f"{{{IMEDNET_NS}}}Status"] = rule.status
        if rule.form is not None:
            cond_attribs[f"{{{IMEDNET_NS}}}FormKey"] = rule.form
        if container_form is not None:
            cond_attribs[f"{{{IMEDNET_NS}}}ContainerForm"] = container_form
        if rule.sequence is not None:
            cond_attribs[f"{{{IMEDNET_NS}}}Sequence"] = str(rule.sequence)
        if rule.extraAttributes:
            cond_attribs[f"{{{IMEDNET_NS}}}ExtraAttributes"] = json.dumps(
                rule.extraAttributes, sort_keys=True
            )

        cond_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}ConditionDef", cond_attribs)
        d_elem = ET.SubElement(cond_elem, f"{{{ODM_NS}}}Description")
        tt_elem = ET.SubElement(d_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"})
        tt_elem.text = str(rule.name or rule.type or cond_oid)

        if rule.logicRawXml is not None:
            fe_xml = ET.SubElement(
                cond_elem,
                f"{{{ODM_NS}}}FormalExpression",
                {"Context": "imednet:logicRawXml"},
            )
            fe_xml.text = rule.logicRawXml
        if rule.logic is not None:
            fe_json = ET.SubElement(
                cond_elem,
                f"{{{ODM_NS}}}FormalExpression",
                {"Context": "imednet:logicJson"},
            )
            fe_json.text = json.dumps(rule.logic, sort_keys=True)

    # Pass 3: Standard ODM MethodDef rules (always after all ConditionDef elements)
    _emit_native_odm_rules(mdv_elem, scoped_rules, "MethodDef", seen_oids, used_cond_oids)


def _collect_referenced_archive_defs(
    clinical_data: Sequence[OdmClinicalData],
    reference_data: Sequence[OdmReferenceData],
) -> tuple[set[str], dict[str, list[str]], dict[str, set[str]]]:
    """Collect StudyEventOIDs, Flag CodeLists, and ReferenceData ItemGroup ItemOIDs."""
    event_oids: set[str] = set()
    flag_codelists: dict[str, list[str]] = {}
    ref_ig_items: dict[str, set[str]] = {}

    def _scan_annotations(anns: Sequence[OdmAnnotation]) -> None:
        for ann in anns:
            for flg in ann.flags:
                bucket = flag_codelists.setdefault(flg.flag_value_codelist_oid, [])
                if flg.flag_value and flg.flag_value not in bucket:
                    bucket.append(flg.flag_value)
                if flg.flag_type and flg.flag_type_codelist_oid:
                    tbucket = flag_codelists.setdefault(flg.flag_type_codelist_oid, [])
                    if flg.flag_type not in tbucket:
                        tbucket.append(flg.flag_type)

    def _scan_item_groups(igs: Sequence[OdmItemGroupData], *, is_ref: bool = False) -> None:
        for ig in igs:
            _scan_annotations(ig.annotations)
            if is_ref:
                ref_ig_items.setdefault(ig.item_group_oid, set())
            for item in ig.items:
                _scan_annotations(item.annotations)
                if is_ref:
                    ref_ig_items[ig.item_group_oid].add(item.item_oid)

    for ref in reference_data:
        _scan_item_groups(ref.item_groups, is_ref=True)

    for clin in clinical_data:
        _scan_annotations(clin.annotations)
        for subj in clin.subjects:
            _scan_annotations(subj.annotations)
            for se in subj.study_events:
                event_oids.add(se.study_event_oid)
                _scan_annotations(se.annotations)
                for fd in se.forms:
                    _scan_annotations(fd.annotations)
                    _scan_item_groups(fd.item_groups)
            for fd in subj.forms:
                event_oids.add("SE.UNSCHEDULED")
                _scan_annotations(fd.annotations)
                _scan_item_groups(fd.item_groups)

    return event_oids, flag_codelists, ref_ig_items


_FREE_TEXT_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_FREE_TEXT_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_FREE_TEXT_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\b\d{3})[\s.-]\d{3}[\s.-]\d{4}\b"
)
_FREE_TEXT_ISO_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_FREE_TEXT_SLASH_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_FREE_TEXT_NAMED_SUBJECT_RE = re.compile(
    r"(?i)\b(patient(?:\s+name)?|subject\s+name)\s*([:=])\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}"
)
_FREE_TEXT_PROSE_PERSON_RE = re.compile(
    r"\b([Pp]atient|[Ss]ubject)\s+"
    r"(?!(?:Key|Id|ID|Identifier|Status|Number|Visit|Data|Record|Report|Reported|Enrolled|Enrollment|Disposition|Consent|Demographics)\b)"
    r"[A-Z][a-z]{1,20}\s+"
    r"(?!(?:Key|Id|ID|Identifier|Status|Number|Visit|Data|Record|Report|Reported|Enrolled|Enrollment|Disposition|Consent|Demographics)\b)"
    r"[A-Z][a-z]{1,20}\b"
)


def _iter_cdisc_identifier_tokens(*identifiers: str | None) -> list[str]:
    """Expand prefixed CDISC OIDs (e.g. ``IT.DM.PATNAME``) and raw variable names."""
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(val: str) -> None:
        cleaned = val.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            tokens.append(cleaned)

    for raw in identifiers:
        if not raw or not isinstance(raw, str):
            continue
        _add(raw)
        stripped = raw.strip()
        if stripped.startswith(("IT.", "IG.", "CL.")):
            rest = stripped[3:]
            _add(rest)
            if "." in rest:
                _add(rest.split(".", 1)[1])
                _add(rest.rsplit(".", 1)[-1])
        elif "." in stripped:
            _add(stripped.split(".", 1)[1])
            _add(stripped.rsplit(".", 1)[-1])
    return tokens


def _is_cdisc_identifier_sensitive(
    registry: SensitivityRegistry,
    *identifiers: str | None,
    label: str | None = None,
) -> bool:
    """Return True when any prefixed OID, unwrapped variable name, or label is PHI-sensitive."""
    tokens = _iter_cdisc_identifier_tokens(*identifiers)
    if any(registry.is_exempt(tok) for tok in tokens):
        return False
    if any(registry.is_sensitive(tok) for tok in tokens):
        return True
    return bool(label and label.strip() and registry.is_sensitive(label))


def _scrub_clinical_free_text(text: str, *, registry: SensitivityRegistry) -> str:
    """Scrub diagnostic key-value pairs plus inline emails, phones, dates, and SSNs."""
    sanitized = sanitize_diagnostic_text(text, registry=registry)
    if registry.is_sensitive("patient_name"):
        sanitized = _FREE_TEXT_NAMED_SUBJECT_RE.sub(rf"\1\2 {MASKED_SENTINEL}", sanitized)
        sanitized = _FREE_TEXT_PROSE_PERSON_RE.sub(rf"\1 {MASKED_SENTINEL}", sanitized)
    if registry.is_sensitive("email"):
        sanitized = _FREE_TEXT_EMAIL_RE.sub(MASKED_SENTINEL, sanitized)
    if registry.is_sensitive("ssn"):
        sanitized = _FREE_TEXT_SSN_RE.sub(MASKED_SENTINEL, sanitized)
    if registry.is_sensitive("phone"):
        sanitized = _FREE_TEXT_PHONE_RE.sub(MASKED_SENTINEL, sanitized)
    if (
        registry.is_sensitive("dob")
        or registry.is_sensitive("date_of_birth")
        or registry.is_sensitive("birth_date")
        or registry.is_sensitive("brthdat")
    ):
        sanitized = _FREE_TEXT_ISO_DATE_RE.sub(MASKED_SENTINEL, sanitized)
        sanitized = _FREE_TEXT_SLASH_DATE_RE.sub(MASKED_SENTINEL, sanitized)
    return sanitized


def _should_emit_transaction_type(tx_type: str | None, *, allow_remove: bool) -> bool:
    return bool(tx_type) and (tx_type != "Remove" or allow_remove)


def _build_audit_record_elem(
    parent: ET.Element,
    audit: OdmAuditRecord,
    *,
    used_xml_ids: set[str],
    reserved_pool_ids: set[str] | None = None,
    is_pool_element: bool = False,
    mask_phi: bool,
    registry: SensitivityRegistry,
) -> ET.Element:
    attribs: dict[str, str] = {}
    if audit.edit_point:
        attribs["EditPoint"] = audit.edit_point
    if audit.used_imputation_method:
        attribs["UsedImputationMethod"] = audit.used_imputation_method
    can_emit_id = (
        bool(audit.id)
        and audit.id not in used_xml_ids
        and (is_pool_element or not reserved_pool_ids or audit.id not in reserved_pool_ids)
    )
    if can_emit_id and audit.id:
        used_xml_ids.add(audit.id)
        attribs["ID"] = audit.id

    ar_elem = ET.SubElement(parent, f"{{{ODM_NS}}}AuditRecord", attribs)
    ET.SubElement(ar_elem, f"{{{ODM_NS}}}UserRef", {"UserOID": audit.user_oid})
    ET.SubElement(ar_elem, f"{{{ODM_NS}}}LocationRef", {"LocationOID": audit.location_oid})
    dt_elem = ET.SubElement(ar_elem, f"{{{ODM_NS}}}DateTimeStamp")
    dt_elem.text = audit.datetime_stamp
    if audit.reason_for_change is not None and audit.reason_for_change != "":
        rfc_elem = ET.SubElement(ar_elem, f"{{{ODM_NS}}}ReasonForChange")
        rfc_elem.text = (
            _scrub_clinical_free_text(audit.reason_for_change, registry=registry)
            if mask_phi
            else audit.reason_for_change
        )
    if audit.source_id is not None and audit.source_id != "":
        sid_elem = ET.SubElement(ar_elem, f"{{{ODM_NS}}}SourceID")
        sid_elem.text = audit.source_id
    return ar_elem


def _build_annotation_elem(
    parent: ET.Element,
    ann: OdmAnnotation,
    *,
    used_xml_ids: set[str],
    reserved_pool_ids: set[str] | None = None,
    is_pool_element: bool = False,
    allow_remove: bool = True,
    mask_phi: bool,
    registry: SensitivityRegistry,
) -> ET.Element:
    attribs: dict[str, str] = {"SeqNum": str(ann.seq_num)}
    if (
        _should_emit_transaction_type(ann.transaction_type, allow_remove=allow_remove)
        and ann.transaction_type
    ):
        attribs["TransactionType"] = ann.transaction_type
    can_emit_id = (
        bool(ann.id)
        and ann.id not in used_xml_ids
        and (is_pool_element or not reserved_pool_ids or ann.id not in reserved_pool_ids)
    )
    if can_emit_id and ann.id:
        used_xml_ids.add(ann.id)
        attribs["ID"] = ann.id

    ann_elem = ET.SubElement(parent, f"{{{ODM_NS}}}Annotation", attribs)
    if ann.comment is not None:
        c_attribs: dict[str, str] = {}
        if ann.sponsor_or_site:
            c_attribs["SponsorOrSite"] = ann.sponsor_or_site
        c_elem = ET.SubElement(ann_elem, f"{{{ODM_NS}}}Comment", c_attribs)
        c_elem.text = (
            _scrub_clinical_free_text(ann.comment, registry=registry) if mask_phi else ann.comment
        )
    for flg in ann.flags:
        f_elem = ET.SubElement(ann_elem, f"{{{ODM_NS}}}Flag")
        fv_elem = ET.SubElement(
            f_elem,
            f"{{{ODM_NS}}}FlagValue",
            {"CodeListOID": flg.flag_value_codelist_oid},
        )
        fv_elem.text = flg.flag_value
        if flg.flag_type and flg.flag_type_codelist_oid:
            ft_elem = ET.SubElement(
                f_elem,
                f"{{{ODM_NS}}}FlagType",
                {"CodeListOID": flg.flag_type_codelist_oid},
            )
            ft_elem.text = flg.flag_type
    return ann_elem


def _build_item_data_elem(
    parent: ET.Element,
    item: OdmItemData,
    *,
    item_meta_by_oid: dict[str, tuple[str, str | None]],
    used_xml_ids: set[str],
    known_target_xml_ids: set[str] | None = None,
    reserved_pool_ids: set[str] | None = None,
    allow_remove: bool = True,
    mask_phi: bool,
    registry: SensitivityRegistry,
) -> ET.Element:
    attribs: dict[str, str] = {"ItemOID": item.item_oid}
    if (
        _should_emit_transaction_type(item.transaction_type, allow_remove=allow_remove)
        and item.transaction_type
    ):
        attribs["TransactionType"] = item.transaction_type

    var_name, var_label = item_meta_by_oid.get(item.item_oid, (item.item_oid, None))
    raw_val = item.value
    if (
        mask_phi
        and raw_val is not None
        and not item.is_null
        and _is_cdisc_identifier_sensitive(registry, item.item_oid, var_name, label=var_label)
    ):
        raw_val = MASKED_SENTINEL

    if item.is_null or raw_val is None:
        attribs["IsNull"] = "Yes"
    else:
        attribs["Value"] = raw_val

    valid_idrefs = (
        used_xml_ids if known_target_xml_ids is None else (used_xml_ids | known_target_xml_ids)
    )
    if item.audit_record_id and item.audit_record_id in valid_idrefs:
        attribs["AuditRecordID"] = item.audit_record_id
    if item.annotation_id and item.annotation_id in valid_idrefs:
        attribs["AnnotationID"] = item.annotation_id

    id_elem = ET.SubElement(parent, f"{{{ODM_NS}}}ItemData", attribs)
    if item.audit_record is not None:
        _build_audit_record_elem(
            id_elem,
            item.audit_record,
            used_xml_ids=used_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            mask_phi=mask_phi,
            registry=registry,
        )
    if item.measurement_unit_oid:
        ET.SubElement(
            id_elem,
            f"{{{ODM_NS}}}MeasurementUnitRef",
            {"MeasurementUnitOID": item.measurement_unit_oid},
        )
    for ann in item.annotations:
        _build_annotation_elem(
            id_elem,
            ann,
            used_xml_ids=used_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            allow_remove=allow_remove,
            mask_phi=mask_phi,
            registry=registry,
        )
    return id_elem


def _build_item_group_data_elem(
    parent: ET.Element,
    ig: OdmItemGroupData,
    *,
    item_meta_by_oid: dict[str, tuple[str, str | None]],
    used_xml_ids: set[str],
    known_target_xml_ids: set[str] | None = None,
    reserved_pool_ids: set[str] | None = None,
    allow_remove: bool = True,
    mask_phi: bool,
    registry: SensitivityRegistry,
) -> ET.Element:
    attribs: dict[str, str] = {"ItemGroupOID": ig.item_group_oid}
    if ig.item_group_repeat_key is not None:
        attribs["ItemGroupRepeatKey"] = ig.item_group_repeat_key
    if (
        _should_emit_transaction_type(ig.transaction_type, allow_remove=allow_remove)
        and ig.transaction_type
    ):
        attribs["TransactionType"] = ig.transaction_type
    ig_elem = ET.SubElement(parent, f"{{{ODM_NS}}}ItemGroupData", attribs)
    if ig.audit_record is not None:
        _build_audit_record_elem(
            ig_elem,
            ig.audit_record,
            used_xml_ids=used_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            mask_phi=mask_phi,
            registry=registry,
        )
    for ann in ig.annotations:
        _build_annotation_elem(
            ig_elem,
            ann,
            used_xml_ids=used_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            allow_remove=allow_remove,
            mask_phi=mask_phi,
            registry=registry,
        )
    for item in ig.items:
        _build_item_data_elem(
            ig_elem,
            item,
            item_meta_by_oid=item_meta_by_oid,
            used_xml_ids=used_xml_ids,
            known_target_xml_ids=known_target_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            allow_remove=allow_remove,
            mask_phi=mask_phi,
            registry=registry,
        )
    return ig_elem


def _build_form_data_elem(
    parent: ET.Element,
    fd: OdmFormData,
    *,
    item_meta_by_oid: dict[str, tuple[str, str | None]],
    used_xml_ids: set[str],
    known_target_xml_ids: set[str] | None = None,
    reserved_pool_ids: set[str] | None = None,
    allow_remove: bool = True,
    mask_phi: bool,
    registry: SensitivityRegistry,
) -> ET.Element:
    attribs: dict[str, str] = {"FormOID": fd.form_oid}
    if fd.form_repeat_key is not None:
        attribs["FormRepeatKey"] = fd.form_repeat_key
    if (
        _should_emit_transaction_type(fd.transaction_type, allow_remove=allow_remove)
        and fd.transaction_type
    ):
        attribs["TransactionType"] = fd.transaction_type
    fd_elem = ET.SubElement(parent, f"{{{ODM_NS}}}FormData", attribs)
    if fd.audit_record is not None:
        _build_audit_record_elem(
            fd_elem,
            fd.audit_record,
            used_xml_ids=used_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            mask_phi=mask_phi,
            registry=registry,
        )
    for ann in fd.annotations:
        _build_annotation_elem(
            fd_elem,
            ann,
            used_xml_ids=used_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            allow_remove=allow_remove,
            mask_phi=mask_phi,
            registry=registry,
        )
    for ig in fd.item_groups:
        _build_item_group_data_elem(
            fd_elem,
            ig,
            item_meta_by_oid=item_meta_by_oid,
            used_xml_ids=used_xml_ids,
            known_target_xml_ids=known_target_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            allow_remove=allow_remove,
            mask_phi=mask_phi,
            registry=registry,
        )
    return fd_elem


def _build_subject_data_elem(
    subj: OdmSubjectData,
    *,
    item_meta_by_oid: dict[str, tuple[str, str | None]],
    used_xml_ids: set[str],
    known_target_xml_ids: set[str] | None = None,
    reserved_pool_ids: set[str] | None = None,
    allow_remove: bool = True,
    mask_phi: bool,
    registry: SensitivityRegistry,
) -> ET.Element:
    attribs: dict[str, str] = {"SubjectKey": subj.subject_key}
    if (
        _should_emit_transaction_type(subj.transaction_type, allow_remove=allow_remove)
        and subj.transaction_type
    ):
        attribs["TransactionType"] = subj.transaction_type
    subj_elem = ET.Element(f"{{{ODM_NS}}}SubjectData", attribs)
    if subj.audit_record is not None:
        _build_audit_record_elem(
            subj_elem,
            subj.audit_record,
            used_xml_ids=used_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            mask_phi=mask_phi,
            registry=registry,
        )
    if subj.investigator_user_oid:
        ET.SubElement(
            subj_elem,
            f"{{{ODM_NS}}}InvestigatorRef",
            {"UserOID": subj.investigator_user_oid},
        )
    if subj.site_location_oid:
        ET.SubElement(
            subj_elem,
            f"{{{ODM_NS}}}SiteRef",
            {"LocationOID": subj.site_location_oid},
        )
    for ann in subj.annotations:
        _build_annotation_elem(
            subj_elem,
            ann,
            used_xml_ids=used_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            allow_remove=allow_remove,
            mask_phi=mask_phi,
            registry=registry,
        )
    unscheduled_se_elem: ET.Element | None = None
    for se in subj.study_events:
        se_attribs: dict[str, str] = {"StudyEventOID": se.study_event_oid}
        if se.study_event_repeat_key is not None:
            se_attribs["StudyEventRepeatKey"] = se.study_event_repeat_key
        if (
            _should_emit_transaction_type(se.transaction_type, allow_remove=allow_remove)
            and se.transaction_type
        ):
            se_attribs["TransactionType"] = se.transaction_type
        se_elem = ET.SubElement(subj_elem, f"{{{ODM_NS}}}StudyEventData", se_attribs)
        if (
            se.study_event_oid == "SE.UNSCHEDULED"
            and se.study_event_repeat_key is None
            and unscheduled_se_elem is None
        ):
            unscheduled_se_elem = se_elem
        if se.audit_record is not None:
            _build_audit_record_elem(
                se_elem,
                se.audit_record,
                used_xml_ids=used_xml_ids,
                reserved_pool_ids=reserved_pool_ids,
                mask_phi=mask_phi,
                registry=registry,
            )
        for ann in se.annotations:
            _build_annotation_elem(
                se_elem,
                ann,
                used_xml_ids=used_xml_ids,
                reserved_pool_ids=reserved_pool_ids,
                allow_remove=allow_remove,
                mask_phi=mask_phi,
                registry=registry,
            )
        for fd in se.forms:
            _build_form_data_elem(
                se_elem,
                fd,
                item_meta_by_oid=item_meta_by_oid,
                used_xml_ids=used_xml_ids,
                known_target_xml_ids=known_target_xml_ids,
                reserved_pool_ids=reserved_pool_ids,
                allow_remove=allow_remove,
                mask_phi=mask_phi,
                registry=registry,
            )
    if subj.forms:
        if unscheduled_se_elem is None:
            unscheduled_se_elem = ET.SubElement(
                subj_elem,
                f"{{{ODM_NS}}}StudyEventData",
                {"StudyEventOID": "SE.UNSCHEDULED"},
            )
        for fd in subj.forms:
            _build_form_data_elem(
                unscheduled_se_elem,
                fd,
                item_meta_by_oid=item_meta_by_oid,
                used_xml_ids=used_xml_ids,
                known_target_xml_ids=known_target_xml_ids,
                reserved_pool_ids=reserved_pool_ids,
                allow_remove=allow_remove,
                mask_phi=mask_phi,
                registry=registry,
            )
    return subj_elem


def _build_admin_data_elem(
    admin: OdmAdminData,
    *,
    default_study_oid: str,
    default_mdv_oid: str,
    mask_phi: bool,
    registry: SensitivityRegistry,
) -> ET.Element:
    attribs: dict[str, str] = {}
    if admin.study_oid:
        attribs["StudyOID"] = admin.study_oid
    admin_elem = ET.Element(f"{{{ODM_NS}}}AdminData", attribs)

    for user in sorted(admin.users, key=lambda u: u.oid):
        u_attribs: dict[str, str] = {"OID": user.oid}
        if user.user_type:
            u_attribs["UserType"] = user.user_type
        u_elem = ET.SubElement(admin_elem, f"{{{ODM_NS}}}User", u_attribs)
        if user.login_name:
            ln = ET.SubElement(u_elem, f"{{{ODM_NS}}}LoginName")
            ln.text = user.login_name
        if user.display_name:
            dn = ET.SubElement(u_elem, f"{{{ODM_NS}}}DisplayName")
            dn.text = user.display_name
        if user.full_name:
            fn = ET.SubElement(u_elem, f"{{{ODM_NS}}}FullName")
            fn.text = user.full_name
        if user.first_name:
            first_el = ET.SubElement(u_elem, f"{{{ODM_NS}}}FirstName")
            first_el.text = user.first_name
        if user.last_name:
            last_el = ET.SubElement(u_elem, f"{{{ODM_NS}}}LastName")
            last_el.text = user.last_name
        if user.organization:
            org_el = ET.SubElement(u_elem, f"{{{ODM_NS}}}Organization")
            org_el.text = user.organization
        if user.email:
            em_el = ET.SubElement(u_elem, f"{{{ODM_NS}}}Email")
            em_el.text = (
                MASKED_SENTINEL if (mask_phi and registry.is_sensitive("email")) else user.email
            )
        if user.phone:
            ph_el = ET.SubElement(u_elem, f"{{{ODM_NS}}}Phone")
            ph_el.text = (
                MASKED_SENTINEL if (mask_phi and registry.is_sensitive("phone")) else user.phone
            )
        for loc_oid in user.location_refs:
            ET.SubElement(u_elem, f"{{{ODM_NS}}}LocationRef", {"LocationOID": loc_oid})

    for loc in sorted(admin.locations, key=lambda item: item.oid):
        loc_attribs: dict[str, str] = {"OID": loc.oid, "Name": loc.name}
        if loc.location_type:
            loc_attribs["LocationType"] = loc.location_type
        loc_elem = ET.SubElement(admin_elem, f"{{{ODM_NS}}}Location", loc_attribs)
        mdv_refs = loc.metadata_version_refs
        if mdv_refs:
            for ref in mdv_refs:
                ET.SubElement(
                    loc_elem,
                    f"{{{ODM_NS}}}MetaDataVersionRef",
                    {
                        "StudyOID": ref.study_oid,
                        "MetaDataVersionOID": ref.metadata_version_oid,
                        "EffectiveDate": ref.effective_date,
                    },
                )
        else:
            ET.SubElement(
                loc_elem,
                f"{{{ODM_NS}}}MetaDataVersionRef",
                {
                    "StudyOID": admin.study_oid or default_study_oid,
                    "MetaDataVersionOID": default_mdv_oid,
                    "EffectiveDate": "1970-01-01",
                },
            )
    return admin_elem


def _build_reference_data_elem(
    ref: OdmReferenceData,
    *,
    item_meta_by_oid: dict[str, tuple[str, str | None]],
    used_xml_ids: set[str],
    known_target_xml_ids: set[str] | None = None,
    reserved_pool_ids: set[str] | None = None,
    allow_remove: bool = True,
    mask_phi: bool,
    registry: SensitivityRegistry,
) -> ET.Element:
    ref_elem = ET.Element(
        f"{{{ODM_NS}}}ReferenceData",
        {
            "StudyOID": ref.study_oid,
            "MetaDataVersionOID": ref.metadata_version_oid,
        },
    )
    for ig in ref.item_groups:
        _build_item_group_data_elem(
            ref_elem,
            ig,
            item_meta_by_oid=item_meta_by_oid,
            used_xml_ids=used_xml_ids,
            known_target_xml_ids=known_target_xml_ids,
            reserved_pool_ids=reserved_pool_ids,
            allow_remove=allow_remove,
            mask_phi=mask_phi,
            registry=registry,
        )
    return ref_elem


def _build_study_metadata_elem(
    datadict: DataDictionary,
    *,
    study_key: str,
    meta_data_version_oid: str,
    mdv_name: str,
    extra_event_oids: set[str],
    extra_flag_codelists: dict[str, list[str]],
    extra_ref_ig_items: dict[str, set[str]],
) -> tuple[ET.Element, dict[str, tuple[str, str | None]]]:
    """Construct ``<Study>`` with ``<BasicDefinitions>`` and ``<MetaDataVersion>``."""
    study_name = (
        getattr(datadict.metadata, "studyName", None)
        or datadict.metadata.extraAttributes.get("studyName")
        or study_key
    )
    study_description = (
        getattr(datadict.metadata, "studyDescription", None)
        or datadict.metadata.extraAttributes.get("studyDescription")
        or ""
    )
    protocol_name = (
        getattr(datadict.metadata, "protocolName", None)
        or datadict.metadata.extraAttributes.get("protocolName")
        or study_key
    )

    study_elem = ET.Element(f"{{{ODM_NS}}}Study", {"OID": study_key})
    gv_elem = ET.SubElement(study_elem, f"{{{ODM_NS}}}GlobalVariables")
    sn_elem = ET.SubElement(gv_elem, f"{{{ODM_NS}}}StudyName")
    sn_elem.text = str(study_name)

    if study_description:
        sd_elem = ET.SubElement(gv_elem, f"{{{ODM_NS}}}StudyDescription")
        sd_elem.text = str(study_description)
    if protocol_name:
        on_elem = ET.SubElement(gv_elem, f"{{{ODM_NS}}}ProtocolName")
        on_elem.text = str(protocol_name)

    sorted_forms = sorted(datadict.forms, key=lambda f: f.formKey or "")

    study_units = _collect_study_measurement_units(datadict, sorted_forms)
    if study_units:
        bd_elem = ET.SubElement(study_elem, f"{{{ODM_NS}}}BasicDefinitions")
        for unit in study_units:
            mu_elem = ET.SubElement(
                bd_elem,
                f"{{{ODM_NS}}}MeasurementUnit",
                {"OID": unit["oid"], "Name": unit["name"]},
            )
            sym_elem = ET.SubElement(mu_elem, f"{{{ODM_NS}}}Symbol")
            tt_sym = ET.SubElement(sym_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"})
            tt_sym.text = unit["symbol"]

    mdv_elem = ET.SubElement(
        study_elem,
        f"{{{ODM_NS}}}MetaDataVersion",
        {"OID": meta_data_version_oid, "Name": mdv_name},
    )

    effective_extra_meta = dict(datadict.metadata.extraAttributes)
    if extra_event_oids:
        visits_list = [
            dict(v) for v in (effective_extra_meta.get("visits") or []) if isinstance(v, dict)
        ]
        known_v_oids = {str(v.get("oid")) for v in visits_list if v.get("oid")}
        missing_events = sorted(extra_event_oids - known_v_oids)
        if missing_events:
            all_form_refs = [
                {"formOid": f.formKey, "orderNumber": idx, "mandatory": "No"}
                for idx, f in enumerate(sorted_forms, 1)
            ]
            for se_oid in missing_events:
                is_unsched = se_oid == "SE.UNSCHEDULED"
                visits_list.append(
                    {
                        "oid": se_oid,
                        "name": "Unscheduled" if is_unsched else se_oid,
                        "repeating": "Yes",
                        "type": "Unscheduled" if is_unsched else "Scheduled",
                        "forms": all_form_refs,
                    }
                )
            effective_extra_meta["visits"] = visits_list
            proto_copy = dict(effective_extra_meta.get("protocol") or {})
            se_refs_copy = [
                dict(r) for r in (proto_copy.get("studyEventRefs") or []) if isinstance(r, dict)
            ]
            known_proto_oids = {str(r.get("studyEventOid")) for r in se_refs_copy}
            for se_oid in missing_events:
                if se_oid not in known_proto_oids:
                    se_refs_copy.append(
                        {
                            "studyEventOid": se_oid,
                            "orderNumber": len(se_refs_copy) + 1,
                            "mandatory": "No",
                        }
                    )
            proto_copy["studyEventRefs"] = se_refs_copy
            effective_extra_meta["protocol"] = proto_copy

    _emit_protocol_and_visits(mdv_elem, effective_extra_meta)

    sorted_vars_by_form, oid_by_occ, referencing_forms_by_oid = _allocate_item_oids(sorted_forms)
    cl_oid_by_occ, codelist_elements = _allocate_codelist_oids(sorted_forms, sorted_vars_by_form)

    item_meta_by_oid: dict[str, tuple[str, str | None]] = {}
    for f_idx, _form in enumerate(sorted_forms):
        for v_idx, var in enumerate(sorted_vars_by_form[f_idx]):
            item_meta_by_oid[oid_by_occ[(f_idx, v_idx)]] = (var.variableName, var.label)

    # 2. FormDef elements
    for form in sorted_forms:
        form_attribs: dict[str, str] = {
            "OID": form.formKey,
            "Name": form.formName or form.formKey,
            "Repeating": str(form.extraAttributes.get("repeating", "No")),
        }
        if form.formType:
            form_attribs["FormType"] = form.formType
        norm_fid = normalize_form_id(form.formId, form_key=form.formKey)
        if norm_fid is not None:
            form_attribs[f"{{{IMEDNET_NS}}}FormId"] = str(norm_fid)

        form_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}FormDef", form_attribs)
        raw_ig_oid = form.extraAttributes.get("itemGroupOID")
        ig_oid = (
            raw_ig_oid.strip()
            if isinstance(raw_ig_oid, str) and raw_ig_oid.strip()
            else f"IG_{form.formKey}"
        )
        ig_ref_attribs: dict[str, str] = {
            "ItemGroupOID": ig_oid,
            "OrderNumber": "1",
            "Mandatory": "No",
        }
        if (ig_cond := form.extraAttributes.get("collectionExceptionConditionOid")) and str(
            ig_cond
        ).strip():
            ig_ref_attribs["CollectionExceptionConditionOID"] = str(ig_cond).strip()
        ET.SubElement(
            form_elem,
            f"{{{ODM_NS}}}ItemGroupRef",
            ig_ref_attribs,
        )

    # 3. ItemGroupDef elements
    ig_oid_by_form_key: dict[str, str] = {}
    emitted_ig_oids: set[str] = set()
    referenced_role_codelists: dict[str, list[str]] = {}
    for f_idx, form in enumerate(sorted_forms):
        raw_ig_oid_val = form.extraAttributes.get("itemGroupOID")
        ig_oid_val = (
            raw_ig_oid_val.strip()
            if isinstance(raw_ig_oid_val, str) and raw_ig_oid_val.strip()
            else f"IG_{form.formKey}"
        )
        ig_oid_by_form_key[form.formKey] = ig_oid_val
        emitted_ig_oids.add(ig_oid_val)
        ig_attribs: dict[str, str] = {
            "OID": ig_oid_val,
            "Name": f"{form.formName or form.formKey} Details",
            "Repeating": str(form.extraAttributes.get("repeating", "No")),
        }
        if ig_oid_val in extra_ref_ig_items or form.extraAttributes.get("isReferenceData") == "Yes":
            ig_attribs["IsReferenceData"] = "Yes"
        domain = _extract_sdtm_attr(form, "Domain")
        sas_dataset = _extract_sdtm_attr(form, "SASDatasetName")
        purpose = _extract_sdtm_attr(form, "Purpose")
        if not domain or not sas_dataset or not purpose:
            for child_var in form.variables:
                if not domain:
                    domain = _extract_sdtm_attr(child_var, "Domain")
                if not sas_dataset:
                    sas_dataset = _extract_sdtm_attr(child_var, "SASDatasetName")
                if not purpose:
                    purpose = _extract_sdtm_attr(child_var, "Purpose")
                if domain and sas_dataset and purpose:
                    break
        if domain:
            ig_attribs["Domain"] = domain
        if sas_dataset:
            ig_attribs["SASDatasetName"] = sas_dataset
        if purpose:
            ig_attribs["Purpose"] = purpose

        ig_elem = ET.SubElement(
            mdv_elem,
            f"{{{ODM_NS}}}ItemGroupDef",
            ig_attribs,
        )
        sorted_vars = sorted_vars_by_form[f_idx]
        for v_idx, var in enumerate(sorted_vars):
            var_idx = v_idx + 1
            req_str = str(var.required).strip() if var.required is not None else ""
            req_lower = req_str.lower()
            is_yes = req_lower in ("yes", "y", "true", "1")
            is_no = req_lower in ("no", "n", "false", "0", "")
            item_oid = oid_by_occ[(f_idx, v_idx)]
            item_ref_attribs: dict[str, str] = {
                "ItemOID": item_oid,
                "OrderNumber": str(var.sequence if var.sequence is not None else var_idx),
                "Mandatory": "Yes" if is_yes else "No",
            }
            if (method_oid := var.extraAttributes.get("methodOid")) and str(method_oid).strip():
                item_ref_attribs["MethodOID"] = str(method_oid).strip()
            role_val = var.extraAttributes.get("Role") or var.extraAttributes.get("role")
            if role_val is not None and str(role_val).strip():
                item_ref_attribs["Role"] = str(role_val).strip()
            role_cl_oid = var.extraAttributes.get("RoleCodeListOID") or var.extraAttributes.get(
                "roleCodeListOid"
            )
            if role_cl_oid is not None and str(role_cl_oid).strip():
                clean_role_cl = str(role_cl_oid).strip()
                item_ref_attribs["RoleCodeListOID"] = clean_role_cl
                role_bucket = referenced_role_codelists.setdefault(clean_role_cl, [])
                if role_val is not None and str(role_val).strip():
                    clean_role = str(role_val).strip()
                    if clean_role not in role_bucket:
                        role_bucket.append(clean_role)
            if (cond_oid := var.extraAttributes.get("collectionExceptionConditionOid")) and str(
                cond_oid
            ).strip():
                item_ref_attribs["CollectionExceptionConditionOID"] = str(cond_oid).strip()
            if req_str and not is_yes and not is_no:
                item_ref_attribs[f"{{{IMEDNET_NS}}}RequiredPolicy"] = req_str

            ET.SubElement(ig_elem, f"{{{ODM_NS}}}ItemRef", item_ref_attribs)

    extra_ref_item_oids: list[str] = []
    for ref_ig_oid in sorted(extra_ref_ig_items):
        if ref_ig_oid in emitted_ig_oids:
            continue
        emitted_ig_oids.add(ref_ig_oid)
        ref_ig_elem = ET.SubElement(
            mdv_elem,
            f"{{{ODM_NS}}}ItemGroupDef",
            {
                "OID": ref_ig_oid,
                "Name": ref_ig_oid,
                "Repeating": "Yes",
                "IsReferenceData": "Yes",
            },
        )
        for idx, ref_item_oid in enumerate(sorted(extra_ref_ig_items[ref_ig_oid]), 1):
            ET.SubElement(
                ref_ig_elem,
                f"{{{ODM_NS}}}ItemRef",
                {"ItemOID": ref_item_oid, "OrderNumber": str(idx), "Mandatory": "No"},
            )
            if ref_item_oid not in extra_ref_item_oids:
                extra_ref_item_oids.append(ref_item_oid)

    # 4. ItemDef elements
    emitted_item_defs: set[str] = set()
    item_def_elements: list[tuple[Variable, str, str, str, str | None]] = []
    for f_idx, form in enumerate(sorted_forms):
        sorted_vars = sorted_vars_by_form[f_idx]
        for v_idx, var in enumerate(sorted_vars):
            item_oid = oid_by_occ[(f_idx, v_idx)]
            if item_oid not in emitted_item_defs:
                emitted_item_defs.add(item_oid)
                datatype, _ = _map_to_odm_datatype(
                    var.variableType,
                    precision=var.precision,
                    has_choices=bool(var.choices),
                    extra_attributes=var.extraAttributes,
                )
                item_def_elements.append(
                    (var, form.formKey, item_oid, datatype, cl_oid_by_occ.get((f_idx, v_idx)))
                )

    for var, form_key, item_oid, datatype, item_cl_oid in item_def_elements:
        _, control_type = _map_to_odm_datatype(
            var.variableType,
            precision=var.precision,
            has_choices=bool(var.choices),
            extra_attributes=var.extraAttributes,
        )
        vt_norm = normalize_variable_type(var.variableType, precision=var.precision)

        item_attribs: dict[str, str] = {
            "OID": item_oid,
            "Name": var.variableName,
            "DataType": datatype,
        }
        vendor_item_attribs: dict[str, str] = {}
        if vt_norm in _ROUNDTRIP_VENDOR_VARIABLE_TYPES or (
            vt_norm
            and datatype in _PRESERVED_ODM_DATA_TYPES
            and vt_norm != normalize_variable_type(datatype)
        ):
            vendor_item_attribs[f"{{{IMEDNET_NS}}}VariableType"] = vt_norm
        ref_forms = referencing_forms_by_oid.get(item_oid, [form_key])
        if any(
            _default_unwrapped_name(item_oid, fk, ig_oid_by_form_key.get(fk)) != var.variableName
            or (fk and var.variableName.startswith((f"{fk}.", f"IT.{fk}.", f"IG_{fk}.")))
            for fk in ref_forms
        ):
            vendor_item_attribs[f"{{{IMEDNET_NS}}}VariableName"] = var.variableName
        if var.format is not None and str(var.format).strip():
            fmt_str = str(var.format).strip()
            if fmt_str.isascii() and fmt_str.isdigit() and int(fmt_str) > 0:
                item_attribs["Length"] = fmt_str
            else:
                vendor_item_attribs[f"{{{IMEDNET_NS}}}Format"] = fmt_str
        if var.precision is not None and str(var.precision).strip():
            prec_str = str(var.precision).strip()
            if prec_str.isascii() and prec_str.isdigit():
                item_attribs["SignificantDigits"] = prec_str
            else:
                vendor_item_attribs[f"{{{IMEDNET_NS}}}Precision"] = prec_str
        if control_type:
            item_attribs["ControlType"] = control_type

        origin_desc = _extract_sdtm_attr(var, "originDescription")
        origin_source = _extract_sdtm_attr(var, "originSource")
        has_origin_child = bool(origin_desc or origin_source)
        for sdtm_key in ("SDSVarName", "SASFieldName", "Origin", "Comment"):
            if sdtm_key == "Origin" and has_origin_child:
                continue
            if sdtm_val := _extract_sdtm_attr(var, sdtm_key):
                item_attribs[sdtm_key] = sdtm_val
        item_attribs.update(vendor_item_attribs)

        item_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}ItemDef", item_attribs)
        if var.label:
            q_elem = ET.SubElement(item_elem, f"{{{ODM_NS}}}Question")
            tt_elem = ET.SubElement(q_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"})
            tt_elem.text = var.label

        for unit in normalize_measurement_units(var.extraAttributes):
            ET.SubElement(
                item_elem,
                f"{{{ODM_NS}}}MeasurementUnitRef",
                {"MeasurementUnitOID": unit["oid"]},
            )

        for rc in normalize_variable_bounds(var.extraAttributes):
            rc_attribs: dict[str, str] = {
                "SoftHard": str(rc.get("softHard") or "Soft"),
            }
            if comp := rc.get("comparator"):
                rc_attribs["Comparator"] = str(comp)
            rc_elem = ET.SubElement(item_elem, f"{{{ODM_NS}}}RangeCheck", rc_attribs)
            if "checkValues" in rc and isinstance(rc["checkValues"], list):
                for cv in rc["checkValues"]:
                    cv_elem = ET.SubElement(rc_elem, f"{{{ODM_NS}}}CheckValue")
                    cv_elem.text = str(cv)
            elif (cv_single := rc.get("checkValue")) is not None:
                cv_elem = ET.SubElement(rc_elem, f"{{{ODM_NS}}}CheckValue")
                cv_elem.text = str(cv_single)
            if fe := rc.get("formalExpression"):
                fe_attribs: dict[str, str] = {}
                if ctx := rc.get("context"):
                    fe_attribs["Context"] = str(ctx)
                fe_elem = ET.SubElement(rc_elem, f"{{{ODM_NS}}}FormalExpression", fe_attribs)
                fe_elem.text = str(fe)
            if rc_mu := rc.get("measurementUnitOid"):
                ET.SubElement(
                    rc_elem,
                    f"{{{ODM_NS}}}MeasurementUnitRef",
                    {"MeasurementUnitOID": str(rc_mu)},
                )
            if err_msg := rc.get("errorMessage"):
                err_elem = ET.SubElement(rc_elem, f"{{{ODM_NS}}}ErrorMessage")
                tt_err = ET.SubElement(err_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"})
                tt_err.text = str(err_msg)

        if item_cl_oid:
            ET.SubElement(item_elem, f"{{{ODM_NS}}}CodeListRef", {"CodeListOID": item_cl_oid})

        if has_origin_child:
            origin_attribs: dict[str, str] = {}
            if origin_type := _extract_sdtm_attr(var, "Origin"):
                origin_attribs["Type"] = origin_type
            if origin_source:
                origin_attribs["Source"] = origin_source
            origin_elem = ET.SubElement(item_elem, f"{{{ODM_NS}}}Origin", origin_attribs)
            if origin_desc:
                od_elem = ET.SubElement(origin_elem, f"{{{ODM_NS}}}Description")
                tt_od = ET.SubElement(od_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"})
                tt_od.text = origin_desc

    for ref_item_oid in extra_ref_item_oids:
        if ref_item_oid not in emitted_item_defs:
            emitted_item_defs.add(ref_item_oid)
            item_meta_by_oid.setdefault(ref_item_oid, (ref_item_oid, None))
            ET.SubElement(
                mdv_elem,
                f"{{{ODM_NS}}}ItemDef",
                {"OID": ref_item_oid, "Name": ref_item_oid, "DataType": "text"},
            )

    # 5. CodeList elements
    emitted_cl_oids: set[str] = set()
    for cl_oid, cl_name, cl_dtype, choices in codelist_elements:
        emitted_cl_oids.add(cl_oid)
        cl_elem = ET.SubElement(
            mdv_elem,
            f"{{{ODM_NS}}}CodeList",
            {"OID": cl_oid, "Name": cl_name, "DataType": cl_dtype},
        )
        sorted_choices = sorted(
            choices, key=lambda c: _sort_key_int_then_str(c.position, c.choiceValue)
        )
        for c_idx, choice in enumerate(sorted_choices, 1):
            pos_str = str(choice.position if choice.position is not None else c_idx)
            if choice.choiceText == choice.choiceValue:
                ET.SubElement(
                    cl_elem,
                    f"{{{ODM_NS}}}EnumeratedItem",
                    {"CodedValue": choice.choiceValue, "OrderNumber": pos_str},
                )
            else:
                item_child = ET.SubElement(
                    cl_elem,
                    f"{{{ODM_NS}}}CodeListItem",
                    {"CodedValue": choice.choiceValue, "OrderNumber": pos_str},
                )
                decode_elem = ET.SubElement(item_child, f"{{{ODM_NS}}}Decode")
                tt_child = ET.SubElement(
                    decode_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"}
                )
                tt_child.text = choice.choiceText

    role_cls = datadict.metadata.extraAttributes.get("roleCodeLists")
    if isinstance(role_cls, list):
        for rcl in role_cls:
            if not isinstance(rcl, dict):
                continue
            rcl_oid = str(rcl.get("oid") or "").strip()
            if not rcl_oid or rcl_oid in emitted_cl_oids:
                continue
            emitted_cl_oids.add(rcl_oid)
            rcl_elem = ET.SubElement(
                mdv_elem,
                f"{{{ODM_NS}}}CodeList",
                {
                    "OID": rcl_oid,
                    "Name": str(rcl.get("name") or rcl_oid),
                    "DataType": str(rcl.get("dataType") or "text"),
                },
            )
            rcl_items = rcl.get("items")
            if isinstance(rcl_items, list):
                for r_idx, r_item in enumerate(rcl_items, 1):
                    if not isinstance(r_item, dict):
                        continue
                    cval = str(r_item.get("choiceValue") or "")
                    ctext = str(r_item.get("choiceText") or cval)
                    pos_str = str(r_item.get("position") or r_idx)
                    if ctext == cval:
                        ET.SubElement(
                            rcl_elem,
                            f"{{{ODM_NS}}}EnumeratedItem",
                            {"CodedValue": cval, "OrderNumber": pos_str},
                        )
                    else:
                        item_child = ET.SubElement(
                            rcl_elem,
                            f"{{{ODM_NS}}}CodeListItem",
                            {"CodedValue": cval, "OrderNumber": pos_str},
                        )
                        decode_elem = ET.SubElement(item_child, f"{{{ODM_NS}}}Decode")
                        tt_child = ET.SubElement(
                            decode_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"}
                        )
                        tt_child.text = ctext

    for rcl_oid, role_values in referenced_role_codelists.items():
        if rcl_oid in emitted_cl_oids:
            continue
        emitted_cl_oids.add(rcl_oid)
        rcl_elem = ET.SubElement(
            mdv_elem,
            f"{{{ODM_NS}}}CodeList",
            {
                "OID": rcl_oid,
                "Name": rcl_oid,
                "DataType": "text",
                f"{{{IMEDNET_NS}}}SyntheticRoleCodeList": "Yes",
            },
        )
        for r_idx, r_val in enumerate(role_values or ["Role"], 1):
            ET.SubElement(
                rcl_elem,
                f"{{{ODM_NS}}}EnumeratedItem",
                {"CodedValue": r_val, "OrderNumber": str(r_idx)},
            )

    for fcl_oid, flag_vals in sorted(extra_flag_codelists.items()):
        if fcl_oid in emitted_cl_oids:
            continue
        emitted_cl_oids.add(fcl_oid)
        fcl_elem = ET.SubElement(
            mdv_elem,
            f"{{{ODM_NS}}}CodeList",
            {
                "OID": fcl_oid,
                "Name": fcl_oid,
                "DataType": "text",
                f"{{{IMEDNET_NS}}}SyntheticRoleCodeList": "Yes",
            },
        )
        for f_idx_v, f_val in enumerate(flag_vals or ["Open"], 1):
            ET.SubElement(
                fcl_elem,
                f"{{{ODM_NS}}}EnumeratedItem",
                {"CodedValue": f_val, "OrderNumber": str(f_idx_v)},
            )

    # 6. ConditionDef and MethodDef elements
    scoped_rules: list[tuple[str | None, BusinessLogicRule]] = []
    for form in sorted_forms:
        sorted_form_rules = sorted(
            form.businessLogic,
            key=lambda r: _sort_key_int_then_str(r.sequence, str(r.id) if r.id is not None else ""),
        )
        for rule in sorted_form_rules:
            scoped_rules.append((form.formKey, rule))
    sorted_unassigned_rules = sorted(
        datadict.unassignedBusinessLogic,
        key=lambda r: _sort_key_int_then_str(r.sequence, str(r.id) if r.id is not None else ""),
    )
    for rule in sorted_unassigned_rules:
        scoped_rules.append((None, rule))
    if scoped_rules:
        _emit_formal_rules(mdv_elem, scoped_rules)

    return study_elem, item_meta_by_oid


def _xml_escape_attr(value: str) -> str:
    """Escape XML attribute text safely."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def write_odm_xml_stream(
    datadict: DataDictionary | OdmArchive,
    target: TextIO | BinaryIO | Path | str,
    *,
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    indent: str = "  ",
    odm_version: str | None = None,
    file_type: str | None = None,
    originator: str | None = None,
    source_system: str | None = None,
    source_system_version: str | None = None,
    admin_data: Sequence[OdmAdminData] = (),
    reference_data: Sequence[OdmReferenceData] = (),
    clinical_data: Sequence[OdmClinicalData] = (),
    subject_stream: Iterable[OdmSubjectData] | None = None,
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
) -> None:
    """Stream CDISC ODM XML (1.3 or 1.3.2) incrementally without materializing all SubjectData."""
    ET.register_namespace("", ODM_NS)
    ET.register_namespace("imednet", IMEDNET_NS)

    registry = build_study_sensitivity_registry(custom_phi_keys)

    if isinstance(datadict, OdmArchive):
        archive = datadict
        dd = archive.data_dictionary
        admin_list = list(admin_data) if admin_data else list(archive.admin_data)
        ref_list = list(reference_data) if reference_data else list(archive.reference_data)
        clin_list = list(clinical_data) if clinical_data else list(archive.clinical_data)
        eff_odm_ver = odm_version or archive.odm_version or "1.3.2"
        eff_file_type = file_type or archive.file_type or "Snapshot"
        eff_file_oid: str | None = file_oid or archive.file_oid
        eff_creation_dt = creation_datetime or archive.creation_datetime
        eff_mdv_oid = (
            meta_data_version_oid
            if meta_data_version_oid != "MDV.001"
            else (archive.metadata_version_oid or "MDV.001")
        )
        eff_mdv_name = meta_data_version_name or archive.metadata_version_name
        eff_originator = originator if originator is not None else archive.originator
        eff_source_sys = source_system if source_system is not None else archive.source_system
        eff_source_ver = (
            source_system_version
            if source_system_version is not None
            else archive.source_system_version
        )
        eff_as_of = archive.as_of_datetime
        eff_desc = archive.description
    else:
        dd = datadict
        admin_list = list(admin_data)
        ref_list = list(reference_data)
        clin_list = list(clinical_data)
        has_containers = bool(admin_list or ref_list or clin_list or subject_stream is not None)
        eff_odm_ver = odm_version or ("1.3.2" if has_containers else "1.3")
        eff_file_type = file_type or "Snapshot"
        eff_file_oid = file_oid
        eff_creation_dt = creation_datetime
        eff_mdv_oid = meta_data_version_oid
        eff_mdv_name = meta_data_version_name
        eff_originator = originator
        eff_source_sys = source_system
        eff_source_ver = source_system_version
        eff_as_of = None
        eff_desc = None

    study_key = dd.metadata.studyKey or "STUDY_001"
    now_iso = eff_creation_dt or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    f_oid = eff_file_oid or f"ODM_{study_key}_{now_iso}"
    mdv_name = (
        eff_mdv_name
        or getattr(dd.metadata, "metaDataVersionName", None)
        or "Initial Metadata Version"
    )

    extra_event_oids, extra_flag_cls, extra_ref_igs = _collect_referenced_archive_defs(
        clin_list, ref_list
    )
    study_elem, item_meta_by_oid = _build_study_metadata_elem(
        dd,
        study_key=study_key,
        meta_data_version_oid=eff_mdv_oid,
        mdv_name=mdv_name,
        extra_event_oids=extra_event_oids,
        extra_flag_codelists=extra_flag_cls,
        extra_ref_ig_items=extra_ref_igs,
    )

    study_serialized = ET.tostring(study_elem, encoding="unicode")
    needs_imednet_ns = (
        f"{{{IMEDNET_NS}}}" in str([el.attrib for el in study_elem.iter()])
        or "imednet:" in study_serialized
    )

    def _write_chunk(writer: Any, text: str, is_binary: bool) -> None:
        if is_binary:
            writer.write(text.encode("utf-8"))
        else:
            writer.write(text)

    def _emit_to_handle(handle: Any, is_binary: bool) -> None:
        _write_chunk(handle, "<?xml version='1.0' encoding='utf-8'?>\n", is_binary)

        root_attrs: list[tuple[str, str]] = [("xmlns", ODM_NS)]
        if needs_imednet_ns:
            root_attrs.append(("xmlns:imednet", IMEDNET_NS))
        root_attrs.extend(
            [
                ("ODMVersion", eff_odm_ver),
                ("FileType", eff_file_type),
                ("FileOID", f_oid),
                ("CreationDateTime", now_iso),
            ]
        )
        if eff_as_of:
            root_attrs.append(("AsOfDateTime", eff_as_of))
        if eff_originator:
            root_attrs.append(("Originator", eff_originator))
        if eff_source_sys:
            root_attrs.append(("SourceSystem", eff_source_sys))
        if eff_source_ver:
            root_attrs.append(("SourceSystemVersion", eff_source_ver))
        if eff_desc:
            root_attrs.append(("Description", eff_desc))

        attrs_str = " ".join(f'{k}="{_xml_escape_attr(v)}"' for k, v in root_attrs)
        _write_chunk(handle, f"<ODM {attrs_str}>\n", is_binary)

        def _strip_redundant_ns(fragment: str) -> str:
            cleaned = fragment.replace(f' xmlns="{ODM_NS}"', "")
            if needs_imednet_ns:
                cleaned = cleaned.replace(f' xmlns:imednet="{IMEDNET_NS}"', "")
            return cleaned

        ET.indent(study_elem, space=indent, level=1)
        _write_chunk(
            handle,
            f"{indent}{_strip_redundant_ns(ET.tostring(study_elem, encoding='unicode'))}\n",
            is_binary,
        )

        used_xml_ids: set[str] = set()
        allow_remove = eff_file_type == "Transactional"

        for admin in admin_list:
            admin_elem = _build_admin_data_elem(
                admin,
                default_study_oid=study_key,
                default_mdv_oid=eff_mdv_oid,
                mask_phi=mask_phi,
                registry=registry,
            )
            ET.indent(admin_elem, space=indent, level=1)
            _write_chunk(
                handle,
                f"{indent}{_strip_redundant_ns(ET.tostring(admin_elem, encoding='unicode'))}\n",
                is_binary,
            )

        effective_clin_list = list(clin_list)
        if subject_stream is not None and not effective_clin_list:
            effective_clin_list = [
                OdmClinicalData(
                    studyOID=study_key,
                    metaDataVersionOID=eff_mdv_oid,
                )
            ]

        reserved_pool_ids: set[str] = set()
        for clin_pre in effective_clin_list:
            for ar_pre in clin_pre.audit_records:
                if ar_pre.id:
                    reserved_pool_ids.add(ar_pre.id)
            for ann_pre in clin_pre.annotations:
                if ann_pre.id:
                    reserved_pool_ids.add(ann_pre.id)

        for ref in ref_list:
            ref_elem = _build_reference_data_elem(
                ref,
                item_meta_by_oid=item_meta_by_oid,
                used_xml_ids=used_xml_ids,
                known_target_xml_ids=reserved_pool_ids,
                reserved_pool_ids=reserved_pool_ids,
                allow_remove=allow_remove,
                mask_phi=mask_phi,
                registry=registry,
            )
            ET.indent(ref_elem, space=indent, level=1)
            _write_chunk(
                handle,
                f"{indent}{_strip_redundant_ns(ET.tostring(ref_elem, encoding='unicode'))}\n",
                is_binary,
            )

        for c_idx, clin in enumerate(effective_clin_list):
            clin_study = _xml_escape_attr(clin.study_oid)
            clin_mdv = _xml_escape_attr(clin.metadata_version_oid)
            subjects_iter: Iterable[OdmSubjectData] = (
                subject_stream if (c_idx == 0 and subject_stream is not None) else clin.subjects
            )

            # Pre-register pool AuditRecord and Annotation XML IDs before streaming SubjectData
            clin_pool_ids: set[str] = set(reserved_pool_ids)
            for ar_pool in clin.audit_records:
                if ar_pool.id:
                    clin_pool_ids.add(ar_pool.id)
            for ann_pool in clin.annotations:
                if ann_pool.id:
                    clin_pool_ids.add(ann_pool.id)

            opened_clin = False

            def _ensure_clin_open(
                c_study: str = clin_study,
                c_mdv: str = clin_mdv,
            ) -> None:
                nonlocal opened_clin
                if not opened_clin:
                    opened_clin = True
                    _write_chunk(
                        handle,
                        f'{indent}<ClinicalData StudyOID="{c_study}" '
                        f'MetaDataVersionOID="{c_mdv}">\n',
                        is_binary,
                    )

            for subj in subjects_iter:
                _ensure_clin_open()
                subj_elem = _build_subject_data_elem(
                    subj,
                    item_meta_by_oid=item_meta_by_oid,
                    used_xml_ids=used_xml_ids,
                    known_target_xml_ids=clin_pool_ids,
                    reserved_pool_ids=reserved_pool_ids,
                    allow_remove=allow_remove,
                    mask_phi=mask_phi,
                    registry=registry,
                )
                ET.indent(subj_elem, space=indent, level=2)
                _write_chunk(
                    handle,
                    f"{indent * 2}"
                    f"{_strip_redundant_ns(ET.tostring(subj_elem, encoding='unicode'))}\n",
                    is_binary,
                )

            if clin.audit_records:
                _ensure_clin_open()
                ars_elem = ET.Element(f"{{{ODM_NS}}}AuditRecords")
                for ar in clin.audit_records:
                    _build_audit_record_elem(
                        ars_elem,
                        ar,
                        used_xml_ids=used_xml_ids,
                        reserved_pool_ids=reserved_pool_ids,
                        is_pool_element=True,
                        mask_phi=mask_phi,
                        registry=registry,
                    )
                ET.indent(ars_elem, space=indent, level=2)
                _write_chunk(
                    handle,
                    f"{indent * 2}"
                    f"{_strip_redundant_ns(ET.tostring(ars_elem, encoding='unicode'))}\n",
                    is_binary,
                )

            if clin.annotations:
                _ensure_clin_open()
                anns_elem = ET.Element(f"{{{ODM_NS}}}Annotations")
                for ann in clin.annotations:
                    _build_annotation_elem(
                        anns_elem,
                        ann,
                        used_xml_ids=used_xml_ids,
                        reserved_pool_ids=reserved_pool_ids,
                        is_pool_element=True,
                        allow_remove=allow_remove,
                        mask_phi=mask_phi,
                        registry=registry,
                    )
                ET.indent(anns_elem, space=indent, level=2)
                _write_chunk(
                    handle,
                    f"{indent * 2}"
                    f"{_strip_redundant_ns(ET.tostring(anns_elem, encoding='unicode'))}\n",
                    is_binary,
                )

            if opened_clin:
                _write_chunk(handle, f"{indent}</ClinicalData>\n", is_binary)
            else:
                _write_chunk(
                    handle,
                    f'{indent}<ClinicalData StudyOID="{clin_study}" '
                    f'MetaDataVersionOID="{clin_mdv}" />\n',
                    is_binary,
                )

        _write_chunk(handle, "</ODM>\n", is_binary)

    if isinstance(target, (str, Path)):
        dest = Path(target)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as fh:
            _emit_to_handle(fh, False)
        return

    mode = getattr(target, "mode", "")
    is_bin = "b" in mode or isinstance(target, BinaryIO)
    _emit_to_handle(target, is_bin)


def to_odm_xml(
    datadict: DataDictionary | OdmArchive,
    *,
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    indent: str = "  ",
    odm_version: str | None = None,
    file_type: str | None = None,
    originator: str | None = None,
    source_system: str | None = None,
    source_system_version: str | None = None,
    admin_data: Sequence[OdmAdminData] = (),
    reference_data: Sequence[OdmReferenceData] = (),
    clinical_data: Sequence[OdmClinicalData] = (),
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
) -> str:
    """Serialize DataDictionary or OdmArchive to CDISC ODM 1.3 / 1.3.2 XML format.

    This function does not mutate the input DataDictionary or OdmArchive instance.
    """
    buf = StringIO()
    write_odm_xml_stream(
        datadict,
        buf,
        file_oid=file_oid,
        creation_datetime=creation_datetime,
        meta_data_version_oid=meta_data_version_oid,
        meta_data_version_name=meta_data_version_name,
        indent=indent,
        odm_version=odm_version,
        file_type=file_type,
        originator=originator,
        source_system=source_system,
        source_system_version=source_system_version,
        admin_data=admin_data,
        reference_data=reference_data,
        clinical_data=clinical_data,
        mask_phi=mask_phi,
        custom_phi_keys=custom_phi_keys,
    )
    return buf.getvalue()


def to_odm_file(
    datadict: DataDictionary | OdmArchive,
    file_path: Path | str,
    *,
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    indent: str = "  ",
    odm_version: str | None = None,
    file_type: str | None = None,
    originator: str | None = None,
    source_system: str | None = None,
    source_system_version: str | None = None,
    admin_data: Sequence[OdmAdminData] = (),
    reference_data: Sequence[OdmReferenceData] = (),
    clinical_data: Sequence[OdmClinicalData] = (),
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
) -> None:
    """Write CDISC ODM 1.3 / 1.3.2 XML content to a UTF-8 encoded file."""
    write_odm_xml_stream(
        datadict,
        file_path,
        file_oid=file_oid,
        creation_datetime=creation_datetime,
        meta_data_version_oid=meta_data_version_oid,
        meta_data_version_name=meta_data_version_name,
        indent=indent,
        odm_version=odm_version,
        file_type=file_type,
        originator=originator,
        source_system=source_system,
        source_system_version=source_system_version,
        admin_data=admin_data,
        reference_data=reference_data,
        clinical_data=clinical_data,
        mask_phi=mask_phi,
        custom_phi_keys=custom_phi_keys,
    )


__all__ = [
    "sort_data_dictionary",
    "to_file",
    "to_json",
    "to_odm_file",
    "to_odm_xml",
    "write_odm_xml_stream",
]
