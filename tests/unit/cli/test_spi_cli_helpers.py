"""Unit tests for shared SPI CLI helpers, simulation mode, and hybrid study_key contracts."""

from __future__ import annotations

import argparse
import json
import os
from enum import Enum
from pathlib import Path
from unittest.mock import patch

import pytest

from imednet import cli as cli_mod
from imednet.cli.utils.output import _format_cell_value
from imednet.simulation import reset_default_simulation_state
from imednet.spi import cli as spi_cli


class _SampleRole(Enum):
    ADMIN = "Admin"
    COORDINATOR = "Coordinator"


@pytest.fixture(autouse=True)
def _clean_cli_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure API credentials and simulation env vars start clean and reset simulation state."""
    for var in (
        "IMEDNET_API_KEY",
        "IMEDNET_SECURITY_KEY",
        "IMEDNET_OIDC_TOKEN",
        "IMEDNET_SIMULATION_MODE",
        "IMEDNET_CLI_OUTPUT_FORMAT",
        "IMEDNET_NON_INTERACTIVE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    reset_default_simulation_state()
    yield
    reset_default_simulation_state()


def test_spi_cli_exports_shared_helpers() -> None:
    """Verify imednet.spi.cli exposes shared study-key, simulation, and redaction helpers."""
    expected = {
        "SIMULATION_MODE_ENV",
        "UniqueStoreAction",
        "add_simulation_arg",
        "add_study_key_args",
        "cli_execution_guard",
        "load_offline_schema",
        "parse_email_notify",
        "positive_int",
        "redact_cli_secrets",
        "resolve_cli_schema",
        "resolve_hybrid_study_and_key",
        "resolve_study_key",
        "simulation_mode_context",
    }
    assert expected.issubset(set(spi_cli.__all__))
    for name in expected:
        assert hasattr(spi_cli, name)


def test_format_cell_value_normalizes_nested_enums() -> None:
    """Verify _format_cell_value serializes nested Enum values without repr brackets."""
    list_text = _format_cell_value([_SampleRole.ADMIN, _SampleRole.COORDINATOR])
    assert list_text == "Admin, Coordinator"
    assert "_SampleRole" not in list_text

    dict_text = _format_cell_value({"role": _SampleRole.ADMIN})
    assert dict_text == "{'role': 'Admin'}"
    assert "_SampleRole" not in dict_text

    nested_list_text = _format_cell_value([{"role": _SampleRole.ADMIN}])
    assert nested_list_text == "[{'role': 'Admin'}]"
    assert "_SampleRole" not in nested_list_text

    tuple_text = _format_cell_value((_SampleRole.COORDINATOR,))
    assert tuple_text == "('Coordinator',)"
    assert "_SampleRole" not in tuple_text


def test_resolve_hybrid_study_and_key_grammar() -> None:
    """Verify deterministic hybrid positional/flag resolution and conflict exit code 2."""
    parser = argparse.ArgumentParser()
    spi_cli.add_study_key_args(parser)
    parser.add_argument(
        "-k",
        "--subject-key",
        dest="subject_key_flag",
        default=None,
        action=spi_cli.UniqueStoreAction,
    )
    parser.add_argument("subject_key", nargs="?", default=None)

    # Positional pair
    ns = parser.parse_args(["STUDY-1", "SUBJ-1"])
    assert spi_cli.resolve_hybrid_study_and_key(parser, ns) == ("STUDY-1", "SUBJ-1")

    # Flag study + single positional -> maps positional to subject_key
    ns = parser.parse_args(["--study-key", "STUDY-1", "SUBJ-1"])
    assert spi_cli.resolve_hybrid_study_and_key(parser, ns) == ("STUDY-1", "SUBJ-1")

    # Flag subject + single positional -> maps positional to study_key
    ns = parser.parse_args(["STUDY-1", "--subject-key", "SUBJ-1"])
    assert spi_cli.resolve_hybrid_study_and_key(parser, ns) == ("STUDY-1", "SUBJ-1")

    # Matching flag + positional pair allowed
    ns = parser.parse_args(["--study-key", "STUDY-1", "STUDY-1", "SUBJ-1"])
    assert spi_cli.resolve_hybrid_study_and_key(parser, ns) == ("STUDY-1", "SUBJ-1")

    # Conflicting duplicate flags rejected by UniqueStoreAction
    with pytest.raises(SystemExit) as dup_exc:
        parser.parse_args(["--study-key", "STUDY-A", "-s", "STUDY-B", "SUBJ-1"])
    assert dup_exc.value.code == 2

    # Conflicting flag vs positional rejected with exit code 2
    ns_conflict = parser.parse_args(["--study-key", "STUDY-A", "STUDY-B", "SUBJ-1"])
    with pytest.raises(SystemExit) as conf_exc:
        spi_cli.resolve_hybrid_study_and_key(parser, ns_conflict)
    assert conf_exc.value.code == 2


@pytest.mark.parametrize(
    ("argv", "expected_resource"),
    [
        (["--simulation", "studies", "list", "--format", "json"], "studies"),
        (["forms", "list", "STUDY", "--simulation", "--format", "json"], "forms"),
        (["forms", "list", "--study-key", "STUDY", "--simulation", "--format", "json"], "forms"),
        (["subjects", "list", "-s", "STUDY", "--simulation", "--format", "json"], "subjects"),
        (
            ["records", "list", "--study-key", "STUDY", "--simulation", "--format", "json"],
            "records",
        ),
        (
            ["codings", "list", "--study-key", "STUDY", "--simulation", "--format", "json"],
            "codings",
        ),
        (["users", "list", "--study-key", "STUDY", "--simulation", "--format", "json"], "users"),
        (
            [
                "workflows",
                "extract-records",
                "--study-key",
                "STUDY",
                "--simulation",
                "--format",
                "json",
            ],
            "records",
        ),
        (
            [
                "subject-data",
                "--study-key",
                "STUDY",
                "SUB1",
                "--simulation",
                "--format",
                "json",
            ],
            "subject_data",
        ),
    ],
)
def test_simulation_mode_offline_json_reads_and_env_restoration(
    argv: list[str],
    expected_resource: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify --simulation executes read commands offline and restores SIMULATION_MODE_ENV."""
    cli_mod.app(argv)
    out, err = capsys.readouterr()
    assert err == ""
    assert spi_cli.SIMULATION_MODE_ENV not in os.environ

    payload = json.loads(out)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == expected_resource
    assert payload["status"] == "complete"
    assert isinstance(payload["items"], list)


