"""Canonical JSON and CDISC ODM XML emission for iMednet Data Dictionary."""

from __future__ import annotations

import copy
import json

# Serialization only: this module never parses XML; defusedxml has no Element builder.
import xml.etree.ElementTree as ET  # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import BusinessLogicRule, Choice, DataDictionary, Form, Variable
from .normalize import (
    normalize_measurement_units,
    normalize_variable_bounds,
    normalize_variable_type,
)

ODM_NS = "http://www.cdisc.org/ns/odm/v1.3"
IMEDNET_NS = "http://www.imednet.com/ns/odm/metadata"


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
) -> tuple[str, str | None]:
    """Map canonical iMednet variableType to CDISC ODM DataType and ControlType."""
    vt_norm = normalize_variable_type(var_type, precision=precision)
    if not vt_norm:
        return "text", ("RadioButton" if has_choices else None)
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


def _item_def_signature(var: Variable) -> Any:
    """Compute an equivalence signature for an ODM ItemDef + CodeList definition."""
    sorted_choices = sorted(
        var.choices, key=lambda c: _sort_key_int_then_str(c.position, c.choiceValue)
    )
    choices_sig = tuple(
        (c.choiceValue, c.choiceText, c.position, _freeze_value(c.extraAttributes))
        for c in sorted_choices
    )
    item_extra = {
        k: v
        for k, v in var.extraAttributes.items()
        if k not in {"Domain", "SASDatasetName", "methodOid", "collectionExceptionConditionOid"}
    }
    return (
        var.label,
        normalize_variable_type(var.variableType, precision=var.precision) or var.variableType,
        var.format,
        var.precision,
        choices_sig,
        _freeze_value(item_extra),
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


def _allocate_scoped_oid(form_key: str, var_name: str, used_oids: set[str]) -> str:
    """Allocate a globally unique form-scoped OID without colliding with raw or prior OIDs."""
    base_var = var_name.strip() or "ITEM"
    candidates: list[str] = []
    if form_key:
        candidates.append(f"{form_key}.{base_var}")
        candidates.append(f"IT.{form_key}.{base_var}")
        prefix = f"IT.{form_key}.{base_var}"
    else:
        candidates.append(base_var)
        candidates.append(f"IT.{base_var}")
        prefix = f"IT.{base_var}"

    for candidate in candidates:
        if candidate not in used_oids:
            used_oids.add(candidate)
            return candidate

    counter = 2
    while True:
        fallback = f"{prefix}__{counter}"
        if fallback not in used_oids:
            used_oids.add(fallback)
            return fallback
        counter += 1


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
    for f_idx, sorted_vars in enumerate(sorted_vars_by_form):
        for v_idx, var in enumerate(sorted_vars):
            sig = _item_def_signature(var)
            sig_by_occ[(f_idx, v_idx)] = sig
            sigs_by_var_name.setdefault(var.variableName, set()).add(sig)

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
                    )
                    scoped_oid_by_group[group_key] = item_oid
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


