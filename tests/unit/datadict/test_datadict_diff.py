"""Tests for structural data dictionary diffing."""

from __future__ import annotations

import json

from imednet.datadict.diff import diff
from imednet.datadict.models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)


def test_diff_identical_dictionaries() -> None:
    """Test diffing two identical data dictionaries."""
    dd1 = DataDictionary(
        metadata=Metadata(studyKey="S1", generatedAt="09-28-2026 10:00:00"),
        forms=[Form(formKey="F1", variables=[Variable(variableName="V1")])],
    )
    dd2 = DataDictionary(
        metadata=Metadata(studyKey="S1", generatedAt="09-28-2026 12:00:00"),
        forms=[Form(formKey="F1", variables=[Variable(variableName="V1")])],
    )
    report = diff(dd1, dd2)
    assert not report.has_changes
    assert "No differences found" in report.to_text()
    assert "No differences found" in report.to_markdown()


def test_diff_structural_changes() -> None:
    """Test detecting additions, removals, and modifications."""
    old_dd = DataDictionary(
        metadata=Metadata(studyKey="OLD_STUDY"),
        forms=[
            Form(
                formKey="F1",
                formName="Form 1",
                variables=[
                    Variable(
                        variableName="V1",
                        label="Old Label",
                        choices=[Choice(choiceText="Old", choiceValue="1")],
                    ),
                    Variable(variableName="V_REMOVED"),
                ],
                businessLogic=[BusinessLogicRule(id=1, name="Rule Old")],
            ),
            Form(formKey="F_REMOVED"),
        ],
    )

    new_dd = DataDictionary(
        metadata=Metadata(studyKey="NEW_STUDY"),
        forms=[
            Form(
                formKey="F1",
                formName="Form 1 Renamed",
                variables=[
                    Variable(
                        variableName="V1",
                        label="New Label",
                        choices=[Choice(choiceText="New", choiceValue="1")],
                    ),
                    Variable(variableName="V_ADDED"),
                ],
                businessLogic=[BusinessLogicRule(id=1, name="Rule Renamed")],
            ),
            Form(formKey="F_ADDED"),
        ],
        unassignedBusinessLogic=[BusinessLogicRule(id=99, name="Workflow Rule")],
    )

    report = diff(old_dd, new_dd)
    assert report.has_changes

    # Test report export formats
    text = report.to_text()
    assert "+ [Added]" in text
    assert "- [Removed]" in text
    assert "~ [Modified]" in text

    md = report.to_markdown()
    assert "### Data Dictionary Diff" in md
    assert "🟢 Added" in md
    assert "🔴 Removed" in md
    assert "🟡 Modified" in md

    json_str = report.to_json()
    parsed = json.loads(json_str)
    assert parsed["has_changes"] is True
    assert parsed["total_changes"] > 0


def test_diff_extra_attributes() -> None:
    """Test detecting additions, removals, and modifications in extraAttributes."""
    dd1 = DataDictionary(
        metadata=Metadata(extraAttributes={"versionTag": "v1", "dropped": "yes"}),
        forms=[
            Form(
                formKey="F1",
                extraAttributes={"formFlag": "alpha"},
                variables=[Variable(variableName="V1", extraAttributes={"custom": "old"})],
            )
        ],
    )
    dd2 = DataDictionary(
        metadata=Metadata(extraAttributes={"versionTag": "v2", "added": "yes"}),
        forms=[
            Form(
                formKey="F1",
                extraAttributes={"formFlag": "beta"},
                variables=[Variable(variableName="V1", extraAttributes={"custom": "new"})],
            )
        ],
    )
    report = diff(dd1, dd2)
    paths = {item.path for item in report.items}
    assert "metadata.versionTag" in paths
    assert "metadata.dropped" in paths
    assert "metadata.added" in paths
    assert "forms[F1].formFlag" in paths
    assert "forms[F1].variables[V1].custom" in paths


