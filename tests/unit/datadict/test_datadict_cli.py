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
    assert "to-odm" in captured.out
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
    mock_sdk.close.assert_called_once()


def test_cli_from_api_closes_sdk_on_error(capsys: pytest.CaptureFixture[str]) -> None:
    """from-api closes the SDK HTTP transport even when load_from_api raises."""
    import httpx

    from imednet.sdk import ImednetSDK

    sdk = ImednetSDK(
        api_key="test_key",  # pragma: allowlist secret
        security_key="test_sec",  # pragma: allowlist secret
        transport=httpx.MockTransport(lambda _req: httpx.Response(500)),
    )
    assert not sdk._client._client.is_closed

    with (
        patch("imednet.cli.datadict.get_sdk", return_value=sdk),
        patch("imednet.cli.datadict.load_from_api", side_effect=RuntimeError("api boom")),
        pytest.raises(SystemExit) as exc_info,
    ):
        app(["datadict", "from-api", "--study-key", "STUDY1"])

    assert exc_info.value.code == 1
    assert sdk._client._client.is_closed
    assert "api boom" in capsys.readouterr().err


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


def test_cli_to_odm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test exporting JSON data dictionary to CDISC ODM XML via CLI."""
    dd_data = {
        "metadata": {
            "schemaVersion": "1.0.0",
            "studyKey": "CLI_ODM_EXPORT_01",
        },
        "forms": [
            {
                "formKey": "DM",
                "formName": "Demographics",
                "variables": [
                    {
                        "variableName": "AGE",
                        "label": "Subject Age",
                        "variableType": "Integer",
                    }
                ],
            }
        ],
    }
    json_path = tmp_path / "study.json"
    json_path.write_text(json.dumps(dd_data), encoding="utf-8")

    # 1. Output to file
    xml_out = tmp_path / "exported.xml"
    app(["datadict", "to-odm", str(json_path), "-o", str(xml_out)])
    assert xml_out.exists()
    content = xml_out.read_text(encoding="utf-8")
    assert "<ODM" in content
    assert '<Study OID="CLI_ODM_EXPORT_01">' in content
    assert '<FormDef OID="DM"' in content
    capsys.readouterr()

    # 2. Output to stdout
    app(["datadict", "to-odm", str(json_path)])
    captured = capsys.readouterr()
    assert "<ODM" in captured.out
    assert '<Study OID="CLI_ODM_EXPORT_01">' in captured.out


def test_cli_from_odm_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    """Test datadict from-odm with nonexistent file produces FileNotFoundError and exits with 1."""
    with pytest.raises(SystemExit) as exc:
        app(["datadict", "from-odm", "nonexistent.xml"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Error loading from CDISC ODM:" in captured.err
    assert "Target file 'nonexistent.xml' does not exist" in captured.err


def test_cli_from_redcap_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    """Test datadict from-redcap with nonexistent file produces FileNotFoundError and exits with 1."""
    with pytest.raises(SystemExit) as exc:
        app(["datadict", "from-redcap", "nonexistent.csv"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Error loading from REDCap:" in captured.err
    assert "Target file 'nonexistent.csv' does not exist" in captured.err


def test_cli_odm_round_trip_divergent_variables_and_auto_query(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI to-odm and from-odm --strict preserve divergent Variables and Auto Query with zero diffs."""
    dd_data = {
        "metadata": {
            "schemaVersion": "1.0.0",
            "studyKey": "CLI_DIVERGENT_ODM",
            "source": "cdisc_odm",
            "extraAttributes": {
                "studyName": "CLI_DIVERGENT_ODM",
                "protocolName": "CLI_DIVERGENT_ODM",
            },
        },
        "forms": [
            {
                "formKey": "DM",
                "formName": "Demographics",
                "formType": "Subject",
                "variables": [
                    {
                        "variableName": "VISDAT",
                        "label": "Demographics Visit Date",
                        "variableType": "Date",
                        "sequence": 1,
                        "required": "Auto Query",
                        "format": "YYYY-MM-DD",
                    },
                    {
                        "variableName": "SEX",
                        "label": "Sex",
                        "variableType": "Radio",
                        "sequence": 2,
                        "required": "Yes",
                        "choices": [
                            {"choiceValue": "M", "choiceText": "Male", "position": 1},
                            {"choiceValue": "F", "choiceText": "Female", "position": 2},
                        ],
                    },
                ],
            },
            {
                "formKey": "SV",
                "formName": "Subject Visit",
                "formType": "Subject",
                "variables": [
                    {
                        "variableName": "VISDAT",
                        "label": "Follow-up Visit Date",
                        "variableType": "Date",
                        "sequence": 1,
                        "required": "Yes",
                        "format": "10",
                    },
                    {
                        "variableName": "SEX",
                        "label": "Sex",
                        "variableType": "Radio",
                        "sequence": 2,
                        "required": "No",
                        "choices": [
                            {"choiceValue": "M", "choiceText": "Male", "position": 1},
                            {"choiceValue": "F", "choiceText": "Female", "position": 2},
                        ],
                    },
                ],
            },
        ],
    }
    src_json = tmp_path / "src.json"
    odm_xml = tmp_path / "exported.xml"
    reloaded_json = tmp_path / "reloaded.json"
    src_json.write_text(json.dumps(dd_data), encoding="utf-8")

    app(["datadict", "to-odm", str(src_json), "-o", str(odm_xml)])
    xml_text = odm_xml.read_text(encoding="utf-8")
    assert 'OID="IT.DM.VISDAT"' in xml_text
    assert 'OID="IT.SV.VISDAT"' in xml_text
    assert 'OID="SEX"' in xml_text
    assert 'OID="CL_SEX"' in xml_text
    assert 'imednet:RequiredPolicy="Auto Query"' in xml_text
    capsys.readouterr()

    app(["datadict", "from-odm", str(odm_xml), "--strict", "-o", str(reloaded_json)])
    capsys.readouterr()

    app(["datadict", "diff", str(src_json), str(reloaded_json), "--format", "json"])
    captured = capsys.readouterr()
    diff_payload = json.loads(captured.out)
    assert diff_payload["has_changes"] is False
    assert diff_payload["total_changes"] == 0


