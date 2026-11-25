"""Canonical JSON and CDISC ODM XML emission for iMednet Data Dictionary."""

from __future__ import annotations

import copy
import json

# Serialization only: this module never parses XML; defusedxml has no Element builder.
import xml.etree.ElementTree as ET  # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO

from .models import BusinessLogicRule, Choice, DataDictionary, Form, Variable
from .normalize import (
    normalize_form_id,
    normalize_measurement_units,
    normalize_variable_bounds,
    normalize_variable_type,
)

if TYPE_CHECKING:
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
        OdmStudyEventData,
        OdmSubjectData,
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


def _default_unwrapped_name(item_oid: str, form_key: str) -> str:
    """Return the variableName that ODM import infers from item_oid without VariableName."""
    raw_oid = item_oid.strip()
    prefixes: list[str] = []
    if form_key:
        prefixes.extend([f"IT.{form_key}.", f"{form_key}."])
    prefixes.append(f"IG_{form_key}.")
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
    unscoped_oid_by_name: dict[str, str] = {}
    scoped_oid_by_group: dict[tuple[str, str, Any], str] = {}

    # Pass 1: Reserve unprefixed OIDs for variables with a single consistent definition
    for sorted_vars in sorted_vars_by_form:
        for var in sorted_vars:
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
            if var.variableName in scoped_var_names:
                group_key = (form.formKey, var.variableName, sig_by_occ[(f_idx, v_idx)])
                item_oid = scoped_oid_by_group.get(group_key)
                if item_oid is None:
                    item_oid = _allocate_scoped_oid(
                        form.formKey,
                        var.variableName,
                        used_item_oids,
                        reserved_raw_names,
                    )
                    scoped_oid_by_group[group_key] = item_oid
            else:
                item_oid = unscoped_oid_by_name[var.variableName]

            oid_by_occ[(f_idx, v_idx)] = item_oid
            ref_forms = referencing_forms_by_oid.setdefault(item_oid, [])
            if form.formKey not in ref_forms:
                ref_forms.append(form.formKey)

    return sorted_vars_by_form, oid_by_occ, referencing_forms_by_oid


def resolve_form_item_oids(datadict: DataDictionary) -> dict[tuple[str, str], str]:
    """Return deterministic ``(formKey, variableName) -> ItemOID`` mappings for ``datadict``."""
    sorted_forms = sorted(datadict.forms, key=lambda f: f.formKey or "")
    sorted_vars_by_form, oid_by_occ, _ = _allocate_item_oids(sorted_forms)
    mapping: dict[tuple[str, str], str] = {}
    for f_idx, form in enumerate(sorted_forms):
        for v_idx, var in enumerate(sorted_vars_by_form[f_idx]):
            mapping[(form.formKey, var.variableName)] = oid_by_occ[(f_idx, v_idx)]
    return mapping


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


