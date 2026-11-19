"""Unit tests for CDISC ODM 1.3 XML data dictionary loader."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from defusedxml.common import DefusedXmlException

from imednet.datadict import get_schema, load_from_cdisc_odm, validate
from imednet.datadict.models import DataDictionary

SAMPLE_ODM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     xmlns:mdsol="http://www.mdsol.com/ns/odm/metadata"
     ODMVersion="1.3"
     FileType="Snapshot"
     FileOID="TEST_STUDY_FILE"
     CreationDateTime="2026-01-01T10:00:00">
  <Study OID="STUDY_MEDIDATA_RAVE_001">
    <GlobalVariables>
      <StudyName>Oncology Protocol Phase II</StudyName>
      <StudyDescription>Open label multicenter study</StudyDescription>
      <ProtocolName>ONCO-2026-01</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV.001" Name="Initial Metadata Version">
      <FormDef OID="DM" Name="Demographics" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG_DM" OrderNumber="1" Mandatory="Yes"/>
      </FormDef>
      <FormDef OID="VS" Name="Vital Signs" Repeating="Yes">
        <ItemGroupRef ItemGroupOID="IG_VS" OrderNumber="1" Mandatory="No"/>
      </FormDef>

      <ItemGroupDef OID="IG_DM" Name="Demographics Details">
        <ItemRef ItemOID="BRTHDAT" OrderNumber="1" Mandatory="Yes"/>
        <ItemRef ItemOID="SEX" OrderNumber="2" Mandatory="Yes"/>
        <ItemRef ItemOID="WEIGHT" OrderNumber="3" Mandatory="No"/>
      </ItemGroupDef>

      <ItemGroupDef OID="IG_VS" Name="Vital Signs Details">
        <ItemRef ItemOID="SYSBP" OrderNumber="1" Mandatory="No"/>
      </ItemGroupDef>

      <ItemDef OID="BRTHDAT" Name="BRTHDAT" DataType="date" Length="10">
        <Question>
          <TranslatedText xml:lang="en">Date of Birth</TranslatedText>
        </Question>
      </ItemDef>

      <ItemDef OID="SEX" Name="SEX" DataType="text" Length="1" mdsol:ControlType="RadioButton">
        <Question>
          <TranslatedText xml:lang="en">Biological Sex</TranslatedText>
        </Question>
        <CodeListRef CodeListOID="CL_SEX"/>
      </ItemDef>

      <ItemDef OID="WEIGHT" Name="WEIGHT" DataType="float" SignificantDigits="1">
        <Question>
          <TranslatedText xml:lang="en">Weight in kg</TranslatedText>
        </Question>
      </ItemDef>

      <ItemDef OID="SYSBP" Name="SYSBP" DataType="integer" Length="3">
        <Question>
          <TranslatedText xml:lang="en">Systolic Blood Pressure</TranslatedText>
        </Question>
      </ItemDef>

      <CodeList OID="CL_SEX" Name="Sex CodeList" DataType="text">
        <CodeListItem CodedValue="M" OrderNumber="1">
          <Decode>
            <TranslatedText xml:lang="en">Male</TranslatedText>
          </Decode>
        </CodeListItem>
        <CodeListItem CodedValue="F" OrderNumber="2">
          <Decode>
            <TranslatedText xml:lang="en">Female</TranslatedText>
          </Decode>
        </CodeListItem>
        <EnumeratedItem CodedValue="U" OrderNumber="3"/>
      </CodeList>
    </MetaDataVersion>
  </Study>
</ODM>
"""


def test_load_from_cdisc_odm_basic(tmp_path: Path) -> None:
    """Test loading valid CDISC ODM 1.3 XML from string buffer and file path."""
    # From string buffer
    dd = load_from_cdisc_odm(SAMPLE_ODM_XML)
    assert isinstance(dd, DataDictionary)
    assert dd.metadata.studyKey == "STUDY_MEDIDATA_RAVE_001"
    assert dd.metadata.source == "cdisc_odm"
    assert len(dd.forms) == 2

    # Verify Form DM
    dm = next(f for f in dd.forms if f.formKey == "DM")
    assert dm.formName == "Demographics"
    assert len(dm.variables) == 3

    # Verify Variable BRTHDAT
    v_dob = next(v for v in dm.variables if v.variableName == "BRTHDAT")
    assert v_dob.label == "Date of Birth"
    assert v_dob.variableType == "Date"
    assert v_dob.required == "Yes"
    assert v_dob.format == "10"

    # Verify Variable SEX with choices
    v_sex = next(v for v in dm.variables if v.variableName == "SEX")
    assert v_sex.label == "Biological Sex"
    assert v_sex.variableType == "Radio"
    assert len(v_sex.choices) == 3
    assert v_sex.choices[0].choiceValue == "M"
    assert v_sex.choices[0].choiceText == "Male"
    assert v_sex.choices[0].position == 1
    assert v_sex.choices[1].choiceValue == "F"
    assert v_sex.choices[1].choiceText == "Female"
    assert v_sex.choices[1].position == 2
    assert v_sex.choices[2].choiceValue == "U"
    assert v_sex.choices[2].choiceText == "U"
    assert v_sex.choices[2].position == 3

    # Verify Variable WEIGHT
    v_weight = next(v for v in dm.variables if v.variableName == "WEIGHT")
    assert v_weight.variableType == "Float"
    assert v_weight.precision == "1"
    assert v_weight.required == "No"

    # Verify Form VS
    vs = next(f for f in dd.forms if f.formKey == "VS")
    assert vs.formName == "Vital Signs"
    assert len(vs.variables) == 1
    v_sysbp = vs.variables[0]
    assert v_sysbp.variableName == "SYSBP"
    assert v_sysbp.variableType == "Integer"

    # Verify loading from File Path
    xml_file = tmp_path / "study.xml"
    xml_file.write_text(SAMPLE_ODM_XML, encoding="utf-8")
    dd_file = load_from_cdisc_odm(xml_file)
    assert dd_file == dd

    # Verify validation against canonical schema
    schema_errors = validate(dd, schema=get_schema())
    assert schema_errors == []


