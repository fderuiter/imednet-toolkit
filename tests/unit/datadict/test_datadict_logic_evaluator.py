"""Unit tests for LogicTreeEvaluator and VariableConstraints."""

from __future__ import annotations

import pytest

from imednet.datadict import (
    BusinessLogicRule,
    Choice,
    LogicTreeEvaluator,
    Variable,
    extract_variable_constraints,
    parse_logic_xml,
)
from imednet.utils.evaluator import RestrictedAstEvaluator


def test_single_condition_blocking_form_rule_true_polarity() -> None:
    """Form rule with <true/> triggers a blocking error when condition matches."""
    xml_str = """
    <rule>
        <conditions>
            <condition>
                <variable>AGE</variable>
                <operator>&lt;</operator>
                <value>18</value>
                <true/>
            </condition>
        </conditions>
        <actions>
            <action>
                <type>error</type>
                <message>Subject must be an adult</message>
            </action>
        </actions>
    </rule>
    """
    rule = BusinessLogicRule(
        id="R1",
        name="Adult Age Check",
        type="Form",
        status="Active",
        form="DM",
        logic=parse_logic_xml(xml_str),
    )
    evaluator = LogicTreeEvaluator()

    failed = evaluator.evaluate_rule(rule, {"AGE": 16}, form_key="DM")
    assert failed.triggered is True
    assert failed.severity == "error"
    assert failed.skipped is False
    assert failed.message == "Subject must be an adult"

    passed = evaluator.evaluate_rule(rule, {"AGE": 21}, form_key="DM")
    assert passed.triggered is False
    assert passed.severity == "error"


def test_single_condition_form_rule_false_polarity() -> None:
    """Form rule with <false/> triggers when the underlying predicate is false."""
    xml_str = """
    <rule>
        <conditions>
            <condition>
                <variable>AGE</variable>
                <operator>&gt;=</operator>
                <value>18</value>
                <false/>
            </condition>
        </conditions>
        <actions>
            <action>
                <type>error</type>
                <message>Minimum age is 18</message>
            </action>
        </actions>
    </rule>
    """
    rule = BusinessLogicRule(
        id="R2",
        name="Min Age False Polarity",
        type="Form",
        status="Active",
        form="DM",
        logic=parse_logic_xml(xml_str),
    )
    evaluator = LogicTreeEvaluator()

    assert evaluator.evaluate_rule(rule, {"AGE": 15}, form_key="DM").triggered is True
    assert evaluator.evaluate_rule(rule, {"AGE": 18}, form_key="DM").triggered is False
    # Unpopulated optional field does not spuriously fire relational <false/> checks
    assert evaluator.evaluate_rule(rule, {}, form_key="DM").triggered is False


