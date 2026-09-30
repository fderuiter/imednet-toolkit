"""Tests for CSV export data dictionary loader."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from imednet.datadict.loaders.csv_export import load_from_csv, load_from_csv_files

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "data_dictionary"


def test_load_from_directory() -> None:
    """Test loading canonical DataDictionary from a directory."""
    dd = load_from_csv(FIXTURES_DIR)
    assert len(dd.forms) == 3
    form_ae = next(f for f in dd.forms if f.formKey == "AE")
    assert form_ae.formName == "Adverse Event"
    assert len(form_ae.variables) == 3
    assert len(form_ae.businessLogic) == 3


def test_load_from_zip() -> None:
    """Test loading canonical DataDictionary from in-memory ZIP buffer."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for fname in ["FORMS.csv", "QUESTIONS.csv", "CHOICES.csv", "BUSINESS_LOGIC.csv"]:
            zf.write(FIXTURES_DIR / fname, arcname=fname)
    buf.seek(0)

    dd = load_from_csv(buf)
    assert len(dd.forms) == 3
    expected = load_from_csv(FIXTURES_DIR)
    assert dd == expected


def test_csv_loader_with_xml_and_no_raw_xml() -> None:
    """Test parsing logic XML and testing no_raw_xml flag."""
    forms_csv = "Form Key,Form Name\nAE,Adverse Event\n"
    questions_csv = "Form,Variable Name\nAE,AESTDAT\n"
    choices_csv = "Form,Variable Name,Choice Text,Choice Value\n"
    rule_xml = "<rule><conditions><condition><variable>AESTDAT</variable><true/></condition></conditions></rule>"
    logic_csv = f'Type,Name,Status,ID,Form,Logic\nForm,Check Date,Active,101,AE,"{rule_xml}"\n'

    # 1. With raw XML preserved
    dd_raw = load_from_csv_files(
        forms_file=io.StringIO(forms_csv),
        questions_file=io.StringIO(questions_csv),
        choices_file=io.StringIO(choices_csv),
        business_logic_file=io.StringIO(logic_csv),
        no_raw_xml=False,
    )
    rule_raw = dd_raw.forms[0].businessLogic[0]
    assert rule_raw.logic is not None
    assert rule_raw.logic["conditions"]["condition"]["true"] is True
    assert rule_raw.logicRawXml == rule_xml

    # 2. With no_raw_xml=True
    dd_no_raw = load_from_csv_files(
        forms_file=io.StringIO(forms_csv),
        questions_file=io.StringIO(questions_csv),
        choices_file=io.StringIO(choices_csv),
        business_logic_file=io.StringIO(logic_csv),
        no_raw_xml=True,
    )
    rule_no_raw = dd_no_raw.forms[0].businessLogic[0]
    assert rule_no_raw.logic is not None
    assert rule_no_raw.logicRawXml is None


def test_csv_loader_rule_type_routing() -> None:
    """Test routing of 5 rule types: Form vs unassigned (Workflow, Query, Approval, Required)."""
    forms_csv = "Form Key,Form Name\nAE,Adverse Event\n"
    questions_csv = "Form,Variable Name\nAE,AESTDAT\n"
    choices_csv = "Form,Variable Name,Choice Text,Choice Value\n"
    logic_csv = (
        "Type,Name,Status,ID,Form\n"
        "Form,Form Rule,Active,1,AE\n"
        "Workflow,Workflow Rule,Active,2,AE\n"
        "Query,Query Rule,Active,3,\n"
        "Approval,Approval Rule,Active,4,\n"
        "Required,Required Rule,Active,5,\n"
    )

    dd = load_from_csv_files(
        forms_file=io.StringIO(forms_csv),
        questions_file=io.StringIO(questions_csv),
        choices_file=io.StringIO(choices_csv),
        business_logic_file=io.StringIO(logic_csv),
    )
    assert len(dd.forms[0].businessLogic) == 1
    assert dd.forms[0].businessLogic[0].name == "Form Rule"

    assert len(dd.unassignedBusinessLogic) == 4
    unassigned_types = {r.type for r in dd.unassignedBusinessLogic}
    assert unassigned_types == {"Workflow", "Query", "Approval", "Required"}


