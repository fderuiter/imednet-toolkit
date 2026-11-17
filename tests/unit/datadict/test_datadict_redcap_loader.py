"""Unit tests for REDCap CSV data dictionary loader."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from imednet.datadict import get_schema, load_from_redcap, validate
from imednet.datadict.models import DataDictionary

SAMPLE_REDCAP_CSV = """"Variable / Field Name","Form Name","Section Header","Field Type","Field Label","Choices, Calculations, OR Slider Labels","Field Note","Text Validation Type OR Show Slider Number","Text Validation Min","Text Validation Max","Identifier?","Branching Logic (Show field only if...)","Required Field?","Custom Alignment","Question Number (surveys only)","Matrix Group Name","Matrix Ranking?","Field Annotation"
"record_id","demographics","Subject Enrollment","text","Participant Study ID",,"Record identifier",,,,"y",,"y",,,,,
"dob","demographics",,"text","Date of Birth",,"YYYY-MM-DD","date_ymd",,"2026-01-01","y",,"y",,,,,
"gender","demographics",,"radio","Gender","1, Male | 2, Female | 3, Non-binary | 99, Prefer not to say",,,,,,"y",,,,,
"ethnicity","demographics",,"dropdown","Ethnicity","1, Hispanic or Latino | 2, Not Hispanic or Latino",,,,,,,,,,,,
"consent_given","demographics",,"yesno","Has subject signed informed consent?",,,,,,,,"y",,,,,
"is_adult","demographics",,"truefalse","Is subject 18 or older?",,,,,,,,,,,,
"symptoms","medical_history","Medical Symptoms","checkbox","Reported Symptoms","1, Headache | 2, Cough, persistent | 3, Fever",,,,,,,,,,,,
"height_cm","vitals","Physical Measurements","text","Height (cm)",,"In centimeters","number","50","250",,,,,,,,
"weight_kg","vitals",,"text","Weight (kg)",,"In kilograms","number","30","300",,,,,,,,
"bmi","vitals",,"calc","Calculated BMI","[weight_kg] / (([height_cm]/100) * ([height_cm]/100))",,,,,,,,,,,,
"ecg_file","vitals",,"file","ECG Upload",,,,,,,,,,,,
"pain_scale","vitals",,"slider","Visual Analog Pain Score","0, No pain | 100, Worst possible pain",,,,,,,,,,,,
"notes","vitals",,"notes","Clinician Comments",,,,,,,,,,,,
"disclaimer","vitals",,"descriptive","End of examination section",,,,,,,,,,,,
"""


def test_load_from_redcap_basic(tmp_path: Path) -> None:
    """Test loading valid REDCap CSV from string and file path."""
    dd = load_from_redcap(SAMPLE_REDCAP_CSV, study_key="REDCAP_TRIAL_01")
    assert isinstance(dd, DataDictionary)
    assert dd.metadata.studyKey == "REDCAP_TRIAL_01"
    assert dd.metadata.source == "redcap"

    # Verify 3 forms were created in order
    assert [f.formKey for f in dd.forms] == ["demographics", "medical_history", "vitals"]

    # Verify demographics form
    demo = dd.forms[0]
    assert demo.formName == "Demographics"
    assert len(demo.variables) == 6

    # Verify record_id
    v_id = demo.variables[0]
    assert v_id.variableName == "record_id"
    assert v_id.label == "Participant Study ID"
    assert v_id.variableType == "Text"
    assert v_id.required == "Yes"
    assert v_id.sequence == 1
    assert v_id.extraAttributes.get("sectionHeader") == "Subject Enrollment"
    assert v_id.extraAttributes.get("identifier") == "y"

    # Verify dob
    v_dob = demo.variables[1]
    assert v_dob.variableName == "dob"
    assert v_dob.variableType == "Date"
    assert v_dob.format == "date_ymd"
    assert v_dob.required == "Yes"

    # Verify gender radio choices
    v_gender = demo.variables[2]
    assert v_gender.variableName == "gender"
    assert v_gender.variableType == "Radio"
    assert len(v_gender.choices) == 4
    assert v_gender.choices[0].choiceValue == "1"
    assert v_gender.choices[0].choiceText == "Male"
    assert v_gender.choices[0].position == 1
    assert v_gender.choices[1].choiceValue == "2"
    assert v_gender.choices[1].choiceText == "Female"
    assert v_gender.choices[3].choiceValue == "99"
    assert v_gender.choices[3].choiceText == "Prefer not to say"

    # Verify dropdown
    v_eth = demo.variables[3]
    assert v_eth.variableName == "ethnicity"
    assert v_eth.variableType == "Single Select"
    assert len(v_eth.choices) == 2

    # Verify yesno auto synthesis
    v_consent = demo.variables[4]
    assert v_consent.variableName == "consent_given"
    assert v_consent.variableType == "Radio"
    assert len(v_consent.choices) == 2
    assert v_consent.choices[0].choiceValue == "1"
    assert v_consent.choices[0].choiceText == "Yes"
    assert v_consent.choices[1].choiceValue == "0"
    assert v_consent.choices[1].choiceText == "No"

    # Verify truefalse auto synthesis
    v_adult = demo.variables[5]
    assert v_adult.variableName == "is_adult"
    assert len(v_adult.choices) == 2
    assert v_adult.choices[0].choiceValue == "1"
    assert v_adult.choices[0].choiceText == "True"
    assert v_adult.choices[1].choiceValue == "0"
    assert v_adult.choices[1].choiceText == "False"

    # Verify checkbox multi-select with comma in label
    med = dd.forms[1]
    assert med.formKey == "medical_history"
    v_sym = med.variables[0]
    assert v_sym.variableName == "symptoms"
    assert v_sym.variableType == "Multi Select"
    assert len(v_sym.choices) == 3
    assert v_sym.choices[1].choiceValue == "2"
    assert v_sym.choices[1].choiceText == "Cough, persistent"

    # Verify calculation field does not parse choices and stores calculation
    vitals = dd.forms[2]
    v_bmi = next(v for v in vitals.variables if v.variableName == "bmi")
    assert v_bmi.variableType == "Calculation"
    assert v_bmi.choices == []
    assert "[weight_kg]" in v_bmi.extraAttributes.get("calculation", "")

    # Verify file, slider, notes, descriptive
    assert next(v for v in vitals.variables if v.variableName == "ecg_file").variableType == "File"
    assert (
        next(v for v in vitals.variables if v.variableName == "pain_scale").variableType == "Slider"
    )
    assert next(v for v in vitals.variables if v.variableName == "notes").variableType == "Text"
    assert (
        next(v for v in vitals.variables if v.variableName == "disclaimer").variableType
        == "Descriptive"
    )

    # Test loading from file
    csv_file = tmp_path / "redcap_export.csv"
    csv_file.write_text(SAMPLE_REDCAP_CSV, encoding="utf-8-sig")
    dd_file = load_from_redcap(csv_file, study_key="REDCAP_TRIAL_01")
    assert dd_file == dd

    # Schema validation
    errors = validate(dd, schema=get_schema())
    assert errors == []


def test_load_from_redcap_streams() -> None:
    """Test loading from io.StringIO and io.BytesIO."""
    sio = io.StringIO(SAMPLE_REDCAP_CSV)
    dd_str = load_from_redcap(sio)
    assert len(dd_str.forms) == 3

    bio = io.BytesIO(SAMPLE_REDCAP_CSV.encode("utf-8-sig"))
    dd_bytes = load_from_redcap(bio)
    assert len(dd_bytes.forms) == 3


def test_load_from_redcap_unsupported_type_and_unmapped_columns() -> None:
    """Test handling of unsupported field types and unknown columns."""
    csv_custom = """"Variable / Field Name","Form Name","Field Type","Field Label","SpecialUnknownCol"
