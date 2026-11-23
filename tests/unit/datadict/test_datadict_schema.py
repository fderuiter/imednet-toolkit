"""Tests for JSON schema generation and validation."""

from __future__ import annotations

from pathlib import Path

from imednet.datadict.models import DataDictionary, Form, Variable
from imednet.datadict.schema import get_schema, validate


def test_get_schema() -> None:
    """Test JSON Schema generation."""
    schema = get_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "iMednet Data Dictionary Schema"
    assert schema["version"] == "1.0.0"
    assert "properties" in schema
    assert "forms" in schema["properties"]


def test_validate_valid_dictionary() -> None:
    """Test validating a valid DataDictionary instance."""
    dd = DataDictionary(forms=[Form(formKey="F1", variables=[Variable(variableName="V1")])])
    errors = validate(dd)
    assert errors == []


def test_validate_invalid_dictionary() -> None:
    """Test validating an invalid dictionary payload missing required formKey."""
    invalid_data = {"forms": [{"formName": "Missing Form Key"}]}
    errors = validate(invalid_data)
    assert len(errors) > 0
    assert any("formKey" in err for err in errors)


def test_validate_committed_schema_file() -> None:
    """Test validating against the committed schema file."""
    schema_path = (
        Path(__file__).parent.parent.parent.parent
        / "packages"
        / "core"
        / "src"
        / "imednet"
        / "datadict"
        / "resources"
        / "data-dictionary-1.0.0.schema.json"
    )
    assert schema_path.exists()
    schema = get_schema()

    valid_dict = {
        "metadata": {"schemaVersion": "1.0.0"},
        "forms": [{"formKey": "AE", "variables": []}],
        "unassignedBusinessLogic": [],
    }
    errors = validate(valid_dict, schema=schema)
    assert errors == []


def test_validate_missing_file_path() -> None:
    """Test validating a missing Path object returns FileNotFoundError."""
    missing_path = Path("nonexistent.json")
    errors = validate(missing_path)
    assert errors == [f"FileNotFoundError: Target file '{missing_path}' does not exist"]


def test_validate_missing_file_string() -> None:
    """Test validating missing string file paths returns FileNotFoundError."""
    for filename in [
        "nonexistent.json",
        "missing_file.xml",
        "missing_data.csv",
        "nested/path/to/missing.json",
        "some/path/nonexistent",
    ]:
        errors = validate(filename)
        assert errors == [f"FileNotFoundError: Target file '{filename}' does not exist"]


def test_validate_raw_json_string() -> None:
    """Test validating raw JSON strings parses normally."""
    # Valid raw JSON string
    raw_json = '{"forms": [{"formKey": "F1", "variables": []}]}'
    errors = validate(raw_json)
    assert errors == []

    # Valid raw JSON string containing path separators in values
    raw_json_with_slash = '{"forms": [{"formKey": "F1", "variables": [{"variableName": "V1", "uri": "http://example.com/api"}]}]}'
    errors = validate(raw_json_with_slash)
    assert errors == []

    # Invalid raw JSON string
    invalid_raw_json = '{"forms": [{"formName": "Missing Key"}]}'
    errors = validate(invalid_raw_json)
    assert len(errors) > 0
    assert any("formKey" in err for err in errors)

    # Malformed JSON string
    malformed_json = "not a valid json"
    errors = validate(malformed_json)
    assert len(errors) == 1
    assert "Failed to parse JSON string" in errors[0]


def test_validate_existing_file(tmp_path: Path) -> None:
    """Test validating an existing JSON file via Path and str."""
    data_file = tmp_path / "valid.json"
    data_file.write_text('{"forms": [{"formKey": "F1", "variables": []}]}', encoding="utf-8")

    # Path
    assert validate(data_file) == []

    # Str
    assert validate(str(data_file)) == []


