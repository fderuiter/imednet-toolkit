"""Comprehensive tests for unified validation kernel and CDISC ODM 1.3 round-trip parity."""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from imednet.cli.utils.write_helpers import parse_key_value_pairs
from imednet.datadict.diff import diff
from imednet.datadict.emit import to_odm_xml
from imednet.datadict.loaders.cdisc_odm import load_from_cdisc_odm
from imednet.datadict.loaders.csv_export import load_from_csv, load_from_csv_files
from imednet.datadict.loaders.redcap import load_from_redcap
from imednet.datadict.logic import LogicTreeEvaluator, parse_logic_xml
from imednet.datadict.models import BusinessLogicRule, Choice, DataDictionary, Form, Variable
from imednet.endpoints.records import RecordsEndpoint
from imednet.errors import ValidationError
from imednet.integrations.sink_base import SinkConfig, apply_quality_gate
from imednet.models.records import Record
from imednet.models.variables import Variable as ApiVariable
from imednet.utils.tabular import csv_to_records, dataframe_rows_to_records
from imednet.validation.cache import (
    SchemaCache,
    SchemaValidator,
    calculate_readiness_score,
    validate_record_data,
    validate_record_entry,
)


def test_csv_datadict_odm_roundtrip_zero_diff_and_type_rule_preservation() -> None:
    """CSV DataDictionary round-trips through to_odm_xml and load_from_cdisc_odm with zero diff."""
    forms_csv = "Form Key,Form Name,Form ID\nDM,Demographics,101\nVS,Vital Signs,102\n"
    questions_csv = (
        "Form,Variable Name,Label,Variable Type,Required,Min Value,Max Value,Units,Sequence\n"
        "DM,SUBJID,Subject ID,Text,Yes,,,,1\n"
        "DM,SEX,Biological Sex,Radio,Yes,,,,2\n"
        "DM,ONSET_DT,Partial Onset Date,Date/Time Precision,No,,,,3\n"
        "DM,CONSENT_PDF,Consent Document,File,No,,,,4\n"
        "DM,HEADER_NOTE,Section Instructions,Descriptive,No,,,,5\n"
        "VS,PAIN_SLIDER,Pain Visual Scale,Slider,No,0,10,,1\n"
        "VS,BMI_CALC,Calculated BMI,Calculation,No,,,kg/m2,2\n"
    )
    choices_csv = (
        "Form,Variable Name,Choice Value,Choice Text,Position\nDM,SEX,1,Male,1\nDM,SEX,2,Female,2\n"
    )
    form_rule_xml = (
        "<rule><conditions><condition><variable>PAIN_SLIDER</variable>"
        "<operator>&gt;</operator><value>10</value></condition></conditions>"
        "<actions><action><type>error</type><message>Pain exceeds scale</message></action></actions></rule>"
    )
    req_rule_xml = "<rule><conditions><condition><variable>DM.SUBJID</variable><false/></condition></conditions></rule>"
    query_rule_xml = (
        "<rule><conditions><condition><variable>VS.PAIN_SLIDER</variable>"
        "<operator>&gt;=</operator><value>8</value></condition></conditions>"
        "<actions><action><type>query</type><message>Verify high pain score</message></action></actions></rule>"
    )
    logic_csv = (
        "Type,Name,Status,ID,Form,Logic\n"
        f'Form,Pain Max Check,Active,R_FORM_1,VS,"{form_rule_xml}"\n'
        f'Required,SUBJID Required Rule,Active,R_REQ_1,,"{req_rule_xml}"\n'
        f'Query,High Pain Query,Active,R_QRY_1,,"{query_rule_xml}"\n'
    )

    dd_csv = load_from_csv_files(
        forms_file=io.StringIO(forms_csv),
        questions_file=io.StringIO(questions_csv),
        choices_file=io.StringIO(choices_csv),
        business_logic_file=io.StringIO(logic_csv),
        study_key="STUDY_CSV_RT",
    )

    odm_xml = to_odm_xml(dd_csv)
    assert 'imednet:VariableType="Date/Time Precision"' in odm_xml
    assert 'imednet:VariableType="File"' in odm_xml
    assert 'imednet:VariableType="Descriptive"' in odm_xml
    assert 'imednet:VariableType="Slider"' in odm_xml
    assert 'imednet:VariableType="Calculation"' in odm_xml
    assert "<ConditionDef" in odm_xml

    dd_reloaded = load_from_cdisc_odm(odm_xml, strict=True)
    assert diff(dd_csv, dd_reloaded).items == []

    # Also verify canonical 4-file CSV fixture directory round-trips with zero structural diff
    fixtures_dir = Path(__file__).parent.parent / "fixtures" / "data_dictionary"
    dd_fixture = load_from_csv(fixtures_dir)
    dd_fixture_reloaded = load_from_cdisc_odm(to_odm_xml(dd_fixture), strict=True)
    assert diff(dd_fixture, dd_fixture_reloaded).items == []

    dm_vars = dd_reloaded.variables_for_form("DM")
    vs_vars = dd_reloaded.variables_for_form("VS")
    assert dm_vars["ONSET_DT"].variableType == "Date/Time Precision"
    assert dm_vars["CONSENT_PDF"].variableType == "File"
    assert dm_vars["HEADER_NOTE"].variableType == "Descriptive"
    assert vs_vars["PAIN_SLIDER"].variableType == "Slider"
    assert vs_vars["BMI_CALC"].variableType == "Calculation"

    # Reloaded dictionary enforces partial dates, calculation values, and business logic rules
    validate_record_data(
        dd_reloaded,
        "DM",
        {"SUBJID": "SUBJ-01", "SEX": "1", "ONSET_DT": "2025-07"},
    )
    validate_record_data(dd_reloaded, "VS", {"PAIN_SLIDER": 5, "BMI_CALC": 22.4})
    with pytest.raises(ValidationError, match="Pain"):
        validate_record_data(dd_reloaded, "VS", {"PAIN_SLIDER": 12})


