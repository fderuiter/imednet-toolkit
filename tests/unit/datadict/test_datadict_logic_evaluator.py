"""Unit tests for LogicTreeEvaluator and VariableConstraints."""

from __future__ import annotations

import time

import pytest

from imednet.datadict import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    LogicTreeEvaluator,
    Variable,
    extract_variable_constraints,
    get_schema,
    parse_logic_xml,
    validate,
)
from imednet.datadict.logic import ExtractedCondition, _compile_safe_regex, validate_regex_pattern
from imednet.errors import ValidationError
from imednet.utils.evaluator import RestrictedAstEvaluator
from imednet.validation.cache import calculate_readiness_score, validate_record_data


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


def test_explicit_or_group_parsed_xml_full_truth_table() -> None:
    """Explicit <or> groups in parsed XML preserve OR semantics across all four truth cases."""
    evaluator = LogicTreeEvaluator()

    grouped_or_xml = """
    <rule>
        <conditions>
            <or>
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
            </or>
        </conditions>
        <actions>
            <action>
                <type>error</type>
                <message>Critical blood pressure</message>
            </action>
        </actions>
    </rule>
    """
    parsed_ast = parse_logic_xml(grouped_or_xml)
    assert parsed_ast is not None
    # Confirm _parse_xml_element represents <or> with two <condition> children as a dict
    assert isinstance(parsed_ast["conditions"]["or"], dict)

    grouped_or_rule = BusinessLogicRule(
        id="R-OR-GROUP",
        name="Grouped OR BP Check",
        type="Form",
        status="Active",
        form="VS",
        logic=parsed_ast,
    )

    # 1. First true, second false -> True
    assert (
        evaluator.evaluate_rule(
            grouped_or_rule, {"SYS_BP": 230, "DIA_BP": 80}, form_key="VS"
        ).triggered
        is True
    )
    # 2. First false, second true -> True
    assert (
        evaluator.evaluate_rule(
            grouped_or_rule, {"SYS_BP": 120, "DIA_BP": 35}, form_key="VS"
        ).triggered
        is True
    )
    # 3. Neither true -> False
    assert (
        evaluator.evaluate_rule(
            grouped_or_rule, {"SYS_BP": 120, "DIA_BP": 80}, form_key="VS"
        ).triggered
        is False
    )
    # 4. Both true -> True
    assert (
        evaluator.evaluate_rule(
            grouped_or_rule, {"SYS_BP": 230, "DIA_BP": 35}, form_key="VS"
        ).triggered
        is True
    )

    meta = evaluator.extract_rule_metadata(grouped_or_rule)
    assert meta is not None
    assert meta.logical_operator == "OR"
    assert len(meta.conditions) == 2

    # Direct <or> under <rule>, <if>/<or> operator tags, and <conditionGroup operator="OR">
    variant_xmls = [
        """
        <rule>
            <or>
                <condition><variable>A</variable><operator>==</operator><value>1</value></condition>
                <condition><variable>B</variable><operator>==</operator><value>2</value></condition>
            </or>
        </rule>
        """,
        """
        <rule>
            <if>
                <or>
                    <eq><variable>A</variable><value>1</value></eq>
                    <eq><variable>B</variable><value>2</value></eq>
                </or>
            </if>
            <then><action><type>error</type><message>Hit OR</message></action></then>
        </rule>
        """,
        """
        <rule>
            <if>
                <or>
                    <lt><variable>A</variable><value>2</value></lt>
                    <gt><variable>B</variable><value>1</value></gt>
                </or>
            </if>
        </rule>
        """,
        """
        <rule>
            <conditions>
                <conditionGroup operator="OR">
                    <condition><variable>A</variable><operator>==</operator><value>1</value></condition>
                    <condition><variable>B</variable><operator>==</operator><value>2</value></condition>
                </conditionGroup>
            </conditions>
        </rule>
        """,
    ]
    for idx, xml_text in enumerate(variant_xmls):
        rule = BusinessLogicRule(
            id=f"VAR-OR-{idx}",
            name=f"Variant OR {idx}",
            type="Form",
            status="Active",
            form="F1",
            logic=parse_logic_xml(xml_text),
        )
        assert evaluator.evaluate_rule(rule, {"A": 1, "B": 0}, form_key="F1").triggered is True
        assert evaluator.evaluate_rule(rule, {"A": 0, "B": 2}, form_key="F1").triggered is True
        assert evaluator.evaluate_rule(rule, {"A": 9, "B": 0}, form_key="F1").triggered is False
        assert evaluator.evaluate_rule(rule, {"A": 1, "B": 2}, form_key="F1").triggered is True
        vmeta = evaluator.extract_rule_metadata(rule)
        assert vmeta is not None
        assert vmeta.logical_operator == "OR"


