"""Canonical JSON and CDISC ODM XML emission for iMednet Data Dictionary."""

from __future__ import annotations

import copy
import json

# Serialization only: this module never parses XML; defusedxml has no Element builder.
import xml.etree.ElementTree as ET  # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Choice, DataDictionary, Variable

ODM_NS = "http://www.cdisc.org/ns/odm/v1.3"


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


def _map_to_odm_datatype(var_type: str | None) -> tuple[str, str | None]:
    """Map canonical iMednet variableType to CDISC ODM DataType and ControlType."""
    if not var_type:
        return "text", None
    vt_lower = var_type.lower()
    if vt_lower in ("integer", "int"):
        return "integer", None
    if vt_lower in ("float", "double", "decimal"):
        return "float", None
    if vt_lower == "date":
        return "date", None
    if vt_lower in ("datetime", "durationdatetime", "intervaldatetime"):
        return "datetime", None
    if vt_lower == "time":
        return "time", None
    if vt_lower in ("boolean", "bool"):
        return "boolean", None
    if vt_lower in ("radio", "radiobutton"):
        return "text", "RadioButton"
    if vt_lower in ("dropdown", "dropdownlist", "select", "singleselect", "single select"):
        return "text", "SingleSelect"
    if vt_lower in ("checkbox", "multiselect", "multi select"):
        return "text", "MultiSelect"
    return "text", None


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

    mdv_elem = ET.SubElement(
        study_elem,
        f"{{{ODM_NS}}}MetaDataVersion",
        {"OID": meta_data_version_oid, "Name": mdv_name},
    )

    # 1. Deterministic form ordering without mutating datadict
    sorted_forms = sorted(datadict.forms, key=lambda f: f.formKey or "")

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
    for form in sorted_forms:
        ig_elem = ET.SubElement(
            mdv_elem,
            f"{{{ODM_NS}}}ItemGroupDef",
            {
                "OID": f"IG_{form.formKey}",
                "Name": f"{form.formName or form.formKey} Details",
                "Repeating": str(form.extraAttributes.get("repeating", "No")),
            },
        )
        sorted_vars = sorted(
            form.variables, key=lambda v: _sort_key_int_then_str(v.sequence, v.variableName)
        )
        for var_idx, var in enumerate(sorted_vars, 1):
            mand = (
                "Yes"
                if var.required and str(var.required).strip().lower() in ("yes", "y", "true", "1")
                else "No"
            )
            ET.SubElement(
                ig_elem,
                f"{{{ODM_NS}}}ItemRef",
                {
                    "ItemOID": var.variableName,
                    "OrderNumber": str(var.sequence if var.sequence is not None else var_idx),
                    "Mandatory": mand,
                },
            )

    # 4. ItemDef elements (deduplicated by variableName across forms)
    emitted_item_defs: set[str] = set()
    item_def_elements: list[tuple[Variable, str]] = []
    for form in sorted_forms:
        for var in form.variables:
            if var.variableName not in emitted_item_defs:
                emitted_item_defs.add(var.variableName)
                item_def_elements.append((var, form.formKey))

    for var, _ in item_def_elements:
        datatype, control_type = _map_to_odm_datatype(var.variableType)
        if var.choices and not control_type:
            control_type = "RadioButton"

        item_attribs: dict[str, str] = {
            "OID": var.variableName,
            "Name": var.variableName,
            "DataType": datatype,
        }
        if var.format and str(var.format).isdigit():
            item_attribs["Length"] = str(var.format)
        if var.precision and str(var.precision).isdigit():
            item_attribs["SignificantDigits"] = str(var.precision)
        if control_type:
            item_attribs["ControlType"] = control_type

        item_elem = ET.SubElement(mdv_elem, f"{{{ODM_NS}}}ItemDef", item_attribs)
        if var.label:
            q_elem = ET.SubElement(item_elem, f"{{{ODM_NS}}}Question")
            tt_elem = ET.SubElement(q_elem, f"{{{ODM_NS}}}TranslatedText", {"xml:lang": "en"})
            tt_elem.text = var.label
        if var.choices:
            cl_oid = f"CL_{var.variableName}"
            ET.SubElement(item_elem, f"{{{ODM_NS}}}CodeListRef", {"CodeListOID": cl_oid})

    # 5. CodeList elements
    emitted_codelists: set[str] = set()
    codelist_elements: list[tuple[str, str, list[Choice]]] = []
    for var, _ in item_def_elements:
        if var.choices:
            cl_oid = f"CL_{var.variableName}"
            if cl_oid not in emitted_codelists:
                emitted_codelists.add(cl_oid)
                codelist_elements.append((cl_oid, f"{var.variableName} CodeList", var.choices))

    for cl_oid, cl_name, choices in codelist_elements:
        cl_elem = ET.SubElement(
            mdv_elem,
            f"{{{ODM_NS}}}CodeList",
            {"OID": cl_oid, "Name": cl_name, "DataType": "text"},
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
