"""CDISC ODM 1.3 XML data dictionary loader for iMednet."""

from __future__ import annotations

import json
import warnings as py_warnings
from pathlib import Path
from typing import Any, BinaryIO, Literal, TextIO

import defusedxml.ElementTree as ET  # noqa: N817
from defusedxml.common import DefusedXmlException

from ..exchange import (
    OdmAdminData,
    OdmAnnotation,
    OdmArchive,
    OdmAuditRecord,
    OdmClinicalData,
    OdmFlag,
    OdmFormData,
    OdmItemData,
    OdmItemGroupData,
    OdmLocation,
    OdmMetaDataVersionRef,
    OdmReferenceData,
    OdmStudyEventData,
    OdmSubjectData,
    OdmUser,
)
from ..logic import parse_logic_xml
from ..models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)
from ..normalize import (
    _is_file_target,
    _safe_is_file,
    normalize_form_id,
    normalize_form_type,
    normalize_variable_type,
    to_camel_case,
)

ODM_NS = "http://www.cdisc.org/ns/odm/v1.3"
IMEDNET_NS = "http://www.imednet.com/ns/odm/metadata"
MDSOL_NS = "http://www.mdsol.com/ns/odm/metadata"

# Standard ODM attributes by element type
KNOWN_FORM_ATTRIBUTES = {
    "oid",
    "name",
    "repeating",
    "comment",
    "formtype",
}

KNOWN_IMEDNET_FORM_ATTRIBUTES = {
    "formid",
}

KNOWN_ITEM_GROUP_ATTRIBUTES = {
    "oid",
    "name",
    "repeating",
    "comment",
    "isreferencedata",
    "sasdatasetname",
    "domain",
    "purpose",
}

KNOWN_ITEM_ATTRIBUTES = {
    "oid",
    "name",
    "datatype",
    "length",
    "significantdigits",
    "origin",
    "comment",
    "sasfieldname",
    "sdsvarname",
    "controltype",
}

KNOWN_IMEDNET_ITEM_ATTRIBUTES = {
    "variablename",
    "variabletype",
    "format",
    "precision",
}

KNOWN_ITEM_REF_ATTRIBUTES = {
    "itemoid",
    "ordernumber",
    "mandatory",
    "keysequence",
    "methodoid",
    "role",
    "rolecodelistoid",
    "collectionexceptionconditionoid",
    "controltype",
}

KNOWN_IMEDNET_ITEM_REF_ATTRIBUTES = {
    "requiredpolicy",
}

KNOWN_ITEM_GROUP_REF_ATTRIBUTES = {
    "itemgroupoid",
    "ordernumber",
    "mandatory",
    "collectionexceptionconditionoid",
}

KNOWN_CODELIST_ATTRIBUTES = {
    "oid",
    "name",
    "datatype",
    "sasformatname",
}

KNOWN_IMEDNET_CODELIST_ATTRIBUTES = {
    "syntheticrolecodelist",
}

KNOWN_ORIGIN_ATTRIBUTES = {
    "type",
    "source",
}

KNOWN_CODELIST_ITEM_ATTRIBUTES = {
    "codedvalue",
    "ordernumber",
    "rank",
}

KNOWN_STUDY_EVENT_ATTRIBUTES = {
    "oid",
    "name",
    "repeating",
    "type",
    "category",
}

KNOWN_STUDY_EVENT_REF_ATTRIBUTES = {
    "studyeventoid",
    "ordernumber",
    "mandatory",
    "collectionexceptionconditionoid",
}

KNOWN_FORM_REF_ATTRIBUTES = {
    "formoid",
    "ordernumber",
    "mandatory",
    "collectionexceptionconditionoid",
}

KNOWN_MEASUREMENT_UNIT_ATTRIBUTES = {
    "oid",
    "name",
}

KNOWN_MEASUREMENT_UNIT_REF_ATTRIBUTES = {
    "measurementunitoid",
}

KNOWN_RANGE_CHECK_ATTRIBUTES = {
    "comparator",
    "softhard",
}

KNOWN_CONDITION_DEF_ATTRIBUTES = {
    "oid",
    "name",
}

KNOWN_IMEDNET_CONDITION_DEF_ATTRIBUTES = {
    "rulesource",
    "ruleid",
    "ruleidtype",
    "rulename",
    "ruletype",
    "status",
    "formkey",
    "containerform",
    "sequence",
    "extraattributes",
}

KNOWN_METHOD_DEF_ATTRIBUTES = {
    "oid",
    "name",
    "type",
}

KNOWN_FORMAL_EXPRESSION_ATTRIBUTES = {
    "context",
}

NON_METADATA_ODM_CONTAINERS = {
    "clinicaldata",
    "admindata",
    "referencedata",
    "association",
}

NON_METADATA_ODM_DESCENDANTS = {
    "subjectdata",
    "studyeventdata",
    "formdata",
    "itemgroupdata",
    "itemdata",
    "auditrecord",
    "auditrecords",
    "annotation",
    "annotations",
    "signature",
    "signatures",
    "user",
    "location",
    "signaturedef",
}

KNOWN_STUDY_CHILD_TAGS = {
    "globalvariables",
    "basicdefinitions",
    "metadataversion",
}

KNOWN_MDV_CHILD_TAGS = {
    "include",
    "protocol",
    "studyeventdef",
    "formdef",
    "itemgroupdef",
    "itemdef",
    "codelist",
    "imputationmethod",
    "presentation",
    "conditiondef",
    "methoddef",
    "alias",
}

SUPPORTED_DATA_TYPES = {
    "text",
    "string",
    "integer",
    "int",
    "float",
    "double",
    "decimal",
    "date",
    "partialdate",
    "incompletedate",
    "datetime",
    "durationdatetime",
    "intervaldatetime",
    "partialdatetime",
    "incompletedatetime",
    "time",
    "partialtime",
    "incompletetime",
    "boolean",
    "bool",
    "uri",
}

_PRESERVED_ODM_RAW_DATA_TYPES = {
    "partialdate",
    "incompletedate",
    "partialdatetime",
    "incompletedatetime",
    "partialtime",
    "incompletetime",
    "durationdatetime",
    "intervaldatetime",
    "uri",
}


def _local_tag(elem: ET.Element) -> str:
    """Return XML tag without namespace prefix."""
    tag = str(elem.tag)
    if "}" in tag:
        return str(tag.split("}", 1)[1])
    return tag


def _split_attr_ns(attr_key: str) -> tuple[str, str]:
    """Split an ElementTree attribute key into (namespace_uri, local_name)."""
    key_str = str(attr_key)
    if key_str.startswith("{") and "}" in key_str:
        ns_uri, local = key_str[1:].split("}", 1)
        return ns_uri, local
    return "", key_str


def _get_attr(elem: ET.Element, *names: str) -> str | None:
    """Case-insensitively retrieve a standard ODM attribute from an Element."""
    names_lower = {n.lower() for n in names}
    fallback_val: str | None = None
    for k, v in elem.attrib.items():
        ns_uri, attr_local = _split_attr_ns(k)
        local_lower = attr_local.lower()
        if local_lower not in names_lower:
            continue
        if ns_uri in ("", ODM_NS):
            return str(v)
        if ns_uri == MDSOL_NS and local_lower == "controltype" and fallback_val is None:
            fallback_val = str(v)
    return fallback_val


def _get_imednet_attr(elem: ET.Element, *names: str) -> str | None:
    """Case-insensitively retrieve an explicit iMednet vendor extension attribute."""
    names_lower = {n.lower() for n in names}
    for k, v in elem.attrib.items():
        ns_uri, attr_local = _split_attr_ns(k)
        if ns_uri == IMEDNET_NS and attr_local.lower() in names_lower:
            return str(v)
    return None


def _find_children(elem: ET.Element, tag_name: str) -> list[ET.Element]:
    """Find all direct child elements matching local tag name."""
    tag_lower = tag_name.lower()
    return [c for c in elem if _local_tag(c).lower() == tag_lower]


def _find_first_child(elem: ET.Element, tag_name: str) -> ET.Element | None:
    """Find first direct child element matching local tag name."""
    tag_lower = tag_name.lower()
    for c in elem:
        if _local_tag(c).lower() == tag_lower:
            return c
    return None


def _find_descendants(elem: ET.Element, tag_name: str) -> list[ET.Element]:
    """Find all descendants matching local tag name."""
    tag_lower = tag_name.lower()
    return [c for c in elem.iter() if _local_tag(c).lower() == tag_lower]


def _extract_unmapped_attributes(
    elem: ET.Element,
    known: set[str],
    elem_type: str,
    record_warning: Any,
    *,
    known_imednet: set[str] | None = None,
) -> dict[str, Any]:
    """Extract attributes not in the known ODM or vendor sets and record warnings."""
    extra: dict[str, Any] = {}
    imednet_allowed = known_imednet or set()
    for k, v in elem.attrib.items():
        ns_uri, local_name = _split_attr_ns(k)
        local_lower = local_name.lower()
        # Ignore XML namespace declarations and xml:* / W3C builtins
        if (
            local_lower == "xmlns"
            or (ns_uri == "" and local_lower == "lang")
            or k.startswith("xmlns")
            or ns_uri.startswith("http://www.w3.org/")
        ):
            continue
        if ns_uri in ("", ODM_NS) and local_lower in known:
            continue
        if ns_uri == IMEDNET_NS and local_lower in imednet_allowed:
            continue
        if ns_uri == MDSOL_NS and local_lower == "controltype" and "controltype" in known:
            continue

        camel = to_camel_case(local_name)
        extra[camel] = v
        elem_id = (
            _get_attr(elem, "OID", "ItemOID", "ItemGroupOID", "FormOID", "StudyEventOID")
            or "unknown"
        )
        record_warning(f"Unmapped attribute '{local_name}' on {elem_type} '{elem_id}'")
    return extra


def _read_source_content(source: Path | str | bytes | bytearray | TextIO | BinaryIO) -> bytes:
    """Read XML content from path, string, bytes, or stream into bytes."""
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, Path):
        if not _safe_is_file(source):
            raise FileNotFoundError(f"Target file '{source}' does not exist")
        return source.read_bytes()
    if isinstance(source, str):
        if "\n" in source or "\r" in source or source.strip().startswith("<"):
            return source.encode("utf-8")
        if _is_file_target(source):
            p = Path(source)
            if not _safe_is_file(p):
                raise FileNotFoundError(f"Target file '{source}' does not exist")
            return p.read_bytes()
        p = Path(source)
        if _safe_is_file(p):
            return p.read_bytes()
        return source.encode("utf-8")
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, str):
            return data.encode("utf-8")
        return data
    raise TypeError(f"Unsupported source type for CDISC ODM loader: {type(source)}")


def _parse_order(elem: ET.Element, fallback: int) -> int:
    """Parse OrderNumber attribute or return fallback integer."""
    val = _get_attr(elem, "OrderNumber")
    if val:
        try:
            return int(float(val))
        except ValueError:
            pass
    return fallback


def _normalize_mandatory(val: str | None) -> str | None:
    """Normalize an ODM Mandatory attribute into canonical 'Yes' or 'No'."""
    if not val or not val.strip():
        return None
    return "Yes" if val.strip().lower() in ("yes", "y", "true", "1") else "No"