def test_simulation_mode_datadict_export_and_write_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify --simulation and hybrid study_key work on datadict, export, and write commands."""
    # 1. datadict from-api positional STUDY
    dd_path = tmp_path / "sim_dd.json"
    cli_mod.app(["datadict", "from-api", "STUDY", "--simulation", "-o", str(dd_path)])
    capsys.readouterr()
    assert dd_path.exists()
    dd_json = json.loads(dd_path.read_text(encoding="utf-8"))
    assert dd_json["metadata"]["studyKey"] == "STUDY"
    assert isinstance(dd_json.get("forms"), list)

    # 2. export json with --study-key flag + positional path
    export_path = tmp_path / "exported_records.json"
    cli_mod.app(
        [
            "export",
            "json",
            "--study-key",
            "STUDY",
            str(export_path),
            "--simulation",
            "--no-interactive",
        ]
    )
    capsys.readouterr()
    assert export_path.exists()
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert isinstance(exported, list)
    assert len(exported) >= 1

    # 3. records create with --study-key and --simulation --dry-run --format json
    cli_mod.app(
        [
            "records",
            "create",
            "--study-key",
            "STUDY",
            "--form-key",
            "F_VS",
            "--site-name",
            "SITE",
            "--subject-key",
            "SUB1",
            "--interval-name",
            "Baseline",
            "-d",
            "V_WEIGHT=72.5",
            "--dry-run",
            "--simulation",
            "--format",
            "json",
        ]
    )
    create_out, create_err = capsys.readouterr()
    assert create_err == ""
    create_payload = json.loads(create_out)
    assert create_payload["resource"] == "records_create"
    assert create_payload["items"][0]["studyKey"] == "STUDY"

    # 4. records import with --study-key and --simulation --dry-run --format json
    import_input = tmp_path / "import_records.json"
    import_input.write_text(
        json.dumps(
            [
                {
                    "formKey": "F_VS",
                    "siteName": "SITE",
                    "subjectKey": "SUB1",
                    "intervalName": "Baseline",
                    "data": {"V_WEIGHT": 71.0},
                }
            ]
        ),
        encoding="utf-8",
    )
    cli_mod.app(
        [
            "records",
            "import",
            "--study-key",
            "STUDY",
            str(import_input),
            "--dry-run",
            "--simulation",
            "--format",
            "json",
        ]
    )
    imp_out, imp_err = capsys.readouterr()
    assert imp_err == ""
    imp_payload = json.loads(imp_out)
    assert imp_payload["resource"] == "records_import"
    assert imp_payload["items"][0]["studyKey"] == "STUDY"

    # 5. subjects register with --study-key and --simulation --dry-run --format json
    cli_mod.app(
        [
            "subjects",
            "register",
            "--study-key",
            "STUDY",
            "--form-key",
            "F_DM",
            "--site-name",
            "SITE",
            "--subject-key",
            "SUBJ-NEW-01",
            "-d",
            "V_AGE=42",
            "--dry-run",
            "--simulation",
            "--format",
            "json",
        ]
    )
    reg_out, reg_err = capsys.readouterr()
    assert reg_err == ""
    reg_payload = json.loads(reg_out)
    assert reg_payload["resource"] == "subjects_register"
    assert reg_payload["items"][0]["studyKey"] == "STUDY"


@pytest.mark.parametrize(
    "argv",
    [
        ["forms", "list", "STUDY-A", "--study-key", "STUDY-B"],
        ["subjects", "list", "STUDY-A", "-s", "STUDY-B"],
        ["records", "list", "STUDY-A", "--study-key", "STUDY-B"],
        ["records", "create", "STUDY-A", "--study-key", "STUDY-B", "--form-key", "F_VS"],
        ["records", "import", "STUDY-A", "input.json", "--study-key", "STUDY-B"],
        [
            "subjects",
            "register",
            "STUDY-A",
            "--study-key",
            "STUDY-B",
            "--form-key",
            "F_DM",
            "--site-name",
            "SITE",
        ],
        ["codings", "list", "STUDY-A", "--study-key", "STUDY-B"],
        ["users", "list", "STUDY-A", "--study-key", "STUDY-B"],
        ["jobs", "status", "STUDY-A", "BATCH-1", "--study-key", "STUDY-B"],
        ["jobs", "wait", "STUDY-A", "BATCH-1", "--study-key", "STUDY-B"],
        ["export", "json", "STUDY-A", "out.json", "--study-key", "STUDY-B"],
        ["datadict", "from-api", "STUDY-A", "--study-key", "STUDY-B"],
        ["datadict", "from-odm", "in.xml", "--study-key", "STUDY-A", "--study-key", "STUDY-B"],
        ["datadict", "from-redcap", "in.csv", "--study-key", "STUDY-A", "--study-key", "STUDY-B"],
        ["workflows", "extract-records", "STUDY-A", "--study-key", "STUDY-B"],
        ["workflows", "sync-worker", "STUDY-A", "--study-key", "STUDY-B", "--once"],
        ["workflows", "uat", "run", "STUDY-A", "--study-key", "STUDY-B"],
        ["workflows", "validate-standards", "STUDY-A", "--study-key", "STUDY-B"],
    ],
)
def test_conflicting_study_keys_exit_two_on_stderr(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify conflicting positional and flag study_key values exit 2 without touching stdout."""
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.app(argv)
    assert exc_info.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "conflicting values provided for study_key" in err


