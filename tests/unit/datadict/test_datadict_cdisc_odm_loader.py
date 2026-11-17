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