def test_multi_condition_and_or_not_trees() -> None:
    """Multi-condition XML trees evaluate AND, OR, and NOT combinators deterministically."""
    and_xml = """
    <rule>
        <conditions operator="AND">
            <condition>
                <variable>SEX</variable>
                <operator>==</operator>
                <value>M</value>
                <true/>
            </condition>
            <condition>
                <variable>PREG</variable>
                <operator>==</operator>
                <value>Yes</value>
                <true/>
            </condition>
        </conditions>
        <actions>
            <action>
                <type>error</type>
                <message>Male subject cannot be pregnant</message>
            </action>
        </actions>
    </rule>
    """
    or_xml = """
    <rule>
        <conditions operator="OR">
            <condition>
                <variable>SYS_BP</variable>
                <operator>&gt;</operator>
                <value>220</value>
                <true/>
            </condition>
            <condition>
                <variable>DIA_BP</variable>
                <operator>&lt;</operator>
                <value>40</value>
                <true/>
            </condition>
        </conditions>
        <actions>
            <action>
                <type>query</type>
                <message>Out-of-range blood pressure</message>
            </action>
        </actions>
    </rule>
    """
    not_xml = """
    <rule>
        <conditions>
            <not>
                <condition>
                    <variable>CONSENT</variable>
                    <operator>==</operator>
                    <value>Y</value>
                </condition>
            </not>
        </conditions>
    </rule>
    """
    evaluator = LogicTreeEvaluator()

    and_rule = BusinessLogicRule(
        name="Sex Pregnancy Check", type="Form", form="DM", logic=parse_logic_xml(and_xml)
    )
    assert evaluator.evaluate_rule(and_rule, {"SEX": "M", "PREG": "Yes"}, form_key="DM").triggered
    assert not evaluator.evaluate_rule(
        and_rule, {"SEX": "F", "PREG": "Yes"}, form_key="DM"
    ).triggered
    assert not evaluator.evaluate_rule(
        and_rule, {"SEX": "M", "PREG": "No"}, form_key="DM"
    ).triggered

    or_rule = BusinessLogicRule(
        name="BP Query", type="Query", form="VS", logic=parse_logic_xml(or_xml)
    )
    res_high = evaluator.evaluate_rule(or_rule, {"SYS_BP": 230, "DIA_BP": 80}, form_key="VS")
    assert res_high.triggered is True
    assert res_high.severity == "warning"

    res_low = evaluator.evaluate_rule(or_rule, {"SYS_BP": 120, "DIA_BP": 35}, form_key="VS")
    assert res_low.triggered is True
    assert res_low.severity == "warning"

    res_ok = evaluator.evaluate_rule(or_rule, {"SYS_BP": 120, "DIA_BP": 80}, form_key="VS")
    assert res_ok.triggered is False

    not_rule = BusinessLogicRule(
        name="Consent Check", type="Form", form="DM", logic=parse_logic_xml(not_xml)
    )
    assert evaluator.evaluate_rule(not_rule, {"CONSENT": "N"}, form_key="DM").triggered is True
    assert evaluator.evaluate_rule(not_rule, {"CONSENT": "Y"}, form_key="DM").triggered is False


def test_distinguishes_blocking_form_errors_from_query_warnings() -> None:
    """evaluate_rules separates blocking Form/Required errors from Query/Auto Query warnings."""
    form_rule = BusinessLogicRule(
        id=1,
        name="Hard Form Rule",
        type="Form",
        status="Active",
        form="VS",
        logic=parse_logic_xml(
            "<rule><conditions><condition><variable>HR</variable>"
            "<operator>&lt;=</operator><value>0</value></condition></conditions>"
            "<actions><action><type>error</type><message>HR must be positive</message></action></actions></rule>"
        ),
    )
    query_rule = BusinessLogicRule(
        id=2,
        name="Soft Query Rule",
        type="Query",
        status="Active",
        form="VS",
        logic=parse_logic_xml(
            "<rule><conditions><condition><variable>TEMP</variable>"
            "<operator>&gt;</operator><value>38.5</value></condition></conditions>"
            "<actions><action><type>query</type><message>Fever noted</message></action></actions></rule>"
        ),
    )
    auto_query_rule = BusinessLogicRule(
        id=3,
        name="Auto Query Rule",
        type="Auto Query",
        status="Active",
        form="VS",
        logicRawXml=(
            "<rule><conditions><condition><variable>RR</variable>"
            "<operator>&gt;</operator><value>25</value></condition></conditions>"
            "<actions><action><type>auto_query</type><message>Elevated RR</message></action></actions></rule>"
        ),
    )

    evaluator = LogicTreeEvaluator()
    warn_only_report = evaluator.evaluate_rules(
        [form_rule, query_rule, auto_query_rule],
        {"HR": 72, "TEMP": 39.1, "RR": 28},
        form_key="VS",
    )
    assert warn_only_report.has_errors is False
    assert len(warn_only_report.warnings) == 2
    assert {w.rule_name for w in warn_only_report.warnings} == {
        "Soft Query Rule",
        "Auto Query Rule",
    }

    err_report = evaluator.evaluate_rules(
        [form_rule, query_rule, auto_query_rule],
        {"HR": 0, "TEMP": 39.1, "RR": 16},
        form_key="VS",
    )
    assert err_report.has_errors is True
    assert len(err_report.errors) == 1
    assert err_report.errors[0].rule_name == "Hard Form Rule"
    assert len(err_report.warnings) == 1


