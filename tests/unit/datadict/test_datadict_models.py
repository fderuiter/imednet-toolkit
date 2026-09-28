"""Tests for canonical data dictionary models."""

from __future__ import annotations

import pytest

from imednet.datadict.models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)


def test_choice_model_and_dict_access() -> None:
    """Test Choice model properties and dict-like compatibility."""
    choice = Choice(
        choiceText="Moderate",
        choiceValue="2",
        position=2,
        extraAttributes={"legacyCode": "MOD"},
    )
    assert choice.choiceText == "Moderate"
    assert choice.choiceValue == "2"
    assert choice.position == 2
    assert choice["Choice Text"] == "Moderate"
    assert choice["choiceValue"] == "2"
    assert choice["legacyCode"] == "MOD"
    assert choice.get("Position") == 2
    assert "choiceText" in choice
    assert "nonExistent" not in choice
    assert "choiceText" in choice.keys()
    assert ("choiceValue", "2") in choice.items()


def test_variable_model() -> None:
    """Test Variable model with choices and non-boolean required field."""
    choice = Choice(choiceText="Yes", choiceValue="1")
    var = Variable(
        variableName="AESEV",
        label="Severity",
        variableType="Radio",
        required="Auto Query",
        choices=[choice],
    )
    assert var.variableName == "AESEV"
    assert var.required == "Auto Query"
    assert len(var.choices) == 1
    assert var["Variable Name"] == "AESEV"
    assert var["Label"] == "Severity"


def test_form_model() -> None:
    """Test Form model containing variables and business logic."""
    var = Variable(variableName="V1")
    rule = BusinessLogicRule(id=101, name="Check V1", type="Form")
    form = Form(formKey="DM", formName="Demographics", variables=[var], businessLogic=[rule])

    assert form.formKey == "DM"
    assert form["Form Key"] == "DM"
    assert form["Form Name"] == "Demographics"
    assert len(form.variables) == 1
    assert len(form.businessLogic) == 1


def test_datadictionary_equality_ignores_generated_at() -> None:
    """Test that DataDictionary equality comparison excludes generatedAt timestamp."""
    meta1 = Metadata(studyKey="STUDY1", generatedAt="09-28-2026 12:00:00")
    meta2 = Metadata(studyKey="STUDY1", generatedAt="09-28-2026 18:00:00")

    dd1 = DataDictionary(metadata=meta1, forms=[Form(formKey="F1")])
    dd2 = DataDictionary(metadata=meta2, forms=[Form(formKey="F1")])

    assert dd1 == dd2


def test_datadictionary_legacy_init() -> None:
    """Test initializing DataDictionary via legacy raw keyword arguments."""
    dd = DataDictionary(
        forms=[{"Form Key": "F1", "Form Name": "Form 1"}],
        questions=[{"Form": "F1", "Variable Name": "V1", "Label": "Prompt 1"}],
        choices=[
            {"Form": "F1", "Variable Name": "V1", "Choice Text": "Opt A", "Choice Value": "A"}
        ],
        business_logic=[
            {"ID": "R1", "Name": "Rule 1", "Type": "Form", "Form": "F1"},
            {"ID": "R2", "Name": "Rule 2", "Type": "Workflow"},
        ],
    )

    assert len(dd.forms) == 1
    form = dd.forms[0]
    assert form.formKey == "F1"
    assert len(form.variables) == 1
    var = form.variables[0]
    assert var.variableName == "V1"
    assert len(var.choices) == 1
    assert var.choices[0].choiceText == "Opt A"
    assert len(form.businessLogic) == 1
    assert form.businessLogic[0].name == "Rule 1"
    assert len(dd.unassignedBusinessLogic) == 1
    assert dd.unassignedBusinessLogic[0].name == "Rule 2"

    # Properties
    assert len(dd.questions) == 1
    assert len(dd.choices) == 1
    assert len(dd.business_logic) == 2


def test_dict_compat_key_error() -> None:
    """Test KeyError on missing attribute."""
    choice = Choice(choiceText="A", choiceValue="1")
    with pytest.raises(KeyError):
        _ = choice["missing_key"]


def test_datadictionary_dict_compat_subscripting() -> None:
    """Test that DataDictionary supports dict-like subscripting and backward compatibility keys."""
    var = Variable(variableName="V1", choices=[Choice(choiceText="Yes", choiceValue="1")])
    rule = BusinessLogicRule(id=1, name="R1", type="Form")
    form = Form(formKey="F1", variables=[var], businessLogic=[rule])
    dd = DataDictionary(forms=[form])

    assert dd["forms"] == [form]
    assert "forms" in dd
    assert len(dd["questions"]) == 1
    assert "questions" in dd
    assert len(dd["choices"]) == 1
    assert "choices" in dd
    assert len(dd["business_logic"]) == 1
    assert "business_logic" in dd
    assert "metadata" in dd
    assert dd.get("forms") == [form]
    assert "questions" in dd.keys()


def test_metadata_equality_with_extra_fields() -> None:
    """Test that Metadata equality accounts for extra fields."""
    m1 = Metadata(studyKey="S", extraAttributes={"custom": "val1"})
    m2 = Metadata(studyKey="S", extraAttributes={"custom": "val2"})
    assert m1 != m2

    m3 = Metadata(studyKey="S", extraAttributes={"custom": "val1"})
    assert m1 == m3


def test_flexible_init_with_xml_and_variable_attributes() -> None:
    """Test flexible initialization extracts format, precision, and parses logic XML."""
    rule_xml = "<rule><conditions><condition><variable>V1</variable><true/></condition></conditions></rule>"
    dd = DataDictionary(
        forms=[{"Form Key": "F1", "Form Name": "Form 1"}],
        questions=[
            {
                "Form": "F1",
                "Variable Name": "V1",
                "Format": "YYYY-MM-DD",
                "Precision": "Day",
            }
        ],
        choices=[],
        business_logic=[
            {
                "ID": "R1",
                "Name": "Rule 1",
                "Type": "Form",
                "Form": "F1",
                "Logic": rule_xml,
            }
        ],
    )
    var = dd.forms[0].variables[0]
    assert var.format == "YYYY-MM-DD"
    assert var.precision == "Day"

    rule = dd.forms[0].businessLogic[0]
    assert rule.logic is not None
    assert rule.logic["conditions"]["condition"]["true"] is True
    assert rule.logicRawXml == rule_xml
