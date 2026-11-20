"""Unit tests for shared SPI CLI helpers, hybrid Study flags, simulation mode, and output contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from imednet import cli
from imednet.cli.utils.output import _format_cell_value
from imednet.models.queries import QueryStatus
from imednet.models.records import RecordStatus
from imednet.simulation.state import get_default_simulation_state
from imednet.simulation.transport import SimulationTransport
from imednet.spi import cli as spi_cli


@pytest.fixture(autouse=True)
def _reset_simulation_state() -> None:
    """Reset the shared simulation state before each test."""
    get_default_simulation_state().reset()


def _invoke_cli(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    capsys.readouterr()
    code = 0
    try:
        cli.app(argv)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_spi_cli_exports_shared_helpers() -> None:
    """Verify imednet.spi.cli exports output, Study flag, and simulation helpers in __all__."""
    expected_exports = {
        "SIMULATION_MODE_ENV",
        "UniqueStoreAction",
        "add_output_format",
        "add_simulation_arg",
        "add_study_key_args",
        "display_list",
        "emit_json",
        "resolve_hybrid_study_and_key",
        "resolve_output_format",
        "resolve_study_key",
        "simulation_mode_context",
        "validate_cli_path_segment",
    }
    assert expected_exports.issubset(set(spi_cli.__all__))
    for symbol in expected_exports:
        assert hasattr(spi_cli, symbol), f"Missing SPI symbol {symbol}"


def test_format_cell_value_normalizes_nested_enums() -> None:
    """Verify _format_cell_value formats nested Enum values without Python repr brackets."""
    nested = {
        "record_status": RecordStatus.COMPLETE,
        "statuses": [QueryStatus.OPEN, ("nested", RecordStatus.COMPLETE)],
    }
    formatted = _format_cell_value(nested)
    assert "Complete" in formatted
    assert "OPEN" in formatted
    assert "<RecordStatus" not in formatted
    assert "<QueryStatus" not in formatted


def test_subject_data_and_extract_records_simulation_json_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify --simulation + --format json on subject-data and extract-records without credentials."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)
    monkeypatch.delenv("IMEDNET_SIMULATION_MODE", raising=False)

    code_subj, out_subj, err_subj = _invoke_cli(
        capsys,
        ["subject-data", "--simulation", "STUDY", "SUB1", "--format", "json"],
    )
    assert code_subj == 0, err_subj
    subj_payload = json.loads(out_subj)
    assert subj_payload["schemaVersion"] == "1.0"
    assert subj_payload["resource"] == "subject_data"
    assert subj_payload["status"] == "complete"
    assert subj_payload["count"] == 1
    assert isinstance(subj_payload["items"], list)
    assert "IMEDNET_SIMULATION_MODE" not in os.environ

    code_ext, out_ext, err_ext = _invoke_cli(
        capsys,
        ["workflows", "extract-records", "--simulation", "DEMO", "--format", "json"],
    )
    assert code_ext == 0, err_ext
    ext_payload = json.loads(out_ext)
    assert ext_payload["schemaVersion"] == "1.0"
    assert ext_payload["resource"] == "records"
    assert ext_payload["status"] == "complete"
    assert isinstance(ext_payload["items"], list)
    assert "IMEDNET_SIMULATION_MODE" not in os.environ

    code_seeded, out_seeded, err_seeded = _invoke_cli(
        capsys,
        ["workflows", "extract-records", "--simulation", "STUDY", "--format", "json"],
    )
    assert code_seeded == 0, err_seeded
    seeded_payload = json.loads(out_seeded)
    assert seeded_payload["count"] == 2
    assert seeded_payload["items"][0]["recordStatus"] == "Complete"


def test_subject_data_and_extract_records_default_tables_clean_enums(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify default table output formats Enum values cleanly without <...> repr brackets."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)

    code_subj, out_subj, err_subj = _invoke_cli(
        capsys,
        ["subject-data", "--simulation", "STUDY", "SUB1", "--no-interactive"],
    )
    assert code_subj == 0, err_subj
    assert "Found 1 subject data:" in out_subj
    assert "SUB1" in out_subj
    assert "<RecordStatus" not in out_subj
    assert "<QueryStatus" not in out_subj
    assert "{" not in out_subj

    code_ext, out_ext, err_ext = _invoke_cli(
        capsys,
        ["workflows", "extract-records", "--simulation", "STUDY", "--no-interactive"],
    )
    assert code_ext == 0, err_ext
    assert "Found 2 matching records:" in out_ext
    assert "Complete" in out_ext
    assert "<RecordStatus" not in out_ext


@pytest.mark.parametrize(
    ("pos_argv", "flag_argv", "conflict_argv"),
    [
        (
            ["forms", "list", "--simulation", "STUDY", "--format", "json"],
            ["forms", "list", "--simulation", "--study-key", "STUDY", "--format", "json"],
            ["forms", "list", "--simulation", "STUDY", "--study-key", "OTHER"],
        ),
        (
            ["records", "list", "--simulation", "STUDY", "--format", "json"],
            ["records", "list", "--simulation", "-s", "STUDY", "--format", "json"],
            ["records", "list", "--simulation", "STUDY", "-s", "OTHER"],
        ),
        (
            ["subjects", "list", "--simulation", "STUDY", "--format", "json"],
            ["subjects", "list", "--simulation", "--study-key", "STUDY", "--format", "json"],
            ["subjects", "list", "--simulation", "STUDY", "--study-key", "OTHER"],
        ),
        (
            ["codings", "list", "--simulation", "STUDY", "--format", "json"],
            ["codings", "list", "--simulation", "--study-key", "STUDY", "--format", "json"],
            ["codings", "list", "--simulation", "STUDY", "--study-key", "OTHER"],
        ),
        (
            ["users", "list", "--simulation", "STUDY", "--format", "json"],
            ["users", "list", "--simulation", "--study-key", "STUDY", "--format", "json"],
            ["users", "list", "--simulation", "STUDY", "--study-key", "OTHER"],
        ),
        (
            ["datadict", "from-api", "--simulation", "STUDY", "--no-raw-xml"],
            ["datadict", "from-api", "--simulation", "--study-key", "STUDY", "--no-raw-xml"],
            ["datadict", "from-api", "--simulation", "STUDY", "--study-key", "OTHER"],
        ),
        (
            ["workflows", "extract-records", "--simulation", "STUDY", "--format", "json"],
            [
                "workflows",
                "extract-records",
                "--simulation",
                "--study-key",
                "STUDY",
                "--format",
                "json",
            ],
            ["workflows", "extract-records", "--simulation", "STUDY", "--study-key", "OTHER"],
        ),
    ],
)
def test_hybrid_positional_and_study_key_flag_parity_and_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pos_argv: list[str],
    flag_argv: list[str],
    conflict_argv: list[str],
) -> None:
    """Verify core Study commands, datadict from-api, and workflows accept STUDY and --study-key and exit 2 on conflict."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)

    code_pos, out_pos, err_pos = _invoke_cli(capsys, pos_argv)
    assert code_pos == 0, err_pos

    code_flag, out_flag, err_flag = _invoke_cli(capsys, flag_argv)
    assert code_flag == 0, err_flag
    pos_json = json.loads(out_pos)
    flag_json = json.loads(out_flag)
    if isinstance(pos_json.get("metadata"), dict):
        pos_json["metadata"].pop("generatedAt", None)
    if isinstance(flag_json.get("metadata"), dict):
        flag_json["metadata"].pop("generatedAt", None)
    assert pos_json == flag_json

    code_conflict, out_conflict, err_conflict = _invoke_cli(capsys, conflict_argv)
    assert code_conflict == 2
    assert out_conflict == ""
    assert "conflicting" in err_conflict


