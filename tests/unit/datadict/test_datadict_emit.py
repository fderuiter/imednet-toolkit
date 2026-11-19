"""Tests for deterministic JSON emission."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from defusedxml.ElementTree import fromstring
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


def test_to_odm_xml_sdtm_attributes_emission() -> None:
    """Verify emission of SDTM attributes on ItemGroupDef and ItemDef elements."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_SDTM"),
        forms=[
            Form(
                formKey="DM",
                formName="Demographics",
                extraAttributes={"Domain": "DM", "SASDatasetName": "DM"},
                variables=[
                    Variable(
                        variableName="SUBJID",
                        label="Subject Identifier",
                        variableType="Text",
                        sequence=1,
                        extraAttributes={
                            "SDSVarName": "SUBJID",
                            "SASFieldName": "SUBJID",
                            "Origin": "CRF",
                            "Comment": "Subject Identifier",
                        },
                    ),
                    Variable(
                        variableName="AGE",
                        label="Age",
                        variableType="Integer",
                        sequence=2,
                        extraAttributes={
                            "SDSVarName": "AGE",
                            "SASFieldName": "AGE",
                            "Origin": "Assigned",
                            "Comment": "Age in years",
                        },
                    ),
                    Variable(
                        variableName="BLANK_SDTM",
                        label="Blank SDTM Attributes",
                        variableType="Text",
                        sequence=3,
                        extraAttributes={
                            "SDSVarName": "",
                            "SASFieldName": "   ",
                            "Origin": None,
                            "Comment": "",
                        },
                    ),
                ],
            )
        ],
    )

    xml_str = to_odm_xml(dd)
    root = fromstring(xml_str)
    ns = {"odm": "http://www.cdisc.org/ns/odm/v1.3"}

    # ItemGroupDef attributes
    ig_elem = root.find(".//odm:ItemGroupDef[@OID='IG_DM']", ns)
    assert ig_elem is not None
    assert ig_elem.attrib.get("Domain") == "DM"
    assert ig_elem.attrib.get("SASDatasetName") == "DM"

    # ItemDef attributes for SUBJID
    subjid_elem = root.find(".//odm:ItemDef[@OID='SUBJID']", ns)
    assert subjid_elem is not None
    assert subjid_elem.attrib.get("SDSVarName") == "SUBJID"
    assert subjid_elem.attrib.get("SASFieldName") == "SUBJID"
    assert subjid_elem.attrib.get("Origin") == "CRF"
    assert subjid_elem.attrib.get("Comment") == "Subject Identifier"

    # ItemDef attributes for AGE
    age_elem = root.find(".//odm:ItemDef[@OID='AGE']", ns)
    assert age_elem is not None
    assert age_elem.attrib.get("SDSVarName") == "AGE"
    assert age_elem.attrib.get("SASFieldName") == "AGE"
    assert age_elem.attrib.get("Origin") == "Assigned"
    assert age_elem.attrib.get("Comment") == "Age in years"

    # ItemDef for BLANK_SDTM: null, empty, and whitespace attributes must be omitted
    blank_elem = root.find(".//odm:ItemDef[@OID='BLANK_SDTM']", ns)
    assert blank_elem is not None
    assert "SDSVarName" not in blank_elem.attrib
    assert "SASFieldName" not in blank_elem.attrib
    assert "Origin" not in blank_elem.attrib
    assert "Comment" not in blank_elem.attrib


