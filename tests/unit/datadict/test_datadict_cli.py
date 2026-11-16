"""Tests for datadict CLI subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from imednet.cli import app

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "data_dictionary"


def test_cli_datadict_help(capsys: pytest.CaptureFixture[str]) -> None:
    """Test datadict CLI help message."""
    with pytest.raises(SystemExit) as exc:
        app(["datadict", "--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "from-csv" in captured.out
    assert "from-odm" in captured.out
    assert "from-redcap" in captured.out
    assert "from-api" in captured.out
    assert "validate" in captured.out
    assert "diff" in captured.out
    assert "schema" in captured.out


def test_cli_from_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test compiling JSON from CSV via CLI."""
    out_file = tmp_path / "compiled.json"
    app(["datadict", "from-csv", str(FIXTURES_DIR), "-o", str(out_file)])

    assert out_file.exists()
    content = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(content["forms"]) == 3


def test_cli_schema_and_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test exporting schema and validating file via CLI."""
    # 1. Export schema
    schema_file = tmp_path / "schema.json"
    app(["datadict", "schema", "-o", str(schema_file)])
    assert schema_file.exists()

    # 2. Compile valid file
    out_file = tmp_path / "datadict.json"
    app(["datadict", "from-csv", str(FIXTURES_DIR), "-o", str(out_file)])

    # 3. Validate
    app(["datadict", "validate", str(out_file), "--schema", str(schema_file)])
    captured = capsys.readouterr()
    assert "Validation passed" in captured.out


def test_cli_validate_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    """Test datadict validate with nonexistent file produces FileNotFoundError."""
    with pytest.raises(SystemExit) as exc:
        app(["datadict", "validate", "nonexistent.json"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "FileNotFoundError: Target file 'nonexistent.json' does not exist" in captured.err


def test_cli_diff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test structural diff command in CLI."""
    file1 = tmp_path / "dd1.json"
    file2 = tmp_path / "dd2.json"

    d1 = {
        "metadata": {"schemaVersion": "1.0.0"},
        "forms": [{"formKey": "DM", "formName": "Demographics", "variables": []}],
        "unassignedBusinessLogic": [],
    }
    d2 = {
        "metadata": {"schemaVersion": "1.0.0"},
        "forms": [{"formKey": "DM", "formName": "Demographics Renamed", "variables": []}],
        "unassignedBusinessLogic": [],
    }

    file1.write_text(json.dumps(d1), encoding="utf-8")
    file2.write_text(json.dumps(d2), encoding="utf-8")

    app(["datadict", "diff", str(file1), str(file2), "--format", "json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["has_changes"] is True


def test_cli_from_api(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test from-api command with mocked SDK."""
    mock_sdk = Mock()
    mock_sdk.forms.list.return_value = [{"form_key": "AE", "form_name": "Adverse Events"}]
    mock_sdk.variables.list.return_value = [{"form_key": "AE", "variable_name": "AESTDAT"}]

    out_file = tmp_path / "api_out.json"
    with patch("imednet.cli.datadict.get_sdk", return_value=mock_sdk):
        app(["datadict", "from-api", "--study-key", "STUDY1", "-o", str(out_file)])

    assert out_file.exists()
    content = json.loads(out_file.read_text(encoding="utf-8"))
    assert content["metadata"]["studyKey"] == "STUDY1"
    assert content["forms"][0]["formKey"] == "AE"


def test_cli_from_odm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test compiling JSON from CDISC ODM XML via CLI."""
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">
  <Study OID="CLI_STUDY_ODM">
    <MetaDataVersion OID="MDV1">
      <FormDef OID="F1" Name="Form One">
        <ItemGroupRef ItemGroupOID="IG1" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG1" Name="Group One">
        <ItemRef ItemOID="ITEM1" OrderNumber="1" Mandatory="Yes"/>
      </ItemGroupDef>
      <ItemDef OID="ITEM1" Name="ITEM1" DataType="text">
        <Question><TranslatedText>Question 1</TranslatedText></Question>
      </ItemDef>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    xml_file = tmp_path / "odm.xml"
    xml_file.write_text(xml_content, encoding="utf-8")

    # 1. Output to file
    out_file = tmp_path / "from_odm.json"
    app(["datadict", "from-odm", str(xml_file), "-o", str(out_file)])
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["metadata"]["studyKey"] == "CLI_STUDY_ODM"
    assert data["forms"][0]["formKey"] == "F1"
    assert data["forms"][0]["variables"][0]["variableName"] == "ITEM1"
    capsys.readouterr()

    # 2. Output to stdout
    app(["datadict", "from-odm", str(xml_file)])
    captured = capsys.readouterr()
    stdout_data = json.loads(captured.out)
    assert stdout_data["metadata"]["studyKey"] == "CLI_STUDY_ODM"

    # 3. Validate via CLI validate command
    app(["datadict", "validate", str(out_file)])
    captured = capsys.readouterr()
    assert "Validation passed" in captured.out


def test_cli_from_redcap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test compiling JSON from REDCap CSV via CLI."""
    csv_content = """"Variable / Field Name","Form Name","Field Type","Field Label","Choices, Calculations, OR Slider Labels"
"subj_id","demographics","text","Subject ID",
"gender","demographics","radio","Gender","1, Male | 2, Female"
"""
    csv_file = tmp_path / "redcap.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    # 1. Output to file with study key override
    out_file = tmp_path / "from_redcap.json"
    app(
        [
            "datadict",
            "from-redcap",
            str(csv_file),
            "--study-key",
            "CLI_REDCAP_STUDY",
            "-o",
            str(out_file),
        ]
    )
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["metadata"]["studyKey"] == "CLI_REDCAP_STUDY"
    assert data["forms"][0]["formKey"] == "demographics"
    assert len(data["forms"][0]["variables"]) == 2
    capsys.readouterr()

    # 2. Output to stdout
    app(["datadict", "from-redcap", str(csv_file)])
    captured = capsys.readouterr()
    stdout_data = json.loads(captured.out)
    assert stdout_data["forms"][0]["formKey"] == "demographics"

    # 3. Validate via CLI validate command
    app(["datadict", "validate", str(out_file)])
    captured = capsys.readouterr()
    assert "Validation passed" in captured.out
