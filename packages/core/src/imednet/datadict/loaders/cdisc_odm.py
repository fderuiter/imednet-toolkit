"""CDISC ODM 1.3 XML data dictionary loader for iMednet."""

from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO, TextIO

import defusedxml.ElementTree as ET  # noqa: N817
from defusedxml.common import DefusedXmlException

from ..models import (
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)
from ..normalize import _is_file_target, _safe_is_file, to_camel_case

# Standard ODM attributes by element type
KNOWN_FORM_ATTRIBUTES = {
    "oid",
    "name",
    "repeating",
    "comment",
    "formtype",
}

KNOWN_ITEM_GROUP_ATTRIBUTES = {
    "oid",
    "name",
    "repeating",
    "comment",
    "isreferencedata",
    "sasdatasetname",
    "domain",
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

KNOWN_ITEM_REF_ATTRIBUTES = {
    "itemoid",
    "ordernumber",
    "mandatory",
    "keysequence",
    "methodoid",
    "role",
}

KNOWN_ITEM_GROUP_REF_ATTRIBUTES = {
    "itemgroupoid",
    "ordernumber",
    "mandatory",
}

KNOWN_CODELIST_ATTRIBUTES = {
    "oid",
    "name",
    "datatype",
    "sasformatname",
}

KNOWN_CODELIST_ITEM_ATTRIBUTES = {
    "codedvalue",
    "ordernumber",
    "rank",
}

SUPPORTED_DATA_TYPES = {
    "text",
    "string",
    "integer",
    "int",
    "float",
    "double",
    "date",
    "datetime",
    "time",
    "boolean",
    "bool",
}


def _local_tag(elem: ET.Element) -> str:
    """Return XML tag without namespace prefix."""
    tag = str(elem.tag)
    if "}" in tag:
        return str(tag.split("}", 1)[1])
    return tag


def _get_attr(elem: ET.Element, *names: str) -> str | None:
    """Case-insensitively retrieve an attribute from an Element ignoring namespaces."""
    names_lower = {n.lower() for n in names}
    for k, v in elem.attrib.items():
        k_str = str(k)
        attr_local = k_str.split("}", 1)[1] if "}" in k_str else k_str
        if attr_local.lower() in names_lower:
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
) -> dict[str, Any]:
    """Extract attributes not in the known set and record warnings."""
    extra: dict[str, Any] = {}
    for k, v in elem.attrib.items():
        local_name = k.split("}", 1)[1] if "}" in k else k
        # Ignore XML namespace declarations and xml:* builtins
        if local_name.lower() in ("xmlns", "lang") or k.startswith(
            ("{http://www.w3.org/", "xmlns")
        ):
            continue
        if local_name.lower() not in known:
            camel = to_camel_case(local_name)
            extra[camel] = v
            record_warning(
                f"Unmapped attribute '{local_name}' on {elem_type} '{_get_attr(elem, 'OID') or 'unknown'}'"
            )
    return extra


def _read_source_content(source: Path | str | TextIO | BinaryIO) -> bytes:
    """Read XML content from path, string, or stream into bytes."""
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


def _extract_item_sdtm_attributes(it_def: ET.Element) -> dict[str, Any]:
    """Extract SDSVarName, SASFieldName, Origin, and Comment from ItemDef element."""
    sdtm: dict[str, Any] = {}
    if (sds_var := _get_attr(it_def, "SDSVarName")) and sds_var.strip():
        sdtm["SDSVarName"] = sds_var.strip()
    if (sas_field := _get_attr(it_def, "SASFieldName")) and sas_field.strip():
        sdtm["SASFieldName"] = sas_field.strip()
    if (origin := _get_attr(it_def, "Origin")) and origin.strip():
        sdtm["Origin"] = origin.strip()
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
) -> str:
    """Map CDISC ODM DataType to canonical iMednet variableType."""
    if control_type:
        ct_lower = control_type.lower()
        if ct_lower in ("radio", "radiobutton"):
            return "Radio"
        if ct_lower in ("dropdown", "dropdownlist", "select", "singleselect"):
            return "Single Select"
        if ct_lower in ("checkbox", "multiselect"):
            return "Multi Select"
        if ct_lower in ("datetime", "datetime_precision"):
            return "DateTime"
        if ct_lower == "text":
            return "Text"

    if has_choices:
        return "Radio"

    if not raw_datatype:
        return "Text"

    dt_lower = raw_datatype.lower()
    if dt_lower in ("text", "string"):
        return "Text"
    if dt_lower in ("integer", "int"):
        return "Integer"
    if dt_lower in ("float", "double", "decimal"):
        return "Float"
    if dt_lower == "date":
        return "Date"
    if dt_lower in ("datetime", "durationdatetime", "intervaldatetime"):
        return "DateTime"
    if dt_lower == "time":
        return "Time"
    if dt_lower in ("boolean", "bool"):
        return "Boolean"

    # Unsupported data type
    record_warning(
        f"Unsupported data type '{raw_datatype}' on item '{item_oid}'; defaulted to 'Text'."
    )
    return "Text"