def test_to_odm_xml_sdtm_fallback_to_child_variables() -> None:
    """Verify fallback to child variables when form metadata lacks dataset attributes."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_FALLBACK"),
        forms=[
            Form(
                formKey="VS",
                formName="Vital Signs",
                extraAttributes={},
                variables=[
                    Variable(
                        variableName="SYSBP",
                        label="Systolic Blood Pressure",
                        variableType="Integer",
                        sequence=1,
                        extraAttributes={
                            "Domain": "VS",
                            "SASDatasetName": "VS",
                            "SDSVarName": "SYSBP",
                        },
                    )
                ],
            ),
            Form(
                formKey="EMPTY_FORM",
                formName="Form Without Variables",
                extraAttributes={"Domain": "   ", "SASDatasetName": ""},
                variables=[],
            ),
        ],
    )

    xml_str = to_odm_xml(dd)
    root = fromstring(xml_str)
    ns = {"odm": "http://www.cdisc.org/ns/odm/v1.3"}

    # VS ItemGroupDef should populate Domain and SASDatasetName from child variable
    vs_ig = root.find(".//odm:ItemGroupDef[@OID='IG_VS']", ns)
    assert vs_ig is not None
    assert vs_ig.attrib.get("Domain") == "VS"
    assert vs_ig.attrib.get("SASDatasetName") == "VS"

    # EMPTY_FORM ItemGroupDef should omit Domain and SASDatasetName cleanly
    empty_ig = root.find(".//odm:ItemGroupDef[@OID='IG_EMPTY_FORM']", ns)
    assert empty_ig is not None
    assert "Domain" not in empty_ig.attrib
    assert "SASDatasetName" not in empty_ig.attrib


def test_odm_round_trip_sdtm_preservation() -> None:
    """Verify round-trip preservation of all six SDTM attributes between loader and emitter."""
    sdtm_source_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3" FileType="Snapshot" FileOID="SDTM_RT_01" CreationDateTime="2026-11-16T12:00:00">
  <Study OID="STUDY_SDTM_RT">
    <MetaDataVersion OID="MDV_001" Name="SDTM Round Trip Version">
      <FormDef OID="DM" Name="Demographics">
        <ItemGroupRef ItemGroupOID="IG_DM" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG_DM" Name="Demographics Details" Domain="DM" SASDatasetName="DM">
        <ItemRef ItemOID="SUBJID" OrderNumber="1"/>
        <ItemRef ItemOID="AGE" OrderNumber="2"/>
      </ItemGroupDef>
      <ItemDef OID="SUBJID" Name="Subject ID" DataType="text"
               SDSVarName="SUBJID" SASFieldName="SUBJID" Origin="CRF" Comment="Subject Identifier"/>
      <ItemDef OID="AGE" Name="Age" DataType="integer"
               SDSVarName="AGE" SASFieldName="AGE" Origin="Assigned" Comment="Age in years"/>
    </MetaDataVersion>
  </Study>
</ODM>"""

    # 1. Load initial XML with strict=True
    dd_initial = load_from_cdisc_odm(sdtm_source_xml, strict=True)
    assert dd_initial.metadata.validationWarnings == []

    # 2. Serialize to ODM XML
    exported_xml = to_odm_xml(dd_initial)

    # 3. Reload exported XML with strict=True
    dd_reloaded = load_from_cdisc_odm(exported_xml, strict=True)
    assert dd_reloaded.metadata.validationWarnings == []

    # 4. Verify all six SDTM attributes preserved on reloaded model
    form_dm = next(f for f in dd_reloaded.forms if f.formKey == "DM")
    assert form_dm.extraAttributes.get("Domain") == "DM"
    assert form_dm.extraAttributes.get("SASDatasetName") == "DM"

    v_subjid = next(v for v in form_dm.variables if v.variableName == "SUBJID")
    assert v_subjid.extraAttributes.get("Domain") == "DM"
    assert v_subjid.extraAttributes.get("SASDatasetName") == "DM"
    assert v_subjid.extraAttributes.get("SDSVarName") == "SUBJID"
    assert v_subjid.extraAttributes.get("SASFieldName") == "SUBJID"
    assert v_subjid.extraAttributes.get("Origin") == "CRF"
    assert v_subjid.extraAttributes.get("Comment") == "Subject Identifier"

    v_age = next(v for v in form_dm.variables if v.variableName == "AGE")
    assert v_age.extraAttributes.get("Domain") == "DM"
    assert v_age.extraAttributes.get("SASDatasetName") == "DM"
    assert v_age.extraAttributes.get("SDSVarName") == "AGE"
    assert v_age.extraAttributes.get("SASFieldName") == "AGE"
    assert v_age.extraAttributes.get("Origin") == "Assigned"
    assert v_age.extraAttributes.get("Comment") == "Age in years"