def test_records_list_legacy_output_emits_deprecation_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify records list -o json writes records.json and emits a stderr deprecation warning."""
    monkeypatch.chdir(tmp_path)
    cli_mod.app(["records", "list", "STUDY", "--simulation", "-o", "json"])
    _, err = capsys.readouterr()
    assert "Warning: '-o/--output' on 'records list' is deprecated" in err
    assert (tmp_path / "records.json").exists()


def test_missing_and_partial_credentials_diagnostics_and_redaction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify get_sdk reports missing counterpart keys, hints --simulation, and redacts secrets."""
    # Neither key set
    with pytest.raises(SystemExit) as exc_none:
        cli_mod.app(["studies", "list"])
    assert exc_none.value.code == 1
    out_none, err_none = capsys.readouterr()
    assert out_none == ""
    assert "IMEDNET_API_KEY and IMEDNET_SECURITY_KEY environment variables must be set." in err_none
    assert "--simulation" in err_none

    # Only API key set -> names IMEDNET_SECURITY_KEY and never prints API key
    dummy_api_secret = "super-secret-api-key-value"  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_API_KEY", dummy_api_secret)
    with pytest.raises(SystemExit) as exc_api_only:
        cli_mod.app(["studies", "list"])
    assert exc_api_only.value.code == 1
    out_api, err_api = capsys.readouterr()
    assert out_api == ""
    assert "IMEDNET_SECURITY_KEY environment variable must be set" in err_api
    assert "--simulation" in err_api
    assert dummy_api_secret not in err_api

    # Only security key set -> names IMEDNET_API_KEY and never prints security key
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    dummy_sec_secret = "super-secret-sec-key-value"  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", dummy_sec_secret)
    with pytest.raises(SystemExit) as exc_sec_only:
        cli_mod.app(["studies", "list"])
    assert exc_sec_only.value.code == 1
    out_sec, err_sec = capsys.readouterr()
    assert out_sec == ""
    assert "IMEDNET_API_KEY environment variable must be set" in err_sec
    assert dummy_sec_secret not in err_sec