def test_diff_markdown_multiline_logic_does_not_break_table() -> None:
    """Test that multiline logic values are formatted on a single line in Markdown tables."""
    multiline_xml = "<rule>\n  <condition>true</condition>\n</rule>"
    dd1 = DataDictionary(
        forms=[
            Form(
                formKey="F1",
                businessLogic=[BusinessLogicRule(id=1, logicRawXml=multiline_xml)],
            )
        ]
    )
    dd2 = DataDictionary(
        forms=[
            Form(
                formKey="F1",
                businessLogic=[BusinessLogicRule(id=1, logicRawXml="<rule><false/></rule>")],
            )
        ]
    )
    report = diff(dd1, dd2)
    md = report.to_markdown()
    # Ensure no table row has unescaped line breaks splitting a row
    table_rows = [line for line in md.splitlines() if line.startswith("| 🟡 Modified")]
    assert len(table_rows) == 1
    assert "\n" not in table_rows[0]


def test_diff_rules_without_id_or_name() -> None:
    """Test that rules without explicit ID or name do not collide in diff."""
    dd1 = DataDictionary(
        unassignedBusinessLogic=[
            BusinessLogicRule(sequence=1, status="Active"),
            BusinessLogicRule(sequence=2, status="Inactive"),
        ]
    )
    dd2 = DataDictionary(
        unassignedBusinessLogic=[
            BusinessLogicRule(sequence=1, status="Inactive"),
            BusinessLogicRule(sequence=2, status="Active"),
        ]
    )
    report = diff(dd1, dd2)
    assert report.has_changes
    assert len(report.items) == 2