def _build_study_elem(
    datadict: DataDictionary,
    *,
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    strict_132: bool = False,
) -> ET.Element:
    """Build the ``<Study>`` metadata subtree for ``datadict`` without mutating inputs."""
    study_key = datadict.metadata.studyKey or "STUDY_001"
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
    mdv_name = (
        meta_data_version_name
        or getattr(datadict.metadata, "metaDataVersionName", None)
        or "Initial Metadata Version"
    )

    study_elem = ET.Element(f"{{{ODM_NS}}}Study", {"OID": study_key})
    gv_elem = ET.SubElement(study_elem, f"{{{ODM_NS}}}GlobalVariables")
    sn_elem = ET.SubElement(gv_elem, f"{{{ODM_NS}}}StudyName")
    sn_elem.text = str(study_name)

    if study_description or strict_132:
        sd_elem = ET.SubElement(gv_elem, f"{{{ODM_NS}}}StudyDescription")
        sd_elem.text = str(study_description)
    if protocol_name or strict_132:
        on_elem = ET.SubElement(gv_elem, f"{{{ODM_NS}}}ProtocolName")
        on_elem.text = str(protocol_name)

    # 1. Deterministic form ordering without mutating datadict
    sorted_forms = sorted(datadict.forms, key=lambda f: f.formKey or "")

    # 1b. BasicDefinitions / MeasurementUnit elements (when units exist)
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

    _emit_protocol_and_visits(mdv_elem, datadict.metadata.extraAttributes)

    # Allocate globally unique ItemDef and CodeList OIDs across unscoped and form-scoped variables
    sorted_vars_by_form, oid_by_occ, referencing_forms_by_oid = _allocate_item_oids(sorted_forms)
    cl_oid_by_occ, codelist_elements = _allocate_codelist_oids(sorted_forms, sorted_vars_by_form)

    # 2. FormDef elements
    for form in sorted_forms:
        form_attribs: dict[str, str] = {
            "OID": form.formKey,
            "Name": form.formName or form.formKey,
            "Repeating": str(form.extraAttributes.get("repeating", "No")),
        }
        if form.formType:
            form_type_attr = f"{{{IMEDNET_NS}}}FormType" if strict_132 else "FormType"
            form_attribs[form_type_attr] = form.formType
        norm_fid = normalize_form_id(form.formId, form_key=form.formKey)
        if norm_fid is not None:
            form_attribs[f"{{{IMEDNET_NS}}}FormId"] = str(norm_fid)

        form_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}FormDef", form_attribs)
        ig_oid = f"IG_{form.formKey}"
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
    referenced_role_codelists: dict[str, list[str]] = {}
    for f_idx, form in enumerate(sorted_forms):
        ig_attribs: dict[str, str] = {
            "OID": f"IG_{form.formKey}",
            "Name": f"{form.formName or form.formKey} Details",
            "Repeating": str(form.extraAttributes.get("repeating", "No")),
        }
        if str(form.extraAttributes.get("isReferenceData", "")).lower() in ("yes", "true", "1") or (
            (form.formType or "").strip().lower()
            in {"reference", "study", "non-subject", "nonsubject"}
        ):
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

    # 4. ItemDef elements (globally unique OIDs preserving source names and distinct definitions)
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
            _default_unwrapped_name(item_oid, fk) != var.variableName
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
            if strict_132:
                vendor_item_attribs[f"{{{IMEDNET_NS}}}ControlType"] = control_type
            else:
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

    # 6. ConditionDef and MethodDef elements (ODM formal rules and iMednet business logic rules)
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

    return study_elem


def _build_audit_record_elem(audit: OdmAuditRecord) -> ET.Element:
    attribs: dict[str, str] = {}
    if audit.edit_point:
        attribs["EditPoint"] = audit.edit_point
    if audit.used_imputation_method:
        attribs["UsedImputationMethod"] = audit.used_imputation_method
    if audit.audit_id:
        attribs["ID"] = audit.audit_id

    ar_elem = ET.Element(f"{{{ODM_NS}}}AuditRecord", attribs)
    ET.SubElement(ar_elem, f"{{{ODM_NS}}}UserRef", {"UserOID": audit.user_oid})
    ET.SubElement(ar_elem, f"{{{ODM_NS}}}LocationRef", {"LocationOID": audit.location_oid})
    dt_elem = ET.SubElement(ar_elem, f"{{{ODM_NS}}}DateTimeStamp")
    dt_elem.text = audit.date_time_stamp
    if audit.reason_for_change is not None:
        rfc_elem = ET.SubElement(ar_elem, f"{{{ODM_NS}}}ReasonForChange")
        rfc_elem.text = audit.reason_for_change
    if audit.source_id is not None:
        src_elem = ET.SubElement(ar_elem, f"{{{ODM_NS}}}SourceID")
        src_elem.text = audit.source_id
    return ar_elem


def _build_annotation_elem(ann: OdmAnnotation) -> ET.Element:
    attribs: dict[str, str] = {"SeqNum": str(ann.seq_num)}
    if ann.transaction_type:
        attribs["TransactionType"] = ann.transaction_type
    if ann.annotation_id:
        attribs["ID"] = ann.annotation_id

    ann_elem = ET.Element(f"{{{ODM_NS}}}Annotation", attribs)
    if ann.comment is not None:
        c_attribs: dict[str, str] = {}
        if ann.comment.sponsor_or_site:
            c_attribs["SponsorOrSite"] = ann.comment.sponsor_or_site
        c_elem = ET.SubElement(ann_elem, f"{{{ODM_NS}}}Comment", c_attribs)
        c_elem.text = ann.comment.text
    for flag in ann.flags:
        f_elem = ET.SubElement(ann_elem, f"{{{ODM_NS}}}Flag")
        fv_elem = ET.SubElement(
            f_elem,
            f"{{{ODM_NS}}}FlagValue",
            {"CodeListOID": flag.flag_value_codelist_oid},
        )
        fv_elem.text = flag.flag_value
        if flag.flag_type is not None:
            ft_elem = ET.SubElement(
                f_elem,
                f"{{{ODM_NS}}}FlagType",
                {"CodeListOID": flag.flag_type_codelist_oid or "CL.FLAG_TYPE"},
            )
            ft_elem.text = flag.flag_type
    return ann_elem