def test_redcap_datadict_odm_roundtrip_zero_diff_and_formula_preservation() -> None:
    """REDCap DataDictionary round-trips calculation and branchingLogic via ODM ConditionDef/MethodDef."""
    redcap_csv = (
        "Variable / Field Name,Form Name,Section Header,Field Type,Field Label,"
        '"Choices, Calculations, or Slider Labels",Field Note,Text Validation Type OR Show Slider Number,'
        "Text Validation Min,Text Validation Max,Identifier?,Branching Logic (Show field only if...),"
        "Required Field?,Custom Alignment,Question Number (surveys only),Matrix Group Name,"
        "Matrix Ranking?,Field Annotation\n"
        "record_id,demographics,,text,Record ID,,,,,,,,y,,,,,\n"
        'sex,demographics,,radio,Sex,"1, Male | 2, Female",,,,,,,y,,,,,\n'
        "preg_status,demographics,,dropdown,Pregnant?,\"1, Yes | 0, No\",,,,,,[sex] = '2',,,,,,\n"
        "weight_kg,vitals,,text,Weight (kg),,,number,20,300,,,y,,,,,\n"
        "height_m,vitals,,text,Height (m),,,number,1.0,2.5,,,y,,,,,\n"
        "bmi,vitals,,calc,BMI,\"round([weight_kg]/([height_m]*[height_m]), 1)\",,,,,,,,,,,,\n"
        'fatigue_score,vitals,,slider,Fatigue,"0, None | 50, Moderate | 100, Severe",,number,0,100,,,,,,,,\n'
        "lab_report,vitals,,file,Lab Upload,,,,,,,,,,,,,\n"
        "vitals_intro,vitals,,descriptive,Please record resting vitals.,,,,,,,,,,,,,\n"
    )

    dd_rc = load_from_redcap(io.StringIO(redcap_csv), study_key="STUDY_REDCAP_RT")
    odm_xml = to_odm_xml(dd_rc)
    assert 'Context="redcap:branchingLogic"' in odm_xml
    assert 'Context="redcap:calculation"' in odm_xml
    assert "<MethodDef" in odm_xml
    assert "<ConditionDef" in odm_xml

    dd_rc_reloaded = load_from_cdisc_odm(odm_xml, strict=True)
    assert diff(dd_rc, dd_rc_reloaded).items == []

    demog_vars = dd_rc_reloaded.variables_for_form("demographics")
    vitals_vars = dd_rc_reloaded.variables_for_form("vitals")
    assert demog_vars["preg_status"].extraAttributes["branchingLogic"] == "[sex] = '2'"
    assert (
        vitals_vars["bmi"].extraAttributes["calculation"]
        == "round([weight_kg]/([height_m]*[height_m]), 1)"
    )
    assert vitals_vars["bmi"].variableType == "Calculation"
    assert vitals_vars["fatigue_score"].variableType == "Slider"
    assert vitals_vars["lab_report"].variableType == "File"
    assert vitals_vars["vitals_intro"].variableType == "Descriptive"


