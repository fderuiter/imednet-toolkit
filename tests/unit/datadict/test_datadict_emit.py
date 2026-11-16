"""Tests for deterministic JSON emission."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from tests.unit.datadict.test_datadict_cdisc_odm_loader import SAMPLE_ODM_XML

from imednet.datadict.emit import (
    sort_data_dictionary,
    to_file,
    to_json,
    to_odm_file,
    to_odm_xml,
)
from imednet.datadict.loaders.cdisc_odm import load_from_cdisc_odm
from imednet.datadict.models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)


def test_sort_data_dictionary_deterministic() -> None:
    """Test deterministic ordering of forms, variables, choices, and rules."""
    form_z = Form(
        formKey="Z_FORM",
        variables=[
            Variable(variableName="B_VAR", sequence=2),
            Variable(variableName="A_VAR", sequence=1),
        ],
    )
    form_a = Form(
        formKey="A_FORM",
        variables=[
            Variable(
                variableName="V1",
                choices=[
                    Choice(choiceText="Two", choiceValue="2", position=2),
                    Choice(choiceText="One", choiceValue="1", position=1),
                ],
            )
        ],
        businessLogic=[
            BusinessLogicRule(id=200, name="Rule B", sequence=2),
            BusinessLogicRule(id=100, name="Rule A", sequence=1),
        ],
    )

    dd = DataDictionary(
        forms=[form_z, form_a],
        unassignedBusinessLogic=[
            BusinessLogicRule(id=999, name="Unassigned B", sequence=2),
            BusinessLogicRule(id=888, name="Unassigned A", sequence=1),
        ],
    )

    sorted_dd = sort_data_dictionary(dd)

    # Forms ordered by formKey
    assert [f.formKey for f in sorted_dd.forms] == ["A_FORM", "Z_FORM"]

    # Variables ordered by sequence
    assert [v.variableName for v in sorted_dd.forms[1].variables] == ["A_VAR", "B_VAR"]

    # Choices ordered by position
    assert [c.choiceValue for c in sorted_dd.forms[0].variables[0].choices] == ["1", "2"]

    # Form rules ordered by sequence
    assert [r.id for r in sorted_dd.forms[0].businessLogic] == [100, 200]

    # Unassigned rules ordered by sequence
    assert [r.id for r in sorted_dd.unassignedBusinessLogic] == [888, 999]


def test_to_json_exclude_generated_at() -> None:
    """Test to_json emission with and without generatedAt."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="TEST", generatedAt="09-28-2026 12:00:00"),
        forms=[Form(formKey="F1")],
    )

    json_with_time = to_json(dd, exclude_generated_at=False)
    assert "generatedAt" in json_with_time
    assert "09-28-2026 12:00:00" in json_with_time

    json_without_time = to_json(dd, exclude_generated_at=True)
    assert "generatedAt" not in json_without_time
    assert "studyKey" in json_without_time