def test_diff_cross_loader_normalization_zero_false_modifications() -> None:
    """Comparing equivalent Study dictionaries across CSV, ODM, REDCap, and SDK yields zero false diffs."""
    import io
    from unittest.mock import Mock

    from imednet.datadict.loaders.api import load_from_api
    from imednet.datadict.loaders.cdisc_odm import load_from_cdisc_odm
    from imednet.datadict.loaders.csv_export import load_from_csv_files
    from imednet.datadict.loaders.redcap import load_from_redcap

    # 1. CSV loader (uses legacy "Patient" formType, numeric formId, "Number"/"Dropdown")
    dd_csv = load_from_csv_files(
        forms_file=io.StringIO(
            "formId,formKey,formName,formType\n1001,demographics,Demographics,Patient\n"
        ),
        questions_file=io.StringIO(
            "formKey,variableName,label,variableType,sequence,required\n"
            "demographics,age,Subject Age,Number,1,Yes\n"
            "demographics,sex,Biological Sex,RadioButton,2,Yes\n"
            "demographics,cohort,Cohort Arm,Dropdown,3,No\n"
        ),
        choices_file=io.StringIO(
            "formKey,variableName,choiceValue,choiceText,position\n"
            "demographics,sex,M,Male,1\n"
            "demographics,sex,F,Female,2\n"
            "demographics,cohort,A,Arm A,1\n"
            "demographics,cohort,B,Arm B,2\n"
        ),
        business_logic_file=io.StringIO("id,name,type,status,formKey,sequence,logic\n"),
        study_key="STUDY_EQ",
    )

    # 2. ODM loader (no numeric formId, no FormType attribute, DataType/ControlType)
    odm_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3" FileType="Snapshot" FileOID="EQ" CreationDateTime="2026-01-01T00:00:00">
  <Study OID="STUDY_EQ">
    <MetaDataVersion OID="MDV.1" Name="v1">
      <FormDef OID="demographics" Name="Demographics" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG_DEMO" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG_DEMO" Name="Demo">
        <ItemRef ItemOID="age" OrderNumber="1" Mandatory="Yes"/>
        <ItemRef ItemOID="sex" OrderNumber="2" Mandatory="Yes"/>
        <ItemRef ItemOID="cohort" OrderNumber="3" Mandatory="No"/>
      </ItemGroupDef>
      <ItemDef OID="age" Name="age" DataType="integer">
        <Question><TranslatedText>Subject Age</TranslatedText></Question>
      </ItemDef>
      <ItemDef OID="sex" Name="sex" DataType="text" ControlType="RadioButton">
        <Question><TranslatedText>Biological Sex</TranslatedText></Question>
        <CodeListRef CodeListOID="CL_SEX"/>
      </ItemDef>
      <ItemDef OID="cohort" Name="cohort" DataType="text" ControlType="SingleSelect">
        <Question><TranslatedText>Cohort Arm</TranslatedText></Question>
        <CodeListRef CodeListOID="CL_COHORT"/>
      </ItemDef>
      <CodeList OID="CL_SEX" Name="Sex" DataType="text">
        <CodeListItem CodedValue="M" OrderNumber="1"><Decode><TranslatedText>Male</TranslatedText></Decode></CodeListItem>
        <CodeListItem CodedValue="F" OrderNumber="2"><Decode><TranslatedText>Female</TranslatedText></Decode></CodeListItem>
      </CodeList>
      <CodeList OID="CL_COHORT" Name="Cohort" DataType="text">
        <CodeListItem CodedValue="A" OrderNumber="1"><Decode><TranslatedText>Arm A</TranslatedText></Decode></CodeListItem>
        <CodeListItem CodedValue="B" OrderNumber="2"><Decode><TranslatedText>Arm B</TranslatedText></Decode></CodeListItem>
      </CodeList>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd_odm = load_from_cdisc_odm(odm_xml, study_key="STUDY_EQ")

    # 3. REDCap loader
    redcap_csv = (
        '"Variable / Field Name","Form Name","Field Type","Field Label",'
        '"Choices, Calculations, OR Slider Labels",'
        '"Text Validation Type OR Show Slider Number","Required Field?"\n'
        '"age","demographics","text","Subject Age",,"integer","y"\n'
        '"sex","demographics","radio","Biological Sex","M, Male | F, Female",,"y"\n'
        '"cohort","demographics","dropdown","Cohort Arm","A, Arm A | B, Arm B",,"n"\n'
    )
    dd_redcap = load_from_redcap(redcap_csv, study_key="STUDY_EQ")

    # 4. SDK API loader (with synthetic form_id == form_key or integer, "CRF" form_type)
    mock_sdk = Mock()
    mock_sdk.forms.list.return_value = [
        {
            "form_key": "demographics",
            "form_name": "Demographics",
            "form_id": "demographics",
            "form_type": "CRF",
        }
    ]
    mock_sdk.variables.list.return_value = [
        {
            "form_key": "demographics",
            "variable_name": "age",
            "label": "Subject Age",
            "variable_type": "Integer",
            "sequence": 1,
            "required": True,
        },
        {
            "form_key": "demographics",
            "variable_name": "sex",
            "label": "Biological Sex",
            "variable_type": "Radio",
            "sequence": 2,
            "required": True,
        },
        {
            "form_key": "demographics",
            "variable_name": "cohort",
            "label": "Cohort Arm",
            "variable_type": "Single Select",
            "sequence": 3,
            "required": False,
        },
    ]
    dd_api = load_from_api(mock_sdk, study_key="STUDY_EQ")

    loaders = [("csv", dd_csv), ("odm", dd_odm), ("redcap", dd_redcap), ("api", dd_api)]
    for name_a, dict_a in loaders:
        for name_b, dict_b in loaders:
            if name_a == name_b:
                continue
            report = diff(dict_a, dict_b)
            noisy_paths = [
                item.path
                for item in report.items
                if item.path.endswith((".formId", ".formType", ".variableType"))
            ]
            assert noisy_paths == [], (
                f"Unexpected {noisy_paths} modifications when diffing {name_a} vs {name_b}"
            )


def test_diff_detects_genuine_normalized_field_modifications() -> None:
    """Genuine changes to formId, formType, and variableType are still reported by diff."""
    dd_old = DataDictionary(
        forms=[
            Form(
                formKey="DM",
                formId=1001,
                formType="Patient",
                variables=[Variable(variableName="AGE", variableType="Integer")],
            )
        ]
    )
    dd_new = DataDictionary(
        forms=[
            Form(
                formKey="DM",
                formId=1002,
                formType="Site",
                variables=[Variable(variableName="AGE", variableType="Float")],
            )
        ]
    )

    report = diff(dd_old, dd_new)
    modified_paths = {item.path for item in report.items if item.action == "modified"}
    assert "forms[DM].formId" in modified_paths
    assert "forms[DM].formType" in modified_paths
    assert "forms[DM].variables[AGE].variableType" in modified_paths