def _build_admin_data_elem(admin: OdmAdminData) -> ET.Element:
    attribs: dict[str, str] = {}
    if admin.study_oid:
        attribs["StudyOID"] = admin.study_oid
    adm_elem = ET.Element(f"{{{ODM_NS}}}AdminData", attribs)

    for usr in sorted(admin.users, key=lambda u: u.oid):
        u_attribs: dict[str, str] = {"OID": usr.oid}
        if usr.user_type:
            u_attribs["UserType"] = usr.user_type
        u_elem = ET.SubElement(adm_elem, f"{{{ODM_NS}}}User", u_attribs)
        for tag_name, val in (
            ("LoginName", usr.login_name),
            ("DisplayName", usr.display_name),
            ("FullName", usr.full_name),
            ("FirstName", usr.first_name),
            ("LastName", usr.last_name),
            ("Organization", usr.organization),
            ("Email", usr.email),
            ("Phone", usr.phone),
        ):
            if val is not None:
                sub = ET.SubElement(u_elem, f"{{{ODM_NS}}}{tag_name}")
                sub.text = val
        for loc_oid in usr.location_oids:
            ET.SubElement(u_elem, f"{{{ODM_NS}}}LocationRef", {"LocationOID": loc_oid})

    for loc in sorted(admin.locations, key=lambda lc: lc.oid):
        l_attribs: dict[str, str] = {
            "OID": loc.oid,
            "Name": loc.name,
            "LocationType": loc.location_type,
        }
        l_elem = ET.SubElement(adm_elem, f"{{{ODM_NS}}}Location", l_attribs)
        for mdv_ref in loc.metadata_version_refs:
            ET.SubElement(
                l_elem,
                f"{{{ODM_NS}}}MetaDataVersionRef",
                {
                    "StudyOID": mdv_ref.study_oid,
                    "MetaDataVersionOID": mdv_ref.metadata_version_oid,
                    "EffectiveDate": mdv_ref.effective_date,
                },
            )

    for sig_def in sorted(admin.signature_defs, key=lambda s: s.oid):
        s_attribs: dict[str, str] = {"OID": sig_def.oid}
        if sig_def.methodology:
            s_attribs["Methodology"] = sig_def.methodology
        s_elem = ET.SubElement(adm_elem, f"{{{ODM_NS}}}SignatureDef", s_attribs)
        meaning_el = ET.SubElement(s_elem, f"{{{ODM_NS}}}Meaning")
        meaning_el.text = sig_def.meaning
        reason_el = ET.SubElement(s_elem, f"{{{ODM_NS}}}LegalReason")
        reason_el.text = sig_def.legal_reason

    return adm_elem


def _build_item_data_elem(item: OdmItemData) -> ET.Element:
    attribs: dict[str, str] = {"ItemOID": item.item_oid}
    if item.transaction_type:
        attribs["TransactionType"] = item.transaction_type
    if item.is_null:
        attribs["IsNull"] = "Yes"
    elif item.value is not None:
        attribs["Value"] = item.value
    if item.audit_record_id:
        attribs["AuditRecordID"] = item.audit_record_id
    if item.annotation_id:
        attribs["AnnotationID"] = item.annotation_id
    if item.measurement_unit_oid:
        attribs["MeasurementUnitOID"] = item.measurement_unit_oid

    it_elem = ET.Element(f"{{{ODM_NS}}}ItemData", attribs)
    if item.audit_record is not None:
        it_elem.append(_build_audit_record_elem(item.audit_record))
    if item.measurement_unit_oid:
        ET.SubElement(
            it_elem,
            f"{{{ODM_NS}}}MeasurementUnitRef",
            {"MeasurementUnitOID": item.measurement_unit_oid},
        )
    for ann in item.annotations:
        it_elem.append(_build_annotation_elem(ann))
    return it_elem


