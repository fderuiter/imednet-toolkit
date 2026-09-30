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
