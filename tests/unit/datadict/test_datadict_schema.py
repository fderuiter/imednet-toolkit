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