def _write_csv_bundle(
    target_dir: Path, *, orphan_choice: bool = False, dup_form: bool = False
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    forms_rows = "Form ID,Form Key,Form Type,Form Name\n101,DM,Subject,Demographics\n"
    if dup_form:
        forms_rows += "102,DM,Subject,Duplicate Demographics\n"
    (target_dir / "FORMS.csv").write_text(forms_rows, encoding="utf-8")
    (target_dir / "QUESTIONS.csv").write_text(
        "Form,Variable Type,Variable Name,Label\n"
        "DM,Radio,SEX,Biological Sex\n"
        "DM,Date,BRTHDAT,Birth Date\n",
        encoding="utf-8",
    )
    choices_rows = (
        "Form,Variable Type,Variable Name,Choice Text,Choice Value,Position\n"
        "DM,Radio,SEX,Male,M,1\n"
        "DM,Radio,SEX,Female,F,2\n"
    )
    if orphan_choice:
        choices_rows += "DM,Radio,MISSING_VAR,Unknown,U,1\n"
    (target_dir / "CHOICES.csv").write_text(choices_rows, encoding="utf-8")
    (target_dir / "BUSINESS_LOGIC.csv").write_text(
        "Type,Name,Status,ID,Form\nForm,Sex Present,Active,R1,DM\n",
        encoding="utf-8",
    )


def test_validate_multiformat_csv_dir_zip_odm_and_redcap(tmp_path: Path) -> None:
    """validate() supports CSV directories, CSV ZIP archives, CDISC ODM XML, and REDCap CSV."""
    import zipfile

    # 1. Valid 4-file CSV directory and ZIP
    csv_dir = tmp_path / "csv_valid"
    _write_csv_bundle(csv_dir)
    assert validate(csv_dir) == []
    assert validate(csv_dir, schema=get_schema()) == []

    zip_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for csv_file in csv_dir.iterdir():
            zf.write(csv_file, arcname=csv_file.name)
    assert validate(zip_path) == []

    # Invalid CSV directory with broken choice variable reference
    bad_csv_dir = tmp_path / "csv_bad"
    _write_csv_bundle(bad_csv_dir, orphan_choice=True)
    csv_errs = validate(bad_csv_dir)
    assert any("MISSING_VAR" in e for e in csv_errs)

    # Duplicate formKey in CSV ZIP
    dup_csv_dir = tmp_path / "csv_dup"
    _write_csv_bundle(dup_csv_dir, dup_form=True)
    dup_zip = tmp_path / "dup_bundle.zip"
    with zipfile.ZipFile(dup_zip, "w") as zf:
        for csv_file in dup_csv_dir.iterdir():
            zf.write(csv_file, arcname=csv_file.name)
    dup_errs = validate(dup_zip)
    assert any("Duplicate formKey 'DM'" in e for e in dup_errs)

    # 2. Valid and invalid CDISC ODM XML
    valid_odm = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2" FileType="Snapshot" FileOID="F1" CreationDateTime="2026-01-01T00:00:00Z">
  <Study OID="S1">
    <GlobalVariables>
      <StudyName>S1</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>P1</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV1" Name="v1">
      <FormDef OID="DM" Name="Demographics" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG.DM" Mandatory="Yes"/>
      </FormDef>
      <ItemGroupDef OID="IG.DM" Name="DM Group" Repeating="No">
        <ItemRef ItemOID="IT.AGE" Mandatory="Yes"/>
      </ItemGroupDef>
      <ItemDef OID="IT.AGE" Name="AGE" DataType="integer">
        <RangeCheck Comparator="GE" SoftHard="Hard"><CheckValue>18</CheckValue></RangeCheck>
        <RangeCheck Comparator="LE" SoftHard="Hard"><CheckValue>99</CheckValue></RangeCheck>
      </ItemDef>
    </MetaDataVersion>
  </Study>
</ODM>"""
    odm_file = tmp_path / "study.xml"
    odm_file.write_text(valid_odm, encoding="utf-8")
    assert validate(odm_file, schema=get_schema()) == []
    assert validate(valid_odm) == []

    bad_odm = valid_odm.replace(
        '<ItemRef ItemOID="IT.AGE" Mandatory="Yes"/>',
        '<ItemRef ItemOID="IT.AGE" Mandatory="Yes" CollectionExceptionConditionOID="COND.MISSING"/>\n'
        '        <ItemRef ItemOID="IT.GHOST" Mandatory="No"/>',
    )
    bad_odm_file = tmp_path / "bad_study.xml"
    bad_odm_file.write_text(bad_odm, encoding="utf-8")
    odm_errs = validate(bad_odm_file)
    assert any("COND.MISSING" in e for e in odm_errs)
    assert any("IT.GHOST" in e for e in odm_errs)

    # 3. Valid and invalid REDCap CSV (including SQL choice exemption)
    redcap_csv = (
        '"Variable / Field Name","Form Name","Field Type","Field Label",'
        '"Choices, Calculations, OR Slider Labels","Text Validation Type OR Show Slider Number",'
        '"Text Validation Min","Text Validation Max","Branching Logic (Show field only if...)","Required Field?"\n'
        '"sex","demographics","radio","Sex","F, Female | M, Male",,,,,"y"\n'
        '"preg","demographics","yesno","Pregnant?",,,,,"[sex] = \'F\'","y"\n'
        '"site_lookup","demographics","sql","Site SQL","SELECT site FROM sites",,,,,""\n'
    )
    rc_file = tmp_path / "redcap.csv"
    rc_file.write_text(redcap_csv, encoding="utf-8")
    assert validate(rc_file, schema=get_schema()) == []

    bad_rc_file = tmp_path / "bad_redcap.csv"
    bad_rc_file.write_text(
        redcap_csv.replace("[sex] = 'F'", "[nonexistent_var] = 'F'"),
        encoding="utf-8",
    )
    rc_errs = validate(bad_rc_file)
    assert any("nonexistent_var" in e for e in rc_errs)


def test_validate_semantics_duplicates_choices_bounds_types_and_references() -> None:
    """Semantic validation catches duplicate keys/choices, empty choices, inverted bounds, bad types, XML, and refs."""
    from imednet.datadict.models import BusinessLogicRule, Choice, Metadata

    # 1. Duplicate formKey, duplicate formId, duplicate variableName, duplicate choice, empty categorical choices
    dd_dups = DataDictionary(
        forms=[
            Form(
                formKey="DM",
                formId=10,
                variables=[
                    Variable(variableName="AGE", variableType="Integer"),
                    Variable(variableName="AGE", variableType="Integer"),
                    Variable(
                        variableName="SEX",
                        variableType="Radio",
                        choices=[
                            Choice(choiceValue="M", choiceText="Male"),
                            Choice(choiceValue="M", choiceText="Male Dup"),
                        ],
                    ),
                    Variable(variableName="RACE", variableType="Dropdown", choices=[]),
                ],
                businessLogic=[BusinessLogicRule(id="RULE_1", form="DM")],
            ),
            Form(
                formKey="DM",
                formId="10",
                variables=[],
                businessLogic=[BusinessLogicRule(id="RULE_1", form="DM")],
            ),
        ]
    )
    dup_errors = validate(dd_dups)
    assert any("Duplicate formKey 'DM'" in e for e in dup_errors)
    assert any("Duplicate formId '10'" in e for e in dup_errors)
    assert any("Duplicate variableName 'AGE'" in e for e in dup_errors)
    assert any("Duplicate choice code 'M'" in e for e in dup_errors)
    assert any("Categorical variable 'RACE'" in e and "empty Choice list" in e for e in dup_errors)
    assert any("Duplicate BusinessLogicRule id 'RULE_1'" in e for e in dup_errors)

    # API-sourced dictionary exempts empty categorical choices
    dd_api = DataDictionary(
        metadata=Metadata.create(source="api"),
        forms=[Form(formKey="DM", variables=[Variable(variableName="RACE", variableType="Radio")])],
    )
    assert validate(dd_api) == []

    # 2. Inverted numeric, soft numeric, RangeCheck, length, and date bounds + unknown variableType
    dd_bounds = DataDictionary(
        forms=[
            Form(
                formKey="VS",
                variables=[
                    Variable(
                        variableName="SYSBP",
                        variableType="Integer",
                        extraAttributes={"minValue": 200, "maxValue": 80},
                    ),
                    Variable(
                        variableName="DIABP",
                        variableType="Integer",
                        extraAttributes={"softMin": 120, "softMax": 60},
                    ),
                    Variable(
                        variableName="PULSE",
                        variableType="Integer",
                        extraAttributes={
                            "rangeChecks": [
                                {"comparator": "GT", "softHard": "Hard", "checkValue": "100"},
                                {"comparator": "LT", "softHard": "Hard", "checkValue": "100"},
                            ]
                        },
                    ),
                    Variable(
                        variableName="NOTES",
                        variableType="Text",
                        extraAttributes={"minLength": 20, "maxLength": 5},
                    ),
                    Variable(
                        variableName="VISDAT",
                        variableType="Date",
                        extraAttributes={"minDate": "2026-12-31", "maxDate": "2026-01-01"},
                    ),
                    Variable(
                        variableName="BAD_DATE_BOUND",
                        variableType="Date",
                        extraAttributes={"minDate": "2026-02-30"},
                    ),
                    Variable(
                        variableName="WIDGET",
                        variableType="QuantumSpinner",
                    ),
                ],
            )
        ]
    )
    bound_errors = validate(dd_bounds)
    assert any("Inverted numeric bounds" in e and "SYSBP" in e for e in bound_errors)
    assert any("Inverted soft numeric bounds" in e and "DIABP" in e for e in bound_errors)
    assert any("Contradictory RangeCheck" in e and "PULSE" in e for e in bound_errors)
    assert any("Inverted length bounds" in e and "NOTES" in e for e in bound_errors)
    assert any("Inverted date bounds" in e and "VISDAT" in e for e in bound_errors)
    assert any(
        "Invalid temporal minimum bound" in e and "BAD_DATE_BOUND" in e for e in bound_errors
    )
    assert any("Unknown variableType 'QuantumSpinner'" in e for e in bound_errors)

    # 3. Malformed logicRawXml and broken Form / Variable / OID references
    dd_refs = DataDictionary(
        forms=[
            Form(
                formKey="AE",
                variables=[
                    Variable(
                        variableName="AETERM",
                        variableType="Text",
                        extraAttributes={
                            "collectionExceptionConditionOid": "COND.MISSING",
                            "methodOid": "METH.MISSING",
                        },
                    )
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="R_BAD_XML",
                        form="AE",
                        logicRawXml="<rule><unclosed>",
                    ),
                    BusinessLogicRule(
                        id="R_BAD_VAR",
                        form="AE",
                        logic={
                            "conditions": {
                                "condition": {
                                    "variable": "NO_SUCH_VAR",
                                    "operator": "==",
                                    "value": "1",
                                }
                            }
                        },
                    ),
                ],
            )
        ],
        unassignedBusinessLogic=[
            BusinessLogicRule(
                id="R_BAD_FORM",
                form="MISSING_FORM",
            )
        ],
    )
    ref_errors = validate(dd_refs)
    assert any("COND.MISSING" in e for e in ref_errors)
    assert any("METH.MISSING" in e for e in ref_errors)
    assert any("Malformed logicRawXml" in e and "R_BAD_XML" in e for e in ref_errors)
    assert any("NO_SUCH_VAR" in e and "R_BAD_VAR" in e for e in ref_errors)
    assert any("MISSING_FORM" in e and "R_BAD_FORM" in e for e in ref_errors)


def test_validate_raw_redcap_string_header_only_csv_and_dict_formal_expression(
    tmp_path: Path,
) -> None:
    """validate() supports raw REDCap CSV strings, rejects non-REDCap CSVs, and normalizes dict FormalExpression."""
    from imednet.datadict.logic import extract_variable_constraints

    valid_rc = (
        "Variable / Field Name,Form Name,Field Type,Field Label\n"
        "subj_id,demographics,text,Subject ID\n"
    )
    assert validate(valid_rc) == []

    bad_rc = (
        "Variable / Field Name,Form Name,Field Type,Field Label,Branching Logic\n"
        "subj_id,demographics,text,Subject ID,[missing_var] = '1'\n"
    )
    rc_errs = validate(bad_rc)
    assert any("missing_var" in e for e in rc_errs)

    header_only_forms = tmp_path / "FORMS.csv"
    header_only_forms.write_text("Form Key,Form Name,Form ID,Form Type\n", encoding="utf-8")
    forms_errs = validate(header_only_forms)
    assert any("Variable / Field Name" in e for e in forms_errs)

    v_dict_fe = Variable(
        variableName="DIA_BP",
        variableType="Integer",
        extraAttributes={
            "rangeChecks": [
                {
                    "softHard": "Hard",
                    "formalExpression": {
                        "expression": ". < ../IT.SYS_BP",
                        "context": "XPath",
                    },
                }
            ]
        },
    )
    dd_fe = DataDictionary(
        forms=[
            Form(
                formKey="VS",
                variables=[
                    Variable(variableName="SYS_BP", variableType="Integer"),
                    v_dict_fe,
                ],
            )
        ]
    )
    assert validate(dd_fe) == []
    c = extract_variable_constraints(v_dict_fe)
    assert len(c.range_checks) == 1
    assert c.range_checks[0].formal_expression == ". < ../IT.SYS_BP"
    assert c.range_checks[0].context == "XPath"