def load_from_cdisc_odm(
    source: Path | str | TextIO | BinaryIO,
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
        no_raw_xml: Whether to omit raw XML logic strings (reserved for future logic parsing).

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

    # Verify root tag
    if _local_tag(root).lower() != "odm":
        record_warning(f"Root XML tag is '{_local_tag(root)}', expected 'ODM'.")

    # 2. Extract Study metadata
    study_elem = _find_first_child(root, "Study")
    study_oid: str | None = None
    study_name: str | None = None
    study_description: str | None = None

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
    else:
        record_warning("No 'Study' element found in ODM document.")

    effective_study_key = study_key or study_oid

    # 3. Locate MetaDataVersion element(s)
    mdv_elements: list[ET.Element] = []
    if study_elem is not None:
        mdv_elements = _find_children(study_elem, "MetaDataVersion")
    if not mdv_elements:
        mdv_elements = _find_descendants(root, "MetaDataVersion")

    if not mdv_elements:
        record_warning("No 'MetaDataVersion' element found in ODM document.")
        metadata = Metadata.create(
            study_key=effective_study_key,
            source="cdisc_odm",
            validation_warnings=warnings,
        )
        return DataDictionary(metadata=metadata, forms=[])

    # Index definitions across MetaDataVersion elements
    codelist_elems: dict[str, ET.Element] = {}
    item_def_elems: dict[str, ET.Element] = {}
    item_group_def_elems: dict[str, ET.Element] = {}
    form_def_elems: list[ET.Element] = []

    for mdv in mdv_elements:
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

    # 4. Parse CodeLists into Choice collections
    parsed_codelists: dict[str, list[Choice]] = {}
    for cl_oid, cl_elem in codelist_elems.items():
        choices: list[Choice] = []
        _extract_unmapped_attributes(cl_elem, KNOWN_CODELIST_ATTRIBUTES, "CodeList", record_warning)

        # Children can be CodeListItem or EnumeratedItem
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

    # 5. Parse Forms and nested ItemGroups/Items
    forms: list[Form] = []

    for form_elem in form_def_elems:
        f_oid = _get_attr(form_elem, "OID") or ""
        f_name = _get_attr(form_elem, "Name") or f_oid
        f_type = _get_attr(form_elem, "FormType") or "Patient"
        f_extra = _extract_unmapped_attributes(
            form_elem,
            KNOWN_FORM_ATTRIBUTES,
            "FormDef",
            record_warning,
        )

        variables: list[Variable] = []
        var_seq = 1

        # Process ItemGroupRef elements
        ig_refs = _find_children(form_elem, "ItemGroupRef")
        ig_refs.sort(key=lambda r: _parse_order(r, 0))

        for ig_ref in ig_refs:
            ig_oid = _get_attr(ig_ref, "ItemGroupOID") or ""
            _extract_unmapped_attributes(
                ig_ref,
                KNOWN_ITEM_GROUP_REF_ATTRIBUTES,
                "ItemGroupRef",
                record_warning,
            )

            ig_def = item_group_def_elems.get(ig_oid)
            if ig_def is None:
                record_warning(
                    f"ItemGroupRef '{ig_oid}' referenced in form '{f_oid}' was not found in MetaDataVersion."
                )
                continue

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
            if ig_domain and ig_domain.strip() and "Domain" not in f_extra:
                f_extra["Domain"] = ig_domain.strip()
            if ig_sas_dataset and ig_sas_dataset.strip() and "SASDatasetName" not in f_extra:
                f_extra["SASDatasetName"] = ig_sas_dataset.strip()

            # Process ItemRef elements inside ItemGroupDef
            item_refs = _find_children(ig_def, "ItemRef")
            item_refs.sort(key=lambda r: _parse_order(r, 0))

            for item_ref in item_refs:
                it_oid = _get_attr(item_ref, "ItemOID") or ""
                ref_extra = _extract_unmapped_attributes(
                    item_ref,
                    KNOWN_ITEM_REF_ATTRIBUTES,
                    "ItemRef",
                    record_warning,
                )

                it_def = item_def_elems.get(it_oid)
                if it_def is None:
                    record_warning(
                        f"ItemRef '{it_oid}' in ItemGroup '{ig_oid}' was not found in MetaDataVersion."
                    )
                    continue

                it_extra = _extract_unmapped_attributes(
                    it_def,
                    KNOWN_ITEM_ATTRIBUTES,
                    "ItemDef",
                    record_warning,
                )
                for k, v in ref_extra.items():
                    it_extra.setdefault(k, v)

                if ig_domain and ig_domain.strip():
                    it_extra["Domain"] = ig_domain.strip()
                if ig_sas_dataset and ig_sas_dataset.strip():
                    it_extra["SASDatasetName"] = ig_sas_dataset.strip()

                it_extra.update(_extract_item_sdtm_attributes(it_def))

                # Variable Name & Label
                var_name = _get_attr(it_def, "OID") or _get_attr(it_def, "Name") or it_oid
                q_elem = _find_first_child(it_def, "Question")
                desc_elem = _find_first_child(it_def, "Description")
                var_label = (
                    _get_translated_text(q_elem)
                    or _get_translated_text(desc_elem)
                    or _get_attr(it_def, "Name")
                )

                # Sequence
                seq = _parse_order(item_ref, var_seq)
                var_seq += 1

                # Mandatory / Required
                mand_val = _get_attr(item_ref, "Mandatory")
                req: str | None = None
                if mand_val:
                    req = "Yes" if mand_val.lower() in ("yes", "y", "true", "1") else "No"

                # DataType & CodeList
                raw_datatype = _get_attr(it_def, "DataType")
                control_type = _get_attr(it_def, "ControlType") or _get_attr(
                    item_ref, "ControlType"
                )

                cl_ref = _find_first_child(it_def, "CodeListRef")
                var_choices: list[Choice] = []
                if cl_ref is not None:
                    cl_oid = _get_attr(cl_ref, "CodeListOID") or ""
                    if cl_oid in parsed_codelists:
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
                )

                length_val = _get_attr(it_def, "Length")
                sig_digits = _get_attr(it_def, "SignificantDigits")

                variable = Variable(
                    variableName=var_name,
                    label=var_label,
                    variableType=var_type,
                    sequence=seq,
                    required=req,
                    format=length_val,
                    precision=sig_digits,
                    choices=var_choices,
                    extraAttributes=it_extra,
                )
                variables.append(variable)

        # Also support direct ItemRef children on FormDef (if any)
        direct_item_refs = _find_children(form_elem, "ItemRef")
        if direct_item_refs:
            direct_item_refs.sort(key=lambda r: _parse_order(r, 0))
            for item_ref in direct_item_refs:
                it_oid = _get_attr(item_ref, "ItemOID") or ""
                ref_extra = _extract_unmapped_attributes(
                    item_ref,
                    KNOWN_ITEM_REF_ATTRIBUTES,
                    "ItemRef",
                    record_warning,
                )

                it_def = item_def_elems.get(it_oid)
                if it_def is None:
                    record_warning(f"ItemRef '{it_oid}' on Form '{f_oid}' not found.")
                    continue

                it_extra = _extract_unmapped_attributes(
                    it_def,
                    KNOWN_ITEM_ATTRIBUTES,
                    "ItemDef",
                    record_warning,
                )
                for k, v in ref_extra.items():
                    it_extra.setdefault(k, v)

                if form_domain := f_extra.get("Domain"):
                    it_extra.setdefault("Domain", form_domain)
                if form_sas := f_extra.get("SASDatasetName"):
                    it_extra.setdefault("SASDatasetName", form_sas)

                it_extra.update(_extract_item_sdtm_attributes(it_def))

                var_name = _get_attr(it_def, "OID") or _get_attr(it_def, "Name") or it_oid
                q_elem = _find_first_child(it_def, "Question")
                desc_elem = _find_first_child(it_def, "Description")
                var_label = (
                    _get_translated_text(q_elem)
                    or _get_translated_text(desc_elem)
                    or _get_attr(it_def, "Name")
                )
                mand_val = _get_attr(item_ref, "Mandatory")
                req = (
                    "Yes"
                    if mand_val and mand_val.lower() in ("yes", "y", "true", "1")
                    else ("No" if mand_val else None)
                )

                cl_ref = _find_first_child(it_def, "CodeListRef")
                var_choices = []
                if cl_ref is not None:
                    cl_oid = _get_attr(cl_ref, "CodeListOID") or ""
                    if cl_oid in parsed_codelists:
                        var_choices = [c.model_copy() for c in parsed_codelists[cl_oid]]

                control_type = _get_attr(it_def, "ControlType") or _get_attr(
                    item_ref, "ControlType"
                )
                var_type = _map_datatype(
                    _get_attr(it_def, "DataType"),
                    it_oid,
                    has_choices=bool(var_choices),
                    control_type=control_type,
                    record_warning=record_warning,
                )

                variables.append(
                    Variable(
                        variableName=var_name,
                        label=var_label,
                        variableType=var_type,
                        sequence=_parse_order(item_ref, var_seq),
                        required=req,
                        format=_get_attr(it_def, "Length"),
                        precision=_get_attr(it_def, "SignificantDigits"),
                        choices=var_choices,
                        extraAttributes=it_extra,
                    )
                )
                var_seq += 1

        form = Form(
            formKey=f_oid,
            formName=f_name,
            formId=f_oid,
            formType=f_type,
            variables=variables,
            extraAttributes=f_extra,
        )
        forms.append(form)

    # 6. Build Metadata envelope
    extra_meta: dict[str, Any] = {}
    if study_name:
        extra_meta["studyName"] = study_name
    if study_description:
        extra_meta["studyDescription"] = study_description

    metadata = Metadata.create(
        study_key=effective_study_key,
        source="cdisc_odm",
        validation_warnings=warnings,
        **extra_meta,
    )

    return DataDictionary(
        metadata=metadata,
        forms=forms,
    )