def test_odm_round_trip_scoped_shared_variables_and_required_policy() -> None:
    """Round-tripping two Forms with distinct STATUS variables preserves definitions and Auto Query."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_SCOPED_STATUS"),
        forms=[
            Form(
                formKey="DM",
                formName="Demographics",
                extraAttributes={"Domain": "DM", "SASDatasetName": "DM"},
                variables=[
                    Variable(
                        variableName="STATUS",
                        label="Enrollment Status",
                        variableType="Radio",
                        sequence=1,
                        required="Auto Query",
                        choices=[
                            Choice(choiceValue="1", choiceText="Enrolled", position=1),
                            Choice(choiceValue="2", choiceText="Screen Failure", position=2),
                        ],
                        extraAttributes={
                            "Domain": "DM",
                            "SASDatasetName": "DM",
                            "SDSVarName": "DMSTAT",
                            "SASFieldName": "DMSTAT",
                            "Origin": "CRF",
                            "Comment": "Demographics enrollment status",
                        },
                    )
                ],
            ),
            Form(
                formKey="DV",
                formName="Protocol Deviations",
                extraAttributes={"Domain": "DV", "SASDatasetName": "DV"},
                variables=[
                    Variable(
                        variableName="STATUS",
                        label="Deviation Resolution Status",
                        variableType="Single Select",
                        sequence=1,
                        required="Yes",
                        choices=[
                            Choice(choiceValue="OPEN", choiceText="Open", position=1),
                            Choice(choiceValue="CLOSED", choiceText="Closed", position=2),
                        ],
                        extraAttributes={
                            "Domain": "DV",
                            "SASDatasetName": "DV",
                            "SDSVarName": "DVSTAT",
                            "SASFieldName": "DVSTAT",
                            "Origin": "Assigned",
                            "Comment": "Deviation workflow status",
                        },
                    )
                ],
            ),
        ],
    )

    xml_str = to_odm_xml(dd)
    root = fromstring(xml_str)
    ns = {
        "odm": "http://www.cdisc.org/ns/odm/v1.3",
        "imednet": "http://www.imednet.com/ns/odm/metadata",
    }

    # Scoped ItemDef and CodeList identifiers per Form
    assert root.find(".//odm:ItemDef[@OID='DM.STATUS']", ns) is not None
    assert root.find(".//odm:ItemDef[@OID='DV.STATUS']", ns) is not None
    assert root.find(".//odm:ItemDef[@OID='STATUS']", ns) is None
    assert root.find(".//odm:CodeList[@OID='CL_DM.STATUS']", ns) is not None
    assert root.find(".//odm:CodeList[@OID='CL_DV.STATUS']", ns) is not None

    # Vendor RequiredPolicy attribute preserved alongside ODM Mandatory="No"
    dm_ref = root.find(".//odm:ItemGroupDef[@OID='IG_DM']/odm:ItemRef[@ItemOID='DM.STATUS']", ns)
    assert dm_ref is not None
    assert dm_ref.attrib.get("Mandatory") == "No"
    assert dm_ref.attrib.get("{http://www.imednet.com/ns/odm/metadata}RequiredPolicy") == (
        "Auto Query"
    )

    reloaded = load_from_cdisc_odm(xml_str, strict=True)
    assert reloaded.metadata.validationWarnings == []

    dm_form = next(f for f in reloaded.forms if f.formKey == "DM")
    dv_form = next(f for f in reloaded.forms if f.formKey == "DV")

    dm_status = dm_form.variables[0]
    assert dm_status.variableName == "STATUS"
    assert dm_status.label == "Enrollment Status"
    assert dm_status.variableType == "Radio"
    assert dm_status.required == "Auto Query"
    assert [(c.choiceValue, c.choiceText) for c in dm_status.choices] == [
        ("1", "Enrolled"),
        ("2", "Screen Failure"),
    ]
    assert dm_status.extraAttributes["Domain"] == "DM"
    assert dm_status.extraAttributes["SASDatasetName"] == "DM"
    assert dm_status.extraAttributes["SDSVarName"] == "DMSTAT"
    assert dm_status.extraAttributes["SASFieldName"] == "DMSTAT"
    assert dm_status.extraAttributes["Origin"] == "CRF"
    assert dm_status.extraAttributes["Comment"] == "Demographics enrollment status"

    dv_status = dv_form.variables[0]
    assert dv_status.variableName == "STATUS"
    assert dv_status.label == "Deviation Resolution Status"
    assert dv_status.variableType == "Single Select"
    assert dv_status.required == "Yes"
    assert [(c.choiceValue, c.choiceText) for c in dv_status.choices] == [
        ("OPEN", "Open"),
        ("CLOSED", "Closed"),
    ]
    assert dv_status.extraAttributes["Domain"] == "DV"
    assert dv_status.extraAttributes["SASDatasetName"] == "DV"
    assert dv_status.extraAttributes["SDSVarName"] == "DVSTAT"
    assert dv_status.extraAttributes["SASFieldName"] == "DVSTAT"
    assert dv_status.extraAttributes["Origin"] == "Assigned"
    assert dv_status.extraAttributes["Comment"] == "Deviation workflow status"


def test_to_odm_xml_retains_unprefixed_oids_for_unique_and_identical_shared_variables() -> None:
    """Unique or identical shared Variables retain unprefixed ItemDef and CodeList OIDs."""
    shared_choices = [
        Choice(choiceValue="Y", choiceText="Yes", position=1),
        Choice(choiceValue="N", choiceText="No", position=2),
    ]
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_UNPREFIXED"),
        forms=[
            Form(
                formKey="VS_BASE",
                formName="Baseline Vitals",
                variables=[
                    Variable(
                        variableName="PERF",
                        label="Was assessment performed?",
                        variableType="Radio",
                        sequence=1,
                        required="Yes",
                        choices=copy.deepcopy(shared_choices),
                    ),
                    Variable(
                        variableName="SUBJID",
                        label="Subject ID",
                        variableType="Text",
                        sequence=2,
                    ),
                ],
            ),
            Form(
                formKey="VS_WK4",
                formName="Week 4 Vitals",
                variables=[
                    Variable(
                        variableName="PERF",
                        label="Was assessment performed?",
                        variableType="Radio",
                        sequence=1,
                        required="Auto Query",
                        choices=copy.deepcopy(shared_choices),
                    ),
                ],
            ),
        ],
    )

    xml_str = to_odm_xml(dd)
    root = fromstring(xml_str)
    ns = {"odm": "http://www.cdisc.org/ns/odm/v1.3"}

    assert root.find(".//odm:ItemDef[@OID='PERF']", ns) is not None
    assert root.find(".//odm:ItemDef[@OID='SUBJID']", ns) is not None
    assert root.find(".//odm:CodeList[@OID='CL_PERF']", ns) is not None
    assert root.find(".//odm:ItemDef[@OID='VS_BASE.PERF']", ns) is None
    assert root.find(".//odm:ItemDef[@OID='VS_WK4.PERF']", ns) is None
    assert root.find(".//odm:CodeList[@OID='CL_VS_BASE.PERF']", ns) is None


def test_to_odm_xml_serializes_bounds_and_measurement_units() -> None:
    """Normalized bounds and units serialize into BasicDefinitions, MeasurementUnitRef, and RangeCheck."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_UNITS_BOUNDS"),
        forms=[
            Form(
                formKey="VS",
                formName="Vital Signs",
                variables=[
                    Variable(
                        variableName="SYSBP",
                        label="Systolic Blood Pressure",
                        variableType="Integer",
                        sequence=1,
                        precision=None,
                        extraAttributes={
                            "unit": "mmHg",
                            "textValidationMin": "70",
                            "textValidationMax": "250",
                        },
                    )
                ],
            )
        ],
    )

    xml_str = to_odm_xml(dd)
    assert "SignificantDigits" not in xml_str

    root = fromstring(xml_str)
    ns = {"odm": "http://www.cdisc.org/ns/odm/v1.3"}

    mu_elem = root.find(".//odm:BasicDefinitions/odm:MeasurementUnit[@OID='MU.mmHg']", ns)
    assert mu_elem is not None
    assert mu_elem.attrib.get("Name") == "mmHg"

    item_elem = root.find(".//odm:ItemDef[@OID='SYSBP']", ns)
    assert item_elem is not None
    mu_ref = item_elem.find("odm:MeasurementUnitRef", ns)
    assert mu_ref is not None
    assert mu_ref.attrib.get("MeasurementUnitOID") == "MU.mmHg"

    range_checks = item_elem.findall("odm:RangeCheck", ns)
    assert len(range_checks) == 2
    assert range_checks[0].attrib.get("Comparator") == "GE"
    assert range_checks[0].findtext("odm:CheckValue", namespaces=ns) == "70"
    assert range_checks[1].attrib.get("Comparator") == "LE"
    assert range_checks[1].findtext("odm:CheckValue", namespaces=ns) == "250"

    reloaded = load_from_cdisc_odm(xml_str, strict=True)
    reloaded_var = reloaded.forms[0].variables[0]
    assert reloaded_var.precision is None
    assert reloaded_var.extraAttributes["measurementUnits"] == [
        {"oid": "MU.mmHg", "name": "mmHg", "symbol": "mmHg"}
    ]
    assert reloaded_var.extraAttributes["rangeChecks"] == [
        {"comparator": "GE", "softHard": "Soft", "checkValue": "70"},
        {"comparator": "LE", "softHard": "Soft", "checkValue": "250"},
    ]