def test_nested_and_or_not_explicit_group_combinations() -> None:
    """Nested explicit <or>, <and>, and <not> XML groups preserve boolean algebra."""
    evaluator = LogicTreeEvaluator()

    # 1. Conjunction with nested explicit OR: SEX == 'F' AND (PREG == 'Y' OR NURSING == 'Y')
    and_with_or = BusinessLogicRule(
        name="Female Preg Or Nursing",
        type="Form",
        status="Active",
        form="DM",
        logic=parse_logic_xml(
            "<rule><conditions>"
            "<condition><variable>SEX</variable><operator>==</operator><value>F</value></condition>"
            "<or>"
            "<condition><variable>PREG</variable><operator>==</operator><value>Y</value></condition>"
            "<condition><variable>NURSING</variable><operator>==</operator><value>Y</value></condition>"
            "</or>"
            "</conditions></rule>"
        ),
    )
    assert (
        evaluator.evaluate_rule(
            and_with_or, {"SEX": "F", "PREG": "Y", "NURSING": "N"}, form_key="DM"
        ).triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(
            and_with_or, {"SEX": "F", "PREG": "N", "NURSING": "Y"}, form_key="DM"
        ).triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(
            and_with_or, {"SEX": "F", "PREG": "Y", "NURSING": "Y"}, form_key="DM"
        ).triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(
            and_with_or, {"SEX": "F", "PREG": "N", "NURSING": "N"}, form_key="DM"
        ).triggered
        is False
    )
    assert (
        evaluator.evaluate_rule(
            and_with_or, {"SEX": "M", "PREG": "Y", "NURSING": "Y"}, form_key="DM"
        ).triggered
        is False
    )

    # 2. DNF: (A == 1 AND B == 1) OR (C == 1 AND D == 1)
    dnf_rule = BusinessLogicRule(
        name="DNF Rule",
        type="Form",
        status="Active",
        form="F1",
        logic=parse_logic_xml(
            "<rule><conditions><or>"
            "<and>"
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>B</variable><operator>==</operator><value>1</value></condition>"
            "</and>"
            "<and>"
            "<condition><variable>C</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>D</variable><operator>==</operator><value>1</value></condition>"
            "</and>"
            "</or></conditions></rule>"
        ),
    )
    assert (
        evaluator.evaluate_rule(dnf_rule, {"A": 1, "B": 1, "C": 0, "D": 0}, form_key="F1").triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(dnf_rule, {"A": 0, "B": 0, "C": 1, "D": 1}, form_key="F1").triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(dnf_rule, {"A": 1, "B": 0, "C": 1, "D": 0}, form_key="F1").triggered
        is False
    )

    # 3. CNF: (A == 1 OR B == 1) AND (C == 1 OR D == 1)
    cnf_rule = BusinessLogicRule(
        name="CNF Rule",
        type="Form",
        status="Active",
        form="F1",
        logic=parse_logic_xml(
            "<rule><conditions>"
            "<or>"
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>B</variable><operator>==</operator><value>1</value></condition>"
            "</or>"
            "<or>"
            "<condition><variable>C</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>D</variable><operator>==</operator><value>1</value></condition>"
            "</or>"
            "</conditions></rule>"
        ),
    )
    assert (
        evaluator.evaluate_rule(cnf_rule, {"A": 1, "B": 0, "C": 0, "D": 1}, form_key="F1").triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(cnf_rule, {"A": 1, "B": 1, "C": 0, "D": 0}, form_key="F1").triggered
        is False
    )
    assert (
        evaluator.evaluate_rule(cnf_rule, {"A": 0, "B": 0, "C": 1, "D": 1}, form_key="F1").triggered
        is False
    )

    # 4. Negated explicit OR group: NOT (A == 1 OR B == 2)
    not_or_rule = BusinessLogicRule(
        name="Not OR Rule",
        type="Form",
        status="Active",
        form="F1",
        logic=parse_logic_xml(
            "<rule><conditions><not><or>"
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>B</variable><operator>==</operator><value>2</value></condition>"
            "</or></not></conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(not_or_rule, {"A": 1, "B": 0}, form_key="F1").triggered is False
    assert evaluator.evaluate_rule(not_or_rule, {"A": 0, "B": 2}, form_key="F1").triggered is False
    assert evaluator.evaluate_rule(not_or_rule, {"A": 1, "B": 2}, form_key="F1").triggered is False
    assert evaluator.evaluate_rule(not_or_rule, {"A": 0, "B": 0}, form_key="F1").triggered is True

    # 5. Asymmetric operator-tag DNF/CNF groups, <and> wrapping <not>, and <or> wrapping <conditions>
    asym_dnf = BusinessLogicRule(
        name="Asymmetric Op-Tag DNF",
        type="Form",
        status="Active",
        form="F1",
        logic=parse_logic_xml(
            "<rule><if><or>"
            "<and><eq><variable>A</variable><value>1</value></eq><gt><variable>B</variable><value>2</value></gt></and>"
            "<and><eq><variable>C</variable><value>3</value></eq></and>"
            "</or></if></rule>"
        ),
    )
    assert (
        evaluator.evaluate_rule(asym_dnf, {"A": 1, "B": 3, "C": 0}, form_key="F1").triggered is True
    )
    assert (
        evaluator.evaluate_rule(asym_dnf, {"A": 0, "B": 0, "C": 3}, form_key="F1").triggered is True
    )
    assert (
        evaluator.evaluate_rule(asym_dnf, {"A": 1, "B": 0, "C": 0}, form_key="F1").triggered
        is False
    )
    assert (
        evaluator.evaluate_rule(asym_dnf, {"A": 1, "B": 3, "C": 3}, form_key="F1").triggered is True
    )

    asym_cnf = BusinessLogicRule(
        name="Asymmetric Op-Tag CNF",
        type="Form",
        status="Active",
        form="F1",
        logic=parse_logic_xml(
            "<rule><if>"
            "<or><eq><variable>A</variable><value>1</value></eq><eq><variable>B</variable><value>1</value></eq></or>"
            "<or><eq><variable>C</variable><value>1</value></eq></or>"
            "</if></rule>"
        ),
    )
    assert (
        evaluator.evaluate_rule(asym_cnf, {"A": 1, "B": 0, "C": 1}, form_key="F1").triggered is True
    )
    assert (
        evaluator.evaluate_rule(asym_cnf, {"A": 1, "B": 1, "C": 0}, form_key="F1").triggered
        is False
    )
    assert (
        evaluator.evaluate_rule(asym_cnf, {"A": 0, "B": 0, "C": 1}, form_key="F1").triggered
        is False
    )

    or_and_not = BusinessLogicRule(
        name="OR With AND Wrapping NOT",
        type="Form",
        status="Active",
        form="F1",
        logic=parse_logic_xml(
            "<rule><conditions><or>"
            "<and><not><condition><variable>A</variable><operator>==</operator><value>1</value></condition></not></and>"
            "<and><condition><variable>B</variable><operator>==</operator><value>2</value></condition></and>"
            "</or></conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(or_and_not, {"A": 0, "B": 0}, form_key="F1").triggered is True
    assert evaluator.evaluate_rule(or_and_not, {"A": 1, "B": 2}, form_key="F1").triggered is True
    assert evaluator.evaluate_rule(or_and_not, {"A": 1, "B": 0}, form_key="F1").triggered is False
    assert evaluator.evaluate_rule(or_and_not, {"A": 0, "B": 2}, form_key="F1").triggered is True

    or_wrapping_conds = BusinessLogicRule(
        name="OR Wrapping Conditions",
        type="Form",
        status="Active",
        form="F1",
        logic=parse_logic_xml(
            "<rule><or><conditions>"
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>B</variable><operator>==</operator><value>2</value></condition>"
            "</conditions></or></rule>"
        ),
    )
    assert (
        evaluator.evaluate_rule(or_wrapping_conds, {"A": 1, "B": 0}, form_key="F1").triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(or_wrapping_conds, {"A": 0, "B": 2}, form_key="F1").triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(or_wrapping_conds, {"A": 0, "B": 0}, form_key="F1").triggered
        is False
    )
    assert (
        evaluator.evaluate_rule(or_wrapping_conds, {"A": 1, "B": 2}, form_key="F1").triggered
        is True
    )
    or_wrap_meta = evaluator.extract_rule_metadata(or_wrapping_conds)
    assert or_wrap_meta is not None
    assert or_wrap_meta.logical_operator == "OR"

    # 6. <or> wrapping <conditionGroup>/<group>, <rule operator="OR">, direct <or> + <actions>,
    #    and symmetric single-child <and>/<eq> siblings inside <or>
    additional_or_xmls = [
        (
            "<rule><conditions><or><conditionGroup>"
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>B</variable><operator>==</operator><value>2</value></condition>"
            "</conditionGroup></or></conditions></rule>"
        ),
        (
            "<rule><or><group>"
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>B</variable><operator>==</operator><value>2</value></condition>"
            "</group></or>"
            "<actions><action><type>error</type><message>Hit OR</message></action></actions>"
            "</rule>"
        ),
        (
            '<rule operator="OR"><conditions>'
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>B</variable><operator>==</operator><value>2</value></condition>"
            "</conditions></rule>"
        ),
        (
            "<rule><conditions><group operator=\"OR\"><conditions>"
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>B</variable><operator>==</operator><value>2</value></condition>"
            "</conditions></group></conditions></rule>"
        ),
        (
            "<rule><if><or>"
            "<and><eq><variable>A</variable><value>1</value></eq></and>"
            "<and><eq><variable>B</variable><value>2</value></eq></and>"
            "</or></if></rule>"
        ),
    ]
    for idx, xml_str in enumerate(additional_or_xmls):
        extra_rule = BusinessLogicRule(
            id=f"EXTRA-OR-{idx}",
            name=f"Extra OR {idx}",
            type="Form",
            status="Active",
            form="F1",
            logic=parse_logic_xml(xml_str),
        )
        assert (
            evaluator.evaluate_rule(extra_rule, {"A": 1, "B": 0}, form_key="F1").triggered is True
        )
        assert (
            evaluator.evaluate_rule(extra_rule, {"A": 0, "B": 2}, form_key="F1").triggered is True
        )
        assert (
            evaluator.evaluate_rule(extra_rule, {"A": 0, "B": 0}, form_key="F1").triggered is False
        )
        assert (
            evaluator.evaluate_rule(extra_rule, {"A": 1, "B": 2}, form_key="F1").triggered is True
        )
        extra_meta = evaluator.extract_rule_metadata(extra_rule)
        assert extra_meta is not None
        assert extra_meta.logical_operator == "OR"

    # Mixed branch <or>: A == 1 OR (B == 2 AND C == 3)
    mixed_or_group = BusinessLogicRule(
        name="Mixed Leaf And Group In OR",
        type="Form",
        status="Active",
        form="F1",
        logic=parse_logic_xml(
            "<rule><conditions><or>"
            "<condition><variable>A</variable><operator>==</operator><value>1</value></condition>"
            "<group>"
            "<condition><variable>B</variable><operator>==</operator><value>2</value></condition>"
            "<condition><variable>C</variable><operator>==</operator><value>3</value></condition>"
            "</group>"
            "</or></conditions></rule>"
        ),
    )
    assert (
        evaluator.evaluate_rule(mixed_or_group, {"A": 1, "B": 0, "C": 0}, form_key="F1").triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(mixed_or_group, {"A": 0, "B": 2, "C": 3}, form_key="F1").triggered
        is True
    )
    assert (
        evaluator.evaluate_rule(mixed_or_group, {"A": 0, "B": 2, "C": 0}, form_key="F1").triggered
        is False
    )


def test_regex_redos_backtracking_guarded_in_validate_record_data() -> None:
    """Nested quantifier regex rules skip safely without stalling validate_record_data."""
    xml_rule = (
        "<rule><conditions><condition>"
        "<variable>CODE</variable><operator>regex</operator><value>^(a+)+$</value>"
        "</condition></conditions>"
        "<actions><action><type>error</type><message>Invalid code</message></action></actions></rule>"
    )
    dd = DataDictionary(
        forms=[
            Form(
                formKey="DEMO",
                variables=[Variable(variableName="CODE", variableType="Text")],
                businessLogic=[
                    BusinessLogicRule(
                        id="R1",
                        name="BacktrackRegexRule",
                        type="Form",
                        status="Active",
                        form="DEMO",
                        logic=parse_logic_xml(xml_rule),
                    )
                ],
            )
        ]
    )
    t0 = time.perf_counter()
    validate_record_data(dd, "DEMO", {"CODE": ("a" * 28) + "!"})
    assert (time.perf_counter() - t0) < 0.1

    t1 = time.perf_counter()
    score, reasons = calculate_readiness_score(dd, "DEMO", {"CODE": ("a" * 28) + "!"})
    assert (time.perf_counter() - t1) < 0.1
    assert score == pytest.approx(100.0)
    assert reasons == []


@pytest.mark.parametrize(
    ("pattern", "matching_value", "non_matching_value"),
    [
        (r"^SUB-\d+$", "SUB-1042", "SUB-XYZ"),
        (r"^ERR[0-9]+$", "ERR42", "OK42"),
        (r"^[A-Z]{2,4}-\d{3,6}$", "SITE-1234", "S-12"),
        (r"^(?:[A-Z]{2,4}-)?\d{3,6}$", "12345", "ABC-12"),
        (r"^\d+(?:\.\d{1,2})?$", "98.6", "98.678"),
        (r"^(YES|NO|UNK)$", "UNK", "MAYBE"),
        (r"^(?:SITE|SUBJ)+$", "SITESUBJ", "VISIT"),
        (r"^(JAN|JUN|JUL)+$", "JANJUL", "FEB"),
        (r"(?i)^[a-zA-Z0-9_-]+$", "Subj_01-A", "Subj 01!"),
        (r"^(?:éa|üa)+$", "éaüa", "öa"),
        (r"^(?:\dx|\sx)+$", "1x x", "ax"),
        (r"^(?:[à-ö]1|[ø-ÿ]2)+$", "é1ü2", "a1"),
        (r"^(?>[A-Z]+)-\d+$", "SUB-01", "SUB-XX"),
        (r"(?a)^(?:[^0-9]a|\da)+$", "xa1a", "xx"),
    ],
)
def test_safe_clinical_regex_patterns_preserved(
    pattern: str,
    matching_value: str,
    non_matching_value: str,
) -> None:
    """Safe flat clinical regex patterns compile via LRU cache and preserve matching behavior."""
    _compile_safe_regex.cache_clear()
    rule = BusinessLogicRule(
        id="R-SAFE",
        name="Safe Clinical Regex",
        type="Form",
        status="Active",
        form="DEMO",
        logic={
            "conditions": {
                "condition": {
                    "variable": "CODE",
                    "operator": "regex",
                    "value": pattern,
                }
            },
            "actions": {"action": {"type": "error", "message": "Matched code"}},
        },
    )
    evaluator = LogicTreeEvaluator()

    hit = evaluator.evaluate_rule(rule, {"CODE": matching_value}, form_key="DEMO")
    assert hit.skipped is False
    assert hit.triggered is True

    miss = evaluator.evaluate_rule(rule, {"CODE": non_matching_value}, form_key="DEMO")
    assert miss.skipped is False
    assert miss.triggered is False
    assert _compile_safe_regex.cache_info().hits >= 1


@pytest.mark.parametrize(
    "unsafe_pattern",
    [
        r"^(a+)+$",
        r"^(a*)*$",
        r"^(a?){25}a{25}$",
        r"^(a{1,3})+$",
        r"^(a++)+$",
        r"^([a-z]+)-\1$",
        r"^(?P<grp>\w+)-(?P=grp)$",
        r"^(a)(?(1)b|c)$",
        r"^(?=a)+b$",
        r"^(?!x)*y$",
        r"^(?:(?=a+)a)+$",
        r"^(?=(a+)+)a+$",
        r"^(a|aa)+$",
        r"^(a|ab)+$",
        r"^(a|a)+$",
        r"^([a-z]x|\wx)+$",
        r"^(.a|aa)+$",
        r"^(\d|[0-9])+$",
        r"^([a-m]|[j-z])+$",
        r"(?i)^(ab|AB)+$",
        r"(?i)^([à-ö]x|Éx)+$",
        r"^(\Dx|\Wx)+$",
        r"^(\Sx|ax)+$",
        r"^([^0-9]x|ax)+$",
        r"^(?:[^0-9]a|\da)+$",
        r"^(?>(a+))+$",
        r"^(CAT1|CAT12|DOG)+$",
        r"^(\b)+$",
        r"^a{1,5000}$",
        r"^a{300}$",
        "^" + ("a" * 600) + "$",
        "[unclosed",
    ],
)
def test_unsafe_and_invalid_regex_patterns_skipped_without_inversion(
    unsafe_pattern: str,
) -> None:
    """Unsafe and malformed regex patterns skip with unsafe_regex under true, false, and not."""
    evaluator = LogicTreeEvaluator()
    adversarial_input = {"CODE": ("a" * 30) + "!"}

    if unsafe_pattern != "[unclosed":
        with pytest.raises(ValueError):
            validate_regex_pattern(unsafe_pattern)

    pos_rule = BusinessLogicRule(
        id="R-POS",
        name="Positive Unsafe Regex",
        type="Form",
        status="Active",
        form="DEMO",
        logic={
            "conditions": {
                "condition": {
                    "variable": "CODE",
                    "operator": "regex",
                    "value": unsafe_pattern,
                }
            },
            "actions": {"action": {"type": "error", "message": "Failed"}},
        },
    )
    false_rule = BusinessLogicRule(
        id="R-FALSE",
        name="False Polarity Unsafe Regex",
        type="Form",
        status="Active",
        form="DEMO",
        logic={
            "conditions": {
                "condition": {
                    "variable": "CODE",
                    "operator": "regex",
                    "value": unsafe_pattern,
                    "false": True,
                }
            },
            "actions": {"action": {"type": "error", "message": "Failed false"}},
        },
    )
    not_rule = BusinessLogicRule(
        id="R-NOT",
        name="Not Wrapped Unsafe Regex",
        type="Form",
        status="Active",
        form="DEMO",
        logic={
            "conditions": {
                "not": {
                    "condition": {
                        "variable": "CODE",
                        "operator": "regex",
                        "value": unsafe_pattern,
                    }
                }
            },
            "actions": {"action": {"type": "error", "message": "Failed not"}},
        },
    )

    t0 = time.perf_counter()
    report = evaluator.evaluate_rules(
        [pos_rule, false_rule, not_rule],
        adversarial_input,
        form_key="DEMO",
    )
    assert (time.perf_counter() - t0) < 0.1
    assert report.has_errors is False
    assert report.warnings == []
    assert len(report.skipped) == 3
    assert all(res.skip_reason == "unsafe_regex" for res in report.skipped)
    assert all(res.triggered is False for res in report.skipped)


def test_regex_compare_variable_and_input_length_caps() -> None:
    """Dynamic compareVariable patterns and oversized actual inputs are guarded."""
    evaluator = LogicTreeEvaluator()
    compare_not_xml = (
        "<rule><conditions><not><condition>"
        "<variable>CODE</variable><operator>regex</operator>"
        "<compareVariable>PATTERN_VAR</compareVariable>"
        "</condition></not></conditions>"
        "<actions><action><type>error</type><message>Mismatch</message></action></actions></rule>"
    )
    compare_rule = BusinessLogicRule(
        id="R-CMP",
        name="CompareVar Regex Rule",
        type="Form",
        status="Active",
        form="DEMO",
        logic=parse_logic_xml(compare_not_xml),
    )

    # Safe dynamic regex evaluates normally under <not>
    ok_res = evaluator.evaluate_rule(
        compare_rule,
        {"CODE": "SUB-123", "PATTERN_VAR": r"^SUB-\d+$"},
        form_key="DEMO",
    )
    assert ok_res.skipped is False
    assert ok_res.triggered is False

    mismatch_res = evaluator.evaluate_rule(
        compare_rule,
        {"CODE": "BAD-123", "PATTERN_VAR": r"^SUB-\d+$"},
        form_key="DEMO",
    )
    assert mismatch_res.skipped is False
    assert mismatch_res.triggered is True

    # Unsafe dynamic regex in compareVariable skips with unsafe_regex instead of inverting <not>
    t0 = time.perf_counter()
    unsafe_cmp_res = evaluator.evaluate_rule(
        compare_rule,
        {"CODE": ("a" * 28) + "!", "PATTERN_VAR": r"^(a+)+$"},
        form_key="DEMO",
    )
    assert (time.perf_counter() - t0) < 0.1
    assert unsafe_cmp_res.skipped is True
    assert unsafe_cmp_res.skip_reason == "unsafe_regex"
    assert unsafe_cmp_res.triggered is False

    # Oversized actual value fails closed quickly without running unbounded re.search
    safe_false_rule = BusinessLogicRule(
        id="R-LEN",
        name="Safe Format Check",
        type="Form",
        status="Active",
        form="DEMO",
        logic=parse_logic_xml(
            "<rule><conditions><condition>"
            "<variable>CODE</variable><operator>regex</operator><value>^SUB-\\d+$</value><false/>"
            "</condition></conditions>"
            "<actions><action><type>error</type><message>Invalid format</message></action></actions></rule>"
        ),
    )
    dd = DataDictionary(
        forms=[
            Form(
                formKey="DEMO",
                variables=[Variable(variableName="CODE", variableType="Text")],
                businessLogic=[safe_false_rule],
            )
        ]
    )
    oversized_code = "SUB-" + ("1" * 5000)
    t1 = time.perf_counter()
    with pytest.raises(ValidationError, match="Invalid format"):
        validate_record_data(dd, "DEMO", {"CODE": oversized_code})
    assert (time.perf_counter() - t1) < 0.1

    # Direct leaf and _compare_values defense-in-depth checks
    direct_cond = ExtractedCondition(
        variable="CODE",
        operator="regex",
        value=r"^(a+)+$",
        expected_truth=False,
    )
    assert evaluator._eval_leaf(direct_cond, {"CODE": "aaa!"}, rule_type="Form") is False
    direct_cmp_cond = ExtractedCondition(
        variable="CODE",
        operator="regex",
        compare_variable="PAT",
        expected_truth=False,
    )
    assert (
        evaluator._eval_leaf(
            direct_cmp_cond,
            {"CODE": "aaa!", "PAT": r"^(a+)+$"},
            rule_type="Form",
        )
        is False
    )
    assert evaluator._compare_values("a" * 5000, "regex", r"^a+$") is False
    assert evaluator._compare_values(10**1100, "regex", r"^\d+$") is False
    assert evaluator._compare_values("abc", "regex", r"^(a+)+$") is False


def test_datadict_schema_validate_reports_unsafe_and_invalid_rule_regexes() -> None:
    """datadict.schema.validate reports errors for invalid or unsafe BusinessLogicRule regexes."""
    safe_dd = DataDictionary(
        forms=[
            Form(
                formKey="DEMO",
                variables=[Variable(variableName="CODE", variableType="Text")],
                businessLogic=[
                    BusinessLogicRule(
                        id="R-SAFE",
                        name="SafeSubjectCode",
                        type="Form",
                        status="Active",
                        form="DEMO",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>CODE</variable><operator>regex</operator><value>^SUB-\\d+$</value>"
                            "</condition></conditions></rule>"
                        ),
                    )
                ],
            )
        ]
    )
    assert validate(safe_dd) == []
    assert validate(safe_dd, schema=get_schema()) == []

    unsafe_dd = DataDictionary(
        forms=[
            Form(
                formKey="DEMO",
                variables=[Variable(variableName="CODE", variableType="Text")],
                businessLogic=[
                    BusinessLogicRule(
                        id="R1",
                        name="BacktrackRegexRule",
                        type="Form",
                        status="Active",
                        form="DEMO",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>CODE</variable><operator>regex</operator><value>^(a+)+$</value>"
                            "</condition></conditions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        id="R2",
                        name="MalformedRegexRule",
                        type="Form",
                        status="Active",
                        form="DEMO",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>CODE</variable><operator>regex</operator><value>[unclosed</value>"
                            "</condition></conditions></rule>"
                        ),
                    ),
                ],
            )
        ],
        unassignedBusinessLogic=[
            BusinessLogicRule(
                id="R3",
                name="BackrefUnassignedRule",
                type="Query",
                status="Active",
                logicRawXml=(
                    "<rule><conditions><condition>"
                    "<variable>DEMO.CODE</variable><visit>Screening</visit>"
                    "<operator>matches</operator><value>^([a-z]+)-\\1$</value>"
                    "</condition></conditions></rule>"
                ),
            )
        ],
    )

    errors = validate(unsafe_dd)
    assert len(errors) == 3
    assert any(
        "forms.0.businessLogic.0.logic" in err and "BacktrackRegexRule" in err for err in errors
    )
    assert any(
        "forms.0.businessLogic.1.logic" in err and "MalformedRegexRule" in err for err in errors
    )
    assert any(
        "unassignedBusinessLogic.0.logic" in err and "BackrefUnassignedRule" in err
        for err in errors
    )

    schema_errors = validate(unsafe_dd, schema=get_schema())
    assert len(schema_errors) == 3
