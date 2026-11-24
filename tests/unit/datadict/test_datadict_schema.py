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
    raw_json_with_slash = (
        '{"forms": [{"formKey": "F1", "variables": '
        '[{"variableName": "V1", "extraAttributes": {"uri": "http://example.com/api"}}]}]}'
    )
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


def test_validate_catches_duplicate_keys_broken_references_and_inverted_bounds() -> None:
    """datadict.schema.validate catches duplicate keys, broken references, and inverted bounds."""
    from imednet.datadict.logic import parse_logic_xml
    from imednet.datadict.models import BusinessLogicRule, Choice

    # 1. Duplicate formKey, formId, variableName, choiceValue, and rule id
    dup_dd = DataDictionary(
        forms=[
            Form(
                formKey="DM",
                formId="10",
                variables=[
                    Variable(
                        variableName="SEX",
                        choices=[
                            Choice(choiceValue="M", choiceText="Male"),
                            Choice(choiceValue="M", choiceText="Duplicate Male"),
                        ],
                    ),
                    Variable(variableName="SEX"),
                ],
                businessLogic=[BusinessLogicRule(id="R1", name="Rule 1", form="DM")],
            ),
            Form(
                formKey="DM",
                formId="10",
                variables=[],
                businessLogic=[BusinessLogicRule(id="R1", name="Rule 1 Dup", form="DM")],
            ),
        ]
    )
    dup_errors = "\n".join(validate(dup_dd))
    assert "Duplicate formKey 'DM'" in dup_errors
    assert "Duplicate formId '10'" in dup_errors
    assert "Duplicate variableName 'SEX'" in dup_errors
    assert "Duplicate choiceValue 'M'" in dup_errors
    assert "Duplicate business logic rule id 'R1'" in dup_errors

    # 2. Broken form, variable, methodOid, and condition references
    broken_dd = DataDictionary(
        forms=[
            Form(
                formKey="VS",
                extraAttributes={"collectionExceptionConditionOid": "MISSING_COND"},
                variables=[
                    Variable(
                        variableName="SYSBP",
                        extraAttributes={"methodOid": "MISSING_METH"},
                    )
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="R_BROKEN_VAR",
                        name="Broken Var Ref",
                        form="VS",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition><variable>NONEXISTENT_VAR</variable>"
                            "<operator>==</operator><value>1</value></condition></conditions></rule>"
                        ),
                    )
                ],
            )
        ],
        unassignedBusinessLogic=[
            BusinessLogicRule(id="R_BROKEN_FORM", name="Missing Form", form="NO_SUCH_FORM")
        ],
    )
    broken_errors = "\n".join(validate(broken_dd, schema=get_schema()))
    assert "MISSING_COND" in broken_errors
    assert "MISSING_METH" in broken_errors
    assert "NONEXISTENT_VAR" in broken_errors
    assert "NO_SUCH_FORM" in broken_errors

    # 3. Inverted numeric bounds (minValue > maxValue) and length bounds (minLength > maxLength)
    inverted_dd = DataDictionary(
        forms=[
            Form(
                formKey="LB",
                variables=[
                    Variable(
                        variableName="GLUC",
                        variableType="Float",
                        extraAttributes={"minValue": 200, "maxValue": 50},
                    ),
                    Variable(
                        variableName="SUBJID",
                        variableType="Text",
                        extraAttributes={"minLength": 12, "maxLength": 6},
                    ),
                ],
            )
        ]
    )
    inv_errors = "\n".join(validate(inverted_dd, schema=get_schema()))
    assert "minValue (200.0) > maxValue (50.0)" in inv_errors
    assert "minLength (12) > maxLength (6)" in inv_errors


def test_validate_requires_root_forms_and_rejects_undeclared_keys() -> None:
    """validate() rejects payloads missing forms or containing misspelled/undeclared keys."""
    import json

    from imednet.datadict.schema import SCHEMA_PATH

    committed_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert get_schema() == committed_schema

    for schema_arg in (None, committed_schema):
        empty_errors = "\n".join(validate({}, schema=schema_arg))
        assert "forms" in empty_errors

        foo_errors = "\n".join(validate('{"foo": "bar"}', schema=schema_arg))
        assert "forms" in foo_errors

        typo_payload = {
            "metadata": {"schemaVersion": "1.0.0", "study_key": "TYPO"},
            "forms": [
                {
                    "formKey": "F1",
                    "formkey_typo": "bad",
                    "variables": [
                        {
                            "variableName": "V1",
                            "varType": "Text",
                            "choices": [{"choiceValue": "1", "choiceText": "Yes", "pos": 1}],
                        }
                    ],
                }
            ],
        }
        typo_errors = "\n".join(validate(typo_payload, schema=schema_arg))
        assert typo_errors != ""


def test_validate_catches_malformed_business_logic_xml_and_csv_rules() -> None:
    """validate() fails on malformed rule.logicRawXml and broken BUSINESS_LOGIC.csv XML."""
    import io

    from imednet.datadict import load_from_csv_files
    from imednet.datadict.models import BusinessLogicRule

    bad_rule_dd = DataDictionary(
        forms=[
            Form(
                formKey="F1",
                variables=[Variable(variableName="V1")],
                businessLogic=[
                    BusinessLogicRule(
                        id="R_MALFORMED",
                        name="Malformed XML",
                        type="Form",
                        form="F1",
                        logic=None,
                        logicRawXml="<rule><conditions><unclosed>",
                    )
                ],
            )
        ]
    )
    errs = "\n".join(validate(bad_rule_dd, schema=get_schema()))
    assert "logicRawXml" in errs
    assert "Malformed business logic XML" in errs

    forms_csv = "Form Key,Form Name\nF1,Form 1\n"
    questions_csv = "Form Key,Variable Name,Label,Variable Type\nF1,V1,Var 1,Text\n"
    choices_csv = "Form Key,Variable Name,Choice Value,Choice Text,Position\n"
    broken_logic_csv = (
        "ID,Name,Type,Status,Form,Sequence,Logic\n"
        '99,Broken Check,Form,Active,F1,1,"<rule><conditions><broken>"\n'
    )

    for no_raw in (False, True):
        compiled = load_from_csv_files(
            forms_file=io.StringIO(forms_csv),
            questions_file=io.StringIO(questions_csv),
            choices_file=io.StringIO(choices_csv),
            business_logic_file=io.StringIO(broken_logic_csv),
            strict=False,
            no_raw_xml=no_raw,
        )
        compiled_errors = "\n".join(validate(compiled, schema=get_schema()))
        assert compiled_errors != ""
        assert "Malformed" in compiled_errors or "logic" in compiled_errors