def test_csv_loader_strict_vs_non_strict() -> None:
    """Test strict mode raising error vs non-strict recording warnings."""
    forms_csv = "Form Key,Form Name\nAE,Adverse Event\n"
    # Variable references undefined form DM
    questions_csv = "Form,Variable Name\nDM,AGE\n"
    choices_csv = "Form,Variable Name,Choice Text,Choice Value\n"
    logic_csv = "Type,Name,Status,ID,Form\n"

    # Strict mode raises ValueError
    with pytest.raises(ValueError, match="references undefined form 'DM'"):
        load_from_csv_files(
            forms_file=io.StringIO(forms_csv),
            questions_file=io.StringIO(questions_csv),
            choices_file=io.StringIO(choices_csv),
            business_logic_file=io.StringIO(logic_csv),
            strict=True,
        )

    # Non-strict mode creates placeholder form and adds warning
    dd = load_from_csv_files(
        forms_file=io.StringIO(forms_csv),
        questions_file=io.StringIO(questions_csv),
        choices_file=io.StringIO(choices_csv),
        business_logic_file=io.StringIO(logic_csv),
        strict=False,
    )
    assert any(f.formKey == "DM" for f in dd.forms)
    assert any("references undefined form 'DM'" in w for w in dd.metadata.validationWarnings)


def test_csv_loader_unknown_columns_in_extra_attributes() -> None:
    """Test unknown columns are placed in extraAttributes and tracked in warnings."""
    forms_csv = "Form Key,Form Name,CustomFormAttribute\nAE,Adverse Event,ExtraVal\n"
    questions_csv = "Form,Variable Name,Patient Record Report\nAE,AESTDAT,PRR_Value\n"
    choices_csv = "Form,Variable Name,Choice Text,Choice Value\n"
    logic_csv = "Type,Name,Status,ID,Form\n"

    dd = load_from_csv_files(
        forms_file=io.StringIO(forms_csv),
        questions_file=io.StringIO(questions_csv),
        choices_file=io.StringIO(choices_csv),
        business_logic_file=io.StringIO(logic_csv),
    )
    assert dd.forms[0].extraAttributes.get("customFormAttribute") == "ExtraVal"
    assert dd.forms[0].variables[0].extraAttributes.get("patientRecordReport") == "PRR_Value"
    assert any(
        "Unknown column 'Custom Form Attribute'" in w or "Unknown column 'CustomFormAttribute'" in w
        for w in dd.metadata.validationWarnings
    )
    assert any(
        "Unknown column 'Patient Record Report'" in w for w in dd.metadata.validationWarnings
    )


def test_csv_loader_timestamp_validation() -> None:
    """Test asserting MM-DD-YYYY timestamps at parse time."""
    # 1. Valid timestamp succeeds
    forms_valid = "Form Key,Form Name,Created Date\nAE,Adverse Event,09-28-2026 12:00:00\n"
    q_csv = "Form,Variable Name\nAE,AESTDAT\n"
    c_csv = "Form,Variable Name,Choice Text,Choice Value\n"
    l_csv = "Type,Name,Status,ID,Form\n"

    dd_valid = load_from_csv_files(
        forms_file=io.StringIO(forms_valid),
        questions_file=io.StringIO(q_csv),
        choices_file=io.StringIO(c_csv),
        business_logic_file=io.StringIO(l_csv),
        strict=False,
    )
    assert dd_valid.forms[0].extraAttributes.get("createdDate") == "09-28-2026 12:00:00"
    assert not any(
        "timestamps must adhere to MM-DD-YYYY format" in w
        for w in dd_valid.metadata.validationWarnings
    )

    # 2. Invalid timestamp in strict mode raises ValueError
    forms_invalid = "Form Key,Form Name,Created Date\nAE,Adverse Event,2026-09-28\n"
    with pytest.raises(ValueError):
        load_from_csv_files(
            forms_file=io.StringIO(forms_invalid),
            questions_file=io.StringIO(q_csv),
            choices_file=io.StringIO(c_csv),
            business_logic_file=io.StringIO(l_csv),
            strict=True,
        )

    # 3. Invalid timestamp in non-strict mode records validation warning
    dd_warn = load_from_csv_files(
        forms_file=io.StringIO(forms_invalid),
        questions_file=io.StringIO(q_csv),
        choices_file=io.StringIO(c_csv),
        business_logic_file=io.StringIO(l_csv),
        strict=False,
    )
    assert any(
        "timestamps must adhere to MM-DD-YYYY format" in w
        for w in dd_warn.metadata.validationWarnings
    )


def test_csv_loader_crlf_and_bom() -> None:
    """Test handling mixed CRLF line endings and UTF-8 BOM."""
    forms_csv = "\ufeffForm Key,Form Name\r\nAE,Adverse Event\r\n"
    questions_csv = "\ufeffForm,Variable Name\r\nAE,AESTDAT\r\n"
    choices_csv = "\ufeffForm,Variable Name,Choice Text,Choice Value\r\n"
    logic_csv = "\ufeffType,Name,Status,ID,Form\r\n"

    dd = load_from_csv_files(
        forms_file=io.StringIO(forms_csv),
        questions_file=io.StringIO(questions_csv),
        choices_file=io.StringIO(choices_csv),
        business_logic_file=io.StringIO(logic_csv),
    )
    assert len(dd.forms) == 1
    assert dd.forms[0].formKey == "AE"
    assert dd.forms[0].variables[0].variableName == "AESTDAT"