def test_load_from_cdisc_odm_streams() -> None:
    """Test loading from io.StringIO and io.BytesIO streams."""
    sio = io.StringIO(SAMPLE_ODM_XML)
    dd_str = load_from_cdisc_odm(sio)
    assert len(dd_str.forms) == 2

    bio = io.BytesIO(SAMPLE_ODM_XML.encode("utf-8"))
    dd_bytes = load_from_cdisc_odm(bio)
    assert len(dd_bytes.forms) == 2


def test_load_from_cdisc_odm_study_key_override() -> None:
    """Test overriding studyKey via argument."""
    dd = load_from_cdisc_odm(SAMPLE_ODM_XML, study_key="CUSTOM_KEY_123")
    assert dd.metadata.studyKey == "CUSTOM_KEY_123"


def test_load_from_cdisc_odm_unsupported_type_and_unmapped_attributes() -> None:
    """Test that unsupported types and unmapped attributes populate metadata warnings."""
    xml_custom = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="S1">
    <MetaDataVersion OID="MDV1">
      <FormDef OID="F1" Name="Form 1" customFormAttr="extraValue">
        <ItemGroupRef ItemGroupOID="IG1" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG1" Name="Group 1">
        <ItemRef ItemOID="IT1" OrderNumber="1"/>
      </ItemGroupDef>
      <ItemDef OID="IT1" Name="Item 1" DataType="geometric_polygon" customItemProp="propValue"/>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    # Non-strict mode should record warnings and default to Text
    dd = load_from_cdisc_odm(xml_custom, strict=False)
    assert len(dd.metadata.validationWarnings) >= 2
    assert any("geometric_polygon" in w for w in dd.metadata.validationWarnings)
    assert any("customFormAttr" in w for w in dd.metadata.validationWarnings)
    var = dd.forms[0].variables[0]
    assert var.variableType == "Text"
    assert dd.forms[0].extraAttributes.get("customFormAttr") == "extraValue"
    assert var.extraAttributes.get("customItemProp") == "propValue"

    # Strict mode should raise ValueError
    with pytest.raises(ValueError, match="Strict loading error"):
        load_from_cdisc_odm(xml_custom, strict=True)