def test_odm_round_trip_scoped_and_raw_variable_name_collision() -> None:
    """Scoped AGE across F1/F2 and raw F1.AGE emit three distinct ItemDefs and round-trip cleanly."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_SCOPED_RAW_COLLISION"),
        forms=[
            Form(
                formKey="F1",
                formName="Form One",
                variables=[
                    Variable(
                        variableName="AGE",
                        label="Screening Age (Years)",
                        variableType="Integer",
                        sequence=1,
                        required="Yes",
                        choices=[
                            Choice(choiceValue="18", choiceText="Adult 18", position=1),
                        ],
                    ),
                    Variable(
                        variableName="F1.AGE",
                        label="Legacy Imported F1.AGE",
                        variableType="Text",
                        sequence=2,
                        required="Auto Query",
                        choices=[
                            Choice(choiceValue="RAW", choiceText="Raw Value", position=1),
                        ],
                    ),
                ],
            ),
            Form(
                formKey="F2",
                formName="Form Two",
                variables=[
                    Variable(
                        variableName="AGE",
                        label="Exact Follow-up Age",
                        variableType="Float",
                        precision="1",
                        sequence=1,
                        required="No",
                    ),
                ],
            ),
        ],
    )

    xml_str = to_odm_xml(dd)
    root = fromstring(xml_str)
    ns = {"odm": "http://www.cdisc.org/ns/odm/v1.3"}

    item_defs = root.findall(".//odm:ItemDef", ns)
    assert len(item_defs) == 3
    item_oids = [elem.attrib["OID"] for elem in item_defs]
    assert len(set(item_oids)) == 3

    f1_refs = root.findall(".//odm:ItemGroupDef[@OID='IG_F1']/odm:ItemRef", ns)
    assert len(f1_refs) == 2
    assert f1_refs[0].attrib["ItemOID"] != f1_refs[1].attrib["ItemOID"]

    codelists = root.findall(".//odm:CodeList", ns)
    assert len(codelists) == 2
    assert len({cl.attrib["OID"] for cl in codelists}) == 2

    reloaded = load_from_cdisc_odm(xml_str, strict=True)
    assert reloaded.metadata.validationWarnings == []

    forms_by_key = {f.formKey: f for f in reloaded.forms}
    f1_vars = {v.variableName: v for v in forms_by_key["F1"].variables}
    assert list(f1_vars.keys()) == ["AGE", "F1.AGE"]
    assert f1_vars["AGE"].label == "Screening Age (Years)"
    assert f1_vars["AGE"].variableType == "Integer"
    assert f1_vars["AGE"].required == "Yes"
    assert [(c.choiceValue, c.choiceText) for c in f1_vars["AGE"].choices] == [("18", "Adult 18")]

    assert f1_vars["F1.AGE"].label == "Legacy Imported F1.AGE"
    assert f1_vars["F1.AGE"].variableType == "Text"
    assert f1_vars["F1.AGE"].required == "Auto Query"
    assert [(c.choiceValue, c.choiceText) for c in f1_vars["F1.AGE"].choices] == [
        ("RAW", "Raw Value")
    ]

    f2_vars = {v.variableName: v for v in forms_by_key["F2"].variables}
    assert list(f2_vars.keys()) == ["AGE"]
    assert f2_vars["AGE"].label == "Exact Follow-up Age"
    assert f2_vars["AGE"].variableType == "Float"
    assert f2_vars["AGE"].precision == "1"


def test_odm_round_trip_multi_level_oid_disambiguation_and_shared_prefixed_raw_name() -> None:
    """Fallback indexed OIDs and shared prefixed raw names preserve all definitions across forms."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="STUDY_MULTI_COLLISION"),
        forms=[
            Form(
                formKey="F0",
                formName="Form Zero",
                variables=[
                    Variable(
                        variableName="F1.AGE",
                        label="Shared Dotted F1.AGE",
                        variableType="Text",
                        sequence=1,
                    ),
                ],
            ),
            Form(
                formKey="F1",
                formName="Form One",
                variables=[
                    Variable(
                        variableName="AGE",
                        label="F1 Scoped Age",
                        variableType="Integer",
                        sequence=1,
                    ),
                    Variable(
                        variableName="F1.AGE",
                        label="Shared Dotted F1.AGE",
                        variableType="Text",
                        sequence=2,
                    ),
                    Variable(
                        variableName="IT.F1.AGE",
                        label="Raw IT.F1.AGE",
                        variableType="Date",
                        sequence=3,
                    ),
                ],
            ),
            Form(
                formKey="F2",
                formName="Form Two",
                variables=[
                    Variable(
                        variableName="AGE",
                        label="F2 Scoped Age",
                        variableType="Float",
                        sequence=1,
                    ),
                ],
            ),
        ],
    )

    xml_str = to_odm_xml(dd)
    root = fromstring(xml_str)
    ns = {"odm": "http://www.cdisc.org/ns/odm/v1.3"}

    item_defs = root.findall(".//odm:ItemDef", ns)
    assert len(item_defs) == 4
    assert len({elem.attrib["OID"] for elem in item_defs}) == 4

    reloaded = load_from_cdisc_odm(xml_str, strict=True)
    assert reloaded.metadata.validationWarnings == []
    forms_by_key = {f.formKey: f for f in reloaded.forms}

    assert [(v.variableName, v.label, v.variableType) for v in forms_by_key["F0"].variables] == [
        ("F1.AGE", "Shared Dotted F1.AGE", "Text"),
    ]
    assert [(v.variableName, v.label, v.variableType) for v in forms_by_key["F1"].variables] == [
        ("AGE", "F1 Scoped Age", "Integer"),
        ("F1.AGE", "Shared Dotted F1.AGE", "Text"),
        ("IT.F1.AGE", "Raw IT.F1.AGE", "Date"),
    ]
    assert [(v.variableName, v.label, v.variableType) for v in forms_by_key["F2"].variables] == [
        ("AGE", "F2 Scoped Age", "Float"),
    ]