def test_to_file(tmp_path: Path) -> None:
    """Test writing canonical JSON to file."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="TEST"),
        forms=[Form(formKey="F1", formName="Form One")],
    )
    dest = tmp_path / "subdir" / "datadict.json"
    to_file(dd, dest)

    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    loaded = json.loads(content)
    assert loaded["forms"][0]["formKey"] == "F1"


def test_to_odm_xml_basic() -> None:
    """Test serializing DataDictionary to valid CDISC ODM 1.3 XML."""
    dd = DataDictionary(
        metadata=Metadata(
            studyKey="STUDY_001",
            extraAttributes={"studyName": "Phase 1 Trial", "studyDescription": "Dose escalation"},
        ),
        forms=[
            Form(
                formKey="DM",
                formName="Demographics",
                variables=[
                    Variable(
                        variableName="BRTHDAT",
                        label="Date of Birth",
                        variableType="Date",
                        sequence=1,
                        required="Yes",
                    ),
                    Variable(
                        variableName="SEX",
                        label="Sex",
                        variableType="Radio",
                        sequence=2,
                        required="Yes",
                        choices=[
                            Choice(choiceValue="M", choiceText="Male", position=1),
                            Choice(choiceValue="F", choiceText="Female", position=2),
                        ],
                    ),
                ],
            )
        ],
    )

    xml_str = to_odm_xml(dd, file_oid="TEST_FILE_01", creation_datetime="2026-11-16T12:00:00")
    assert 'xmlns="http://www.cdisc.org/ns/odm/v1.3"' in xml_str
    assert 'ODMVersion="1.3"' in xml_str
    assert 'FileOID="TEST_FILE_01"' in xml_str
    assert 'CreationDateTime="2026-11-16T12:00:00"' in xml_str
    assert '<Study OID="STUDY_001">' in xml_str
    assert "<StudyName>Phase 1 Trial</StudyName>" in xml_str
    assert '<FormDef OID="DM"' in xml_str
    assert '<ItemGroupRef ItemGroupOID="IG_DM"' in xml_str
    assert '<ItemGroupDef OID="IG_DM"' in xml_str
    assert '<ItemRef ItemOID="BRTHDAT"' in xml_str
    assert '<ItemDef OID="BRTHDAT" Name="BRTHDAT" DataType="date"' in xml_str
    assert '<ItemDef OID="SEX" Name="SEX" DataType="text"' in xml_str
    assert '<CodeList OID="CL_SEX"' in xml_str
    assert '<CodeListItem CodedValue="M"' in xml_str
    assert "<TranslatedText" in xml_str


def test_to_odm_xml_immutability() -> None:
    """Verify to_odm_xml does not mutate the input DataDictionary instance."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_IMMUTABLE"),
        forms=[
            Form(
                formKey="Z_FORM",
                variables=[
                    Variable(variableName="VAR_B", sequence=2),
                    Variable(variableName="VAR_A", sequence=1),
                ],
            ),
            Form(formKey="A_FORM", variables=[]),
        ],
    )

    dd_clone = copy.deepcopy(dd)
    _ = to_odm_xml(dd)

    # Forms and variables retain their original ordering and identity
    assert [f.formKey for f in dd.forms] == [f.formKey for f in dd_clone.forms]
    assert [v.variableName for v in dd.forms[0].variables] == [
        v.variableName for v in dd_clone.forms[0].variables
    ]
    assert dd == dd_clone


def test_to_odm_file(tmp_path: Path) -> None:
    """Test writing CDISC ODM XML to a file on disk."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_FILE_TEST"),
        forms=[Form(formKey="AE", formName="Adverse Events", variables=[])],
    )
    dest = tmp_path / "exports" / "study_odm.xml"
    to_odm_file(dd, dest, file_oid="TEST_FILE_WRITE")

    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert "<ODM" in content
    assert '<Study OID="STUDY_FILE_TEST">' in content


def test_odm_round_trip_fidelity() -> None:
    """Verify round-trip fidelity between load_from_cdisc_odm and to_odm_xml."""
    # 1. Load canonical test fixture
    dd_initial = load_from_cdisc_odm(SAMPLE_ODM_XML)

    # 2. Serialize to ODM XML
    xml_output = to_odm_xml(dd_initial)

    # 3. Reload from serialized ODM XML in strict mode
    dd_reloaded = load_from_cdisc_odm(xml_output, strict=True)

    # 4. Verify equivalent study schemas
    assert dd_reloaded.metadata.studyKey == dd_initial.metadata.studyKey
    assert len(dd_reloaded.forms) == len(dd_initial.forms)

    initial_forms = {f.formKey: f for f in dd_initial.forms}
    reloaded_forms = {f.formKey: f for f in dd_reloaded.forms}
    assert set(initial_forms.keys()) == set(reloaded_forms.keys())

    for key, f_init in initial_forms.items():
        f_reloaded = reloaded_forms[key]
        assert f_reloaded.formName == f_init.formName
        assert len(f_reloaded.variables) == len(f_init.variables)

        init_vars = {v.variableName: v for v in f_init.variables}
        reloaded_vars = {v.variableName: v for v in f_reloaded.variables}
        assert set(init_vars.keys()) == set(reloaded_vars.keys())

        for var_name, v_init in init_vars.items():
            v_reloaded = reloaded_vars[var_name]
            assert v_reloaded.label == v_init.label
            assert v_reloaded.variableType == v_init.variableType
            assert v_reloaded.required == v_init.required
            assert len(v_reloaded.choices) == len(v_init.choices)

            for c_init, c_reloaded in zip(v_init.choices, v_reloaded.choices):
                assert c_reloaded.choiceValue == c_init.choiceValue
                assert c_reloaded.choiceText == c_init.choiceText
                assert c_reloaded.position == c_init.position
