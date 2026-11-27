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


def test_metadata_create() -> None:
    """Test Metadata.create factory method sets defaults and timestamp."""
    meta = Metadata.create(
        study_key="STUDY_123",
        source="api",
        validation_warnings=["warning 1"],
        vendorExportVersion="2026.1",
    )
    assert meta.schemaVersion == "1.0.0"
    assert meta.studyKey == "STUDY_123"
    assert meta.source == "api"
    assert meta.validationWarnings == ["warning 1"]
    assert meta.generatedAt is not None
    assert meta.extraAttributes["vendorExportVersion"] == "2026.1"


def test_datadict_models_forbid_undeclared_keys_and_preserve_extra_attributes() -> None:
    """All canonical datadict models reject undeclared keys while preserving extraAttributes."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Metadata(studyKey="S1", misspelledStudy="bad")  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Choice(choiceValue="1", choiceText="Yes", order=1)  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Variable(variableName="V1", varType="Text")  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BusinessLogicRule(id="R1", targetVariables=["V1"])  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Form(formKey="DM", sequence=1)  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DataDictionary.model_validate({"forms": [], "unexpectedRootKey": True})

    # Legacy flexible kwargs preserve custom study metadata in metadata.extraAttributes
    legacy_dd = DataDictionary(
        forms=[{"Form Key": "F1", "Form Name": "Form 1"}],
        questions=[],
        customVendorTag="preserved",
    )
    assert legacy_dd.metadata.extraAttributes["customVendorTag"] == "preserved"


def test_datadictionary_legacy_init_preserves_integer_zero_values() -> None:
    """DataDictionary initialized with integer 0 retains choiceValue='0', formId=0, sequence=0, position=0, id=0."""
    dd = DataDictionary(
        forms=[{"Form Key": "F0", "Form Name": "Zero Form", "Form ID": 0}],
        questions=[
            {
                "Form": "F0",
                "Variable Name": "V0",
                "Label": "Zero Coded Question",
                "Variable Type": "Radio",
                "Sequence": 0,
            }
        ],
        choices=[
            {
                "Form": "F0",
                "Variable Name": "V0",
                "Choice Value": 0,
                "Choice Text": "Absent",
                "Position": 0,
            },
            {
                "Form": "F0",
                "Variable Name": "V0",
                "Choice Value": 1,
                "Choice Text": "Present",
                "Position": 1,
            },
        ],
        business_logic=[
            {
                "ID": 0,
                "Name": "Zero Rule",
                "Type": "Form",
                "Form": "F0",
                "Sequence": 0,
            }
        ],
    )

    form = dd.forms[0]
    assert form.formKey == "F0"
    assert form.formId == 0
    assert dd.form_key_from_id(0) == "F0"

    var = form.variables[0]
    assert var.variableName == "V0"
    assert var.sequence == 0

    zero_choice = var.choices[0]
    assert zero_choice.choiceValue == "0"
    assert zero_choice.choiceText == "Absent"
    assert zero_choice.position == 0

    zero_rule = form.businessLogic[0]
    assert zero_rule.id == 0
    assert zero_rule.sequence == 0