def test_load_from_cdisc_odm_sdtm_attributes() -> None:
    """Test preserving SDTM attributes on forms and variables without spurious warnings."""
    xml_sdtm = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="STUDY_SDTM">
    <MetaDataVersion OID="MDV_SDTM" Name="SDTM Study">
      <FormDef OID="DM" Name="Demographics">
        <ItemGroupRef ItemGroupOID="IG_DM" OrderNumber="1"/>
        <ItemGroupRef ItemGroupOID="IG_SUPP" OrderNumber="2"/>
      </FormDef>
      <FormDef OID="DIRECT_FORM" Name="Direct Form">
        <ItemRef ItemOID="DIRECT_ITEM" OrderNumber="1"/>
      </FormDef>

      <ItemGroupDef OID="IG_DM" Name="Demographics Group" Domain="DM" SASDatasetName="DM">
        <ItemRef ItemOID="SUBJID" OrderNumber="1"/>
        <ItemRef ItemOID="AGE" OrderNumber="2"/>
      </ItemGroupDef>
      <ItemGroupDef OID="IG_SUPP" Name="Supplemental Group">
        <ItemRef ItemOID="SUPPQUAL" OrderNumber="1"/>
      </ItemGroupDef>

      <ItemDef OID="SUBJID" Name="Subject ID" DataType="text"
               SDSVarName="SUBJID" SASFieldName="SUBJID" Origin="CRF" Comment="Subject Identifier"/>
      <ItemDef OID="AGE" Name="Age" DataType="integer">
        <Comment><TranslatedText>Age in years</TranslatedText></Comment>
      </ItemDef>
      <ItemDef OID="SUPPQUAL" Name="Supplemental Qual" DataType="text"/>
      <ItemDef OID="DIRECT_ITEM" Name="Direct Item" DataType="text"
               SDSVarName="DIRVAR" SASFieldName="DIRFIELD" Origin="eCOA" Comment="Direct comment"/>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(xml_sdtm, strict=True)
    # Zero warnings should be emitted for known SDTM attributes
    assert dd.metadata.validationWarnings == []

    form_dm = next(f for f in dd.forms if f.formKey == "DM")
    # Form should have Domain and SASDatasetName preserved from ItemGroupDef
    assert form_dm.extraAttributes.get("Domain") == "DM"
    assert form_dm.extraAttributes.get("SASDatasetName") == "DM"

    # Variable with all SDTM attributes set
    v_subjid = next(v for v in form_dm.variables if v.variableName == "SUBJID")
    assert v_subjid.extraAttributes.get("Domain") == "DM"
    assert v_subjid.extraAttributes.get("SASDatasetName") == "DM"
    assert v_subjid.extraAttributes.get("SDSVarName") == "SUBJID"
    assert v_subjid.extraAttributes.get("SASFieldName") == "SUBJID"
    assert v_subjid.extraAttributes.get("Origin") == "CRF"
    assert v_subjid.extraAttributes.get("Comment") == "Subject Identifier"

    # Variable lacking SDSVarName and SASFieldName: keys must be omitted
    v_age = next(v for v in form_dm.variables if v.variableName == "AGE")
    assert v_age.extraAttributes.get("Domain") == "DM"
    assert v_age.extraAttributes.get("SASDatasetName") == "DM"
    assert "SDSVarName" not in v_age.extraAttributes
    assert "SASFieldName" not in v_age.extraAttributes
    assert "Origin" not in v_age.extraAttributes
    assert v_age.extraAttributes.get("Comment") == "Age in years"

    # ItemGroup without Domain should not overwrite existing form attributes
    v_supp = next(v for v in form_dm.variables if v.variableName == "SUPPQUAL")
    assert "Domain" not in v_supp.extraAttributes
    assert "SASDatasetName" not in v_supp.extraAttributes
    assert form_dm.extraAttributes.get("Domain") == "DM"

    # Direct ItemRef on FormDef should also preserve SDTM attributes
    form_direct = next(f for f in dd.forms if f.formKey == "DIRECT_FORM")
    v_dir = form_direct.variables[0]
    assert v_dir.extraAttributes.get("SDSVarName") == "DIRVAR"
    assert v_dir.extraAttributes.get("SASFieldName") == "DIRFIELD"
    assert v_dir.extraAttributes.get("Origin") == "eCOA"
    assert v_dir.extraAttributes.get("Comment") == "Direct comment"


def test_load_from_cdisc_odm_direct_item_refs_unmapped_and_sdtm() -> None:
    """Test direct ItemRef with custom unmapped attributes and SDTM attributes."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="S3">
    <MetaDataVersion OID="MDV3">
      <FormDef OID="F3" Name="Direct SDTM Form">
        <ItemRef ItemOID="IT_DIR" OrderNumber="1" customRefAttr="refVal"/>
      </FormDef>
      <ItemDef OID="IT_DIR" Name="IT_DIR" DataType="text"
               SDSVarName="VAR_D" SASFieldName="SAS_D" Origin="Source" Comment="Direct Item Comment"
               customItemProp="itemPropVal"/>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(xml, strict=False)
    var = dd.forms[0].variables[0]
    assert var.extraAttributes.get("SDSVarName") == "VAR_D"
    assert var.extraAttributes.get("SASFieldName") == "SAS_D"
    assert var.extraAttributes.get("Origin") == "Source"
    assert var.extraAttributes.get("Comment") == "Direct Item Comment"
    assert var.extraAttributes.get("customRefAttr") == "refVal"
    assert var.extraAttributes.get("customItemProp") == "itemPropVal"


def test_load_from_cdisc_odm_missing_references() -> None:
    """Test handling of broken references (missing CodeListRef or ItemGroupRef)."""
    xml_broken = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="S1">
    <MetaDataVersion OID="MDV1">
      <FormDef OID="F1" Name="Form 1">
        <ItemGroupRef ItemGroupOID="MISSING_IG" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG2">
        <ItemRef ItemOID="MISSING_ITEM"/>
      </ItemGroupDef>
      <ItemDef OID="IT1" DataType="text">
        <CodeListRef CodeListOID="MISSING_CL"/>
      </ItemDef>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(xml_broken, strict=False)
    assert len(dd.metadata.validationWarnings) >= 1
    assert any("MISSING_IG" in w for w in dd.metadata.validationWarnings)

    with pytest.raises(ValueError, match="Strict loading error"):
        load_from_cdisc_odm(xml_broken, strict=True)


def test_load_from_cdisc_odm_malicious_entity_expansion() -> None:
    """Test defusedxml entity expansion prevention."""
    xml_bomb = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ELEMENT lolz (#PCDATA)>
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="&lol2;"/>
</ODM>
"""
    with pytest.raises(DefusedXmlException):
        load_from_cdisc_odm(xml_bomb)


