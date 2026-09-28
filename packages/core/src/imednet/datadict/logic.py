"""Business logic XML parsing for iMednet data dictionary."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from .normalize import to_camel_case


def _parse_xml_element(elem: ET.Element) -> Any:
    """Parse an XML Element into a structured Python dictionary/value.

    Rules:
    - Empty XML elements (<true/>, <new_record/>, <last/>) become boolean True.
    - Sibling tags with identical names collapse into arrays.
    - Tags are camelCased.
    - Text values are preserved.
    """
    text = (elem.text or "").strip()
    has_children = len(elem) > 0
    has_attrib = len(elem.attrib) > 0

    # 1. Empty element without children, text, or attributes becomes boolean True
    if not has_children and not text and not has_attrib:
        return True

    # 2. Leaf element with only text (no children, no attributes)
    if not has_children and not has_attrib:
        return text

    node_dict: dict[str, Any] = {}

    # Store XML attributes prefixed with @
    for attr_name, attr_val in elem.attrib.items():
        node_dict[f"@{attr_name}"] = attr_val

    # Group children by camelCased tag name
    children_by_tag: dict[str, list[Any]] = {}
    for child in elem:
        camel_tag = to_camel_case(child.tag)
        parsed_child = _parse_xml_element(child)
        if camel_tag not in children_by_tag:
            children_by_tag[camel_tag] = []
        children_by_tag[camel_tag].append(parsed_child)

    # Collapse lists: single elements are stored directly, multiple become lists
    for camel_tag, val_list in children_by_tag.items():
        if len(val_list) == 1:
            node_dict[camel_tag] = val_list[0]
        else:
            node_dict[camel_tag] = val_list

    # If element has both text and children/attributes
    if text:
        node_dict["#text"] = text

    return node_dict


def parse_logic_xml(xml_content: str | None) -> dict[str, Any] | None:
    """Parse BusinessLogic XML into a structured AST dictionary.

    Args:
        xml_content: Verbatim XML string, or None.

    Returns:
        Structured dictionary representation of the rule logic, or None if input is empty.

    Raises:
        ValueError: If XML parsing fails due to invalid syntax.
    """
    if not xml_content or not xml_content.strip():
        return None

    stripped = xml_content.strip()
    try:
        root = ET.fromstring(stripped)  # noqa: S314
    except ET.ParseError as exc:
        raise ValueError(f"Malformed business logic XML: {exc}") from exc

    parsed = _parse_xml_element(root)

    # If root is a dict and the tag is a generic rule container, unwrap its contents
    root_tag = root.tag.lower()
    if isinstance(parsed, dict) and root_tag in {
        "rule",
        "logic",
        "businesslogic",
        "business_logic",
        "root",
    }:
        return parsed

    # Otherwise return rooted under camelCased root tag
    return {to_camel_case(root.tag): parsed}