def test_export_and_jobs_hybrid_study_key_and_conflict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Verify export and jobs commands support --study-key and reject conflicting Study keys."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)

    dest = tmp_path / "exported.json"
    code_ok, _, err_ok = _invoke_cli(
        capsys,
        ["export", "json", "--simulation", "--study-key", "STUDY", str(dest)],
    )
    assert code_ok == 0, err_ok
    assert dest.exists()

    code_export_conflict, _, err_export_conflict = _invoke_cli(
        capsys,
        ["export", "json", "--simulation", "STUDY", str(dest), "--study-key", "OTHER"],
    )
    assert code_export_conflict == 2
    assert "conflicting" in err_export_conflict

    code_jobs_conflict, _, err_jobs_conflict = _invoke_cli(
        capsys,
        ["jobs", "status", "STUDY", "BATCH-1", "--study-key", "OTHER"],
    )
    assert code_jobs_conflict == 2
    assert "conflicting" in err_jobs_conflict


def test_records_list_output_flag_writes_file_and_emits_deprecation_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Verify records list --simulation DEMO -o json writes ./records.json and warns on stderr."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)

    code, out, err = _invoke_cli(capsys, ["records", "list", "--simulation", "DEMO", "-o", "json"])
    assert code == 0, err
    assert (tmp_path / "records.json").exists()
    assert "Saved" in out
    assert "deprecated" in err.lower()
    assert "--format json" in err
    assert "imednet export" in err


