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
        "positive_int",
        "redact_cli_secrets",
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
        ["jobs", "wait", "../evil", "BATCH-1"],
        ["records", "create", "../evil", "--form-key", "F_VS"],
        ["records", "import", "../evil", "nonexistent.csv"],
        ["subjects", "register", "../evil", "--form-key", "F_DM", "--site-name", "SITE"],
        ["workflows", "sync-worker", "../evil", "--once"],
        ["workflows", "uat", "run", "../evil"],
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