def test_load_from_cdisc_odm_direct_item_refs_and_types() -> None:
    """Test direct ItemRef on FormDef, control types, boolean, and time types."""
    xml_direct = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="S2">
    <MetaDataVersion OID="MDV2">
      <FormDef OID="F2" Name="Direct Form">
        <ItemRef ItemOID="DIRECT_ITEM" OrderNumber="1" Mandatory="Yes"/>
        <ItemRef ItemOID="BOOL_ITEM" OrderNumber="2"/>
        <ItemRef ItemOID="TIME_ITEM" OrderNumber="3"/>
        <ItemRef ItemOID="SELECT_ITEM" OrderNumber="4" ControlType="dropdown"/>
        <ItemRef ItemOID="CHECKBOX_ITEM" OrderNumber="5" ControlType="checkbox"/>
      </FormDef>
      <ItemDef OID="DIRECT_ITEM" Name="DIRECT_ITEM" DataType="text">
        <Description><TranslatedText>Direct question prompt</TranslatedText></Description>
      </ItemDef>
      <ItemDef OID="BOOL_ITEM" Name="BOOL_ITEM" DataType="boolean">
        <Question>Simple Boolean</Question>
      </ItemDef>
      <ItemDef OID="TIME_ITEM" Name="TIME_ITEM" DataType="time"/>
      <ItemDef OID="SELECT_ITEM" Name="SELECT_ITEM" DataType="text">
        <CodeListRef CodeListOID="CL_SAMPLE"/>
      </ItemDef>
      <ItemDef OID="CHECKBOX_ITEM" Name="CHECKBOX_ITEM" DataType="text">
        <CodeListRef CodeListOID="CL_SAMPLE"/>
      </ItemDef>
      <CodeList OID="CL_SAMPLE" Name="Sample">
        <CodeListItem CodedValue="A">
          <Decode><TranslatedText>Choice A</TranslatedText></Decode>
        </CodeListItem>
      </CodeList>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(xml_direct)
    assert len(dd.forms) == 1
    form = dd.forms[0]
    assert len(form.variables) == 5

    v_dir = form.variables[0]
    assert v_dir.variableName == "DIRECT_ITEM"
    assert v_dir.label == "Direct question prompt"
    assert v_dir.required == "Yes"

    v_bool = form.variables[1]
    assert v_bool.variableType == "Boolean"
    assert v_bool.label == "Simple Boolean"

    v_time = form.variables[2]
    assert v_time.variableType == "Time"

    v_sel = form.variables[3]
    assert v_sel.variableType == "Single Select"

    v_chk = form.variables[4]
    assert v_chk.variableType == "Multi Select"


def test_load_from_cdisc_odm_invalid_root_and_type_errors() -> None:
    """Test invalid XML, non-ODM root, and type errors."""
    with pytest.raises(TypeError, match="Unsupported source type"):
        load_from_cdisc_odm(12345)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Failed to parse CDISC ODM XML"):
        load_from_cdisc_odm("not valid xml <<<>>>")

    # Non-ODM root warning
    xml_non_odm = "<ClinicalData><Study OID='S1'/></ClinicalData>"
    dd = load_from_cdisc_odm(xml_non_odm)
    assert any("expected 'ODM'" in w for w in dd.metadata.validationWarnings)

    # Empty document without MetaDataVersion
    xml_empty = "<ODM><Study OID='S1'></Study></ODM>"
    dd_empty = load_from_cdisc_odm(xml_empty)
    assert any(
        "No 'MetaDataVersion' element found" in w for w in dd_empty.metadata.validationWarnings
    )


def test_load_from_cdisc_odm_missing_file() -> None:
    """Test that missing XML file paths raise FileNotFoundError instead of XML parse errors."""
    for missing in [
        "nonexistent.xml",
        "nested/path/to/missing.xml",
        Path("nonexistent.xml"),
        "some/path/nonexistent",
    ]:
        with pytest.raises(FileNotFoundError, match="Target file .* does not exist"):
            load_from_cdisc_odm(missing)


def test_load_from_cdisc_odm_xml_string_bypass_file_check() -> None:
    """Test that raw XML strings bypass file target checks even if containing slashes."""
    single_line_xml = (
        '<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">'
        '<Study OID="S1">'
        '<MetaDataVersion OID="M1">'
        '<FormDef OID="F1" Name="Form1"/>'
        "</MetaDataVersion>"
        "</Study>"
        "</ODM>"
    )
    dd = load_from_cdisc_odm(single_line_xml)
    assert len(dd.forms) == 1
    assert dd.forms[0].formKey == "F1"