def _build_item_group_data_elem(ig: OdmItemGroupData) -> ET.Element:
    attribs: dict[str, str] = {"ItemGroupOID": ig.item_group_oid}
    if ig.item_group_repeat_key is not None:
        attribs["ItemGroupRepeatKey"] = ig.item_group_repeat_key
    if ig.transaction_type:
        attribs["TransactionType"] = ig.transaction_type
    if ig.audit_record_id:
        attribs["AuditRecordID"] = ig.audit_record_id
    if ig.annotation_id:
        attribs["AnnotationID"] = ig.annotation_id

    ig_elem = ET.Element(f"{{{ODM_NS}}}ItemGroupData", attribs)
    if ig.audit_record is not None:
        ig_elem.append(_build_audit_record_elem(ig.audit_record))
    for ann in ig.annotations:
        ig_elem.append(_build_annotation_elem(ann))
    for item in ig.items:
        ig_elem.append(_build_item_data_elem(item))
    return ig_elem


def _build_reference_data_elem(ref: OdmReferenceData) -> ET.Element:
    ref_elem = ET.Element(
        f"{{{ODM_NS}}}ReferenceData",
        {
            "StudyOID": ref.study_oid,
            "MetaDataVersionOID": ref.metadata_version_oid,
        },
    )
    for ig in ref.item_group_data:
        ref_elem.append(_build_item_group_data_elem(ig))
    return ref_elem


def _build_form_data_elem(fd: OdmFormData) -> ET.Element:
    attribs: dict[str, str] = {"FormOID": fd.form_oid}
    if fd.form_repeat_key is not None:
        attribs["FormRepeatKey"] = fd.form_repeat_key
    if fd.transaction_type:
        attribs["TransactionType"] = fd.transaction_type
    if fd.audit_record_id:
        attribs["AuditRecordID"] = fd.audit_record_id
    if fd.annotation_id:
        attribs["AnnotationID"] = fd.annotation_id

    fd_elem = ET.Element(f"{{{ODM_NS}}}FormData", attribs)
    if fd.audit_record is not None:
        fd_elem.append(_build_audit_record_elem(fd.audit_record))
    for ann in fd.annotations:
        fd_elem.append(_build_annotation_elem(ann))
    for ig in fd.item_group_data:
        fd_elem.append(_build_item_group_data_elem(ig))
    return fd_elem


def _build_study_event_data_elem(sed: OdmStudyEventData) -> ET.Element:
    attribs: dict[str, str] = {"StudyEventOID": sed.study_event_oid}
    if sed.study_event_repeat_key is not None:
        attribs["StudyEventRepeatKey"] = sed.study_event_repeat_key
    if sed.transaction_type:
        attribs["TransactionType"] = sed.transaction_type
    if sed.audit_record_id:
        attribs["AuditRecordID"] = sed.audit_record_id
    if sed.annotation_id:
        attribs["AnnotationID"] = sed.annotation_id

    sed_elem = ET.Element(f"{{{ODM_NS}}}StudyEventData", attribs)
    if sed.audit_record is not None:
        sed_elem.append(_build_audit_record_elem(sed.audit_record))
    for ann in sed.annotations:
        sed_elem.append(_build_annotation_elem(ann))
    for fd in sed.form_data:
        sed_elem.append(_build_form_data_elem(fd))
    return sed_elem


def _build_subject_data_elem(subj: OdmSubjectData) -> ET.Element:
    attribs: dict[str, str] = {"SubjectKey": subj.subject_key}
    if subj.transaction_type:
        attribs["TransactionType"] = subj.transaction_type
    if subj.audit_record_id:
        attribs["AuditRecordID"] = subj.audit_record_id
    if subj.annotation_id:
        attribs["AnnotationID"] = subj.annotation_id

    subj_elem = ET.Element(f"{{{ODM_NS}}}SubjectData", attribs)
    if subj.audit_record is not None:
        subj_elem.append(_build_audit_record_elem(subj.audit_record))
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
        subj_elem.append(_build_annotation_elem(ann))
    for sed in subj.study_event_data:
        subj_elem.append(_build_study_event_data_elem(sed))
    return subj_elem