def test_odm_loader_dedups_formdef_across_mdv_enforces_range_checks_and_handles_it_prefix() -> None:
    """load_from_cdisc_odm deduplicates FormDef by OID across MetaDataVersions, enforces Hard/Soft RangeChecks, and unwraps matching IT. prefixes while keeping external IT.BRTHDAT."""
    odm_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2" FileType="Snapshot" FileOID="F1" CreationDateTime="2026-01-01T00:00:00Z">
  <Study OID="S.MULTI">
    <GlobalVariables>
      <StudyName>Multi MDV Study</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>Proto</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV.1" Name="v1">
      <FormDef OID="F_VS" Name="Vital Signs" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG.VS1" Mandatory="Yes"/>
      </FormDef>
      <ItemGroupDef OID="IG.VS1" Name="VS Group 1" Repeating="No">
        <ItemRef ItemOID="IT.HR" Mandatory="Yes" OrderNumber="1"/>
        <ItemRef ItemOID="IT.BRTHDAT" Mandatory="No" OrderNumber="2"/>
      </ItemGroupDef>
      <ItemDef OID="IT.HR" Name="HR" DataType="integer">
        <Question><TranslatedText>Heart Rate</TranslatedText></Question>
        <RangeCheck Comparator="GE" SoftHard="Hard">
          <CheckValue>30</CheckValue>
          <ErrorMessage><TranslatedText>HR below physiological minimum</TranslatedText></ErrorMessage>
        </RangeCheck>
        <RangeCheck Comparator="LE" SoftHard="Hard">
          <CheckValue>220</CheckValue>
        </RangeCheck>
        <RangeCheck Comparator="LE" SoftHard="Soft">
          <CheckValue>100</CheckValue>
          <ErrorMessage><TranslatedText>Tachycardia soft check</TranslatedText></ErrorMessage>
        </RangeCheck>
      </ItemDef>
      <ItemDef OID="IT.BRTHDAT" Name="Birth Date Prompt" DataType="date">
        <Question><TranslatedText>Date of Birth</TranslatedText></Question>
      </ItemDef>
    </MetaDataVersion>
    <MetaDataVersion OID="MDV.2" Name="v2">
      <FormDef OID="F_VS" Name="Vital Signs Updated" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG.VS2" Mandatory="Yes"/>
      </FormDef>
      <ItemGroupDef OID="IG.VS2" Name="VS Group 2" Repeating="No">
        <ItemRef ItemOID="IT.HR" Mandatory="Yes" OrderNumber="1"/>
        <ItemRef ItemOID="IT.BRTHDAT" Mandatory="No" OrderNumber="2"/>
        <ItemRef ItemOID="IT.TEMP" Mandatory="No" OrderNumber="3"/>
      </ItemGroupDef>
      <ItemDef OID="IT.TEMP" Name="TEMP" DataType="float">
        <Question><TranslatedText>Body Temperature</TranslatedText></Question>
      </ItemDef>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(odm_xml)
    assert len(dd.forms) == 1
    assert dd.forms[0].formKey == "F_VS"

    vs_vars = dd.variables_for_form("F_VS")
    assert set(vs_vars.keys()) == {"HR", "IT.BRTHDAT", "TEMP"}

    # Valid HR and valid calendar date on IT.BRTHDAT
    report_ok = validate_record_data(
        dd,
        "F_VS",
        {"HR": 72, "IT.BRTHDAT": "1990-05-15", "TEMP": 36.8},
        collect_errors=True,
    )
    assert report_ok.is_valid is True
    assert report_ok.warnings == []

    # Soft RangeCheck produces warning without failing validation
    report_soft = validate_record_data(
        dd,
        "F_VS",
        {"HR": 115},
        collect_errors=True,
    )
    assert report_soft.is_valid is True
    assert len(report_soft.warnings) == 1
    assert report_soft.warnings[0].rule_id == "range_check_soft"

    # Hard RangeCheck raises ValidationError (and rejects NaN/Inf)
    with pytest.raises(ValidationError, match="RangeCheck"):
        validate_record_data(dd, "F_VS", {"HR": 20})
    with pytest.raises(ValidationError, match="finite"):
        validate_record_data(dd, "F_VS", {"HR": 72, "TEMP": float("nan")})