def test_variable_comparison_emptiness_and_membership_operators() -> None:
    """Compare variables, emptiness tags, and set/string operators without eval."""
    evaluator = LogicTreeEvaluator()

    bp_rule = BusinessLogicRule(
        name="SYS <= DIA",
        type="Form",
        form="VS",
        logic=parse_logic_xml(
            "<rule><conditions><condition>"
            "<variable>VS.SYS</variable><operator>&lt;=</operator>"
            "<compareVariable>VS.DIA</compareVariable>"
            "</condition></conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(bp_rule, {"SYS": 80, "DIA": 90}, form_key="VS").triggered
    assert not evaluator.evaluate_rule(bp_rule, {"SYS": 120, "DIA": 80}, form_key="VS").triggered

    cond_req_rule = BusinessLogicRule(
        name="Specify Other Race",
        type="Form",
        form="DM",
        logic=parse_logic_xml(
            "<rule><conditions>"
            "<condition><variable>RACE</variable><operator>in</operator><value>OTHER,UNK</value></condition>"
            "<condition><variable>RACE_OTH</variable><empty/></condition>"
            "</conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(
        cond_req_rule, {"RACE": "OTHER", "RACE_OTH": ""}, form_key="DM"
    ).triggered
    assert not evaluator.evaluate_rule(
        cond_req_rule, {"RACE": "OTHER", "RACE_OTH": "Mixed"}, form_key="DM"
    ).triggered

    string_ops_rule = BusinessLogicRule(
        name="Subjid Prefix",
        type="Form",
        form="DM",
        logic=parse_logic_xml(
            "<rule><conditions>"
            "<condition><variable>SUBJID</variable><operator>starts_with</operator><value>BAD-</value></condition>"
            "<condition><variable>SYMPTOMS</variable><operator>contains</operator><value>NAUSEA</value></condition>"
            "<condition><variable>CODE</variable><operator>regex</operator><value>^ERR[0-9]+$</value></condition>"
            "</conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(
        string_ops_rule,
        {"SUBJID": "BAD-01", "SYMPTOMS": "HEADACHE`NAUSEA", "CODE": "ERR42"},
        form_key="DM",
    ).triggered


def test_skips_inactive_ui_only_and_cross_scope_rules() -> None:
    """Inactive, UI-only, cross-Form, and cross-Visit rules are skipped safely."""
    evaluator = LogicTreeEvaluator()

    inactive = BusinessLogicRule(
        name="Inactive Rule",
        type="Form",
        status="Inactive",
        form="DM",
        logic=parse_logic_xml(
            "<rule><conditions><condition><variable>AGE</variable><operator>&lt;</operator><value>18</value></condition></conditions></rule>"
        ),
    )
    ui_only = BusinessLogicRule(
        name="Disable AENUM",
        type="Form",
        status="Active",
        form="AE",
        logic=parse_logic_xml(
            "<rule><conditions><condition><variable>AETERM</variable><notEmpty/></condition></conditions>"
            "<actions><action><type>disable</type></action></actions></rule>"
        ),
    )
    cross_form = BusinessLogicRule(
        name="Cross Form Rule",
        type="Form",
        status="Active",
        form="AE",
        logic=parse_logic_xml(
            "<rule><conditions>"
            "<condition><variable>AE.AESTDTC</variable><operator>&lt;</operator><compareVariable>DM.RFICDTC</compareVariable></condition>"
            "</conditions></rule>"
        ),
    )
    cross_visit = BusinessLogicRule(
        name="Cross Visit Rule",
        type="Form",
        status="Active",
        form="VS",
        logic=parse_logic_xml(
            "<rule><conditions>"
            "<condition><variable>WEIGHT</variable><visit>Screening</visit><operator>&gt;</operator><value>100</value></condition>"
            "</conditions></rule>"
        ),
    )
    malformed = BusinessLogicRule(
        name="Broken XML",
        type="Form",
        status="Active",
        form="DM",
        logicRawXml="<rule><unclosed></rule>",
    )

    report = evaluator.evaluate_rules(
        [inactive, ui_only, cross_form, cross_visit, malformed],
        {"AGE": 10, "AETERM": "Headache", "AESTDTC": "2020-01-01", "WEIGHT": 150},
        form_key="AE",
    )
    assert report.has_errors is False
    assert report.warnings == []
    assert len(report.skipped) == 5


def test_restricted_ast_evaluator_preserved() -> None:
    """Ensure RestrictedAstEvaluator for string expressions remains intact."""
    ast_eval = RestrictedAstEvaluator("AGE >= 18 and SEX == 'F'")
    assert ast_eval.evaluate({"AGE": 25, "SEX": "F"}) is True
    assert ast_eval.evaluate({"AGE": 16, "SEX": "F"}) is False


def test_extract_variable_constraints() -> None:
    """Verify extraction of choices, required policy, numeric bounds, and text lengths."""
    var = Variable(
        variableName="WEIGHT",
        variableType="Float",
        required="Auto Query",
        choices=[Choice(choiceText="Normal", choiceValue="N")],
        extraAttributes={"minValue": "30.5", "maxValue": 250},
    )
    c = extract_variable_constraints(var)
    assert c.choices == ["N"]
    assert c.required_policy == "auto_query"
    assert c.min_value == pytest.approx(30.5)
    assert c.max_value == pytest.approx(250.0)

    txt_var = Variable(
        variableName="INITIALS",
        variableType="Text",
        required="Yes",
        format="3",
        extraAttributes={"minLength": "2"},
    )
    c_txt = extract_variable_constraints(txt_var)
    assert c_txt.required_policy == "yes"
    assert c_txt.min_length == 2
    assert c_txt.max_length == 3
    assert extract_variable_constraints(None).choices == []


def test_extract_rule_metadata_and_boolean_string_regex_operators() -> None:
    """Verify extract_rule_metadata, boolean/lexical/regex/prefix/suffix operators, and unassigned rules."""
    evaluator = LogicTreeEvaluator()

    # 1. extract_rule_metadata on multi-condition XML, inferred form, cross-form, and invalid XML
    rule_inferred = BusinessLogicRule(
        id="R-META-1",
        name="Inferred Form Rule",
        type="Form",
        status="Active",
        logicRawXml=(
            "<rule><conditions operator='OR'>"
            "<condition><variable>VS.HR</variable><operator>&gt;</operator><value>120</value></condition>"
            "<condition><variable>VS.HR</variable><operator>&lt;</operator><value>40</value></condition>"
            "</conditions><actions><action type='error'><message>Abnormal HR</message></action></actions></rule>"
        ),
    )
    meta = evaluator.extract_rule_metadata(rule_inferred)
    assert meta is not None
    assert meta.form == "VS"
    assert meta.logical_operator == "OR"
    assert meta.severity == "error"
    assert meta.message == "Abnormal HR"
    assert len(meta.conditions) == 2

    rule_cross = BusinessLogicRule(
        id="R-META-2",
        name="Cross Form Meta",
        type="Form",
        status="Active",
        logic=parse_logic_xml(
            "<rule><conditions>"
            "<condition><variable>VS.HR</variable><operator>&gt;</operator><value>100</value></condition>"
            "<condition><variable>DM.AGE</variable><operator>&lt;</operator><value>18</value></condition>"
            "</conditions></rule>"
        ),
    )
    meta_cross = evaluator.extract_rule_metadata(rule_cross)
    assert meta_cross is not None
    assert meta_cross.is_cross_scope is True

    assert (
        evaluator.extract_rule_metadata(
            BusinessLogicRule(name="Bad XML", logicRawXml="<rule><unclosed>")
        )
        is None
    )

    # 2. Boolean, starts_with, ends_with, regex, list membership, and lexical string comparisons
    ops_rules = [
        BusinessLogicRule(
            name="Bool Eq True",
            type="Form",
            status="Active",
            logic=parse_logic_xml(
                "<rule><conditions><condition><variable>CONSENT</variable>"
                "<operator>==</operator><value>true</value></condition></conditions></rule>"
            ),
        ),
        BusinessLogicRule(
            name="Bool Ne False",
            type="Form",
            status="Active",
            logic=parse_logic_xml(
                "<rule><conditions><condition><variable>CONSENT</variable>"
                "<operator>!=</operator><value>false</value></condition></conditions></rule>"
            ),
        ),
        BusinessLogicRule(
            name="Starts With",
            type="Form",
            status="Active",
            logic=parse_logic_xml(
                "<rule><conditions><condition><variable>CODE</variable>"
                "<operator>starts_with</operator><value>SUB-</value></condition></conditions></rule>"
            ),
        ),
        BusinessLogicRule(
            name="Ends With",
            type="Form",
            status="Active",
            logic=parse_logic_xml(
                "<rule><conditions><condition><variable>CODE</variable>"
                "<operator>ends_with</operator><value>-99</value></condition></conditions></rule>"
            ),
        ),
        BusinessLogicRule(
            name="Regex Match",
            type="Form",
            status="Active",
            logic=parse_logic_xml(
                "<rule><conditions><condition><variable>CODE</variable>"
                "<operator>regex</operator><value>^SUB-\\d+$</value></condition></conditions></rule>"
            ),
        ),
        BusinessLogicRule(
            name="Invalid Regex",
            type="Form",
            status="Active",
            logic=parse_logic_xml(
                "<rule><conditions><condition><variable>CODE</variable>"
                "<operator>regex</operator><value>[unclosed</value></condition></conditions></rule>"
            ),
        ),
        BusinessLogicRule(
            name="List In And Contains",
            type="Form",
            status="Active",
            logic=parse_logic_xml(
                "<rule><conditions>"
                "<condition><variable>TAGS</variable><operator>in</operator><value>A`B`C</value></condition>"
                "<condition><variable>TAGS</variable><operator>contains</operator><value>B</value></condition>"
                "</conditions></rule>"
            ),
        ),
        BusinessLogicRule(
            name="Lexical Date Range",
            type="Form",
            status="Active",
            logic=parse_logic_xml(
                "<rule><conditions>"
                "<condition><variable>DT</variable><operator>&gt;=</operator><value>2026-01-01</value></condition>"
                "<condition><variable>DT</variable><operator>&lt;=</operator><value>2026-12-31</value></condition>"
                "<condition><variable>DT</variable><operator>!=</operator><value>2026-06-01</value></condition>"
                "</conditions></rule>"
            ),
        ),
    ]
    report = evaluator.evaluate_rules(
        ops_rules,
        {"CONSENT": True, "CODE": "SUB-99", "TAGS": ["A", "B"], "DT": "2026-05-15"},
    )
    triggered_names = {r.rule_name for r in report.errors}
    assert triggered_names == {
        "Bool Eq True",
        "Bool Ne False",
        "Starts With",
        "Ends With",
        "Regex Match",
        "List In And Contains",
        "Lexical Date Range",
    }

    # 3. DataDictionary unassignedBusinessLogic scoping in rules_for_form and form_key_from_id edge cases
    from imednet.datadict import DataDictionary, Form

    dd = DataDictionary(
        forms=[Form(formKey="VS", formId="50", variables=[])],
        unassignedBusinessLogic=[
            rule_inferred,
            BusinessLogicRule(
                name="Explicit VS Rule",
                type="Query",
                status="Active",
                form="VS",
                logic=parse_logic_xml(
                    "<rule><if><empty><variable>HR</variable></empty></if>"
                    "<then><message>HR missing</message></then></rule>"
                ),
            ),
        ],
    )
    assert dd.form_key_from_id(50) == "VS"
    assert dd.form_key_from_id("50") == "VS"
    assert dd.form_key_from_id(True) is None
    assert dd.form_key_from_id("") is None
    assert dd.form_key_from_id(999) is None
    assert len(dd.rules_for_form("VS")) == 2