def _build_clinical_data_elem(clin: OdmClinicalData) -> ET.Element:
    clin_elem = ET.Element(
        f"{{{ODM_NS}}}ClinicalData",
        {
            "StudyOID": clin.study_oid,
            "MetaDataVersionOID": clin.metadata_version_oid,
        },
    )
    for subj in clin.subject_data:
        clin_elem.append(_build_subject_data_elem(subj))
    if clin.audit_records:
        ars_elem = ET.SubElement(clin_elem, f"{{{ODM_NS}}}AuditRecords")
        for aud in clin.audit_records:
            ars_elem.append(_build_audit_record_elem(aud))
    if clin.annotations:
        anns_elem = ET.SubElement(clin_elem, f"{{{ODM_NS}}}Annotations")
        for ann in clin.annotations:
            anns_elem.append(_build_annotation_elem(ann))
    return clin_elem


def _normalize_container_seq(items: Sequence[Any] | Any | None) -> list[Any]:
    if items is None:
        return []
    if isinstance(items, (list, tuple)):
        return list(items)
    return [items]


def to_odm_xml(
    datadict: DataDictionary,
    *,
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    creation_date_time: str | None = None,
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    odm_version: Literal["1.3", "1.3.2"] | None = None,
    file_type: Literal["Snapshot", "Transactional"] = "Snapshot",
    granularity: str | None = None,
    originator: str | None = None,
    source_system: str | None = None,
    source_system_version: str | None = None,
    admin_data: Sequence[OdmAdminData] | OdmAdminData | None = None,
    reference_data: Sequence[OdmReferenceData] | OdmReferenceData | None = None,
    clinical_data: Sequence[OdmClinicalData] | OdmClinicalData | None = None,
    indent: str = "  ",
) -> str:
    """Serialize DataDictionary (and optional AdminData/ReferenceData/ClinicalData) to ODM XML.

    This function does not mutate the input DataDictionary instance.

    Args:
        datadict: DataDictionary instance to serialize.
        file_oid: Optional FileOID attribute for root ODM element.
        creation_datetime: Optional ISO 8601 creation datetime string.
        creation_date_time: Optional alias for ``creation_datetime``.
        meta_data_version_oid: MetaDataVersion OID identifier (default: 'MDV.001').
        meta_data_version_name: Optional MetaDataVersion name.
        odm_version: ODMVersion attribute ('1.3' or '1.3.2'). Defaults to '1.3.2' when
            AdminData/ReferenceData/ClinicalData are present, or '1.3' for metadata-only calls.
        file_type: ODM FileType ('Snapshot' or 'Transactional').
        granularity: Optional ODM Granularity attribute.
        originator: Optional Originator attribute on the root ODM element.
        source_system: Optional SourceSystem attribute.
        source_system_version: Optional SourceSystemVersion attribute.
        admin_data: Optional AdminData container(s) for Site/User metadata.
        reference_data: Optional ReferenceData container(s) for non-subject datasets.
        clinical_data: Optional ClinicalData container(s) for subject clinical records.
        indent: Indentation string for XML formatting (default: two spaces).

    Returns:
        Valid CDISC ODM XML document string.
    """
    ET.register_namespace("", ODM_NS)
    ET.register_namespace("imednet", IMEDNET_NS)

    admin_list = _normalize_container_seq(admin_data)
    ref_list = _normalize_container_seq(reference_data)
    clin_list = _normalize_container_seq(clinical_data)
    has_archive_containers = bool(admin_list or ref_list or clin_list)
    resolved_version = odm_version or ("1.3.2" if has_archive_containers else "1.3")
    strict_132 = resolved_version == "1.3.2"

    study_key = datadict.metadata.studyKey or "STUDY_001"
    resolved_creation = creation_datetime or creation_date_time
    now_iso = resolved_creation or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    f_oid = file_oid or f"ODM_{study_key}_{now_iso}"

    root_attribs: dict[str, str] = {
        "ODMVersion": resolved_version,
        "FileType": file_type,
        "FileOID": f_oid,
        "CreationDateTime": now_iso,
    }
    if granularity:
        root_attribs["Granularity"] = granularity
    if originator:
        root_attribs["Originator"] = originator
    if source_system:
        root_attribs["SourceSystem"] = source_system
    if source_system_version:
        root_attribs["SourceSystemVersion"] = source_system_version

    root = ET.Element(f"{{{ODM_NS}}}ODM", root_attribs)
    root.append(
        _build_study_elem(
            datadict,
            meta_data_version_oid=meta_data_version_oid,
            meta_data_version_name=meta_data_version_name,
            strict_132=strict_132,
        )
    )
    for adm in admin_list:
        root.append(_build_admin_data_elem(adm))
    for ref in ref_list:
        root.append(_build_reference_data_elem(ref))
    for clin in clin_list:
        root.append(_build_clinical_data_elem(clin))

    ET.indent(root, space=indent)
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    xml_str = xml_bytes.decode("utf-8")
    if not xml_str.endswith("\n"):
        xml_str += "\n"
    return str(xml_str)


