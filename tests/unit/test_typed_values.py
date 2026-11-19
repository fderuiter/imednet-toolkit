"""Unit tests for typed values."""

from imednet.testing import typed_values


def test_value_for_each_type() -> None:
    """Test that value for each type."""
    assert typed_values.value_for("text") == "example"
    assert typed_values.value_for("date") == "2024-01-01"
    assert typed_values.value_for("integer") == 1
    assert typed_values.value_for("radio") == "1"
    assert typed_values.value_for("dropdown") == "1"
    assert typed_values.value_for("memo") == "example memo"
    assert typed_values.value_for("checkbox") is True


def test_canonical_type_synonyms() -> None:
    """Test that canonical type synonyms."""
    assert typed_values.canonical_type("Text") == "string"
    assert typed_values.canonical_type("int") == "number"


def test_typed_values_canonical_taxonomy_and_loader_parity() -> None:
    """Every canonical, loader-emitted, and fake_data variable type validates with its sample value."""
    import io

    from imednet.datadict import load_from_cdisc_odm, load_from_csv_files, load_from_redcap
    from imednet.datadict.loaders.api import load_from_api
    from imednet.datadict.normalize import _CANONICAL_VARIABLE_TYPES, normalize_variable_type
    from imednet.models.forms import Form as ApiForm
    from imednet.models.variables import Variable as ApiVariable
    from imednet.testing import fake_data
    from imednet.validation.cache import (
        _TYPE_VALIDATORS,
        SchemaCache,
        validate_record_data,
        validate_record_entry,
    )

    # 1. Every pre-indexed validator type key resolves in typed_values and passes SchemaCache
    for idx, type_name in enumerate(_TYPE_VALIDATORS):
        canon = typed_values.canonical_type(type_name)
        sample = typed_values.value_for(type_name)
        assert canon is not None, f"Missing canonical_type for {type_name!r}"
        assert sample is not None, f"Missing value_for for {type_name!r}"

        cache = SchemaCache()
        cache.populate(
            [
                ApiVariable(
                    variable_name=f"V_{idx}",
                    variable_type=type_name,
                    form_id=1,
                    form_key="F_TAX",
                )
            ]
        )
        validate_record_data(cache, "F_TAX", {f"V_{idx}": sample})

        generated = fake_data.fake_record(cache)
        validate_record_entry(cache, generated)

    # 2. Normalization of whitespace and casing in typed_values, plus None for unknown types
    assert typed_values.canonical_type("  Single   Select  ") == "single select"
    assert typed_values.canonical_type("Date / Time Precision") == "date/time precision"
    assert typed_values.value_for("  Yes / No  ") == "1"
    assert typed_values.canonical_type("unknown_widget") is None
    assert typed_values.value_for("unknown_widget") is None

    # 3. Every type emitted by normalize_variable_type, REDCap, CDISC ODM, CSV export, and API loaders
    redcap_csv = (
        "Variable / Field Name,Form Name,Field Type,Field Label,"
        '"Choices, Calculations, or Slider Labels",Text Validation Type OR Show Slider Number\n'
        "v_text,f1,text,Text,,\n"
        "v_int,f1,text,Int,,integer\n"
        "v_float,f1,text,Float,,number\n"
        "v_date,f1,text,Date,,date_ymd\n"
        "v_dt,f1,text,DT,,datetime_ymd\n"
        "v_time,f1,text,Time,,time\n"
        "v_notes,f1,notes,Notes,,\n"
        "v_calc,f1,calc,Calc,[v_int]*2,\n"
        'v_drop,f1,dropdown,Drop,"1, One | 2, Two",\n'
        'v_radio,f1,radio,Radio,"1, One | 2, Two",\n'
        'v_check,f1,checkbox,Check,"1, One | 2, Two",\n'
        "v_yesno,f1,yesno,YesNo,,\n"
        "v_tf,f1,truefalse,TF,,\n"
        "v_file,f1,file,File,,\n"
        "v_slider,f1,slider,Slider,,\n"
        "v_desc,f1,descriptive,Desc,,\n"
        "v_sql,f1,sql,SQL,,\n"
    )
    dd_redcap = load_from_redcap(redcap_csv, study_key="S")

    odm_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" FileType="Snapshot" FileOID="F1" CreationDateTime="2026-01-01T00:00:00">
      <Study OID="S">
        <GlobalVariables><StudyName>S</StudyName><StudyDescription>S</StudyDescription><ProtocolName>S</ProtocolName></GlobalVariables>
        <MetaDataVersion OID="MDV.1" Name="v1">
          <FormDef OID="F_ODM" Name="ODM Form" Repeating="No">
            <ItemGroupRef ItemGroupOID="IG.1" Mandatory="No"/>
          </FormDef>
          <ItemGroupDef OID="IG.1" Name="IG1" Repeating="No">
            <ItemRef ItemOID="IT.STR" Mandatory="No"/>
            <ItemRef ItemOID="IT.INT" Mandatory="No"/>
            <ItemRef ItemOID="IT.FLT" Mandatory="No"/>
            <ItemRef ItemOID="IT.DT" Mandatory="No"/>
            <ItemRef ItemOID="IT.DTM" Mandatory="No"/>
            <ItemRef ItemOID="IT.TM" Mandatory="No"/>
            <ItemRef ItemOID="IT.BOOL" Mandatory="No"/>
            <ItemRef ItemOID="IT.RAD" Mandatory="No" ControlType="Radio"/>
            <ItemRef ItemOID="IT.SEL" Mandatory="No" ControlType="Dropdown"/>
            <ItemRef ItemOID="IT.CHK" Mandatory="No" ControlType="Checkbox"/>
          </ItemGroupDef>
          <ItemDef OID="IT.STR" Name="STR" DataType="text"/>
          <ItemDef OID="IT.INT" Name="INT" DataType="integer"/>
          <ItemDef OID="IT.FLT" Name="FLT" DataType="float"/>
          <ItemDef OID="IT.DT" Name="DT" DataType="date"/>
          <ItemDef OID="IT.DTM" Name="DTM" DataType="datetime"/>
          <ItemDef OID="IT.TM" Name="TM" DataType="time"/>
          <ItemDef OID="IT.BOOL" Name="BOOL" DataType="boolean"/>
          <ItemDef OID="IT.RAD" Name="RAD" DataType="text"><CodeListRef CodeListOID="CL.1"/></ItemDef>
          <ItemDef OID="IT.SEL" Name="SEL" DataType="text"><CodeListRef CodeListOID="CL.1"/></ItemDef>
          <ItemDef OID="IT.CHK" Name="CHK" DataType="text"><CodeListRef CodeListOID="CL.1"/></ItemDef>
          <CodeList OID="CL.1" Name="CL1" DataType="text">
            <CodeListItem CodedValue="1"><Decode><TranslatedText>One</TranslatedText></Decode></CodeListItem>
          </CodeList>
        </MetaDataVersion>
      </Study>
    </ODM>"""
    dd_odm = load_from_cdisc_odm(odm_xml, study_key="S")

    dd_csv = load_from_csv_files(
        forms_file=io.StringIO("formId,formKey,formName,formType\n1,F_CSV,CSV Form,Subject\n"),
        questions_file=io.StringIO(
            "formKey,variableName,variableType,label,sequence\n"
            "F_CSV,V_CODED,Coded,Coded,1\n"
            "F_CSV,V_MEMO,Memo,Memo,2\n"
        ),
        choices_file=io.StringIO(
            "formKey,variableName,choiceValue,choiceText,position\nF_CSV,V_CODED,1,One,1\n"
        ),
        business_logic_file=io.StringIO("id,name,type,status,formKey,sequence,logic\n"),
        study_key="S",
    )

    from types import SimpleNamespace

    stub_sdk = SimpleNamespace(
        forms=SimpleNamespace(
            list=lambda study_key: [
                ApiForm(form_id=1, form_key="F_API", form_name="API", study_key=study_key)
            ]
        ),
        variables=SimpleNamespace(
            list=lambda study_key: [
                ApiVariable(
                    variable_name="V_NUM",
                    variable_type="Number",
                    form_id=1,
                    form_key="F_API",
                    study_key=study_key,
                ),
                ApiVariable(
                    variable_name="V_DTP",
                    variable_type="Date/Time Precision",
                    form_id=1,
                    form_key="F_API",
                    study_key=study_key,
                ),
            ]
        ),
    )

    dd_api = load_from_api(stub_sdk, "S")

    emitted_types: set[str] = set(_CANONICAL_VARIABLE_TYPES.values())
    for raw_key in _CANONICAL_VARIABLE_TYPES:
        norm_vt = normalize_variable_type(raw_key)
        if norm_vt:
            emitted_types.add(norm_vt)

    for dd in (dd_redcap, dd_odm, dd_csv, dd_api):
        for form in dd.forms:
            cache = SchemaCache()
            cache.populate(
                [
                    ApiVariable(
                        variable_name=v.variableName,
                        variable_type=v.variableType,
                        form_id=1,
                        form_key=form.formKey,
                    )
                    for v in form.variables
                ]
            )
            sample_payload = {
                v.variableName: typed_values.value_for(v.variableType or "") for v in form.variables
            }
            assert all(val is not None for val in sample_payload.values())
            validate_record_data(cache, form.formKey, sample_payload)
            validate_record_data(dd, form.formKey, sample_payload)
            for v in form.variables:
                if v.variableType:
                    emitted_types.add(v.variableType)

    for vt in sorted(emitted_types):
        sample_val = typed_values.value_for(vt)
        assert sample_val is not None
        cache = SchemaCache()
        cache.populate([ApiVariable(variable_name="V", variable_type=vt, form_id=1, form_key="F")])
        validate_record_data(cache, "F", {"V": sample_val})