"custom_field","form1","hologram_scanner","Scan Iris","extraValue"
"""
    # Non-strict mode should record warnings and default to Text
    dd = load_from_redcap(csv_custom, strict=False)
    assert len(dd.metadata.validationWarnings) >= 2
    assert any("hologram_scanner" in w for w in dd.metadata.validationWarnings)
    assert any("SpecialUnknownCol" in w for w in dd.metadata.validationWarnings)
    var = dd.forms[0].variables[0]
    assert var.variableType == "Text"
    assert var.extraAttributes.get("specialUnknownCol") == "extraValue"

    # Strict mode should raise ValueError
    with pytest.raises(ValueError, match="Strict loading error"):
        load_from_redcap(csv_custom, strict=True)


def test_load_from_redcap_type_and_choice_variations() -> None:
    """Test SQL field, time, integer validation, choice without comma, and missing form name."""
    csv_var = """"Variable / Field Name","Form Name","Field Type","Field Label","Choices, Calculations, OR Slider Labels","Text Validation Type OR Show Slider Number"
"time_field","f1","text","Time Prompt",,"time"
"int_field","f1","text","Int Prompt",,"int"
"sql_field","f1","sql","SQL Query Prompt",,
"choice_no_comma","f1","radio","Simple Choice","Alpha | Beta",
"missing_form_var",,"text","No Form Field",,
,,"text","Missing Var Name",,
"""
    dd = load_from_redcap(csv_var)
    f1 = next(f for f in dd.forms if f.formKey == "f1")
    v_time = next(v for v in f1.variables if v.variableName == "time_field")
    assert v_time.variableType == "Time"

    v_int = next(v for v in f1.variables if v.variableName == "int_field")
    assert v_int.variableType == "Integer"

    v_sql = next(v for v in f1.variables if v.variableName == "sql_field")
    assert v_sql.variableType == "Single Select"

    v_choice = next(v for v in f1.variables if v.variableName == "choice_no_comma")
    assert len(v_choice.choices) == 2
    assert v_choice.choices[0].choiceValue == "Alpha"
    assert v_choice.choices[0].choiceText == "Alpha"

    default_form = next(f for f in dd.forms if f.formKey == "default_form")
    assert any(v.variableName == "missing_form_var" for v in default_form.variables)

    # Type error test
    with pytest.raises(TypeError, match="Unsupported source type"):
        load_from_redcap(9999)  # type: ignore[arg-type]


def test_load_from_redcap_empty_and_no_header() -> None:
    """Test empty REDCap CSV handling."""
    dd = load_from_redcap("", strict=False)
    assert len(dd.metadata.validationWarnings) >= 1
    assert dd.forms == []

    with pytest.raises(ValueError, match="Strict loading error"):
        load_from_redcap("", strict=True)


def test_load_from_redcap_missing_file() -> None:
    """Test that missing CSV file paths raise FileNotFoundError instead of silent fallback."""
    for missing in [
        "nonexistent.csv",
        "nested/path/to/missing.csv",
        Path("nonexistent.csv"),
        "some/path/nonexistent",
    ]:
        with pytest.raises(FileNotFoundError, match="Target file .* does not exist"):
            load_from_redcap(missing)


def test_load_from_redcap_single_line_csv_no_delimiters() -> None:
    """Test that single-line CSV without file path delimiters or standard extensions parses directly."""
    single_line_csv = '"variable_name","form_name","field_type"'
    dd = load_from_redcap(single_line_csv)
    assert isinstance(dd, DataDictionary)