def to_odm_file(
    datadict: DataDictionary,
    file_path: Path | str,
    *,
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    creation_date_time: str | None = None,
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    odm_version: Literal["1.3", "1.3.2"] | None = None,
    file_type: Literal["Snapshot", "Transactional"] = "Snapshot",
    granularity: str | None = None,
    originator: str | None = None,
    source_system: str | None = None,
    source_system_version: str | None = None,
    admin_data: Sequence[OdmAdminData] | OdmAdminData | None = None,
    reference_data: Sequence[OdmReferenceData] | OdmReferenceData | None = None,
    clinical_data: Sequence[OdmClinicalData] | OdmClinicalData | None = None,
    indent: str = "  ",
) -> None:
    """Write CDISC ODM XML content to a UTF-8 encoded file."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    xml_str = to_odm_xml(
        datadict,
        file_oid=file_oid,
        creation_datetime=creation_datetime or creation_date_time,
        meta_data_version_oid=meta_data_version_oid,
        meta_data_version_name=meta_data_version_name,
        odm_version=odm_version,
        file_type=file_type,
        granularity=granularity,
        originator=originator,
        source_system=source_system,
        source_system_version=source_system_version,
        admin_data=admin_data,
        reference_data=reference_data,
        clinical_data=clinical_data,
        indent=indent,
    )
    path.write_text(xml_str, encoding="utf-8")


def to_odm_archive_xml(
    archive: OdmArchive,
    *,
    meta_data_version_oid: str | None = None,
    meta_data_version_name: str | None = None,
    indent: str = "  ",
) -> str:
    """Serialize a canonical ``OdmArchive`` snapshot to CDISC ODM 1.3.2 XML."""
    ET.register_namespace("", ODM_NS)
    ET.register_namespace("imednet", IMEDNET_NS)

    root_attribs: dict[str, str] = {
        "ODMVersion": archive.odm_version,
        "FileType": archive.file_type,
        "FileOID": archive.file_oid,
        "CreationDateTime": archive.creation_date_time,
    }
    if archive.description:
        root_attribs["Description"] = archive.description
    if archive.granularity:
        root_attribs["Granularity"] = archive.granularity
    if archive.archival:
        root_attribs["Archival"] = archive.archival
    if archive.prior_file_oid:
        root_attribs["PriorFileOID"] = archive.prior_file_oid
    if archive.as_of_date_time:
        root_attribs["AsOfDateTime"] = archive.as_of_date_time
    if archive.originator:
        root_attribs["Originator"] = archive.originator
    if archive.source_system:
        root_attribs["SourceSystem"] = archive.source_system
    if archive.source_system_version:
        root_attribs["SourceSystemVersion"] = archive.source_system_version
    if archive.archive_id:
        root_attribs["ID"] = archive.archive_id

    root = ET.Element(f"{{{ODM_NS}}}ODM", root_attribs)

    resolved_mdv_oid = meta_data_version_oid
    if resolved_mdv_oid is None:
        if archive.clinical_data:
            resolved_mdv_oid = archive.clinical_data[0].metadata_version_oid
        elif archive.reference_data:
            resolved_mdv_oid = archive.reference_data[0].metadata_version_oid
        elif archive.admin_data and archive.admin_data[0].locations:
            refs = archive.admin_data[0].locations[0].metadata_version_refs
            resolved_mdv_oid = refs[0].metadata_version_oid if refs else "MDV.001"
        else:
            resolved_mdv_oid = "MDV.001"

    if archive.data_dictionary is not None:
        root.append(
            _build_study_elem(
                archive.data_dictionary,
                meta_data_version_oid=resolved_mdv_oid,
                meta_data_version_name=meta_data_version_name,
                strict_132=(archive.odm_version == "1.3.2"),
            )
        )
    for adm in archive.admin_data:
        root.append(_build_admin_data_elem(adm))
    for ref in archive.reference_data:
        root.append(_build_reference_data_elem(ref))
    for clin in archive.clinical_data:
        root.append(_build_clinical_data_elem(clin))

    ET.indent(root, space=indent)
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    xml_str = xml_bytes.decode("utf-8")
    if not xml_str.endswith("\n"):
        xml_str += "\n"
    return str(xml_str)


def to_odm_archive_file(
    archive: OdmArchive,
    file_path: Path | str,
    *,
    meta_data_version_oid: str | None = None,
    meta_data_version_name: str | None = None,
    indent: str = "  ",
) -> None:
    """Write a canonical ``OdmArchive`` snapshot to a UTF-8 ODM 1.3.2 XML file."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        to_odm_archive_xml(
            archive,
            meta_data_version_oid=meta_data_version_oid,
            meta_data_version_name=meta_data_version_name,
            indent=indent,
        ),
        encoding="utf-8",
    )