def test_cli_datadict_redacts_credentials_across_stderr_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """datadict subcommands redact IMEDNET_API_KEY, IMEDNET_SECURITY_KEY, and IMEDNET_OIDC_TOKEN on stderr."""
    from imednet.errors import AuthenticationError

    api_secret = "datadict_api_secret_val_111"  # pragma: allowlist secret
    sec_secret = "datadict_sec_secret_val_222"  # pragma: allowlist secret
    oidc_secret = "datadict_oidc_token_val_333"  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_API_KEY", api_secret)
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", sec_secret)
    monkeypatch.setenv("IMEDNET_OIDC_TOKEN", oidc_secret)

    leak_msg = f"failure with {api_secret} and {sec_secret} and {oidc_secret}"

    # 1. from-api with AuthenticationError and unexpected RuntimeError closes SDK and redacts
    for err in (AuthenticationError(leak_msg), RuntimeError(leak_msg)):
        mock_sdk = Mock()
        with (
            patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk),
            patch("imednet.cli.datadict.load_from_api", side_effect=err),
            pytest.raises(SystemExit) as exc_api,
        ):
            app(["datadict", "from-api", "--study-key", "STUDY1"])
        assert exc_api.value.code == 1
        mock_sdk.close.assert_called_once()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "********" in captured.err
        for secret in (api_secret, sec_secret, oidc_secret):
            assert secret not in captured.err

    # 2. File-based datadict subcommands redact secrets in exception/validation messages
    valid_dd = tmp_path / "valid_dd.json"
    valid_dd.write_text(
        json.dumps({"metadata": {"schemaVersion": "1.0.0"}, "forms": []}),
        encoding="utf-8",
    )

    subcommand_patches = [
        (
            ["datadict", "from-csv", "in.csv"],
            "imednet.cli.datadict.load_from_csv",
            RuntimeError(leak_msg),
        ),
        (
            ["datadict", "from-odm", "in.xml"],
            "imednet.cli.datadict.load_from_cdisc_odm",
            RuntimeError(leak_msg),
        ),
        (
            ["datadict", "from-redcap", "in.csv"],
            "imednet.cli.datadict.load_from_redcap",
            RuntimeError(leak_msg),
        ),
        (
            ["datadict", "to-odm", str(valid_dd)],
            "imednet.cli.datadict.to_odm_xml",
            RuntimeError(leak_msg),
        ),
        (["datadict", "validate", str(valid_dd)], "imednet.cli.datadict.validate", [leak_msg]),
    ]
    for argv, target, effect in subcommand_patches:
        kwargs = {"return_value": effect} if isinstance(effect, list) else {"side_effect": effect}
        with patch(target, **kwargs), pytest.raises(SystemExit) as exc_sub:
            app(argv)
        assert exc_sub.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "********" in captured.err
        for secret in (api_secret, sec_secret, oidc_secret):
            assert secret not in captured.err