@pytest.mark.parametrize(
    "argv",
    [
        ["users", "list", "../evil"],
        ["jobs", "status", "../evil", "BATCH-1"],
        ["jobs", "status", "STUDY", "../evil"],
        ["jobs", "status", "--study-key", "STUDY", "../evil"],
        ["jobs", "wait", "../evil", "BATCH-1"],
        ["jobs", "wait", "STUDY", "../evil"],
        ["jobs", "wait", "--study-key", "STUDY", "../evil"],
        ["records", "create", "../evil", "--form-key", "F_VS"],
        ["records", "import", "../evil", "nonexistent.csv"],
        ["subjects", "register", "../evil", "--form-key", "F_DM", "--site-name", "SITE"],
        ["workflows", "sync-worker", "../evil", "--once"],
        ["workflows", "uat", "run", "../evil"],
        ["workflows", "validate-standards", "../evil"],
        ["subject-data", "../evil", "SUBJ-001"],
        ["subject-data", "STUDY", "../evil"],
    ],
)
def test_preflight_path_traversal_rejected_before_sdk_init(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify invalid path segments exit 1 on stderr before get_sdk() is invoked."""
    with patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk:
        with pytest.raises(SystemExit) as exc_info:
            cli_mod.app(argv)
    assert exc_info.value.code == 1
    mock_get_sdk.assert_not_called()
    out, err = capsys.readouterr()
    assert out == ""
    assert "Error:" in err


def test_extract_records_rejects_interactive_with_json_format(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify workflows extract-records rejects combining --interactive and --format json."""
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.app(["workflows", "extract-records", "--interactive", "--format", "json"])
    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "Cannot combine --interactive with --format json" in err


def test_validate_standards_supports_simulation_and_study_key_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify workflows validate-standards supports --study-key and --simulation offline."""
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.app(
            [
                "workflows",
                "validate-standards",
                "--study-key",
                "STUDY",
                "--simulation",
                "--format",
                "json",
            ]
        )
    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    assert err == ""
    assert spi_cli.SIMULATION_MODE_ENV not in os.environ
    payload = json.loads(out)
    assert payload["study_key"] == "STUDY"
    assert payload["passed"] is False
    assert "score" in payload


def test_spi_parse_email_notify_normalizes_and_rejects_invalid_headers() -> None:
    """Verify spi_cli.parse_email_notify normalizes booleans/emails and rejects header injection."""
    assert spi_cli.parse_email_notify(None) is None
    assert spi_cli.parse_email_notify(True) is True
    assert spi_cli.parse_email_notify(" TRUE ") is True
    assert spi_cli.parse_email_notify("false") is False
    assert spi_cli.parse_email_notify("  ops@example.com  ") == "ops@example.com"

    for bad_input in (
        "",
        "   ",
        "ops@example.com\n",
        "ops@example.com\r\nX-Injected: 1",
        "not-an-email",
        "tru",
        "bad email@example.com",
        "@example.com",
        "user@",
        "user@localhost",
    ):
        with pytest.raises(ValueError, match="email-notify"):
            spi_cli.parse_email_notify(bad_input)


def test_spi_load_offline_schema_supports_json_odm_redcap_and_csv_dir(tmp_path: Path) -> None:
    """Verify spi_cli.load_offline_schema loads JSON, ODM XML, REDCap CSV, CSV dirs, and ZIP bundles."""
    import zipfile

    from imednet import ImednetSDK
    from imednet.datadict import Choice, DataDictionary, Form, Metadata, Variable

    dd = DataDictionary(
        metadata=Metadata.create(study_key="STUDY"),
        forms=[
            Form(
                formKey="F_DM",
                formName="Demographics",
                variables=[
                    Variable(
                        variableName="V_SEX",
                        variableType="Radio",
                        required="Yes",
                        choices=[
                            Choice(choiceValue="F", choiceText="Female"),
                            Choice(choiceValue="M", choiceText="Male"),
                        ],
                    )
                ],
            )
        ],
    )

    # 1. JSON DataDictionary
    json_path = tmp_path / "dd.json"
    json_path.write_text(dd.model_dump_json(indent=2), encoding="utf-8")
    loaded_json = spi_cli.load_offline_schema(str(json_path), study_key="STUDY")
    assert "V_SEX" in loaded_json.variables_for_form("F_DM")

    # 2. CSV directory (4-file package) + .zip archive
    csv_dir = tmp_path / "csv_bundle"
    csv_dir.mkdir()
    (csv_dir / "FORMS.csv").write_text(
        "formId,formKey,formType,formName\n1,F_DM,DataEntry,Demographics\n",
        encoding="utf-8",
    )
    (csv_dir / "QUESTIONS.csv").write_text(
        "formKey,variableName,variableType,label,required\nF_DM,V_SEX,Radio,Sex,Yes\n",
        encoding="utf-8",
    )
    (csv_dir / "CHOICES.csv").write_text(
        "formKey,variableName,choiceValue,choiceText\nF_DM,V_SEX,F,Female\nF_DM,V_SEX,M,Male\n",
        encoding="utf-8",
    )
    (csv_dir / "BUSINESS_LOGIC.csv").write_text(
        "id,name,status,formKey,logic\n",
        encoding="utf-8",
    )
    loaded_dir = spi_cli.load_offline_schema(str(csv_dir), study_key="STUDY")
    assert "V_SEX" in loaded_dir.variables_for_form("F_DM")

    zip_path = tmp_path / "csv_bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for csv_name in ("FORMS.csv", "QUESTIONS.csv", "CHOICES.csv", "BUSINESS_LOGIC.csv"):
            zf.write(csv_dir / csv_name, arcname=csv_name)
    loaded_zip = spi_cli.load_offline_schema(str(zip_path), study_key="STUDY")
    assert "V_SEX" in loaded_zip.variables_for_form("F_DM")

    # 3. CDISC ODM XML
    odm_path = tmp_path / "study.xml"
    odm_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2">
  <Study OID="S.STUDY">
    <GlobalVariables>
      <StudyName>Study</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>Proto</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV.1" Name="v1">
      <FormDef OID="F_DM" Name="Demographics" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG_DM" Mandatory="Yes"/>
      </FormDef>
      <ItemGroupDef OID="IG_DM" Name="Demographics" Repeating="No">
        <ItemRef ItemOID="IT_AGE" Mandatory="Yes"/>
      </ItemGroupDef>
      <ItemDef OID="IT_AGE" Name="V_AGE" DataType="integer"/>
    </MetaDataVersion>
  </Study>
</ODM>""",
        encoding="utf-8",
    )
    loaded_odm = spi_cli.load_offline_schema(str(odm_path), study_key="STUDY")
    assert "IT_AGE" in loaded_odm.variables_for_form("F_DM")

    # 4. REDCap CSV
    redcap_path = tmp_path / "redcap_dd.csv"
    redcap_path.write_text(
        "Variable / Field Name,Form Name,Section Header,Field Type,Field Label,"
        "Choices, Calculations, OR Slider Labels,Field Note,Text Validation Type OR Show Slider Number,"
        "Text Validation Min,Text Validation Max,Identifier?,Branching Logic (Show field only if...),"
        "Required Field?,Custom Alignment,Question Number (surveys only),Matrix Group Name,"
        "Matrix Ranking?,Field Annotation\n"
        "v_age,f_dm,,text,Age,,,integer,18,90,,,y,,,,,\n",
        encoding="utf-8",
    )
    loaded_redcap = spi_cli.load_offline_schema(str(redcap_path), study_key="STUDY")
    assert len(loaded_redcap.forms) >= 1  # type: ignore[attr-defined]

    # 5. resolve_cli_schema with 'api' and None
    with ImednetSDK(simulation_mode=True) as sdk:
        assert spi_cli.resolve_cli_schema(sdk, "STUDY", None) is None
        api_cache = spi_cli.resolve_cli_schema(sdk, "STUDY", "api")
        assert api_cache is not None
        assert len(api_cache.variables_for_form("F_DM", study_key="STUDY")) >= 1