def test_logic_evaluator_chronological_dates_nan_and_de_morgan_negation() -> None:
    """LogicTreeEvaluator evaluates '12-31-2020' > '01-01-2025', float('nan') > 42, and <not> on blank optional Variable values as False."""
    evaluator = LogicTreeEvaluator()

    date_gt_rule = BusinessLogicRule(
        id="R_DT",
        name="Date Chronological Check",
        type="Form",
        status="Active",
        logic=parse_logic_xml(
            "<rule><conditions><condition><variable>VISDAT</variable>"
            "<operator>&gt;</operator><value>01-01-2025</value>"
            "</condition></conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(date_gt_rule, {"VISDAT": "12-31-2020"}).triggered is False
    assert evaluator.evaluate_rule(date_gt_rule, {"VISDAT": "06-15-2025"}).triggered is True

    nan_gt_rule = BusinessLogicRule(
        id="R_NAN",
        name="NaN Comparison Check",
        type="Form",
        status="Active",
        logic=parse_logic_xml(
            "<rule><conditions><condition><variable>LABVAL</variable>"
            "<operator>&gt;</operator><value>42</value>"
            "</condition></conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(nan_gt_rule, {"LABVAL": float("nan")}).triggered is False
    assert evaluator.evaluate_rule(nan_gt_rule, {"LABVAL": float("inf")}).triggered is False
    assert evaluator.evaluate_rule(nan_gt_rule, {"LABVAL": "NaN"}).triggered is False
    assert evaluator.evaluate_rule(nan_gt_rule, {"LABVAL": Decimal("NaN")}).triggered is False

    # <not> on blank/omitted optional Variable evaluates to False (propagated to leaf)
    not_opt_rule = BusinessLogicRule(
        id="R_NOT_OPT",
        name="Negated Optional Equality",
        type="Form",
        status="Active",
        logic=parse_logic_xml(
            "<rule><conditions><not><condition><variable>OPT_FLAG</variable>"
            "<operator>==</operator><value>Y</value></condition></not></conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(not_opt_rule, {}).triggered is False
    assert evaluator.evaluate_rule(not_opt_rule, {"OPT_FLAG": ""}).triggered is False
    assert evaluator.evaluate_rule(not_opt_rule, {"OPT_FLAG": None}).triggered is False
    assert evaluator.evaluate_rule(not_opt_rule, {"OPT_FLAG": "Y"}).triggered is False
    assert evaluator.evaluate_rule(not_opt_rule, {"OPT_FLAG": "N"}).triggered is True

    # Compound <not><and>...</and></not> De Morgan propagation
    not_and_rule = BusinessLogicRule(
        id="R_NOT_AND",
        name="De Morgan Not And",
        type="Form",
        status="Active",
        logic=parse_logic_xml(
            "<rule><conditions><not><and>"
            "<condition><variable>OPT_A</variable><operator>==</operator><value>1</value></condition>"
            "<condition><variable>OPT_B</variable><operator>==</operator><value>2</value></condition>"
            "</and></not></conditions></rule>"
        ),
    )
    assert evaluator.evaluate_rule(not_and_rule, {}).triggered is False
    assert evaluator.evaluate_rule(not_and_rule, {"OPT_A": "1", "OPT_B": "2"}).triggered is False
    assert evaluator.evaluate_rule(not_and_rule, {"OPT_A": "1", "OPT_B": "9"}).triggered is True


def test_unassigned_csv_required_and_query_rules_skip_unrelated_forms() -> None:
    """Unassigned Required and Query rules in unassignedBusinessLogic skip unrelated Form keys without false Record errors."""
    forms_csv = "Form Key,Form Name\nDM,Demographics\nAE,Adverse Events\n"
    questions_csv = (
        "Form,Variable Name,Question Type\nDM,SUBJID,Text\nDM,AGE,Number\nAE,AETERM,Text\n"
    )
    choices_csv = "Form,Variable Name,Choice Value,Choice Text\n"
    req_xml = "<rule><conditions><condition><variable>SUBJID</variable><false/></condition></conditions></rule>"
    qry_xml = (
        "<rule><conditions><condition><variable>AGE</variable>"
        "<operator>&lt;</operator><value>18</value></condition></conditions>"
        "<actions><action><type>query</type><message>Minor subject</message></action></actions></rule>"
    )
    logic_csv = (
        "Type,Name,Status,ID,Form,Logic\n"
        f'Required,SUBJID Missing,Active,R1,,"{req_xml}"\n'
        f'Query,Pediatric Query,Active,Q1,,"{qry_xml}"\n'
    )
    dd = load_from_csv_files(
        forms_file=io.StringIO(forms_csv),
        questions_file=io.StringIO(questions_csv),
        choices_file=io.StringIO(choices_csv),
        business_logic_file=io.StringIO(logic_csv),
    )

    assert dd.rules_for_form("AE") == []
    ae_report = validate_record_data(dd, "AE", {"AETERM": "Headache"}, collect_errors=True)
    assert ae_report.is_valid is True
    assert ae_report.warnings == []

    # Direct LogicTreeEvaluator with allowed_variables also skips foreign unassigned rules
    eval_report = LogicTreeEvaluator().evaluate_rules(
        dd.unassignedBusinessLogic,
        {"AETERM": "Headache"},
        form_key="AE",
        allowed_variables=dd.variables_for_form("AE"),
    )
    assert all(r.skipped and r.skip_reason == "foreign_variable" for r in eval_report.results)
    assert eval_report.errors == []
    assert eval_report.warnings == []

    # On DM, the rules DO apply
    with pytest.raises(ValidationError, match="SUBJID Missing"):
        validate_record_data(dd, "DM", {"AGE": 25})
    dm_warn_report = validate_record_data(
        dd, "DM", {"SUBJID": "S01", "AGE": 16}, collect_errors=True
    )
    assert dm_warn_report.is_valid is True
    assert len(dm_warn_report.warnings) == 1


def test_validate_record_entry_missing_form_recorddata_none_non_mapping_and_temporal_finite() -> (
    None
):
    """validate_record_entry rejects missing formKey/formId, handles recordData=None fallback, rejects non-mapping data, and rejects invalid dates, NaN, and Inf."""
    dd = DataDictionary(
        forms=[
            Form(
                formKey="F_DM",
                formId=10,
                variables=[
                    Variable(variableName="VISDAT", variableType="Date"),
                    Variable(variableName="WEIGHT", variableType="Float"),
                ],
            )
        ]
    )
    cache = SchemaCache()
    cache.populate(
        [
            ApiVariable(
                study_key="S1",
                form_id=10,
                form_key="F_DM",
                variable_name="VISDAT",
                variable_type="Date",
            ),
            ApiVariable(
                study_key="S1",
                form_id=10,
                form_key="F_DM",
                variable_name="WEIGHT",
                variable_type="Float",
            ),
        ],
        study_key="S1",
    )

    for schema in (dd, cache):
        kw = {"study_key": "S1"} if isinstance(schema, SchemaCache) else {}

        # Missing both formKey and formId without update target raises ValidationError
        with pytest.raises(ValidationError, match="formKey"):
            validate_record_entry(schema, {"data": {"VISDAT": "2025-01-15"}}, **kw)

        # recordData=None falls back to data (and {} when data is absent) without TypeError
        validate_record_entry(
            schema,
            {
                "formKey": "F_DM",
                "recordData": None,
                "data": {"VISDAT": "2025-01-15", "WEIGHT": 70.5},
            },
            **kw,
        )
        validate_record_entry(schema, {"formKey": "F_DM", "recordData": None}, **kw)

        # Non-mapping record entry and non-mapping record data raise ValidationError
        with pytest.raises(ValidationError, match="mapping"):
            validate_record_entry(schema, "not-a-record", **kw)  # type: ignore[arg-type]
        with pytest.raises(ValidationError, match="mapping"):
            validate_record_entry(schema, {"formKey": "F_DM", "recordData": ["bad"]}, **kw)
        with pytest.raises(ValidationError, match="mapping"):
            validate_record_entry(schema, {"recordId": 99, "recordData": "bad"}, **kw)

        # Invalid calendar dates, NaN, and Inf are rejected
        for bad_date in ("2024-02-30", "2023-02-29", "02-30-2024", "13-01-2024", "not-a-date"):
            with pytest.raises(ValidationError, match="date"):
                validate_record_entry(
                    schema,
                    {"formKey": "F_DM", "recordData": {"VISDAT": bad_date}},
                    **kw,
                )
        for non_finite in (float("nan"), float("inf"), float("-inf"), Decimal("NaN")):
            with pytest.raises(ValidationError, match="finite"):
                validate_record_entry(
                    schema,
                    {"formKey": "F_DM", "recordData": {"WEIGHT": non_finite}},
                    **kw,
                )

    # SchemaValidator.validate_record handles recordData=None fallback and rejects non-mapping data
    sdk = MagicMock()
    sdk.get_variables.return_value = list(cache.forms_for_study("S1")["F_DM"].values())
    sv = SchemaValidator(sdk)
    sv.validate_record("S1", {"formKey": "F_DM", "recordData": None, "data": {"WEIGHT": 70.0}})
    with pytest.raises(ValidationError, match="mapping"):
        sv.validate_record("S1", {"formKey": "F_DM", "recordData": ["bad"]})


def test_tabular_cli_and_records_endpoint_preserve_choice_strings_across_multi_study_cache(
    tmp_path: Path,
) -> None:
    """csv_to_records, parse_key_value_pairs, and RecordsEndpoint preserve Choice strings and isolate multi-Study SchemaCache lookups without ambiguity."""
    pd = pytest.importorskip("pandas")

    cache = SchemaCache()
    cache.populate(
        [
            ApiVariable(
                study_key="STUDY_A",
                form_id=10,
                form_key="F_DM",
                variable_name="ARM_CODE",
                variable_type="Radio",
            ),
            ApiVariable(
                study_key="STUDY_A",
                form_id=10,
                form_key="F_DM",
                variable_name="ELIGIBLE",
                variable_type="Yes/No",
            ),
        ],
        study_key="STUDY_A",
    )
    cache.populate(
        [
            ApiVariable(
                study_key="STUDY_B",
                form_id=10,
                form_key="F_DM",
                variable_name="ARM_CODE",
                variable_type="Int",
            ),
            ApiVariable(
                study_key="STUDY_B",
                form_id=10,
                form_key="F_DM",
                variable_name="ELIGIBLE",
                variable_type="Boolean",
            ),
        ],
        study_key="STUDY_B",
    )

    csv_file = tmp_path / "dm.csv"
    csv_file.write_text("formKey,ARM_CODE,ELIGIBLE\nF_DM,1,true\n", encoding="utf-8")

    recs_a = csv_to_records(csv_file, schema=cache, study_key="STUDY_A")
    assert recs_a == [{"formKey": "F_DM", "data": {"ARM_CODE": "1", "ELIGIBLE": "true"}}]

    recs_b = csv_to_records(csv_file, schema=cache, study_key="STUDY_B")
    assert recs_b == [{"formKey": "F_DM", "data": {"ARM_CODE": 1, "ELIGIBLE": True}}]

    # DataFrame with integer/float/bool scalars also coerces Choice columns to strings in STUDY_A
    df = pd.DataFrame([{"formKey": "F_DM", "ARM_CODE": 1, "ELIGIBLE": True}])
    df_recs_a = dataframe_rows_to_records(df, schema=cache, study_key="STUDY_A")
    assert df_recs_a == [{"formKey": "F_DM", "data": {"ARM_CODE": "1", "ELIGIBLE": "true"}}]

    # CLI parse_key_value_pairs preserves Choice strings per study_key
    cli_a = parse_key_value_pairs(
        ["ARM_CODE=1", "ELIGIBLE=true"],
        schema=cache,
        form_id=10,
        study_key="STUDY_A",
    )
    assert cli_a == {"ARM_CODE": "1", "ELIGIBLE": "true"}

    cli_b = parse_key_value_pairs(
        ["ARM_CODE=1", "ELIGIBLE=true"],
        schema=cache,
        form_id=10,
        study_key="STUDY_B",
    )
    assert cli_b == {"ARM_CODE": 1, "ELIGIBLE": True}

    # RecordsEndpoint.create forwards study_key for normalization + validation across both studies
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"batchId": "B-100", "studyKey": "STUDY_A", "state": "COMPLETED"}
    mock_client.post.return_value = mock_resp

    ep = RecordsEndpoint(client=mock_client)
    ep.create("STUDY_A", csv_file, schema=cache)
    sent_a = mock_client.post.call_args_list[-1].kwargs["json"]
    assert sent_a == [{"formKey": "F_DM", "data": {"ARM_CODE": "1", "ELIGIBLE": "true"}}]

    ep.create("STUDY_B", csv_file, schema=cache)
    sent_b = mock_client.post.call_args_list[-1].kwargs["json"]
    assert sent_b == [{"formKey": "F_DM", "data": {"ARM_CODE": 1, "ELIGIBLE": True}}]


def test_readiness_score_and_quality_gate_sparse_masked_unknown_and_rules() -> None:
    """calculate_readiness_score and apply_quality_gate retain valid sparse Records while penalizing/dropping ***MASKED***, unknown Variable keys, and failed BusinessLogicRules."""
    rule_xml = (
        "<rule><conditions>"
        "<condition><variable>SEX</variable><operator>==</operator><value>M</value></condition>"
        "<condition><variable>PREG</variable><operator>==</operator><value>Y</value></condition>"
        "</conditions><actions><action><type>error</type><message>Invalid pregnancy status</message></action></actions></rule>"
    )
    dd = DataDictionary(
        forms=[
            Form(
                formKey="F_DM",
                formId=10,
                variables=[
                    Variable(variableName="SUBJID", variableType="Text", required="Yes"),
                    Variable(
                        variableName="SEX",
                        variableType="Radio",
                        required="Yes",
                        choices=[
                            Choice(choiceValue="M", choiceText="Male"),
                            Choice(choiceValue="F", choiceText="Female"),
                        ],
                    ),
                    Variable(
                        variableName="PREG",
                        variableType="Radio",
                        required="No",
                        choices=[
                            Choice(choiceValue="Y", choiceText="Yes"),
                            Choice(choiceValue="N", choiceText="No"),
                        ],
                    ),
                    Variable(variableName="NOTES", variableType="Memo", required="No"),
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="R_PREG",
                        name="Male Pregnancy Check",
                        type="Form",
                        status="Active",
                        form="F_DM",
                        logic=parse_logic_xml(rule_xml),
                    )
                ],
            )
        ]
    )

    # 1. Empty {} stays at 0.0
    empty_score, _ = calculate_readiness_score(dd, "F_DM", {})
    assert empty_score == pytest.approx(0.0)

    # 2. Sparse valid Record omitting optional PREG and NOTES scores 100.0
    sparse_score, sparse_reasons = calculate_readiness_score(
        dd, "F_DM", {"SUBJID": "S-01", "SEX": "M"}
    )
    assert sparse_score == pytest.approx(100.0)
    assert sparse_reasons == []

    # 3. Masked PHI sentinel penalizes score below 100.0
    masked_score, masked_reasons = calculate_readiness_score(
        dd, "F_DM", {"SUBJID": "***MASKED***", "SEX": "F"}
    )
    assert masked_score < 100.0
    assert any("***MASKED***" in r for r in masked_reasons)

    # 4. Unknown variable penalizes score below 100.0
    unk_score, unk_reasons = calculate_readiness_score(
        dd, "F_DM", {"SUBJID": "S-01", "SEX": "F", "BOGUS_VAR": "123"}
    )
    assert unk_score < 100.0
    assert any("Unknown variables: BOGUS_VAR" in r for r in unk_reasons)

    # 5. Rule-only failure preserves 100.0 variable score while reporting blocking rule failure
    rule_score, rule_reasons = calculate_readiness_score(
        dd, "F_DM", {"SUBJID": "S-02", "SEX": "M", "PREG": "Y"}
    )
    assert rule_score == pytest.approx(100.0)
    assert any("Rule Male Pregnancy Check failed" in r for r in rule_reasons)

    # 6. apply_quality_gate retains sparse_rec and drops masked, unknown, empty, and rule-failed Records
    rec_sparse = Record(
        record_id=1,
        form_id=10,
        form_key="F_DM",
        record_data={"SUBJID": "S-01", "SEX": "M"},
    )
    rec_masked = Record(
        record_id=2,
        form_id=10,
        form_key="F_DM",
        record_data={"SUBJID": "***MASKED***", "SEX": "F"},
    )
    rec_unknown = Record(
        record_id=3,
        form_id=10,
        form_key="F_DM",
        record_data={"SUBJID": "S-03", "SEX": "F", "EXTRA_COL": "X"},
    )
    rec_rule_fail = Record(
        record_id=4,
        form_id=10,
        form_key="F_DM",
        record_data={"SUBJID": "S-04", "SEX": "M", "PREG": "Y"},
    )
    rec_empty = Record(
        record_id=5,
        form_id=10,
        form_key="F_DM",
        record_data={},
    )

    cfg = SinkConfig(
        study_key="S_GATE",
        quality_gate_enabled=True,
        min_schema_readiness_score=50.0,
        validation_schema=dd,
    )
    retained = list(
        apply_quality_gate(
            MagicMock(),
            "S_GATE",
            [rec_sparse, rec_masked, rec_unknown, rec_rule_fail, rec_empty],
            cfg,
        )
    )
    assert retained == [rec_sparse]

    cfg_plain = SinkConfig(
        study_key="S_GATE",
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
    )
    retained_kwarg = list(
        apply_quality_gate(
            MagicMock(),
            "S_GATE",
            [rec_sparse, rec_masked, rec_unknown, rec_rule_fail, rec_empty],
            cfg_plain,
            schema=dd,
        )
    )
    assert retained_kwarg == [rec_sparse]