FULL_ODM_13_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     ODMVersion="1.3"
     FileType="Snapshot"
     FileOID="FULL_ODM_13"
     CreationDateTime="2026-06-01T10:00:00">
  <Study OID="STUDY_ODM_FULL">
    <GlobalVariables>
      <StudyName>Cardiology Trial</StudyName>
      <StudyDescription>Phase III Study</StudyDescription>
      <ProtocolName>CARDIO-301</ProtocolName>
    </GlobalVariables>
    <BasicDefinitions>
      <MeasurementUnit OID="MU.KG" Name="Kilograms">
        <Symbol><TranslatedText xml:lang="en">kg</TranslatedText></Symbol>
      </MeasurementUnit>
      <MeasurementUnit OID="MU.MMHG" Name="Millimeters of Mercury">
        <Symbol><TranslatedText xml:lang="en">mmHg</TranslatedText></Symbol>
      </MeasurementUnit>
    </BasicDefinitions>
    <MetaDataVersion OID="MDV.301" Name="v1.0">
      <Protocol>
        <Description><TranslatedText xml:lang="en">Standard Visit Schedule</TranslatedText></Description>
        <StudyEventRef StudyEventOID="SE.SCREEN" OrderNumber="1" Mandatory="Yes"/>
        <StudyEventRef StudyEventOID="SE.FOLLOWUP" OrderNumber="2" Mandatory="No"
                       CollectionExceptionConditionOID="COND.FOLLOWUP"/>
      </Protocol>

      <StudyEventDef OID="SE.SCREEN" Name="Screening Visit" Repeating="No" Type="Scheduled" Category="Baseline">
        <Description><TranslatedText xml:lang="en">Initial screening visit</TranslatedText></Description>
        <FormRef FormOID="VS" OrderNumber="1" Mandatory="Yes"/>
      </StudyEventDef>
      <StudyEventDef OID="SE.FOLLOWUP" Name="Follow-up Visit" Repeating="Yes" Type="Unscheduled">
        <FormRef FormOID="VS" OrderNumber="1" Mandatory="No"/>
      </StudyEventDef>

      <FormDef OID="VS" Name="Vital Signs" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG_VS" OrderNumber="1" Mandatory="Yes"/>
      </FormDef>

      <ItemGroupDef OID="IG_VS" Name="Vital Signs Group" Domain="VS" SASDatasetName="VS">
        <ItemRef ItemOID="WEIGHT" OrderNumber="1" Mandatory="Yes"/>
        <ItemRef ItemOID="SYSBP" OrderNumber="2" Mandatory="No"
                 CollectionExceptionConditionOID="COND.SYSBP"/>
        <ItemRef ItemOID="MAP" OrderNumber="3" Mandatory="No" MethodOID="METH.MAP"/>
      </ItemGroupDef>

      <ItemDef OID="WEIGHT" Name="WEIGHT" DataType="float" SignificantDigits="1">
        <Question><TranslatedText xml:lang="en">Body Weight</TranslatedText></Question>
        <MeasurementUnitRef MeasurementUnitOID="MU.KG"/>
        <RangeCheck Comparator="GE" SoftHard="Soft">
          <CheckValue>30</CheckValue>
          <MeasurementUnitRef MeasurementUnitOID="MU.KG"/>
          <ErrorMessage><TranslatedText xml:lang="en">Weight below expected minimum</TranslatedText></ErrorMessage>
        </RangeCheck>
      </ItemDef>

      <ItemDef OID="SYSBP" Name="SYSBP" DataType="integer" Length="3">
        <Question><TranslatedText xml:lang="en">Systolic BP</TranslatedText></Question>
        <MeasurementUnitRef MeasurementUnitOID="MU.MMHG"/>
        <RangeCheck Comparator="IN" SoftHard="Hard">
          <CheckValue>90</CheckValue>
          <CheckValue>120</CheckValue>
          <CheckValue>140</CheckValue>
        </RangeCheck>
        <RangeCheck SoftHard="Soft">
          <FormalExpression Context="XPath">../SYSBP &gt; 70</FormalExpression>
        </RangeCheck>
      </ItemDef>

      <ItemDef OID="MAP" Name="MAP" DataType="float" SignificantDigits="1">
        <Question><TranslatedText xml:lang="en">Mean Arterial Pressure</TranslatedText></Question>
        <MeasurementUnitRef MeasurementUnitOID="MU.MMHG"/>
      </ItemDef>

      <ConditionDef OID="COND.SYSBP" Name="Seated Position Condition">
        <Description><TranslatedText xml:lang="en">Collect when subject is seated</TranslatedText></Description>
        <FormalExpression Context="XPath">../POS = 'SEATED'</FormalExpression>
        <Alias Context="SDTM" Name="VSPOS_COND"/>
      </ConditionDef>

      <ConditionDef OID="COND.FOLLOWUP" Name="Follow-up Required Condition">
        <Description><TranslatedText xml:lang="en">Required if randomized</TranslatedText></Description>
        <FormalExpression Context="XPath">//ItemData[@ItemOID='RAND'] = 'Y'</FormalExpression>
      </ConditionDef>

      <MethodDef OID="METH.MAP" Name="Compute MAP" Type="Computation">
        <Description><TranslatedText xml:lang="en">Calculate Mean Arterial Pressure</TranslatedText></Description>
        <FormalExpression Context="XPath">(2 * ../DIABP + ../SYSBP) div 3</FormalExpression>
        <Alias Context="Algorithm" Name="MAP_V1"/>
      </MethodDef>
    </MetaDataVersion>
  </Study>