def test_cli_datadict_keyboard_interrupt_exits_130(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C in datadict subcommands prints Aborted by user. to stderr and exits 130."""
    with (
        patch("imednet.cli.datadict.load_from_csv", side_effect=KeyboardInterrupt()),
        pytest.raises(SystemExit) as exc_csv,
    ):
        app(["datadict", "from-csv", "some_dir"])
    assert exc_csv.value.code == 130
    cap_csv = capsys.readouterr()
    assert cap_csv.out == ""
    assert "Aborted by user." in cap_csv.err

    mock_sdk = Mock()
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk),
        patch("imednet.cli.datadict.load_from_api", side_effect=KeyboardInterrupt()),
        pytest.raises(SystemExit) as exc_api,
    ):
        app(["datadict", "from-api", "--study-key", "STUDY1"])
    assert exc_api.value.code == 130
    mock_sdk.close.assert_called_once()
    cap_api = capsys.readouterr()
    assert cap_api.out == ""
    assert "Aborted by user." in cap_api.err


def test_cli_from_redcap_bare_missing_filename_exits_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """from-redcap with a bare missing filename exits 1 with FileNotFoundError and no traceback."""
    with pytest.raises(SystemExit) as exc:
        app(["datadict", "from-redcap", "missing_bare_redcap_file"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error loading from REDCap:" in captured.err
    assert "missing_bare_redcap_file" in captured.err
    assert "Traceback" not in captured.err


def test_cli_datadict_output_confirmations_on_stderr_and_oserror_handling(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """datadict -o commands write confirmations to stderr and exit 1 cleanly on OSError."""
    odm_file = tmp_path / "sample.xml"
    odm_file.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3">'
        '<Study OID="S1"><MetaDataVersion OID="M1">'
        '<FormDef OID="F1" Name="Form 1"/></MetaDataVersion></Study></ODM>',
        encoding="utf-8",
    )
    redcap_file = tmp_path / "sample_redcap.csv"
    redcap_file.write_text(
        '"Variable / Field Name","Form Name","Field Type","Field Label"\n'
        '"v1","demographics","text","Label 1"\n',
        encoding="utf-8",
    )
    valid_dd = tmp_path / "valid_dd.json"
    valid_dd.write_text(
        json.dumps({"metadata": {"schemaVersion": "1.0.0"}, "forms": [{"formKey": "F1"}]}),
        encoding="utf-8",
    )

    mock_sdk = Mock()
    mock_sdk.forms.list.return_value = [{"form_key": "F1", "form_name": "Form 1"}]
    mock_sdk.variables.list.return_value = []

    commands_with_outputs = [
        (["datadict", "from-csv", str(FIXTURES_DIR), "-o", str(tmp_path / "o_csv.json")], False),
        (["datadict", "from-odm", str(odm_file), "-o", str(tmp_path / "o_odm.json")], False),
        (
            ["datadict", "from-redcap", str(redcap_file), "-o", str(tmp_path / "o_rc.json")],
            False,
        ),
        (
            ["datadict", "from-api", "--study-key", "S1", "-o", str(tmp_path / "o_api.json")],
            True,
        ),
        (["datadict", "to-odm", str(valid_dd), "-o", str(tmp_path / "o_export.xml")], False),
        (["datadict", "schema", "-o", str(tmp_path / "o_schema.json")], False),
    ]

    for argv, needs_sdk in commands_with_outputs:
        if needs_sdk:
            with patch("imednet.cli.datadict.get_sdk", return_value=mock_sdk):
                app(argv)
        else:
            app(argv)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Successfully wrote" in captured.err

    # Unwritable directory target triggers OSError -> exit 1 without traceback
    unwritable_dir = tmp_path / "dir_target"
    unwritable_dir.mkdir()
    oserror_commands = [
        (["datadict", "from-csv", str(FIXTURES_DIR), "-o", str(unwritable_dir)], False),
        (["datadict", "from-odm", str(odm_file), "-o", str(unwritable_dir)], False),
        (["datadict", "from-redcap", str(redcap_file), "-o", str(unwritable_dir)], False),
        (["datadict", "from-api", "--study-key", "S1", "-o", str(unwritable_dir)], True),
        (["datadict", "to-odm", str(valid_dd), "-o", str(unwritable_dir)], False),
        (["datadict", "schema", "-o", str(unwritable_dir)], False),
    ]
    for argv, needs_sdk in oserror_commands:
        with pytest.raises(SystemExit) as exc:
            if needs_sdk:
                with patch("imednet.cli.datadict.get_sdk", return_value=mock_sdk):
                    app(argv)
            else:
                app(argv)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Error" in captured.err
        assert "Traceback" not in captured.err


def test_cli_datadict_validate_json_format_and_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """datadict validate supports --format json and IMEDNET_CLI_OUTPUT_FORMAT=json."""
    valid_dd = tmp_path / "valid.json"
    valid_dd.write_text(
        json.dumps({"metadata": {"schemaVersion": "1.0.0"}, "forms": [{"formKey": "F1"}]}),
        encoding="utf-8",
    )

    app(["datadict", "validate", str(valid_dd), "--format", "json"])
    cap_ok = capsys.readouterr()
    assert cap_ok.err == ""
    payload_ok = json.loads(cap_ok.out)
    assert payload_ok["schemaVersion"] == "1.0"
    assert payload_ok["valid"] is True
    assert payload_ok["error_count"] == 0
    assert payload_ok["errors"] == []

    invalid_dd = tmp_path / "invalid.json"
    invalid_dd.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    with pytest.raises(SystemExit) as exc_bad:
        app(["datadict", "validate", str(invalid_dd)])
    assert exc_bad.value.code == 1
    cap_bad = capsys.readouterr()
    assert cap_bad.err == ""
    payload_bad = json.loads(cap_bad.out)
    assert payload_bad["valid"] is False
    assert payload_bad["error_count"] >= 1
    assert any(
        "forms" in err or "foo" in err or "Additional" in err for err in payload_bad["errors"]
    )


def test_cli_from_csv_study_key_and_multiformat_validate_diff_to_odm(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """from-csv supports --study-key, and validate/diff/to-odm accept CSV dir, .zip, .xml, and REDCap .csv."""
    import zipfile

    # 1. Build a .zip archive from the 4-file CSV fixtures
    zip_path = tmp_path / "study_export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for csv_name in ("FORMS.csv", "QUESTIONS.csv", "CHOICES.csv", "BUSINESS_LOGIC.csv"):
            zf.write(FIXTURES_DIR / csv_name, arcname=csv_name)

    # 2. from-csv --study-key STUDY sets metadata.studyKey on both directory and .zip
    app(["datadict", "from-csv", str(FIXTURES_DIR), "--study-key", "STUDY"])
    cap_csv = capsys.readouterr()
    dd_from_csv = json.loads(cap_csv.out)
    assert dd_from_csv["metadata"]["studyKey"] == "STUDY"

    out_json = tmp_path / "study.json"
    app(["datadict", "from-csv", str(zip_path), "--study-key", "STUDY", "-o", str(out_json)])
    capsys.readouterr()
    assert json.loads(out_json.read_text(encoding="utf-8"))["metadata"]["studyKey"] == "STUDY"

    # Duplicate --study-key rejected by UniqueStoreAction with exit code 2
    with pytest.raises(SystemExit) as dup_exc:
        app(["datadict", "from-csv", str(FIXTURES_DIR), "--study-key", "S1", "--study-key", "S2"])
    assert dup_exc.value.code == 2
    capsys.readouterr()

    # 3. Prepare .xml ODM and REDCap .csv fixtures
    odm_path = tmp_path / "study.xml"
    app(["datadict", "to-odm", str(out_json), "-o", str(odm_path)])
    capsys.readouterr()
    assert odm_path.is_file()

    redcap_path = tmp_path / "redcap.csv"
    redcap_path.write_text(
        '"Variable / Field Name","Form Name","Field Type","Field Label",'
        '"Choices, Calculations, OR Slider Labels","Text Validation Type OR Show Slider Number","Required Field?"\n'
        '"wt","vitals","text","Weight",,"number_2dp","y"\n'
        '"pain","vitals","slider","Pain Score","0, Low | 100, High",,\n'
        '"sym","vitals","checkbox","Symptoms","0, None | 1, Cough",,\n',
        encoding="utf-8",
    )

    sources = [FIXTURES_DIR, zip_path, odm_path, redcap_path, out_json]

    # 4. validate exits 0 across CSV dir, .zip, .xml, REDCap .csv, and .json
    for src in sources:
        app(["datadict", "validate", str(src)])
        cap_val = capsys.readouterr()
        assert "Validation passed" in cap_val.out
        assert cap_val.err == ""

    # Preserve custom --schema JSON Schema validation on .json targets
    strict_custom_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["customRequiredTopLevelField"],
    }
    custom_schema_path = tmp_path / "custom_schema.json"
    custom_schema_path.write_text(json.dumps(strict_custom_schema), encoding="utf-8")
    with pytest.raises(SystemExit) as schema_exc:
        app(["datadict", "validate", str(out_json), "--schema", str(custom_schema_path)])
    assert schema_exc.value.code == 1
    cap_schema = capsys.readouterr()
    assert "customRequiredTopLevelField" in cap_schema.err

    # 5. diff exits 0 across CSV dir, .zip, .xml, REDCap .csv, and .json
    for src in sources:
        app(["datadict", "diff", str(src), str(src), "--format", "json"])
        cap_diff = capsys.readouterr()
        diff_payload = json.loads(cap_diff.out)
        assert diff_payload["has_changes"] is False

    # Cross-format diff (CSV directory vs .zip archive)
    app(["datadict", "diff", str(FIXTURES_DIR), str(zip_path), "--format", "json"])
    cap_cross = capsys.readouterr()
    assert json.loads(cap_cross.out)["has_changes"] is False

    # 6. to-odm exits 0 across CSV dir, .zip, .xml, and REDCap .csv
    for idx, src in enumerate(sources):
        exported_xml = tmp_path / f"exported_{idx}.xml"
        app(["datadict", "to-odm", str(src), "-o", str(exported_xml)])
        cap_odm = capsys.readouterr()
        assert "Successfully wrote CDISC ODM XML" in cap_odm.err
        assert "<ODM" in exported_xml.read_text(encoding="utf-8")