def _emit_formal_rules(mdv_elem: ET.Element, rules: list[BusinessLogicRule]) -> None:
    """Emit ODM ConditionDef and MethodDef elements for rules carrying ODM formal logic."""
    seen_oids: set[tuple[str, str]] = set()
    for target_tag in ("ConditionDef", "MethodDef"):
        for rule in rules:
            logic = rule.logic if isinstance(rule.logic, dict) else {}
            odm_elem_type = logic.get("odmElement") or rule.extraAttributes.get("odmElement")
            if odm_elem_type != target_tag or rule.id is None:
                continue
            r_oid = str(rule.id).strip()
            if not r_oid or (target_tag, r_oid) in seen_oids:
                continue
            seen_oids.add((target_tag, r_oid))

            attribs: dict[str, str] = {
                "OID": r_oid,
                "Name": str(rule.name or r_oid),
            }
            if target_tag == "MethodDef":
                m_type = (
                    logic.get("methodType")
                    or rule.extraAttributes.get("methodType")
                    or "Computation"
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


def to_odm_xml(
    datadict: DataDictionary,
    *,
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    indent: str = "  ",
) -> str:
    """Serialize DataDictionary to CDISC ODM 1.3 XML format.

    This function does not mutate the input DataDictionary instance.

    Args:
        datadict: DataDictionary instance to serialize.
        file_oid: Optional FileOID attribute for root ODM element.
        creation_datetime: Optional ISO 8601 creation datetime string.
        meta_data_version_oid: MetaDataVersion OID identifier (default: 'MDV.001').
        meta_data_version_name: Optional MetaDataVersion name.
        indent: Indentation string for XML formatting (default: two spaces).

    Returns:
        Valid CDISC ODM 1.3 XML document string.
    """
    ET.register_namespace("", ODM_NS)
    ET.register_namespace("imednet", IMEDNET_NS)

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

    now_iso = creation_datetime or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    f_oid = file_oid or f"ODM_{study_key}_{now_iso}"
    mdv_name = (
        meta_data_version_name
        or getattr(datadict.metadata, "metaDataVersionName", None)
        or "Initial Metadata Version"
    )

    root = ET.Element(
        f"{{{ODM_NS}}}ODM",
        {
            "ODMVersion": "1.3",
            "FileType": "Snapshot",
            "FileOID": f_oid,
            "CreationDateTime": now_iso,
        },
    )

    study_elem = ET.SubElement(root, f"{{{ODM_NS}}}Study", {"OID": study_key})
    gv_elem = ET.SubElement(study_elem, f"{{{ODM_NS}}}GlobalVariables")
    sn_elem = ET.SubElement(gv_elem, f"{{{ODM_NS}}}StudyName")
    sn_elem.text = str(study_name)

    if study_description:
        sd_elem = ET.SubElement(gv_elem, f"{{{ODM_NS}}}StudyDescription")
        sd_elem.text = str(study_description)
    if protocol_name:
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

    # Allocate globally unique ItemDef OIDs across unscoped and form-scoped variable names
    sorted_vars_by_form, oid_by_occ, referencing_forms_by_oid = _allocate_item_oids(sorted_forms)

    # 2. FormDef elements
    for form in sorted_forms:
        form_attribs: dict[str, str] = {
            "OID": form.formKey,
            "Name": form.formName or form.formKey,
            "Repeating": str(form.extraAttributes.get("repeating", "No")),
        }
        if form.formType:
            form_attribs["FormType"] = form.formType

        form_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}FormDef", form_attribs)
        ig_oid = f"IG_{form.formKey}"
        ET.SubElement(
            form_elem,
            f"{{{ODM_NS}}}ItemGroupRef",
            {"ItemGroupOID": ig_oid, "OrderNumber": "1", "Mandatory": "No"},
        )

    # 3. ItemGroupDef elements
    for f_idx, form in enumerate(sorted_forms):
        ig_attribs: dict[str, str] = {
            "OID": f"IG_{form.formKey}",
            "Name": f"{form.formName or form.formKey} Details",
            "Repeating": str(form.extraAttributes.get("repeating", "No")),
        }
        domain = _extract_sdtm_attr(form, "Domain")
        sas_dataset = _extract_sdtm_attr(form, "SASDatasetName")
        if not domain or not sas_dataset:
            for child_var in form.variables:
                if not domain:
                    domain = _extract_sdtm_attr(child_var, "Domain")
                if not sas_dataset:
                    sas_dataset = _extract_sdtm_attr(child_var, "SASDatasetName")
                if domain and sas_dataset:
                    break
        if domain:
            ig_attribs["Domain"] = domain
        if sas_dataset:
            ig_attribs["SASDatasetName"] = sas_dataset

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
            if req_str and not is_yes and not is_no:
                item_ref_attribs[f"{{{IMEDNET_NS}}}RequiredPolicy"] = req_str
            if (method_oid := var.extraAttributes.get("methodOid")) and str(method_oid).strip():
                item_ref_attribs["MethodOID"] = str(method_oid).strip()
            if (cond_oid := var.extraAttributes.get("collectionExceptionConditionOid")) and str(
                cond_oid
            ).strip():
                item_ref_attribs["CollectionExceptionConditionOID"] = str(cond_oid).strip()

            ET.SubElement(ig_elem, f"{{{ODM_NS}}}ItemRef", item_ref_attribs)

    # 4. ItemDef elements (globally unique OIDs preserving source names and distinct definitions)
    emitted_item_defs: set[str] = set()
    item_def_elements: list[tuple[Variable, str, str, str]] = []
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
                )
                item_def_elements.append((var, form.formKey, item_oid, datatype))

    for var, form_key, item_oid, datatype in item_def_elements:
        _, control_type = _map_to_odm_datatype(
            var.variableType,
            precision=var.precision,
            has_choices=bool(var.choices),
        )

        item_attribs: dict[str, str] = {
            "OID": item_oid,
            "Name": var.variableName,
            "DataType": datatype,
        }
        ref_forms = referencing_forms_by_oid.get(item_oid, [form_key])
        if any(_default_unwrapped_name(item_oid, fk) != var.variableName for fk in ref_forms):
            item_attribs[f"{{{IMEDNET_NS}}}VariableName"] = var.variableName
        if var.format and str(var.format).isdigit():
            item_attribs["Length"] = str(var.format)
        if var.precision and str(var.precision).isdigit():
            item_attribs["SignificantDigits"] = str(var.precision)
        if control_type:
            item_attribs["ControlType"] = control_type
        for sdtm_key in ("SDSVarName", "SASFieldName", "Origin", "Comment"):
            if sdtm_val := _extract_sdtm_attr(var, sdtm_key):
                item_attribs[sdtm_key] = sdtm_val

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

        if var.choices:
            cl_oid = f"CL_{item_oid}"
            ET.SubElement(item_elem, f"{{{ODM_NS}}}CodeListRef", {"CodeListOID": cl_oid})

    # 5. CodeList elements
    emitted_codelists: set[str] = set()
    codelist_elements: list[tuple[str, str, str, list[Choice]]] = []
    for var, _, item_oid, datatype in item_def_elements:
        if var.choices:
            cl_oid = f"CL_{item_oid}"
            if cl_oid not in emitted_codelists:
                emitted_codelists.add(cl_oid)
                cl_dtype = datatype if datatype in ("integer", "float", "text") else "text"
                codelist_elements.append((cl_oid, f"{item_oid} CodeList", cl_dtype, var.choices))

    for cl_oid, cl_name, cl_dtype, choices in codelist_elements:
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

    # 6. ConditionDef and MethodDef elements (when formal ODM rules are present)
    all_rules: list[BusinessLogicRule] = []
    for form in sorted_forms:
        all_rules.extend(form.businessLogic)
    all_rules.extend(datadict.unassignedBusinessLogic)
    if all_rules:
        _emit_formal_rules(mdv_elem, all_rules)

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
    meta_data_version_oid: str = "MDV.001",
    meta_data_version_name: str | None = None,
    indent: str = "  ",
) -> None:
    """Write CDISC ODM 1.3 XML content to a UTF-8 encoded file.

    Args:
        datadict: DataDictionary instance.
        file_path: Destination file path.
        file_oid: Optional FileOID attribute for root ODM element.
        creation_datetime: Optional ISO 8601 creation datetime string.
        meta_data_version_oid: MetaDataVersion OID identifier (default: 'MDV.001').
        meta_data_version_name: Optional MetaDataVersion name.
        indent: Indentation string for XML formatting.
    """
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    xml_str = to_odm_xml(
        datadict,
        file_oid=file_oid,
        creation_datetime=creation_datetime,
        meta_data_version_oid=meta_data_version_oid,
        meta_data_version_name=meta_data_version_name,
        indent=indent,
    )
    path.write_text(xml_str, encoding="utf-8")


__all__ = [
    "sort_data_dictionary",
    "to_file",
    "to_json",
    "to_odm_file",
    "to_odm_xml",
]