def test_root_simulation_flag_restores_previous_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify imednet --simulation initializes SimulationTransport and restores env vars on exit."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)
    monkeypatch.setenv("IMEDNET_SIMULATION_MODE", "false")

    code, out, err = _invoke_cli(capsys, ["--simulation", "studies", "list", "--format", "json"])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["schemaVersion"] == "1.0"
    assert payload["count"] >= 1
    assert os.environ.get("IMEDNET_SIMULATION_MODE") == "false"

    monkeypatch.delenv("IMEDNET_SIMULATION_MODE", raising=False)
    code2, _, err2 = _invoke_cli(capsys, ["--simulation", "studies", "list", "--format", "json"])
    assert code2 == 0, err2
    assert "IMEDNET_SIMULATION_MODE" not in os.environ


def test_tui_simulation_initializes_simulation_transport_and_restores_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify imednet tui --simulation --force uses SimulationTransport and restores env."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)
    monkeypatch.setenv("IMEDNET_SIMULATION_MODE", "false")

    captured_sdks: list[Any] = []

    def _fake_run_tui(sdk: Any, **kwargs: Any) -> None:
        captured_sdks.append((sdk, kwargs))

    monkeypatch.setattr("imednet_tui.app.run_tui", _fake_run_tui)

    code, out, err = _invoke_cli(capsys, ["tui", "DEMO-STUDY", "--simulation", "--force"])
    assert code == 0, err
    assert out == ""
    assert len(captured_sdks) == 1
    sdk_obj, run_kwargs = captured_sdks[0]
    assert run_kwargs["force"] is True
    assert run_kwargs["study_key"] == "DEMO-STUDY"
    assert sdk_obj.config.simulation_mode is True
    assert isinstance(sdk_obj._client._client._transport, SimulationTransport)
    assert os.environ.get("IMEDNET_SIMULATION_MODE") == "false"


def test_partial_credentials_surface_missing_key_and_simulation_hint_with_redaction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Verify partial credentials print specific missing key error, simulation hint, and redact secrets."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMEDNET_API_KEY", "super-secret-api-key-value")
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)
    monkeypatch.delenv("IMEDNET_SIMULATION_MODE", raising=False)

    code, out, err = _invoke_cli(capsys, ["studies", "list"])
    assert code == 1
    assert out == ""
    assert "IMEDNET_SECURITY_KEY" in err
    assert "Security key is required" in err
    assert "--simulation" in err
    assert "super-secret-api-key-value" not in err

    # Also test missing IMEDNET_API_KEY when only IMEDNET_SECURITY_KEY is set
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "super-secret-sec-key-value")

    code2, out2, err2 = _invoke_cli(capsys, ["studies", "list"])
    assert code2 == 1
    assert out2 == ""
    assert "IMEDNET_API_KEY" in err2
    assert "API key is required" in err2
    assert "--simulation" in err2
    assert "super-secret-sec-key-value" not in err2


@pytest.mark.parametrize(
    "argv",
    [
        ["codings", "list", "--study-key", "../CANARY_STUDY"],
        ["subjects", "list", "--study-key", "../CANARY_STUDY"],
        ["users", "list", "--study-key", "../CANARY_STUDY"],
        ["records", "list", "--study-key", "../CANARY_STUDY"],
        ["forms", "list", "--study-key", "../CANARY_STUDY"],
        ["export", "json", "--study-key", "../CANARY_STUDY", "/tmp/unused.json"],
        ["datadict", "from-api", "--study-key", "../CANARY_STUDY"],
        ["workflows", "extract-records", "--study-key", "../CANARY_STUDY"],
        ["tui", "--study-key", "../CANARY_STUDY", "--force"],
        ["tui", "STUDY", "--batch", "../CANARY_BATCH", "--force"],
    ],
)
def test_hybrid_study_key_and_tui_reject_unsafe_path_segments_before_sdk(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """Verify --study-key and tui flags reject path traversal via validate_cli_path_segment before SDK init."""

    def _fail_get_sdk() -> Any:
        raise AssertionError("get_sdk must not be called")

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _fail_get_sdk)
    monkeypatch.setattr("imednet.cli.datadict.get_sdk", _fail_get_sdk)

    code, out, err = _invoke_cli(capsys, argv)
    assert code == 1
    assert out == ""
    assert "Error:" in err