def _get_translated_text(elem: ET.Element | None) -> str | None:
    """Extract text from TranslatedText inside Question or Description element."""
    if elem is None:
        return None
    translated = _find_first_child(elem, "TranslatedText")
    if translated is not None and translated.text:
        return str(translated.text).strip()
    if elem.text:
        return str(elem.text).strip()
    return None


def _extract_item_sdtm_attributes(it_def: ET.Element, record_warning: Any) -> dict[str, Any]:
    """Extract SDSVarName, SASFieldName, Origin, and Comment from ItemDef element."""
    sdtm: dict[str, Any] = {}
    if (sds_var := _get_attr(it_def, "SDSVarName")) and sds_var.strip():
        sdtm["SDSVarName"] = sds_var.strip()
    if (sas_field := _get_attr(it_def, "SASFieldName")) and sas_field.strip():
        sdtm["SASFieldName"] = sas_field.strip()
    if (origin := _get_attr(it_def, "Origin")) and origin.strip():
        sdtm["Origin"] = origin.strip()
    else:
        origin_elem = _find_first_child(it_def, "Origin")
        if origin_elem is not None:
            origin_extra = _extract_unmapped_attributes(
                origin_elem,
                KNOWN_ORIGIN_ATTRIBUTES,
                "Origin",
                record_warning,
            )
            origin_type = (_get_attr(origin_elem, "Type") or "").strip()
            origin_source = (_get_attr(origin_elem, "Source") or "").strip()
            origin_desc = _get_translated_text(_find_first_child(origin_elem, "Description"))
            if not origin_type and not origin_desc:
                origin_type = _get_translated_text(origin_elem) or ""
            if origin_type:
                sdtm["Origin"] = origin_type
            if origin_source:
                sdtm["originSource"] = origin_source
            if origin_desc:
                sdtm["originDescription"] = origin_desc
            sdtm.update(origin_extra)
    comment_val = _get_attr(it_def, "Comment")
    if not comment_val:
        c_elem = _find_first_child(it_def, "Comment")
        if c_elem is not None:
            comment_val = _get_translated_text(c_elem) or (
                c_elem.text.strip() if c_elem.text else None
            )
    if comment_val and comment_val.strip():
        sdtm["Comment"] = comment_val.strip()
    return sdtm


def _map_datatype(
    raw_datatype: str | None,
    item_oid: str,
    has_choices: bool,
    control_type: str | None,
    record_warning: Any,
    *,
    imednet_variable_type: str | None = None,
) -> str:
    """Map CDISC ODM DataType and ControlType to canonical iMednet variableType."""
    if imednet_variable_type and imednet_variable_type.strip():
        vt_norm = normalize_variable_type(imednet_variable_type.strip())
        if vt_norm:
            return vt_norm

    if control_type:
        ct_lower = control_type.strip().lower()
        if ct_lower in ("radio", "radiobutton"):
            return "Radio"
        if ct_lower in ("dropdown", "dropdownlist", "select", "singleselect"):
            return "Single Select"
        if ct_lower in ("checkbox", "multiselect"):
            return "Multi Select"
        if ct_lower in ("datetime", "datetime_precision"):
            return "DateTime"
        if ct_lower in ("calculation", "calculated", "calc"):
            return "Calculation"
        if ct_lower == "slider":
            return "Slider"
        if ct_lower in ("file", "fileupload", "file_upload", "upload"):
            return "File"
        if ct_lower in ("descriptive", "label"):
            return "Descriptive"
        if ct_lower == "text":
            return "Text"

    if raw_datatype:
        dt_lower = raw_datatype.strip().lower()
        if dt_lower in ("integer", "int"):
            return "Integer"
        if dt_lower in ("float", "double", "decimal"):
            return "Float"
        if dt_lower in ("date", "partialdate", "incompletedate"):
            return "Date"
        if dt_lower in (
            "datetime",
            "durationdatetime",
            "intervaldatetime",
            "partialdatetime",
            "incompletedatetime",
        ):
            return "DateTime"
        if dt_lower in ("time", "partialtime", "incompletetime"):
            return "Time"
        if dt_lower in ("boolean", "bool"):
            return "Boolean"
        if dt_lower == "uri":
            return "File"
        if dt_lower in ("text", "string"):
            return "Radio" if has_choices else "Text"

        record_warning(
            f"Unsupported data type '{raw_datatype}' on item '{item_oid}'. Defaulted to 'Text'."
        )
        return "Text"

    if has_choices:
        return "Radio"
    return "Text"


def _unwrap_scoped_oid(raw_oid: str, f_oid: str, ig_oid: str | None) -> str:
    """Strip a matching form or item-group prefix from an ItemDef OID when non-empty."""
    prefixes: list[str] = []
    if f_oid:
        prefixes.extend([f"IT.{f_oid}.", f"{f_oid}."])
        for f_lead in ("F.", "F_", "FORM.", "FORM_"):
            if f_oid.startswith(f_lead) and len(f_oid) > len(f_lead):
                f_stem = f_oid[len(f_lead) :]
                prefixes.extend([f"IT.{f_stem}.", f"{f_stem}."])
    if ig_oid:
        prefixes.append(f"{ig_oid}.")
        for ig_lead in ("IG.", "IG_"):
            if ig_oid.startswith(ig_lead) and len(ig_oid) > len(ig_lead):
                ig_stem = ig_oid[len(ig_lead) :]
                prefixes.extend([f"IT.{ig_stem}.", f"{ig_stem}."])

    for prefix in prefixes:
        if raw_oid.startswith(prefix):
            suffix = raw_oid[len(prefix) :].strip()
            if suffix:
                return suffix
    return raw_oid


def _resolve_variable_name(
    it_def: ET.Element,
    it_oid: str,
    f_oid: str,
    ig_oid: str | None,
    sibling_refs: list[tuple[str, str | None]] | None = None,
    item_def_elems: dict[str, ET.Element] | None = None,
) -> str:
    """Resolve canonical variableName from ItemDef, unwrapping form-scoped OID prefixes."""
    explicit_var_name = _get_imednet_attr(it_def, "VariableName")
    if explicit_var_name and explicit_var_name.strip():
        return explicit_var_name.strip()

    raw_oid = (_get_attr(it_def, "OID") or it_oid).strip()
    unwrapped = _unwrap_scoped_oid(raw_oid, f_oid, ig_oid)
    if unwrapped != raw_oid and sibling_refs:
        it_name = (_get_attr(it_def, "Name") or "").strip()
        for other_oid_raw, other_ig_oid in sibling_refs:
            other_oid = other_oid_raw.strip()
            if not other_oid or other_oid == raw_oid:
                continue
            other_def = item_def_elems.get(other_oid) if item_def_elems is not None else None
            other_explicit = (
                (_get_imednet_attr(other_def, "VariableName") or "").strip()
                if other_def is not None
                else ""
            )
            other_name = (
                (_get_attr(other_def, "Name") or "").strip() if other_def is not None else ""
            )
            other_unwrapped = _unwrap_scoped_oid(other_oid, f_oid, other_ig_oid)
            if other_oid == unwrapped and (
                other_unwrapped == other_oid
                or other_explicit == unwrapped
                or other_name == other_oid
            ):
                return raw_oid
            if (
                it_name == raw_oid
                and item_def_elems is not None
                and other_unwrapped == unwrapped
                and other_name != other_oid
            ):
                return raw_oid

    return unwrapped or (_get_attr(it_def, "Name") or "").strip() or it_oid