</ODM>
"""


def test_load_from_cdisc_odm_full_1_3_fidelity_and_round_trip() -> None:
    """Protocol, StudyEventDef, MeasurementUnit, RangeCheck, ConditionDef, and MethodDef round-trip."""
    from imednet.datadict.emit import to_json, to_odm_xml

    dd = load_from_cdisc_odm(FULL_ODM_13_XML, strict=True)
    assert dd.metadata.validationWarnings == []
    assert validate(dd, schema=get_schema()) == []

    # 1. Study metadata: ProtocolName, Protocol, StudyEventDef visits, MeasurementUnits
    extra_meta = dd.metadata.extraAttributes
    assert extra_meta["studyName"] == "Cardiology Trial"
    assert extra_meta["studyDescription"] == "Phase III Study"
    assert extra_meta["protocolName"] == "CARDIO-301"
    assert extra_meta["protocol"]["description"] == "Standard Visit Schedule"
    assert len(extra_meta["protocol"]["studyEventRefs"]) == 2
    assert extra_meta["protocol"]["studyEventRefs"][1]["collectionExceptionConditionOid"] == (
        "COND.FOLLOWUP"
    )
    assert len(extra_meta["visits"]) == 2
    assert extra_meta["visits"][0]["oid"] == "SE.SCREEN"
    assert extra_meta["visits"][0]["category"] == "Baseline"
    assert extra_meta["visits"][0]["description"] == "Initial screening visit"
    assert len(extra_meta["measurementUnits"]) == 2

    # 2. Form schedule and normalized defaults
    vs_form = dd.forms[0]
    assert vs_form.formId is None
    assert vs_form.formType == "Subject"
    assert len(vs_form.extraAttributes["visits"]) == 2
    assert [v["oid"] for v in vs_form.extraAttributes["visits"]] == ["SE.SCREEN", "SE.FOLLOWUP"]

    # 3. Variable MeasurementUnits and RangeChecks
    v_weight = next(v for v in vs_form.variables if v.variableName == "WEIGHT")
    assert v_weight.extraAttributes["measurementUnits"] == [
        {"oid": "MU.KG", "name": "Kilograms", "symbol": "kg"}
    ]
    assert v_weight.extraAttributes["rangeChecks"] == [
        {
            "comparator": "GE",
            "softHard": "Soft",
            "checkValue": "30",
            "measurementUnitOid": "MU.KG",
            "errorMessage": "Weight below expected minimum",
        }
    ]

    v_sysbp = next(v for v in vs_form.variables if v.variableName == "SYSBP")
    assert v_sysbp.extraAttributes["collectionExceptionConditionOid"] == "COND.SYSBP"
    assert v_sysbp.extraAttributes["rangeChecks"][0]["checkValues"] == ["90", "120", "140"]
    assert v_sysbp.extraAttributes["rangeChecks"][1]["formalExpression"] == "../SYSBP > 70"

    v_map = next(v for v in vs_form.variables if v.variableName == "MAP")
    assert v_map.extraAttributes["methodOid"] == "METH.MAP"

    # 4. ConditionDef and MethodDef mapped into BusinessLogicRule entries
    form_rule_ids = {r.id: r for r in vs_form.businessLogic}
    assert "COND.SYSBP" in form_rule_ids
    assert "METH.MAP" in form_rule_ids
    assert form_rule_ids["METH.MAP"].logic["methodType"] == "Computation"
    assert form_rule_ids["METH.MAP"].logic["formalExpressions"] == [
        {"context": "XPath", "expression": "(2 * ../DIABP + ../SYSBP) div 3"}
    ]
    assert form_rule_ids["METH.MAP"].logicRawXml is not None

    unassigned_ids = {r.id: r for r in dd.unassignedBusinessLogic}
    assert "COND.FOLLOWUP" in unassigned_ids
    assert unassigned_ids["COND.FOLLOWUP"].type == "Condition"

    # Verify no_raw_xml=True omits logicRawXml
    dd_no_raw = load_from_cdisc_odm(FULL_ODM_13_XML, strict=True, no_raw_xml=True)
    assert all(r.logicRawXml is None for r in dd_no_raw.forms[0].businessLogic)
    assert all(r.logicRawXml is None for r in dd_no_raw.unassignedBusinessLogic)

    # Canonical JSON validates cleanly
    json_payload = to_json(dd)
    assert "CARDIO-301" in json_payload

    # Round-trip through to_odm_xml preserves all six constructs
    re_emitted_xml = to_odm_xml(dd)
    dd_reloaded = load_from_cdisc_odm(re_emitted_xml, strict=True)
    assert dd_reloaded.metadata.extraAttributes["protocolName"] == "CARDIO-301"
    assert len(dd_reloaded.metadata.extraAttributes["protocol"]["studyEventRefs"]) == 2
    assert len(dd_reloaded.metadata.extraAttributes["visits"]) == 2
    assert len(dd_reloaded.metadata.extraAttributes["measurementUnits"]) == 2
    assert len(dd_reloaded.forms[0].businessLogic) == 2
    assert len(dd_reloaded.unassignedBusinessLogic) == 1


def test_load_from_cdisc_odm_broken_schedule_unit_and_rule_references() -> None:
    """Broken references in Protocol, StudyEventDef, MeasurementUnitRef, and ItemRef warn/error."""
    xml_missing_refs = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="S_BROKEN">
    <MetaDataVersion OID="MDV1">
      <Protocol>
        <StudyEventRef StudyEventOID="SE_MISSING" OrderNumber="1"
                       CollectionExceptionConditionOID="COND_MISSING_PROTO"/>
      </Protocol>
      <StudyEventDef OID="SE_VALID" Name="Visit 1" Repeating="No" Type="Scheduled">
        <FormRef FormOID="FORM_MISSING" OrderNumber="1"
                 CollectionExceptionConditionOID="COND_MISSING_FORM"/>
      </StudyEventDef>
      <FormDef OID="F1" Name="Form 1">
        <ItemGroupRef ItemGroupOID="IG1" OrderNumber="1"
                      CollectionExceptionConditionOID="COND_MISSING_IG"/>
      </FormDef>
      <ItemGroupDef OID="IG1" Name="Group 1">
        <ItemRef ItemOID="IT1" OrderNumber="1"
                 MethodOID="METH_MISSING"
                 CollectionExceptionConditionOID="COND_MISSING_ITEM"/>
      </ItemGroupDef>
      <ItemDef OID="IT1" Name="IT1" DataType="integer">
        <MeasurementUnitRef MeasurementUnitOID="MU_MISSING"/>
        <RangeCheck Comparator="GT" SoftHard="Soft">
          <CheckValue>0</CheckValue>
          <MeasurementUnitRef MeasurementUnitOID="MU_RC_MISSING"/>
        </RangeCheck>
      </ItemDef>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(xml_missing_refs, strict=False)
    warnings_joined = "\n".join(dd.metadata.validationWarnings)
    for expected_token in (
        "SE_MISSING",
        "COND_MISSING_PROTO",
        "FORM_MISSING",
        "COND_MISSING_FORM",
        "COND_MISSING_IG",
        "METH_MISSING",
        "COND_MISSING_ITEM",
        "MU_MISSING",
        "MU_RC_MISSING",
    ):
        assert expected_token in warnings_joined

    with pytest.raises(ValueError, match="Strict loading error"):
        load_from_cdisc_odm(xml_missing_refs, strict=True)


def test_load_from_cdisc_odm_preserves_sibling_raw_and_scoped_names() -> None:
    """Sibling items on the same Form do not collapse when one OID is raw F1.AGE."""
    xml_sibling_collision = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="S_SIBLING">
    <MetaDataVersion OID="MDV1">
      <FormDef OID="F1" Name="Form 1">
        <ItemGroupRef ItemGroupOID="IG_F1" OrderNumber="1"/>
      </FormDef>
      <FormDef OID="F2" Name="Form 2">
        <ItemGroupRef ItemGroupOID="IG_F2" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG_F1" Name="Group F1">
        <ItemRef ItemOID="IT.F1.AGE" OrderNumber="1"/>
        <ItemRef ItemOID="F1.AGE" OrderNumber="2"/>
      </ItemGroupDef>
      <ItemGroupDef OID="IG_F2" Name="Group F2">
        <ItemRef ItemOID="AGE" OrderNumber="1"/>
        <ItemRef ItemOID="F2.AGE" OrderNumber="2"/>
      </ItemGroupDef>
      <ItemDef OID="IT.F1.AGE" Name="AGE" DataType="integer"/>
      <ItemDef OID="F1.AGE" Name="F1.AGE" DataType="text"/>
      <ItemDef OID="AGE" Name="AGE" DataType="float"/>
      <ItemDef OID="F2.AGE" Name="F2.AGE" DataType="date"/>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(xml_sibling_collision, strict=True)
    assert dd.metadata.validationWarnings == []
    forms_by_key = {f.formKey: f for f in dd.forms}
    assert [(v.variableName, v.variableType) for v in forms_by_key["F1"].variables] == [
        ("AGE", "Integer"),
        ("F1.AGE", "Text"),
    ]
    assert [(v.variableName, v.variableType) for v in forms_by_key["F2"].variables] == [
        ("AGE", "Float"),
        ("F2.AGE", "Date"),
    ]


def test_load_from_cdisc_odm_unwraps_nested_scoped_oids_without_vendor_extension() -> None:
    """Nested scoped OIDs (F1.AGE -> AGE and F1.F1.AGE -> F1.AGE) unwrap cleanly without colliding."""
    xml_nested_scoped = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="S_NESTED">
    <MetaDataVersion OID="MDV1">
      <FormDef OID="F1" Name="Form 1">
        <ItemGroupRef ItemGroupOID="IG_F1" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG_F1" Name="Group F1">
        <ItemRef ItemOID="F1.AGE" OrderNumber="1"/>
        <ItemRef ItemOID="F1.F1.AGE" OrderNumber="2"/>
      </ItemGroupDef>
      <ItemDef OID="F1.AGE" Name="AGE" DataType="integer"/>
      <ItemDef OID="F1.F1.AGE" Name="F1.AGE" DataType="text"/>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(xml_nested_scoped, strict=True)
    assert dd.metadata.validationWarnings == []
    assert [(v.variableName, v.variableType) for v in dd.forms[0].variables] == [
        ("AGE", "Integer"),
        ("F1.AGE", "Text"),
    ]


def test_load_from_cdisc_odm_strict_rejects_unrecognized_third_party_attributes() -> None:
    """Unrecognized third-party namespace attributes warn in default mode and fail in strict mode."""
    xml_third_party = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     xmlns:acme="http://acme.example/ns/odm">
  <Study OID="S_VENDOR">
    <MetaDataVersion OID="MDV1">
      <FormDef OID="DM" Name="Demographics">
        <ItemGroupRef ItemGroupOID="IG_DM" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG_DM" Name="Demographics Group">
        <ItemRef ItemOID="VISDAT" OrderNumber="1" Mandatory="Yes" acme:RequiredPolicy="VendorOverride"/>
      </ItemGroupDef>
      <ItemDef OID="VISDAT" Name="Visit Date" DataType="date" acme:VariableName="HIJACKED_NAME"/>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    with pytest.raises(ValueError, match="Strict loading error: Unmapped attribute"):
        load_from_cdisc_odm(xml_third_party, strict=True)

    dd = load_from_cdisc_odm(xml_third_party, strict=False)
    assert len(dd.metadata.validationWarnings) == 2
    var = dd.forms[0].variables[0]
    assert var.variableName == "VISDAT"
    assert var.required == "Yes"
    assert var.extraAttributes.get("requiredPolicy") == "VendorOverride"
    assert var.extraAttributes.get("variableName") == "HIJACKED_NAME"

    xml_unknown_imednet = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     xmlns:imednet="http://www.imednet.com/ns/odm/metadata">
  <Study OID="S_IMEDNET_UNKNOWN">
    <MetaDataVersion OID="MDV1">
      <FormDef OID="DM" Name="Demographics">
        <ItemGroupRef ItemGroupOID="IG_DM" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG_DM" Name="Demographics Group">
        <ItemRef ItemOID="VISDAT" OrderNumber="1" Mandatory="No" RequiredPolicy="Unqualified"/>
      </ItemGroupDef>
      <ItemDef OID="VISDAT" Name="VISDAT" DataType="date" imednet:UnsupportedExtension="1"/>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    with pytest.raises(ValueError, match="Strict loading error: Unmapped attribute"):
        load_from_cdisc_odm(xml_unknown_imednet, strict=True)


def test_load_from_cdisc_odm_preserves_external_unprefixed_oids_over_display_names() -> None:
    """External CDISC ODM files preserve unprefixed ItemDef OIDs as variableName."""
    external_odm = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="STUDY_EXT">
    <MetaDataVersion OID="MDV1">
      <FormDef OID="DM" Name="Demographics">
        <ItemGroupRef ItemGroupOID="IG_DM" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG_DM" Name="Demographics Group">
        <ItemRef ItemOID="VISDAT" OrderNumber="1" Mandatory="Yes"/>
        <ItemRef ItemOID="IT.BRTHDAT" OrderNumber="2" Mandatory="No"/>
      </ItemGroupDef>
      <ItemDef OID="VISDAT" Name="Visit Date Prompt" DataType="date"/>
      <ItemDef OID="IT.BRTHDAT" Name="Birth Date Prompt" DataType="date"/>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(external_odm, strict=True)
    assert dd.metadata.validationWarnings == []
    assert [(v.variableName, v.label) for v in dd.forms[0].variables] == [
        ("VISDAT", "Visit Date Prompt"),
        ("IT.BRTHDAT", "Birth Date Prompt"),
    ]