def _quote_xml_attr(value: str) -> str:
    """Escape and double-quote an XML attribute value without importing xml.sax."""
    escaped = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\t", "&#9;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )
    return f'"{escaped}"'


def _serialize_subtree_indented(elem: ET.Element, *, indent: str, level: int = 1) -> str:
    """Serialize a standalone ODM subtree indented at ``level`` without repeating root xmlns."""
    ET.indent(elem, space=indent, level=level)
    raw = ET.tostring(elem, encoding="unicode")
    # Strip redundant default ODM namespace redeclaration when already declared on <ODM>
    cleaned = raw.replace(f' xmlns="{ODM_NS}"', "", 1).replace(
        f' xmlns:imednet="{IMEDNET_NS}"', "", 1
    )
    prefix = indent * level
    return f"{prefix}{cleaned}\n"


def stream_odm_xml(
    datadict: DataDictionary | None = None,
    *,
    study_oid: str | None = None,
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    creation_date_time: str | None = None,
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    odm_version: Literal["1.2", "1.3", "1.3.1", "1.3.2"] = "1.3.2",
    file_type: Literal["Snapshot", "Transactional"] = "Snapshot",
    granularity: str | None = None,
    originator: str | None = None,
    source_system: str | None = "imednet-toolkit",
    source_system_version: str | None = None,
    admin_data: Iterable[OdmAdminData] = (),
    reference_data: Iterable[OdmReferenceData] = (),
    clinical_data: Sequence[OdmClinicalData] | OdmClinicalData | None = None,
    subject_data: Iterable[OdmSubjectData] = (),
    audit_records: Iterable[OdmAuditRecord] = (),
    annotations: Iterable[OdmAnnotation] = (),
    indent: str = "  ",
) -> Iterator[str]:
    """Stream CDISC ODM 1.3.2 XML chunks without holding full ClinicalData DOM in memory."""
    ET.register_namespace("", ODM_NS)
    ET.register_namespace("imednet", IMEDNET_NS)

    clin_blocks = _normalize_container_seq(clinical_data)
    resolved_study_oid = (
        study_oid
        or (clin_blocks[0].study_oid if clin_blocks else None)
        or (
            datadict.metadata.studyKey
            if datadict is not None and datadict.metadata.studyKey
            else None
        )
        or "STUDY_001"
    )
    resolved_mdv_oid = (
        clin_blocks[0].metadata_version_oid
        if (clin_blocks and meta_data_version_oid == "MDV.001")
        else meta_data_version_oid
    )

    now_iso = (
        creation_datetime or creation_date_time or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    f_oid = file_oid or f"ODM.{resolved_study_oid}"

    attrs = [
        f"xmlns={_quote_xml_attr(ODM_NS)}",
        f"xmlns:imednet={_quote_xml_attr(IMEDNET_NS)}",
        f"ODMVersion={_quote_xml_attr(odm_version)}",
        f"FileType={_quote_xml_attr(file_type)}",
        f"FileOID={_quote_xml_attr(f_oid)}",
        f"CreationDateTime={_quote_xml_attr(now_iso)}",
    ]
    if granularity:
        attrs.append(f"Granularity={_quote_xml_attr(granularity)}")
    if originator:
        attrs.append(f"Originator={_quote_xml_attr(originator)}")
    if source_system:
        attrs.append(f"SourceSystem={_quote_xml_attr(source_system)}")
    if source_system_version:
        attrs.append(f"SourceSystemVersion={_quote_xml_attr(source_system_version)}")

    yield "<?xml version='1.0' encoding='utf-8'?>\n"
    yield f"<ODM {' '.join(attrs)}>\n"

    if datadict is not None:
        study_elem = _build_study_elem(
            datadict,
            meta_data_version_oid=resolved_mdv_oid,
            meta_data_version_name=meta_data_version_name,
            strict_132=(odm_version == "1.3.2"),
        )
        yield _serialize_subtree_indented(study_elem, indent=indent, level=1)

    for adm in admin_data:
        yield _serialize_subtree_indented(_build_admin_data_elem(adm), indent=indent, level=1)

    for ref in reference_data:
        yield _serialize_subtree_indented(_build_reference_data_elem(ref), indent=indent, level=1)

    for clin_block in clin_blocks:
        yield (
            f"{indent}<ClinicalData StudyOID={_quote_xml_attr(clin_block.study_oid)} "
            f"MetaDataVersionOID={_quote_xml_attr(clin_block.metadata_version_oid)}>\n"
        )
        for subj in clin_block.subject_data:
            yield _serialize_subtree_indented(
                _build_subject_data_elem(subj), indent=indent, level=2
            )
        if clin_block.audit_records:
            yield f"{indent * 2}<AuditRecords>\n"
            for aud in clin_block.audit_records:
                yield _serialize_subtree_indented(
                    _build_audit_record_elem(aud), indent=indent, level=3
                )
            yield f"{indent * 2}</AuditRecords>\n"
        if clin_block.annotations:
            yield f"{indent * 2}<Annotations>\n"
            for ann in clin_block.annotations:
                yield _serialize_subtree_indented(
                    _build_annotation_elem(ann), indent=indent, level=3
                )
            yield f"{indent * 2}</Annotations>\n"
        yield f"{indent}</ClinicalData>\n"

    clin_opened = False

    def _ensure_clin_open() -> str:
        nonlocal clin_opened
        if not clin_opened:
            clin_opened = True
            return (
                f"{indent}<ClinicalData StudyOID={_quote_xml_attr(resolved_study_oid)} "
                f"MetaDataVersionOID={_quote_xml_attr(resolved_mdv_oid)}>\n"
            )
        return ""

    for subj in subject_data:
        header = _ensure_clin_open()
        if header:
            yield header
        yield _serialize_subtree_indented(_build_subject_data_elem(subj), indent=indent, level=2)

    aud_iter = iter(audit_records)
    first_aud = next(aud_iter, None)
    if first_aud is not None:
        header = _ensure_clin_open()
        if header:
            yield header
        yield f"{indent * 2}<AuditRecords>\n"
        yield _serialize_subtree_indented(
            _build_audit_record_elem(first_aud), indent=indent, level=3
        )
        for aud in aud_iter:
            yield _serialize_subtree_indented(_build_audit_record_elem(aud), indent=indent, level=3)
        yield f"{indent * 2}</AuditRecords>\n"

    ann_iter = iter(annotations)
    first_ann = next(ann_iter, None)
    if first_ann is not None:
        header = _ensure_clin_open()
        if header:
            yield header
        yield f"{indent * 2}<Annotations>\n"
        yield _serialize_subtree_indented(_build_annotation_elem(first_ann), indent=indent, level=3)
        for ann in ann_iter:
            yield _serialize_subtree_indented(_build_annotation_elem(ann), indent=indent, level=3)
        yield f"{indent * 2}</Annotations>\n"

    if clin_opened:
        yield f"{indent}</ClinicalData>\n"

    yield "</ODM>\n"


def write_odm_xml_stream(
    dest: Path | str | TextIO,
    datadict: DataDictionary | None = None,
    **stream_kwargs: Any,
) -> None:
    """Write streamed CDISC ODM 1.3.2 XML chunks directly to ``dest``."""
    if datadict is not None and "datadict" not in stream_kwargs:
        stream_kwargs["datadict"] = datadict
    if isinstance(dest, (str, Path)):
        out_path = Path(dest)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for chunk in stream_odm_xml(**stream_kwargs):
                fh.write(chunk)
    else:
        for chunk in stream_odm_xml(**stream_kwargs):
            dest.write(chunk)


__all__ = [
    "resolve_form_item_oids",
    "sort_data_dictionary",
    "stream_odm_xml",
    "to_file",
    "to_json",
    "to_odm_archive_file",
    "to_odm_archive_xml",
    "to_odm_file",
    "to_odm_xml",
    "write_odm_xml_stream",
]