def _extract_item_units_and_ranges(
    it_def: ET.Element,
    it_oid: str,
    measurement_units_by_oid: dict[str, dict[str, Any]],
    record_warning: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract MeasurementUnitRef and RangeCheck child elements from an ItemDef."""
    item_units: list[dict[str, Any]] = []
    for mu_ref in _find_children(it_def, "MeasurementUnitRef"):
        _extract_unmapped_attributes(
            mu_ref,
            KNOWN_MEASUREMENT_UNIT_REF_ATTRIBUTES,
            "MeasurementUnitRef",
            record_warning,
        )
        mu_oid = (_get_attr(mu_ref, "MeasurementUnitOID") or "").strip()
        if not mu_oid:
            continue
        if mu_oid in measurement_units_by_oid:
            item_units.append(dict(measurement_units_by_oid[mu_oid]))
        else:
            record_warning(
                f"MeasurementUnitRef '{mu_oid}' on item '{it_oid}' was not found in BasicDefinitions."
            )
            item_units.append({"oid": mu_oid, "name": mu_oid, "symbol": mu_oid})

    range_checks: list[dict[str, Any]] = []
    for rc_elem in _find_children(it_def, "RangeCheck"):
        rc_extra = _extract_unmapped_attributes(
            rc_elem,
            KNOWN_RANGE_CHECK_ATTRIBUTES,
            "RangeCheck",
            record_warning,
        )
        rc_entry: dict[str, Any] = {}
        if comp := _get_attr(rc_elem, "Comparator"):
            rc_entry["comparator"] = comp.strip()
        if soft_hard := _get_attr(rc_elem, "SoftHard"):
            rc_entry["softHard"] = soft_hard.strip()

        check_vals = [
            cv.text.strip()
            for cv in _find_children(rc_elem, "CheckValue")
            if cv.text and cv.text.strip()
        ]
        if len(check_vals) == 1:
            rc_entry["checkValue"] = check_vals[0]
        elif len(check_vals) > 1:
            rc_entry["checkValues"] = check_vals

        fe_elem = _find_first_child(rc_elem, "FormalExpression")
        if fe_elem is not None:
            _extract_unmapped_attributes(
                fe_elem,
                KNOWN_FORMAL_EXPRESSION_ATTRIBUTES,
                "FormalExpression",
                record_warning,
            )
            if fe_elem.text and fe_elem.text.strip():
                rc_entry["formalExpression"] = fe_elem.text.strip()
            if ctx := _get_attr(fe_elem, "Context"):
                rc_entry["context"] = ctx.strip()

        rc_mu_ref = _find_first_child(rc_elem, "MeasurementUnitRef")
        if rc_mu_ref is not None:
            _extract_unmapped_attributes(
                rc_mu_ref,
                KNOWN_MEASUREMENT_UNIT_REF_ATTRIBUTES,
                "MeasurementUnitRef",
                record_warning,
            )
            rc_mu_oid = (_get_attr(rc_mu_ref, "MeasurementUnitOID") or "").strip()
            if rc_mu_oid:
                if rc_mu_oid not in measurement_units_by_oid:
                    record_warning(
                        f"MeasurementUnitRef '{rc_mu_oid}' on RangeCheck for item '{it_oid}' "
                        "was not found in BasicDefinitions."
                    )
                rc_entry["measurementUnitOid"] = rc_mu_oid

        err_elem = _find_first_child(rc_elem, "ErrorMessage")
        if err_msg := _get_translated_text(err_elem):
            rc_entry["errorMessage"] = err_msg

        rc_entry.update(rc_extra)
        if rc_entry:
            range_checks.append(rc_entry)

    return item_units, range_checks


def _parse_imednet_condition_rule(
    elem: ET.Element,
    fallback_seq: int,
    no_raw_xml: bool,
    record_warning: Any,
) -> tuple[str | None, BusinessLogicRule]:
    """Reconstruct a round-tripped iMednet BusinessLogicRule from ConditionDef."""
    extra = _extract_unmapped_attributes(
        elem,
        KNOWN_CONDITION_DEF_ATTRIBUTES,
        "ConditionDef",
        record_warning,
        known_imednet=KNOWN_IMEDNET_CONDITION_DEF_ATTRIBUTES,
    )
    extra_json = _get_imednet_attr(elem, "ExtraAttributes")
    if extra_json:
        try:
            loaded_extra = json.loads(extra_json)
            if isinstance(loaded_extra, dict):
                loaded_extra.update(extra)
                extra = loaded_extra
        except json.JSONDecodeError as exc:
            record_warning(f"Malformed imednet:ExtraAttributes JSON on ConditionDef: {exc}")

    raw_rule_id = _get_imednet_attr(elem, "RuleId")
    rule_id_type = (_get_imednet_attr(elem, "RuleIdType") or "str").strip().lower()
    rule_id: str | int | None
    if raw_rule_id is None:
        rule_id = None
    elif rule_id_type == "int" and raw_rule_id.lstrip("-").isdigit():
        rule_id = int(raw_rule_id)
    else:
        rule_id = raw_rule_id

    rule_name = _get_imednet_attr(elem, "RuleName")
    if rule_name is None:
        rule_name = (_get_attr(elem, "Name") or "").strip() or None
    rule_type = _get_imednet_attr(elem, "RuleType")
    rule_status = _get_imednet_attr(elem, "Status")
    rule_form = _get_imednet_attr(elem, "FormKey")
    container_form = _get_imednet_attr(elem, "ContainerForm")
    raw_seq = _get_imednet_attr(elem, "Sequence")
    seq: int | None
    if raw_seq is not None and raw_seq.strip().lstrip("-").isdigit():
        seq = int(raw_seq.strip())
    elif raw_seq is None:
        seq = None
    else:
        seq = fallback_seq

    raw_logic_xml: str | None = None
    parsed_logic: dict[str, Any] | None = None
    for fe in _find_children(elem, "FormalExpression"):
        _extract_unmapped_attributes(
            fe,
            KNOWN_FORMAL_EXPRESSION_ATTRIBUTES,
            "FormalExpression",
            record_warning,
        )
        ctx = (_get_attr(fe, "Context") or "").strip()
        body = fe.text if fe.text is not None else ""
        if ctx == "imednet:logicRawXml":
            raw_logic_xml = body
        elif ctx == "imednet:logicJson" and body.strip():
            try:
                loaded_logic = json.loads(body)
                if isinstance(loaded_logic, dict):
                    parsed_logic = loaded_logic
            except json.JSONDecodeError as exc:
                record_warning(f"Malformed imednet:logicJson on ConditionDef: {exc}")

    if parsed_logic is None and raw_logic_xml and raw_logic_xml.strip():
        try:
            parsed_logic = parse_logic_xml(raw_logic_xml)
        except ValueError as exc:
            record_warning(f"Malformed logic XML on ConditionDef '{rule_id}': {exc}")

    return (
        container_form,
        BusinessLogicRule(
            id=rule_id,
            name=rule_name,
            type=rule_type,
            status=rule_status,
            form=rule_form,
            sequence=seq,
            logic=parsed_logic,
            logicRawXml=None if no_raw_xml else raw_logic_xml,
            extraAttributes=extra,
        ),
    )


def _parse_formal_rule_element(
    elem: ET.Element,
    odm_element_type: str,
    seq: int,
    referencing_forms: set[str],
    is_protocol_scoped: bool,
    target_vars: list[str],
    no_raw_xml: bool,
    record_warning: Any,
) -> BusinessLogicRule:
    """Convert an ODM ConditionDef or MethodDef element into a BusinessLogicRule without eval."""
    known_attrs = (
        KNOWN_METHOD_DEF_ATTRIBUTES
        if odm_element_type == "MethodDef"
        else KNOWN_CONDITION_DEF_ATTRIBUTES
    )
    extra = _extract_unmapped_attributes(elem, known_attrs, odm_element_type, record_warning)

    rule_oid = (_get_attr(elem, "OID") or "").strip() or None
    rule_name = (_get_attr(elem, "Name") or "").strip() or rule_oid
    method_type = (_get_attr(elem, "Type") or "").strip() if odm_element_type == "MethodDef" else ""
    desc = _get_translated_text(_find_first_child(elem, "Description"))

    formal_expressions: list[dict[str, str]] = []
    for fe in _find_children(elem, "FormalExpression"):
        _extract_unmapped_attributes(
            fe,
            KNOWN_FORMAL_EXPRESSION_ATTRIBUTES,
            "FormalExpression",
            record_warning,
        )
        expr_entry: dict[str, str] = {}
        if ctx := _get_attr(fe, "Context"):
            expr_entry["context"] = ctx.strip()
        if fe.text and fe.text.strip():
            expr_entry["expression"] = fe.text.strip()
        if expr_entry:
            formal_expressions.append(expr_entry)

    aliases: list[dict[str, str]] = []
    for alias_elem in _find_children(elem, "Alias"):
        alias_ctx = (_get_attr(alias_elem, "Context") or "").strip()
        alias_name = (_get_attr(alias_elem, "Name") or "").strip()
        if alias_ctx or alias_name:
            aliases.append({"context": alias_ctx, "name": alias_name})

    logic_ast: dict[str, Any] = {"odmElement": odm_element_type}
    if method_type:
        logic_ast["methodType"] = method_type
        extra["methodType"] = method_type
    if desc:
        logic_ast["description"] = desc
        extra["description"] = desc
    if formal_expressions:
        logic_ast["formalExpressions"] = formal_expressions
    if aliases:
        extra["aliases"] = aliases
    if target_vars:
        extra["targetVariables"] = list(dict.fromkeys(target_vars))
    extra["odmElement"] = odm_element_type

    if len(referencing_forms) == 1 and not is_protocol_scoped:
        attached_form = next(iter(referencing_forms))
        rule_type = "Form"
    else:
        attached_form = None
        rule_type = "Method" if odm_element_type == "MethodDef" else "Condition"

    raw_xml = None if no_raw_xml else str(ET.tostring(elem, encoding="unicode")).strip()

    return BusinessLogicRule(
        id=rule_oid,
        name=rule_name,
        type=rule_type,
        status="Active",
        form=attached_form,
        sequence=seq,
        logic=logic_ast,
        logicRawXml=raw_xml,
        extraAttributes=extra,
    )


def load_from_cdisc_odm(
    source: Path | str | bytes | bytearray | TextIO | BinaryIO,
    *,
    study_key: str | None = None,
    strict: bool = False,
    no_raw_xml: bool = False,
) -> DataDictionary:
    """Parse CDISC ODM 1.3 XML file or stream into a canonical DataDictionary.

    Args:
        source: XML file path, string buffer, or file-like stream.
        study_key: Optional study key override. If omitted, uses Study OID from XML.
        strict: If True, raise ValueError on validation warnings or unmapped definitions.
        no_raw_xml: If True, omit verbatim logicRawXml strings on BusinessLogicRule entries.

    Returns:
        Canonical DataDictionary representation.

    Raises:
        ValueError: If strict is True and validation warnings are encountered, or XML is invalid.
        DefusedXmlException: If XML entity expansion or malicious XML is detected.
    """
    warnings: list[str] = []

    def record_warning(msg: str) -> None:
        warnings.append(msg)
        if strict:
            raise ValueError(f"Strict loading error: {msg}")

    # 1. Read source bytes and parse safely with defusedxml
    content = _read_source_content(source)
    try:
        root = ET.fromstring(content)
    except DefusedXmlException:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to parse CDISC ODM XML: {exc}") from exc

    if _local_tag(root).lower() != "odm":
        record_warning(f"Root XML tag is '{_local_tag(root)}', expected 'ODM'.")

    _check_unparsed_containers_in_metadata_loader(root, record_warning)

    # 2. Extract Study metadata & BasicDefinitions MeasurementUnits
    study_elems = _find_children(root, "Study")
    if len(study_elems) > 1:
        record_warning(
            f"Multiple 'Study' elements ({len(study_elems)}) found in ODM document; "
            "only the first Study is loaded by load_from_cdisc_odm."
        )
    study_elem = study_elems[0] if study_elems else None
    study_oid: str | None = None
    study_name: str | None = None
    study_description: str | None = None
    protocol_name: str | None = None
    measurement_units_by_oid: dict[str, dict[str, Any]] = {}
    study_measurement_units: list[dict[str, Any]] = []

    if study_elem is not None:
        study_oid = _get_attr(study_elem, "OID")
        gv_elem = _find_first_child(study_elem, "GlobalVariables")
        if gv_elem is not None:
            name_elem = _find_first_child(gv_elem, "StudyName")
            if name_elem is not None and name_elem.text:
                study_name = name_elem.text.strip()
            desc_elem = _find_first_child(gv_elem, "StudyDescription")
            if desc_elem is not None and desc_elem.text:
                study_description = desc_elem.text.strip()
            proto_name_elem = _find_first_child(gv_elem, "ProtocolName")
            if proto_name_elem is not None and proto_name_elem.text:
                protocol_name = proto_name_elem.text.strip()

        for bd_elem in _find_children(study_elem, "BasicDefinitions"):
            for mu_elem in _find_children(bd_elem, "MeasurementUnit"):
                mu_extra = _extract_unmapped_attributes(
                    mu_elem,
                    KNOWN_MEASUREMENT_UNIT_ATTRIBUTES,
                    "MeasurementUnit",
                    record_warning,
                )
                mu_oid = (_get_attr(mu_elem, "OID") or "").strip()
                mu_name = (_get_attr(mu_elem, "Name") or mu_oid).strip()
                mu_symbol = _get_translated_text(_find_first_child(mu_elem, "Symbol")) or mu_name
                if mu_oid:
                    mu_dict: dict[str, Any] = {
                        "oid": mu_oid,
                        "name": mu_name,
                        "symbol": mu_symbol,
                        **mu_extra,
                    }
                    measurement_units_by_oid[mu_oid] = mu_dict
                    study_measurement_units.append(mu_dict)
    else:
        record_warning("No 'Study' element found in ODM document.")

    effective_study_key = study_key or study_oid

    # 3. Locate MetaDataVersion element(s)
    mdv_elements: list[ET.Element] = []
    if study_elem is not None:
        mdv_elements = _find_children(study_elem, "MetaDataVersion")
    if not mdv_elements:
        mdv_elements = _find_descendants(root, "MetaDataVersion")

    extra_meta: dict[str, Any] = {}
    if study_name:
        extra_meta["studyName"] = study_name
    if study_description:
        extra_meta["studyDescription"] = study_description
    if protocol_name:
        extra_meta["protocolName"] = protocol_name
    if study_measurement_units:
        extra_meta["measurementUnits"] = study_measurement_units

    if not mdv_elements:
        record_warning("No 'MetaDataVersion' element found in ODM document.")
        metadata = Metadata.create(
            study_key=effective_study_key,
            source="cdisc_odm",
            validation_warnings=warnings,
            extraAttributes=extra_meta,
        )
        return DataDictionary(metadata=metadata, forms=[])

    # Index definitions across MetaDataVersion elements
    codelist_elems: dict[str, ET.Element] = {}
    item_def_elems: dict[str, ET.Element] = {}
    item_group_def_elems: dict[str, ET.Element] = {}
    form_def_elems: list[ET.Element] = []
    study_event_def_elems: dict[str, ET.Element] = {}
    protocol_elems: list[ET.Element] = []
    condition_def_elems: dict[str, ET.Element] = {}
    method_def_elems: dict[str, ET.Element] = {}

    for mdv in mdv_elements:
        for proto in _find_children(mdv, "Protocol"):
            protocol_elems.append(proto)
        for se in _find_children(mdv, "StudyEventDef"):
            se_oid = _get_attr(se, "OID")
            if se_oid:
                study_event_def_elems[se_oid] = se
        for cl in _find_children(mdv, "CodeList"):
            cl_oid = _get_attr(cl, "OID")
            if cl_oid:
                codelist_elems[cl_oid] = cl
        for item in _find_children(mdv, "ItemDef"):
            item_oid = _get_attr(item, "OID")
            if item_oid:
                item_def_elems[item_oid] = item
        for ig in _find_children(mdv, "ItemGroupDef"):
            ig_oid = _get_attr(ig, "OID")
            if ig_oid:
                item_group_def_elems[ig_oid] = ig
        for f in _find_children(mdv, "FormDef"):
            form_def_elems.append(f)
        for cond in _find_children(mdv, "ConditionDef"):
            cond_oid = _get_attr(cond, "OID")
            if cond_oid:
                condition_def_elems[cond_oid] = cond
        for meth in _find_children(mdv, "MethodDef"):
            meth_oid = _get_attr(meth, "OID")
            if meth_oid:
                method_def_elems[meth_oid] = meth

    known_form_oids = {oid for f in form_def_elems if (oid := (_get_attr(f, "OID") or "").strip())}

    condition_form_refs: dict[str, set[str]] = {oid: set() for oid in condition_def_elems}
    condition_protocol_scoped: set[str] = set()
    condition_target_vars: dict[str, list[str]] = {oid: [] for oid in condition_def_elems}
    method_form_refs: dict[str, set[str]] = {oid: set() for oid in method_def_elems}
    method_target_vars: dict[str, list[str]] = {oid: [] for oid in method_def_elems}

    # 3b. Parse Protocol and StudyEventDef Visit schedules
    protocol_se_meta: dict[str, dict[str, Any]] = {}
    protocol_se_order: list[str] = []
    if protocol_elems:
        study_event_refs_data: list[dict[str, Any]] = []
        protocol_desc: str | None = None
        for proto_elem in protocol_elems:
            if not protocol_desc:
                protocol_desc = _get_translated_text(_find_first_child(proto_elem, "Description"))
            se_refs = _find_children(proto_elem, "StudyEventRef")
            se_refs.sort(key=lambda r: _parse_order(r, 0))
            for se_idx, se_ref in enumerate(se_refs, 1):
                se_ref_extra = _extract_unmapped_attributes(
                    se_ref,
                    KNOWN_STUDY_EVENT_REF_ATTRIBUTES,
                    "StudyEventRef",
                    record_warning,
                )
                se_oid = (_get_attr(se_ref, "StudyEventOID") or "").strip()
                if se_oid and se_oid not in study_event_def_elems:
                    record_warning(
                        f"StudyEventRef '{se_oid}' in Protocol was not found in MetaDataVersion."
                    )
                order_num = _parse_order(se_ref, se_idx)
                mand = _normalize_mandatory(_get_attr(se_ref, "Mandatory"))
                cond_oid = (_get_attr(se_ref, "CollectionExceptionConditionOID") or "").strip()
                if cond_oid:
                    if cond_oid in condition_def_elems:
                        condition_protocol_scoped.add(cond_oid)
                    else:
                        record_warning(
                            f"CollectionExceptionConditionOID '{cond_oid}' on StudyEventRef "
                            f"'{se_oid}' was not found in MetaDataVersion."
                        )

                ref_entry: dict[str, Any] = {
                    "studyEventOid": se_oid,
                    "orderNumber": order_num,
                }
                if mand:
                    ref_entry["mandatory"] = mand
                if cond_oid:
                    ref_entry["collectionExceptionConditionOid"] = cond_oid
                ref_entry.update(se_ref_extra)
                study_event_refs_data.append(ref_entry)
                if se_oid and se_oid not in protocol_se_meta:
                    protocol_se_order.append(se_oid)
                    protocol_se_meta[se_oid] = {
                        "orderNumber": order_num,
                        **({"mandatory": mand} if mand else {}),
                    }

        proto_payload: dict[str, Any] = {"studyEventRefs": study_event_refs_data}
        if protocol_desc:
            proto_payload["description"] = protocol_desc
        extra_meta["protocol"] = proto_payload

    visits_by_form: dict[str, list[dict[str, Any]]] = {}
    parsed_visits: list[dict[str, Any]] = []
    ordered_se_oids = list(protocol_se_order) + [
        oid for oid in study_event_def_elems if oid not in protocol_se_meta
    ]
    for se_oid in ordered_se_oids:
        se_elem = study_event_def_elems.get(se_oid)
        if se_elem is None:
            continue
        se_extra = _extract_unmapped_attributes(
            se_elem,
            KNOWN_STUDY_EVENT_ATTRIBUTES,
            "StudyEventDef",
            record_warning,
        )
        se_name = (_get_attr(se_elem, "Name") or se_oid).strip()
        se_repeating = (_get_attr(se_elem, "Repeating") or "No").strip()
        se_type = (_get_attr(se_elem, "Type") or "Scheduled").strip()
        se_category = (_get_attr(se_elem, "Category") or "").strip()
        se_desc = _get_translated_text(_find_first_child(se_elem, "Description"))

        form_refs = _find_children(se_elem, "FormRef")
        form_refs.sort(key=lambda r: _parse_order(r, 0))
        visit_forms: list[dict[str, Any]] = []
        for f_idx, f_ref in enumerate(form_refs, 1):
            f_ref_extra = _extract_unmapped_attributes(
                f_ref,
                KNOWN_FORM_REF_ATTRIBUTES,
                "FormRef",
                record_warning,
            )
            ref_form_oid = (_get_attr(f_ref, "FormOID") or "").strip()
            if ref_form_oid and ref_form_oid not in known_form_oids:
                record_warning(
                    f"FormRef '{ref_form_oid}' in StudyEventDef '{se_oid}' was not found in MetaDataVersion."
                )
            f_order = _parse_order(f_ref, f_idx)
            f_mand = _normalize_mandatory(_get_attr(f_ref, "Mandatory"))
            f_cond_oid = (_get_attr(f_ref, "CollectionExceptionConditionOID") or "").strip()
            if f_cond_oid:
                if f_cond_oid in condition_def_elems:
                    if ref_form_oid:
                        condition_form_refs[f_cond_oid].add(ref_form_oid)
                else:
                    record_warning(
                        f"CollectionExceptionConditionOID '{f_cond_oid}' on FormRef "
                        f"'{ref_form_oid}' was not found in MetaDataVersion."
                    )

            vf_entry: dict[str, Any] = {
                "formOid": ref_form_oid,
                "orderNumber": f_order,
            }
            if f_mand:
                vf_entry["mandatory"] = f_mand
            if f_cond_oid:
                vf_entry["collectionExceptionConditionOid"] = f_cond_oid
            vf_entry.update(f_ref_extra)
            visit_forms.append(vf_entry)

            if ref_form_oid:
                form_visit_entry: dict[str, Any] = {
                    "oid": se_oid,
                    "name": se_name,
                    "type": se_type,
                    "repeating": se_repeating,
                    "orderNumber": f_order,
                }
                if f_mand:
                    form_visit_entry["mandatory"] = f_mand
                if f_cond_oid:
                    form_visit_entry["collectionExceptionConditionOid"] = f_cond_oid
                visits_by_form.setdefault(ref_form_oid, []).append(form_visit_entry)

        visit_dict: dict[str, Any] = {
            "oid": se_oid,
            "name": se_name,
            "type": se_type,
            "repeating": se_repeating,
            "forms": visit_forms,
        }
        if se_category:
            visit_dict["category"] = se_category
        if se_desc:
            visit_dict["description"] = se_desc
        if se_oid in protocol_se_meta:
            visit_dict.update(protocol_se_meta[se_oid])
        visit_dict.update(se_extra)
        parsed_visits.append(visit_dict)

    if parsed_visits:
        extra_meta["visits"] = parsed_visits

    # 4. Parse CodeLists into Choice collections
    parsed_codelists: dict[str, list[Choice]] = {}
    synthetic_role_codelists: set[str] = set()
    referenced_item_codelists: set[str] = set()
    referenced_role_codelists: set[str] = set()
    for cl_oid, cl_elem in codelist_elems.items():
        choices: list[Choice] = []
        _extract_unmapped_attributes(
            cl_elem,
            KNOWN_CODELIST_ATTRIBUTES,
            "CodeList",
            record_warning,
            known_imednet=KNOWN_IMEDNET_CODELIST_ATTRIBUTES,
        )
        if (_get_imednet_attr(cl_elem, "SyntheticRoleCodeList") or "").strip().lower() == "yes":
            synthetic_role_codelists.add(cl_oid)

        pos_idx = 1
        for child in cl_elem:
            child_tag = _local_tag(child).lower()
            if child_tag in ("codelistitem", "enumerateditem"):
                coded_val = _get_attr(child, "CodedValue") or ""
                extra = _extract_unmapped_attributes(
                    child,
                    KNOWN_CODELIST_ITEM_ATTRIBUTES,
                    "CodeListItem",
                    record_warning,
                )

                if child_tag == "codelistitem":
                    decode_elem = _find_first_child(child, "Decode")
                    choice_text = _get_translated_text(decode_elem) or coded_val
                else:
                    choice_text = coded_val

                pos = _parse_order(child, pos_idx)
                pos_idx += 1

                choices.append(
                    Choice(
                        choiceValue=coded_val,
                        choiceText=choice_text,
                        position=pos,
                        extraAttributes=extra,
                    )
                )

        parsed_codelists[cl_oid] = choices

    def _build_variable_from_item_ref(
        item_ref: ET.Element,
        *,
        f_oid: str,
        ig_oid: str | None,
        inherited_domain: str | None,
        inherited_sas_dataset: str | None,
        fallback_seq: int,
        not_found_context: str,
        sibling_refs: list[tuple[str, str | None]] | None = None,
    ) -> Variable | None:
        it_oid = _get_attr(item_ref, "ItemOID") or ""
        ref_extra = _extract_unmapped_attributes(
            item_ref,
            KNOWN_ITEM_REF_ATTRIBUTES,
            "ItemRef",
            record_warning,
            known_imednet=KNOWN_IMEDNET_ITEM_REF_ATTRIBUTES,
        )

        it_def = item_def_elems.get(it_oid)
        if it_def is None:
            record_warning(
                f"ItemRef '{it_oid}' {not_found_context} was not found in MetaDataVersion."
            )
            return None

        it_extra = _extract_unmapped_attributes(
            it_def,
            KNOWN_ITEM_ATTRIBUTES,
            "ItemDef",
            record_warning,
            known_imednet=KNOWN_IMEDNET_ITEM_ATTRIBUTES,
        )
        for k, v in ref_extra.items():
            it_extra.setdefault(k, v)

        if inherited_domain and inherited_domain.strip():
            it_extra["Domain"] = inherited_domain.strip()
        if inherited_sas_dataset and inherited_sas_dataset.strip():
            it_extra["SASDatasetName"] = inherited_sas_dataset.strip()

        it_extra.update(_extract_item_sdtm_attributes(it_def, record_warning))

        item_units, range_checks = _extract_item_units_and_ranges(
            it_def,
            it_oid,
            measurement_units_by_oid,
            record_warning,
        )
        if item_units:
            it_extra["measurementUnits"] = item_units
        if range_checks:
            it_extra["rangeChecks"] = range_checks

        var_name = _resolve_variable_name(
            it_def,
            it_oid,
            f_oid,
            ig_oid,
            sibling_refs=sibling_refs,
            item_def_elems=item_def_elems,
        )
        raw_item_oid = (_get_attr(it_def, "OID") or it_oid).strip()

        method_oid = (_get_attr(item_ref, "MethodOID") or "").strip()
        if method_oid:
            it_extra["methodOid"] = method_oid
            if method_oid in method_def_elems:
                if f_oid:
                    method_form_refs[method_oid].add(f_oid)
                method_target_vars[method_oid].append(var_name)
            else:
                record_warning(
                    f"MethodOID '{method_oid}' on ItemRef '{it_oid}' was not found in MetaDataVersion."
                )

        role_val = (_get_attr(item_ref, "Role") or "").strip()
        if role_val:
            it_extra["Role"] = role_val
        role_cl_oid = (_get_attr(item_ref, "RoleCodeListOID") or "").strip()
        if role_cl_oid:
            it_extra["RoleCodeListOID"] = role_cl_oid
            if role_cl_oid in parsed_codelists:
                referenced_role_codelists.add(role_cl_oid)
            else:
                record_warning(
                    f"RoleCodeListOID '{role_cl_oid}' on ItemRef '{it_oid}' "
                    "was not found in MetaDataVersion."
                )

        cond_oid = (_get_attr(item_ref, "CollectionExceptionConditionOID") or "").strip()
        if cond_oid:
            it_extra["collectionExceptionConditionOid"] = cond_oid
            if cond_oid in condition_def_elems:
                if f_oid:
                    condition_form_refs[cond_oid].add(f_oid)
                condition_target_vars[cond_oid].append(var_name)
            else:
                record_warning(
                    f"CollectionExceptionConditionOID '{cond_oid}' on ItemRef '{it_oid}' "
                    "was not found in MetaDataVersion."
                )

        q_elem = _find_first_child(it_def, "Question")
        desc_elem = _find_first_child(it_def, "Description")
        var_label = (
            _get_translated_text(q_elem)
            or _get_translated_text(desc_elem)
            or _get_attr(it_def, "Name")
        )

        seq = _parse_order(item_ref, fallback_seq)

        req_policy = _get_imednet_attr(item_ref, "RequiredPolicy")
        mand_val = _get_attr(item_ref, "Mandatory")
        req: str | None = None
        if req_policy and req_policy.strip():
            req = req_policy.strip()
        elif mand_val:
            req = _normalize_mandatory(mand_val)

        raw_datatype = _get_attr(it_def, "DataType")
        if raw_datatype and raw_datatype.strip().lower() in _PRESERVED_ODM_RAW_DATA_TYPES:
            it_extra["odmDataType"] = raw_datatype.strip()
        control_type = _get_attr(it_def, "ControlType") or _get_attr(item_ref, "ControlType")
        imednet_var_type = _get_imednet_attr(it_def, "VariableType")

        cl_ref = _find_first_child(it_def, "CodeListRef")
        var_choices: list[Choice] = []
        if cl_ref is not None:
            cl_oid = _get_attr(cl_ref, "CodeListOID") or ""
            if cl_oid in parsed_codelists:
                referenced_item_codelists.add(cl_oid)
                var_choices = [c.model_copy() for c in parsed_codelists[cl_oid]]
            else:
                record_warning(
                    f"CodeListRef '{cl_oid}' on item '{it_oid}' was not found in MetaDataVersion."
                )

        var_type = _map_datatype(
            raw_datatype,
            it_oid,
            has_choices=bool(var_choices),
            control_type=control_type,
            record_warning=record_warning,
            imednet_variable_type=imednet_var_type,
        )

        built_var = Variable(
            variableName=var_name,
            label=var_label,
            variableType=var_type,
            sequence=seq,
            required=req,
            format=_get_attr(it_def, "Length") or _get_imednet_attr(it_def, "Format"),
            precision=_get_attr(it_def, "SignificantDigits")
            or _get_imednet_attr(it_def, "Precision"),
            choices=var_choices,
            extraAttributes=it_extra,
        )
        if raw_item_oid:
            raw_oid_by_var_id[id(built_var)] = raw_item_oid
        return built_var

    # 5. Parse Forms and nested ItemGroups/Items
    raw_oid_by_var_id: dict[int, str] = {}
    forms: list[Form] = []
    forms_by_oid: dict[str, Form] = {}

    for form_elem in form_def_elems:
        f_oid = _get_attr(form_elem, "OID") or ""
        f_name = _get_attr(form_elem, "Name") or f_oid
        f_type = normalize_form_type(_get_attr(form_elem, "FormType"), default="Subject")
        raw_form_id = _get_imednet_attr(form_elem, "FormId")
        f_id = normalize_form_id(raw_form_id, form_key=f_oid)
        f_extra = _extract_unmapped_attributes(
            form_elem,
            KNOWN_FORM_ATTRIBUTES,
            "FormDef",
            record_warning,
            known_imednet=KNOWN_IMEDNET_FORM_ATTRIBUTES,
        )
        if (repeating := _get_attr(form_elem, "Repeating")) and repeating.strip().lower() == "yes":
            f_extra.setdefault("repeating", repeating.strip())
        if f_oid in visits_by_form:
            f_extra["visits"] = visits_by_form[f_oid]

        variables: list[Variable] = []
        var_seq = 1

        ig_refs = _find_children(form_elem, "ItemGroupRef")
        ig_refs.sort(key=lambda r: _parse_order(r, 0))
        direct_item_refs = _find_children(form_elem, "ItemRef")
        if direct_item_refs:
            direct_item_refs.sort(key=lambda r: _parse_order(r, 0))

        form_sibling_refs: list[tuple[str, str | None]] = []
        for ig_ref in ig_refs:
            ig_oid_preview = _get_attr(ig_ref, "ItemGroupOID") or ""
            ig_def_preview = item_group_def_elems.get(ig_oid_preview)
            if ig_def_preview is not None:
                for ir in _find_children(ig_def_preview, "ItemRef"):
                    form_sibling_refs.append((_get_attr(ir, "ItemOID") or "", ig_oid_preview))
        for ir in direct_item_refs:
            form_sibling_refs.append((_get_attr(ir, "ItemOID") or "", None))

        for ig_ref in ig_refs:
            ig_oid = _get_attr(ig_ref, "ItemGroupOID") or ""
            _extract_unmapped_attributes(
                ig_ref,
                KNOWN_ITEM_GROUP_REF_ATTRIBUTES,
                "ItemGroupRef",
                record_warning,
            )
            ig_cond_oid = (_get_attr(ig_ref, "CollectionExceptionConditionOID") or "").strip()
            if ig_cond_oid:
                f_extra.setdefault("collectionExceptionConditionOid", ig_cond_oid)
                if ig_cond_oid in condition_def_elems:
                    if f_oid:
                        condition_form_refs[ig_cond_oid].add(f_oid)
                else:
                    record_warning(
                        f"CollectionExceptionConditionOID '{ig_cond_oid}' on ItemGroupRef "
                        f"'{ig_oid}' was not found in MetaDataVersion."
                    )

            ig_def = item_group_def_elems.get(ig_oid)
            if ig_def is None:
                record_warning(
                    f"ItemGroupRef '{ig_oid}' referenced in form '{f_oid}' was not found in MetaDataVersion."
                )
                continue

            if ig_oid and ig_oid != f"IG_{f_oid}" and "itemGroupOID" not in f_extra:
                f_extra["itemGroupOID"] = ig_oid

            ig_extra = _extract_unmapped_attributes(
                ig_def,
                KNOWN_ITEM_GROUP_ATTRIBUTES,
                "ItemGroupDef",
                record_warning,
            )
            for k, v in ig_extra.items():
                if k not in f_extra:
                    f_extra[k] = v

            ig_domain = _get_attr(ig_def, "Domain")
            ig_sas_dataset = _get_attr(ig_def, "SASDatasetName")
            ig_purpose = _get_attr(ig_def, "Purpose")
            if ig_domain and ig_domain.strip() and "Domain" not in f_extra:
                f_extra["Domain"] = ig_domain.strip()
            if ig_sas_dataset and ig_sas_dataset.strip() and "SASDatasetName" not in f_extra:
                f_extra["SASDatasetName"] = ig_sas_dataset.strip()
            if ig_purpose and ig_purpose.strip() and "Purpose" not in f_extra:
                f_extra["Purpose"] = ig_purpose.strip()

            item_refs = _find_children(ig_def, "ItemRef")
            item_refs.sort(key=lambda r: _parse_order(r, 0))

            for item_ref in item_refs:
                parsed_var = _build_variable_from_item_ref(
                    item_ref,
                    f_oid=f_oid,
                    ig_oid=ig_oid,
                    inherited_domain=ig_domain,
                    inherited_sas_dataset=ig_sas_dataset,
                    fallback_seq=var_seq,
                    not_found_context=f"in ItemGroup '{ig_oid}'",
                    sibling_refs=form_sibling_refs,
                )
                if parsed_var is not None:
                    variables.append(parsed_var)
                    var_seq += 1

        if direct_item_refs:
            for item_ref in direct_item_refs:
                parsed_var = _build_variable_from_item_ref(
                    item_ref,
                    f_oid=f_oid,
                    ig_oid=None,
                    inherited_domain=f_extra.get("Domain"),
                    inherited_sas_dataset=f_extra.get("SASDatasetName"),
                    fallback_seq=var_seq,
                    not_found_context=f"on Form '{f_oid}'",
                    sibling_refs=form_sibling_refs,
                )
                if parsed_var is not None:
                    variables.append(parsed_var)
                    var_seq += 1

        form = Form(
            formKey=f_oid,
            formName=f_name,
            formId=f_id,
            formType=f_type,
            variables=variables,
            extraAttributes=f_extra,
        )
        forms.append(form)
        if f_oid:
            forms_by_oid[f_oid] = form

    if forms and raw_oid_by_var_id:
        from ..emit import _allocate_item_oids

        sorted_loaded_forms = sorted(forms, key=lambda f: f.formKey or "")
        sorted_vars_by_f, default_oid_by_occ, _ = _allocate_item_oids(sorted_loaded_forms)
        for f_idx, loaded_vars in enumerate(sorted_vars_by_f):
            for v_idx, loaded_var in enumerate(loaded_vars):
                raw_oid_val = raw_oid_by_var_id.get(id(loaded_var))
                default_oid_val = default_oid_by_occ.get((f_idx, v_idx))
                if raw_oid_val and raw_oid_val != default_oid_val:
                    loaded_var.extraAttributes.setdefault("itemOID", raw_oid_val)

    standalone_role_cls: list[dict[str, Any]] = []
    for rcl_oid in referenced_role_codelists:
        if rcl_oid in referenced_item_codelists or rcl_oid in synthetic_role_codelists:
            continue
        rcl_elem = codelist_elems.get(rcl_oid)
        if rcl_elem is None:
            continue
        standalone_role_cls.append(
            {
                "oid": rcl_oid,
                "name": (_get_attr(rcl_elem, "Name") or rcl_oid).strip(),
                "dataType": (_get_attr(rcl_elem, "DataType") or "text").strip(),
                "items": [
                    {
                        "choiceValue": c.choiceValue,
                        "choiceText": c.choiceText,
                        "position": c.position,
                    }
                    for c in parsed_codelists.get(rcl_oid, [])
                ],
            }
        )
    if standalone_role_cls:
        extra_meta["roleCodeLists"] = standalone_role_cls

    # 5b. Map ConditionDef and MethodDef elements into BusinessLogicRule entries
    unassigned_logic: list[BusinessLogicRule] = []
    rule_seq = 1

    for cond_oid, cond_elem in condition_def_elems.items():
        if (_get_imednet_attr(cond_elem, "RuleSource") or "").strip().lower() == "imednet":
            container_form, rule = _parse_imednet_condition_rule(
                cond_elem,
                rule_seq,
                no_raw_xml,
                record_warning,
            )
            rule_seq += 1
            if container_form and container_form in forms_by_oid:
                forms_by_oid[container_form].businessLogic.append(rule)
            else:
                unassigned_logic.append(rule)
            continue

        rule = _parse_formal_rule_element(
            cond_elem,
            "ConditionDef",
            rule_seq,
            condition_form_refs.get(cond_oid, set()),
            cond_oid in condition_protocol_scoped,
            condition_target_vars.get(cond_oid, []),
            no_raw_xml,
            record_warning,
        )
        rule_seq += 1
        if rule.form and rule.form in forms_by_oid:
            forms_by_oid[rule.form].businessLogic.append(rule)
        else:
            unassigned_logic.append(rule)

    for meth_oid, meth_elem in method_def_elems.items():
        rule = _parse_formal_rule_element(
            meth_elem,
            "MethodDef",
            rule_seq,
            method_form_refs.get(meth_oid, set()),
            False,
            method_target_vars.get(meth_oid, []),
            no_raw_xml,
            record_warning,
        )
        rule_seq += 1
        if rule.form and rule.form in forms_by_oid:
            forms_by_oid[rule.form].businessLogic.append(rule)
        else:
            unassigned_logic.append(rule)

    # 6. Build Metadata envelope
    metadata = Metadata.create(
        study_key=effective_study_key,
        source="cdisc_odm",
        validation_warnings=warnings,
        extraAttributes=extra_meta,
    )

    return DataDictionary(
        metadata=metadata,
        forms=forms,
        unassignedBusinessLogic=unassigned_logic,
    )


def _check_unparsed_containers_in_metadata_loader(
    root: ET.Element,
    record_warning: Any,
) -> None:
    """Report non-metadata ODM containers or unknown elements in ``load_from_cdisc_odm``."""
    unparsed_top_tags: list[str] = []
    for child in root:
        c_tag = _local_tag(child)
        c_low = c_tag.lower()
        if c_low == "study":
            for s_child in child:
                sc_tag = _local_tag(s_child)
                sc_low = sc_tag.lower()
                if sc_low not in KNOWN_STUDY_CHILD_TAGS:
                    record_warning(
                        f"Unsupported child element '<{sc_tag}>' under <Study> in load_from_cdisc_odm."
                    )
                elif sc_low == "metadataversion":
                    for mdv_child in s_child:
                        mc_tag = _local_tag(mdv_child)
                        if mc_tag.lower() not in KNOWN_MDV_CHILD_TAGS:
                            record_warning(
                                f"Unsupported child element '<{mc_tag}>' under <MetaDataVersion> "
                                "in load_from_cdisc_odm."
                            )
        elif c_low in NON_METADATA_ODM_CONTAINERS or c_low in NON_METADATA_ODM_DESCENDANTS:
            if c_tag not in unparsed_top_tags:
                unparsed_top_tags.append(c_tag)
        else:
            record_warning(f"Unknown child element '<{c_tag}>' under <ODM>.")

    if unparsed_top_tags:
        tags_str = ", ".join(f"<{t}>" for t in unparsed_top_tags)
        msg = (
            f"Unparsed non-metadata ODM container(s) {tags_str} encountered in "
            "load_from_cdisc_odm; use load_odm_archive() to parse full ODM 1.3.2 archives."
        )
        record_warning(msg)
        py_warnings.warn(msg, UserWarning, stacklevel=3)
    else:
        for desc in root.iter():
            if desc is root:
                continue
            d_tag = _local_tag(desc)
            if d_tag.lower() in NON_METADATA_ODM_DESCENDANTS:
                msg = (
                    f"Unparsed clinical/audit ODM element '<{d_tag}>' encountered in "
                    "load_from_cdisc_odm; use load_odm_archive() to parse full ODM 1.3.2 archives."
                )
                record_warning(msg)
                py_warnings.warn(msg, UserWarning, stacklevel=3)


def _parse_audit_record_elem(elem: ET.Element, record_warning: Any) -> OdmAuditRecord:
    user_ref = _find_first_child(elem, "UserRef")
    loc_ref = _find_first_child(elem, "LocationRef")
    dt_elem = _find_first_child(elem, "DateTimeStamp")
    rfc_elem = _find_first_child(elem, "ReasonForChange")
    sid_elem = _find_first_child(elem, "SourceID")

    for child in elem:
        ctag = _local_tag(child)
        if ctag.lower() not in {
            "userref",
            "locationref",
            "datetimestamp",
            "reasonforchange",
            "sourceid",
        }:
            record_warning(f"Unknown child element '<{ctag}>' inside <AuditRecord>.")

    user_oid = (_get_attr(user_ref, "UserOID") if user_ref is not None else None) or ""
    if not user_oid:
        record_warning("<AuditRecord> is missing required UserRef/@UserOID.")
        user_oid = "USR.UNKNOWN"

    loc_oid = (_get_attr(loc_ref, "LocationOID") if loc_ref is not None else None) or ""
    if not loc_oid:
        record_warning("<AuditRecord> is missing required LocationRef/@LocationOID.")
        loc_oid = "LOC.UNKNOWN"

    dt_val = dt_elem.text.strip() if (dt_elem is not None and dt_elem.text) else ""
    if not dt_val:
        record_warning("<AuditRecord> is missing required <DateTimeStamp> text.")
        dt_val = "1970-01-01T00:00:00Z"

    return OdmAuditRecord(
        id=_get_attr(elem, "ID"),
        editPoint=_get_attr(elem, "EditPoint"),
        usedImputationMethod=_get_attr(elem, "UsedImputationMethod"),
        userOID=user_oid,
        locationOID=loc_oid,
        dateTimeStamp=dt_val,
        reasonForChange=(rfc_elem.text.strip() if rfc_elem is not None and rfc_elem.text else None),
        sourceID=(sid_elem.text.strip() if sid_elem is not None and sid_elem.text else None),
    )


def _parse_annotation_elem(
    elem: ET.Element,
    fallback_seq: int,
    known_codelists: set[str],
    record_warning: Any,
) -> OdmAnnotation:
    for child in elem:
        ctag = _local_tag(child)
        if ctag.lower() not in {"comment", "flag"}:
            record_warning(f"Unknown child element '<{ctag}>' inside <Annotation>.")

    seq_raw = _get_attr(elem, "SeqNum")
    seq_num = fallback_seq
    if seq_raw is not None and seq_raw.strip().lstrip("-").isdigit():
        seq_num = int(seq_raw.strip())
    elif seq_raw is not None:
        record_warning(f"Invalid SeqNum '{seq_raw}' on <Annotation>.")

    comment_elem = _find_first_child(elem, "Comment")
    comment_text = (
        comment_elem.text if (comment_elem is not None and comment_elem.text is not None) else None
    )
    sponsor_or_site = _get_attr(comment_elem, "SponsorOrSite") if comment_elem is not None else None

    flags: list[OdmFlag] = []
    for flg_elem in _find_children(elem, "Flag"):
        fv_elem = _find_first_child(flg_elem, "FlagValue")
        ft_elem = _find_first_child(flg_elem, "FlagType")
        if fv_elem is None:
            record_warning("<Flag> inside <Annotation> is missing <FlagValue>.")
            continue
        fv_cl = (_get_attr(fv_elem, "CodeListOID") or "").strip()
        if fv_cl and known_codelists and fv_cl not in known_codelists:
            record_warning(
                f"FlagValue CodeListOID '{fv_cl}' on <Annotation> was not found in MetaDataVersion."
            )
        ft_cl = (_get_attr(ft_elem, "CodeListOID") or "").strip() if ft_elem is not None else None
        if ft_cl and known_codelists and ft_cl not in known_codelists:
            record_warning(
                f"FlagType CodeListOID '{ft_cl}' on <Annotation> was not found in MetaDataVersion."
            )
        flags.append(
            OdmFlag(
                flagValue=(fv_elem.text.strip() if fv_elem.text else ""),
                flagValueCodeListOID=fv_cl or "CL.IMEDNET_FLAG_VALUE",
                flagType=(ft_elem.text.strip() if ft_elem is not None and ft_elem.text else None),
                flagTypeCodeListOID=ft_cl or None,
            )
        )

    return OdmAnnotation(
        seqNum=seq_num,
        id=_get_attr(elem, "ID"),
        transactionType=_get_attr(elem, "TransactionType"),
        comment=comment_text,
        sponsorOrSite=sponsor_or_site,
        flags=tuple(flags),
    )


def _parse_item_data_elem(
    elem: ET.Element,
    *,
    known_item_oids: set[str],
    known_codelists: set[str],
    known_user_oids: set[str],
    known_loc_oids: set[str],
    record_warning: Any,
) -> OdmItemData:
    for child in elem:
        ctag = _local_tag(child)
        if ctag.lower() not in {"auditrecord", "measurementunitref", "annotation"}:
            record_warning(f"Unknown child element '<{ctag}>' inside <ItemData>.")

    item_oid = (_get_attr(elem, "ItemOID") or "").strip()
    if not item_oid:
        record_warning("<ItemData> is missing required ItemOID attribute.")
    elif known_item_oids and item_oid not in known_item_oids:
        record_warning(f"ItemData ItemOID '{item_oid}' was not found in MetaDataVersion.")

    raw_val = _get_attr(elem, "Value")
    is_null_attr = (_get_attr(elem, "IsNull") or "").strip().lower()
    is_null = is_null_attr == "yes" or (raw_val is None and is_null_attr != "no")

    ar_elem = _find_first_child(elem, "AuditRecord")
    ar_obj = _parse_audit_record_elem(ar_elem, record_warning) if ar_elem is not None else None
    if ar_obj is not None:
        if known_user_oids and ar_obj.user_oid not in known_user_oids:
            record_warning(f"AuditRecord UserOID '{ar_obj.user_oid}' not found in AdminData.")
        if known_loc_oids and ar_obj.location_oid not in known_loc_oids:
            record_warning(
                f"AuditRecord LocationOID '{ar_obj.location_oid}' not found in AdminData."
            )

    mu_elem = _find_first_child(elem, "MeasurementUnitRef")
    mu_oid = _get_attr(mu_elem, "MeasurementUnitOID") if mu_elem is not None else None

    anns = tuple(
        _parse_annotation_elem(ann_el, idx, known_codelists, record_warning)
        for idx, ann_el in enumerate(_find_children(elem, "Annotation"), 1)
    )

    return OdmItemData(
        itemOID=item_oid or "UNKNOWN",
        value=None if is_null else raw_val,
        isNull=is_null,
        transactionType=_get_attr(elem, "TransactionType"),
        auditRecordID=_get_attr(elem, "AuditRecordID"),
        annotationID=_get_attr(elem, "AnnotationID"),
        measurementUnitOID=mu_oid,
        auditRecord=ar_obj,
        annotations=anns,
    )


def _parse_item_group_data_elem(
    elem: ET.Element,
    *,
    known_ig_oids: set[str],
    known_item_oids: set[str],
    known_codelists: set[str],
    known_user_oids: set[str],
    known_loc_oids: set[str],
    record_warning: Any,
) -> OdmItemGroupData:
    for child in elem:
        ctag = _local_tag(child)
        if ctag.lower() not in {"auditrecord", "annotation", "itemdata"}:
            record_warning(f"Unknown child element '<{ctag}>' inside <ItemGroupData>.")

    ig_oid = (_get_attr(elem, "ItemGroupOID") or "").strip()
    if not ig_oid:
        record_warning("<ItemGroupData> is missing required ItemGroupOID attribute.")
    elif known_ig_oids and ig_oid not in known_ig_oids:
        record_warning(f"ItemGroupData ItemGroupOID '{ig_oid}' was not found in MetaDataVersion.")

    ar_elem = _find_first_child(elem, "AuditRecord")
    ar_obj = _parse_audit_record_elem(ar_elem, record_warning) if ar_elem is not None else None
    if ar_obj is not None:
        if known_user_oids and ar_obj.user_oid not in known_user_oids:
            record_warning(f"AuditRecord UserOID '{ar_obj.user_oid}' not found in AdminData.")
        if known_loc_oids and ar_obj.location_oid not in known_loc_oids:
            record_warning(
                f"AuditRecord LocationOID '{ar_obj.location_oid}' not found in AdminData."
            )

    anns = tuple(
        _parse_annotation_elem(ann_el, idx, known_codelists, record_warning)
        for idx, ann_el in enumerate(_find_children(elem, "Annotation"), 1)
    )
    items = tuple(
        _parse_item_data_elem(
            it_el,
            known_item_oids=known_item_oids,
            known_codelists=known_codelists,
            known_user_oids=known_user_oids,
            known_loc_oids=known_loc_oids,
            record_warning=record_warning,
        )
        for it_el in _find_children(elem, "ItemData")
    )

    return OdmItemGroupData(
        itemGroupOID=ig_oid or "UNKNOWN",
        itemGroupRepeatKey=_get_attr(elem, "ItemGroupRepeatKey"),
        transactionType=_get_attr(elem, "TransactionType"),
        auditRecord=ar_obj,
        annotations=anns,
        items=items,
    )


def _parse_form_data_elem(
    elem: ET.Element,
    *,
    known_form_oids: set[str],
    known_ig_oids: set[str],
    known_item_oids: set[str],
    known_codelists: set[str],
    known_user_oids: set[str],
    known_loc_oids: set[str],
    record_warning: Any,
) -> OdmFormData:
    for child in elem:
        ctag = _local_tag(child)
        if ctag.lower() not in {"auditrecord", "annotation", "itemgroupdata"}:
            record_warning(f"Unknown child element '<{ctag}>' inside <FormData>.")

    form_oid = (_get_attr(elem, "FormOID") or "").strip()
    if not form_oid:
        record_warning("<FormData> is missing required FormOID attribute.")
    elif known_form_oids and form_oid not in known_form_oids:
        record_warning(f"FormData FormOID '{form_oid}' was not found in MetaDataVersion.")

    ar_elem = _find_first_child(elem, "AuditRecord")
    ar_obj = _parse_audit_record_elem(ar_elem, record_warning) if ar_elem is not None else None
    anns = tuple(
        _parse_annotation_elem(ann_el, idx, known_codelists, record_warning)
        for idx, ann_el in enumerate(_find_children(elem, "Annotation"), 1)
    )
    igs = tuple(
        _parse_item_group_data_elem(
            ig_el,
            known_ig_oids=known_ig_oids,
            known_item_oids=known_item_oids,
            known_codelists=known_codelists,
            known_user_oids=known_user_oids,
            known_loc_oids=known_loc_oids,
            record_warning=record_warning,
        )
        for ig_el in _find_children(elem, "ItemGroupData")
    )
    return OdmFormData(
        formOID=form_oid or "UNKNOWN",
        formRepeatKey=_get_attr(elem, "FormRepeatKey"),
        transactionType=_get_attr(elem, "TransactionType"),
        auditRecord=ar_obj,
        annotations=anns,
        itemGroups=igs,
    )


def _parse_admin_data_elem(elem: ET.Element, record_warning: Any) -> OdmAdminData:
    for child in elem:
        ctag = _local_tag(child)
        if ctag.lower() not in {"user", "location", "signaturedef"}:
            record_warning(f"Unknown child element '<{ctag}>' inside <AdminData>.")

    users: list[OdmUser] = []
    for u_el in _find_children(elem, "User"):
        u_oid = (_get_attr(u_el, "OID") or "").strip()
        if not u_oid:
            record_warning("<User> inside <AdminData> is missing required OID attribute.")
            continue
        loc_refs = tuple(
            ref_oid
            for lr in _find_children(u_el, "LocationRef")
            if (ref_oid := (_get_attr(lr, "LocationOID") or "").strip())
        )

        def _child_txt(parent_el: ET.Element, tag: str) -> str | None:
            found = _find_first_child(parent_el, tag)
            return found.text.strip() if (found is not None and found.text) else None

        users.append(
            OdmUser(
                oid=u_oid,
                userType=_get_attr(u_el, "UserType"),
                loginName=_child_txt(u_el, "LoginName"),
                displayName=_child_txt(u_el, "DisplayName"),
                fullName=_child_txt(u_el, "FullName"),
                firstName=_child_txt(u_el, "FirstName"),
                lastName=_child_txt(u_el, "LastName"),
                organization=_child_txt(u_el, "Organization"),
                email=_child_txt(u_el, "Email"),
                phone=_child_txt(u_el, "Phone"),
                locationRefs=loc_refs,
            )
        )

    locations: list[OdmLocation] = []
    for loc_el in _find_children(elem, "Location"):
        loc_oid = (_get_attr(loc_el, "OID") or "").strip()
        loc_name = (_get_attr(loc_el, "Name") or loc_oid).strip()
        if not loc_oid:
            record_warning("<Location> inside <AdminData> is missing required OID attribute.")
            continue
        mdv_refs: list[OdmMetaDataVersionRef] = []
        for mr_el in _find_children(loc_el, "MetaDataVersionRef"):
            s_oid = (_get_attr(mr_el, "StudyOID") or "").strip()
            m_oid = (_get_attr(mr_el, "MetaDataVersionOID") or "").strip()
            eff = (_get_attr(mr_el, "EffectiveDate") or "1970-01-01").strip()
            if s_oid and m_oid:
                mdv_refs.append(
                    OdmMetaDataVersionRef(
                        studyOID=s_oid,
                        metaDataVersionOID=m_oid,
                        effectiveDate=eff,
                    )
                )
        locations.append(
            OdmLocation(
                oid=loc_oid,
                name=loc_name,
                locationType=_get_attr(loc_el, "LocationType") or "Site",
                metaDataVersionRefs=tuple(mdv_refs),
            )
        )

    return OdmAdminData(
        studyOID=_get_attr(elem, "StudyOID"),
        users=tuple(users),
        locations=tuple(locations),
    )


def load_odm_archive(
    source: Path | str | bytes | bytearray | TextIO | BinaryIO,
    *,
    study_key: str | None = None,
    strict: bool = False,
    no_raw_xml: bool = False,
) -> OdmArchive:
    """Parse a full CDISC ODM 1.3 / 1.3.2 XML snapshot into a read-only :class:`OdmArchive`.

    Enforces ``defusedxml`` parsing and preserves ``DataDictionary`` metadata alongside
    ``<AdminData>``, ``<ReferenceData>``, ``<ClinicalData>``, ``<AuditRecord>``, and
    ``<Annotation>`` elements.
    """
    warnings: list[str] = []

    def record_warning(msg: str) -> None:
        warnings.append(msg)
        if strict:
            raise ValueError(f"Strict loading error: {msg}")

    content = _read_source_content(source)
    try:
        root = ET.fromstring(content)
    except DefusedXmlException:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to parse CDISC ODM XML: {exc}") from exc

    if _local_tag(root).lower() != "odm":
        record_warning(f"Root XML tag is '{_local_tag(root)}', expected 'ODM'.")

    # Build a metadata-only copy of the root containing only <Study> for DataDictionary extraction
    study_only_root = ET.fromstring(b'<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"/>')
    study_only_root.attrib.update(root.attrib)
    for child in root:
        ctag = _local_tag(child)
        clow = ctag.lower()
        if clow == "study":
            study_only_root.append(child)
        elif clow not in {"admindata", "referencedata", "clinicaldata"}:
            record_warning(f"Unknown top-level element '<{ctag}>' under <ODM>.")

    dd = load_from_cdisc_odm(
        ET.tostring(study_only_root, encoding="utf-8"),
        study_key=study_key,
        strict=strict,
        no_raw_xml=no_raw_xml,
    )
    warnings.extend(dd.metadata.validationWarnings)

    # Collect known metadata OIDs from <Study>/<MetaDataVersion> for reference checks
    known_mdv_oids: set[str] = set()
    known_se_oids: set[str] = set()
    known_form_oids: set[str] = set()
    known_ig_oids: set[str] = set()
    known_item_oids: set[str] = set()
    known_codelists: set[str] = set()
    first_mdv_oid = "MDV.001"
    first_mdv_name: str | None = None

    for idx_m, mdv_el in enumerate(_find_descendants(study_only_root, "MetaDataVersion")):
        m_oid = (_get_attr(mdv_el, "OID") or "").strip()
        if m_oid:
            known_mdv_oids.add(m_oid)
            if idx_m == 0:
                first_mdv_oid = m_oid
                first_mdv_name = _get_attr(mdv_el, "Name")
        for se_el in _find_children(mdv_el, "StudyEventDef"):
            if oid := (_get_attr(se_el, "OID") or "").strip():
                known_se_oids.add(oid)
        for fd_el in _find_children(mdv_el, "FormDef"):
            if oid := (_get_attr(fd_el, "OID") or "").strip():
                known_form_oids.add(oid)
        for ig_el in _find_children(mdv_el, "ItemGroupDef"):
            if oid := (_get_attr(ig_el, "OID") or "").strip():
                known_ig_oids.add(oid)
        for it_el in _find_children(mdv_el, "ItemDef"):
            if oid := (_get_attr(it_el, "OID") or "").strip():
                known_item_oids.add(oid)
        for cl_el in _find_children(mdv_el, "CodeList"):
            if oid := (_get_attr(cl_el, "OID") or "").strip():
                known_codelists.add(oid)

    admin_containers: list[OdmAdminData] = []
    known_user_oids: set[str] = set()
    known_loc_oids: set[str] = set()
    for admin_el in _find_children(root, "AdminData"):
        parsed_admin = _parse_admin_data_elem(admin_el, record_warning)
        admin_containers.append(parsed_admin)
        for u in parsed_admin.users:
            known_user_oids.add(u.oid)
        for loc in parsed_admin.locations:
            known_loc_oids.add(loc.oid)
            for mref in loc.metadata_version_refs:
                if known_mdv_oids and mref.metadata_version_oid not in known_mdv_oids:
                    record_warning(
                        f"Location '{loc.oid}' references unknown MetaDataVersionOID "
                        f"'{mref.metadata_version_oid}'."
                    )
        for u in parsed_admin.users:
            for lref in u.location_refs:
                if known_loc_oids and lref not in known_loc_oids:
                    record_warning(
                        f"User '{u.oid}' references unknown LocationOID '{lref}' in AdminData."
                    )

    ref_containers: list[OdmReferenceData] = []
    for ref_el in _find_children(root, "ReferenceData"):
        for child in ref_el:
            ctag = _local_tag(child)
            if ctag.lower() not in {"itemgroupdata", "auditrecords", "annotations"}:
                record_warning(f"Unknown child element '<{ctag}>' inside <ReferenceData>.")
        r_study = (_get_attr(ref_el, "StudyOID") or dd.metadata.studyKey or "STUDY_001").strip()
        r_mdv = (_get_attr(ref_el, "MetaDataVersionOID") or first_mdv_oid).strip()
        if known_mdv_oids and r_mdv not in known_mdv_oids:
            record_warning(f"ReferenceData references unknown MetaDataVersionOID '{r_mdv}'.")
        ref_igs = tuple(
            _parse_item_group_data_elem(
                ig_el,
                known_ig_oids=known_ig_oids,
                known_item_oids=known_item_oids,
                known_codelists=known_codelists,
                known_user_oids=known_user_oids,
                known_loc_oids=known_loc_oids,
                record_warning=record_warning,
            )
            for ig_el in _find_children(ref_el, "ItemGroupData")
        )
        ref_containers.append(
            OdmReferenceData(
                studyOID=r_study,
                metaDataVersionOID=r_mdv,
                itemGroups=ref_igs,
            )
        )

    clin_containers: list[OdmClinicalData] = []
    for clin_el in _find_children(root, "ClinicalData"):
        for child in clin_el:
            ctag = _local_tag(child)
            if ctag.lower() not in {"subjectdata", "auditrecords", "signatures", "annotations"}:
                record_warning(f"Unknown child element '<{ctag}>' inside <ClinicalData>.")

        c_study = (_get_attr(clin_el, "StudyOID") or dd.metadata.studyKey or "STUDY_001").strip()
        c_mdv = (_get_attr(clin_el, "MetaDataVersionOID") or first_mdv_oid).strip()
        if known_mdv_oids and c_mdv not in known_mdv_oids:
            record_warning(f"ClinicalData references unknown MetaDataVersionOID '{c_mdv}'.")

        subjects: list[OdmSubjectData] = []
        for subj_el in _find_children(clin_el, "SubjectData"):
            for child in subj_el:
                ctag = _local_tag(child)
                if ctag.lower() not in {
                    "auditrecord",
                    "investigatorref",
                    "siteref",
                    "annotation",
                    "studyeventdata",
                    "formdata",
                }:
                    record_warning(f"Unknown child element '<{ctag}>' inside <SubjectData>.")

            sk = (_get_attr(subj_el, "SubjectKey") or "").strip()
            if not sk:
                record_warning("<SubjectData> is missing required SubjectKey attribute.")
                sk = "UNKNOWN"

            inv_el = _find_first_child(subj_el, "InvestigatorRef")
            inv_oid = (_get_attr(inv_el, "UserOID") or "").strip() if inv_el is not None else None
            if inv_oid and known_user_oids and inv_oid not in known_user_oids:
                record_warning(
                    f"SubjectData '{sk}' InvestigatorRef '{inv_oid}' not found in AdminData."
                )

            site_el = _find_first_child(subj_el, "SiteRef")
            site_oid = (
                (_get_attr(site_el, "LocationOID") or "").strip() if site_el is not None else None
            )
            if site_oid and known_loc_oids and site_oid not in known_loc_oids:
                record_warning(f"SubjectData '{sk}' SiteRef '{site_oid}' not found in AdminData.")

            s_ar_el = _find_first_child(subj_el, "AuditRecord")
            s_ar = (
                _parse_audit_record_elem(s_ar_el, record_warning) if s_ar_el is not None else None
            )
            s_anns = tuple(
                _parse_annotation_elem(ann_el, idx, known_codelists, record_warning)
                for idx, ann_el in enumerate(_find_children(subj_el, "Annotation"), 1)
            )

            study_events: list[OdmStudyEventData] = []
            for se_el in _find_children(subj_el, "StudyEventData"):
                for child in se_el:
                    ctag = _local_tag(child)
                    if ctag.lower() not in {"auditrecord", "annotation", "formdata"}:
                        record_warning(f"Unknown child element '<{ctag}>' inside <StudyEventData>.")
                se_oid = (_get_attr(se_el, "StudyEventOID") or "").strip()
                if not se_oid:
                    record_warning("<StudyEventData> is missing required StudyEventOID attribute.")
                elif known_se_oids and se_oid not in known_se_oids:
                    record_warning(
                        f"StudyEventData StudyEventOID '{se_oid}' was not found in MetaDataVersion."
                    )
                se_ar_el = _find_first_child(se_el, "AuditRecord")
                se_ar = (
                    _parse_audit_record_elem(se_ar_el, record_warning)
                    if se_ar_el is not None
                    else None
                )
                se_anns = tuple(
                    _parse_annotation_elem(ann_el, idx, known_codelists, record_warning)
                    for idx, ann_el in enumerate(_find_children(se_el, "Annotation"), 1)
                )
                se_forms = tuple(
                    _parse_form_data_elem(
                        fd_el,
                        known_form_oids=known_form_oids,
                        known_ig_oids=known_ig_oids,
                        known_item_oids=known_item_oids,
                        known_codelists=known_codelists,
                        known_user_oids=known_user_oids,
                        known_loc_oids=known_loc_oids,
                        record_warning=record_warning,
                    )
                    for fd_el in _find_children(se_el, "FormData")
                )
                study_events.append(
                    OdmStudyEventData(
                        studyEventOID=se_oid or "SE.UNSCHEDULED",
                        studyEventRepeatKey=_get_attr(se_el, "StudyEventRepeatKey"),
                        transactionType=_get_attr(se_el, "TransactionType"),
                        auditRecord=se_ar,
                        annotations=se_anns,
                        forms=se_forms,
                    )
                )

            direct_forms = tuple(
                _parse_form_data_elem(
                    fd_el,
                    known_form_oids=known_form_oids,
                    known_ig_oids=known_ig_oids,
                    known_item_oids=known_item_oids,
                    known_codelists=known_codelists,
                    known_user_oids=known_user_oids,
                    known_loc_oids=known_loc_oids,
                    record_warning=record_warning,
                )
                for fd_el in _find_children(subj_el, "FormData")
            )

            subjects.append(
                OdmSubjectData(
                    subjectKey=sk,
                    transactionType=_get_attr(subj_el, "TransactionType"),
                    investigatorUserOID=inv_oid or None,
                    siteLocationOID=site_oid or None,
                    auditRecord=s_ar,
                    annotations=s_anns,
                    studyEvents=tuple(study_events),
                    forms=direct_forms,
                )
            )

        global_audits: list[OdmAuditRecord] = []
        for ars_el in _find_children(clin_el, "AuditRecords"):
            for ar_el in _find_children(ars_el, "AuditRecord"):
                parsed_ar = _parse_audit_record_elem(ar_el, record_warning)
                if known_user_oids and parsed_ar.user_oid not in known_user_oids:
                    record_warning(
                        f"AuditRecord UserOID '{parsed_ar.user_oid}' not found in AdminData."
                    )
                if known_loc_oids and parsed_ar.location_oid not in known_loc_oids:
                    record_warning(
                        f"AuditRecord LocationOID '{parsed_ar.location_oid}' not found in AdminData."
                    )
                global_audits.append(parsed_ar)

        global_anns: list[OdmAnnotation] = []
        for anns_el in _find_children(clin_el, "Annotations"):
            for idx_a, ann_el in enumerate(_find_children(anns_el, "Annotation"), 1):
                global_anns.append(
                    _parse_annotation_elem(ann_el, idx_a, known_codelists, record_warning)
                )

        clin_containers.append(
            OdmClinicalData(
                studyOID=c_study,
                metaDataVersionOID=c_mdv,
                subjects=tuple(subjects),
                auditRecords=tuple(global_audits),
                annotations=tuple(global_anns),
            )
        )

    file_type_raw = (_get_attr(root, "FileType") or "Snapshot").strip()
    file_type_val: Literal["Snapshot", "Transactional"] = (
        "Transactional" if file_type_raw == "Transactional" else "Snapshot"
    )

    return OdmArchive(
        fileOID=_get_attr(root, "FileOID") or "IMEDNET_ODM_EXPORT",
        fileType=file_type_val,
        odmVersion=_get_attr(root, "ODMVersion") or "1.3.2",
        creationDateTime=_get_attr(root, "CreationDateTime"),
        asOfDateTime=_get_attr(root, "AsOfDateTime"),
        originator=_get_attr(root, "Originator"),
        sourceSystem=_get_attr(root, "SourceSystem"),
        sourceSystemVersion=_get_attr(root, "SourceSystemVersion"),
        description=_get_attr(root, "Description"),
        metaDataVersionOID=first_mdv_oid,
        metaDataVersionName=first_mdv_name,
        dataDictionary=dd,
        adminData=tuple(admin_containers),
        referenceData=tuple(ref_containers),
        clinicalData=tuple(clin_containers),
        validationWarnings=tuple(dict.fromkeys(warnings)),
    )


load_cdisc_odm_archive = load_odm_archive
