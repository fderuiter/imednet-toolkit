"""Tests for business logic XML parsing."""

from __future__ import annotations

import pytest

from imednet.datadict.logic import parse_logic_xml


def test_parse_logic_xml_none_or_empty() -> None:
    """Test parsing empty or None inputs."""
    assert parse_logic_xml(None) is None
    assert parse_logic_xml("") is None
    assert parse_logic_xml("   ") is None


def test_parse_logic_xml_malformed() -> None:
    """Test handling of invalid XML syntax."""
    with pytest.raises(ValueError, match="Malformed business logic XML"):
        parse_logic_xml("<rule><unclosed></rule>")


def test_parse_logic_xml_recursive_entity_expansion() -> None:
    """Test that recursive entity expansion payloads fail safely with ValueError."""
    xml_str = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
     <!ENTITY lol "lol">
     <!ELEMENT lolz (#PCDATA)>
     <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
     <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
    ]>
    <rule>&lol2;</rule>
    """
    with pytest.raises(ValueError, match="Malformed business logic XML"):
        parse_logic_xml(xml_str)


def test_parse_logic_xml_external_entity_declaration() -> None:
    """Test that external entity declarations fail safely with ValueError."""
    xml_str = """<?xml version="1.0"?>
    <!DOCTYPE data [
      <!ENTITY ext SYSTEM "http://127.0.0.1/evil.xml">
    ]>
    <rule>&ext;</rule>
    """
    with pytest.raises(ValueError, match="Malformed business logic XML"):
        parse_logic_xml(xml_str)


def test_parse_logic_xml_empty_elements_as_true() -> None:
    """Test that empty XML elements (<true/>, <new_record/>, <last/>) become True."""
    xml_str = """
    <rule>
        <systemGenerated>false</systemGenerated>
        <conditions>
            <condition>
                <variable>AE_SEV</variable>
                <operator>&gt;=</operator>
                <value>3</value>
                <true/>
            </condition>
        </conditions>
        <actions>
            <action>
                <type>query</type>
                <message>Severe event</message>
                <new_record/>
                <last/>
            </action>
        </actions>
    </rule>
    """
    result = parse_logic_xml(xml_str)
    assert result is not None
    assert result["systemGenerated"] == "false"

    condition = result["conditions"]["condition"]
    assert condition["variable"] == "AE_SEV"
    assert condition["operator"] == ">="
    assert condition["value"] == "3"
    assert condition["true"] is True

    action = result["actions"]["action"]
    assert action["type"] == "query"
    assert action["message"] == "Severe event"
    assert action["newRecord"] is True
    assert action["last"] is True


def test_parse_logic_xml_sibling_arrays() -> None:
    """Test that repeated sibling XML tags collapse into arrays."""
    xml_str = """
    <rule>
        <conditions>
            <condition>
                <variable>VAR1</variable>
            </condition>
            <condition>
                <variable>VAR2</variable>
            </condition>
        </conditions>
        <actions>
            <action>
                <type>query</type>
            </action>
            <action>
                <type>email</type>
            </action>
            <action>
                <type>lock</type>
            </action>
        </actions>
    </rule>
    """
    result = parse_logic_xml(xml_str)
    assert result is not None
    conditions = result["conditions"]["condition"]
    assert isinstance(conditions, list)
    assert len(conditions) == 2
    assert conditions[0]["variable"] == "VAR1"
    assert conditions[1]["variable"] == "VAR2"

    actions = result["actions"]["action"]
    assert isinstance(actions, list)
    assert len(actions) == 3
    assert actions[0]["type"] == "query"
    assert actions[1]["type"] == "email"
    assert actions[2]["type"] == "lock"


def test_parse_logic_xml_non_rule_root_tag_preserved() -> None:
    """Test that non-generic root tags (e.g. editCheck, calculation) are preserved in AST."""
    xml_str = "<editCheck><message>Value out of range</message></editCheck>"
    result = parse_logic_xml(xml_str)
    assert result is not None
    assert "editCheck" in result
    assert result["editCheck"]["message"] == "Value out of range"
