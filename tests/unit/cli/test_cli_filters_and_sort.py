"""Unit and integration tests for CLI --filter, --record-data-filter, --sort, and SPI exports."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet.spi import cli as spi_cli


class CliResult:
    """Captured CLI invocation result with separated stdout and stderr."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def invoke_cli(args: list[str]) -> CliResult:
    """Invoke imednet.cli.app while capturing stdout and stderr independently."""
    out = io.StringIO()
    err = io.StringIO()
    exit_code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli.app(args)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return CliResult(exit_code, out.getvalue(), err.getvalue())


@pytest.fixture(autouse=True)
def default_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide dummy credentials for mocked SDK CLI invocations."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_secret")


@pytest.fixture
def mock_sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch get_sdk to return a controllable MagicMock."""
    sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=sdk))
    return sdk


def test_spi_cli_exports_formatting_and_query_helpers() -> None:
    """Verify imednet.spi.cli exports formatting and shared query helpers for plugins."""
    expected_symbols = {
        "add_output_format",
        "display_list",
        "emit_json",
        "add_filter_arg",
        "add_sort_arg",
        "add_record_data_filter_arg",
        "add_list_query_args",
        "build_list_query_kwargs",
        "parse_filter_args",
    }
    assert expected_symbols.issubset(set(spi_cli.__all__))
    for symbol in expected_symbols:
        assert callable(getattr(spi_cli, symbol))


FILTER_CAPABLE_COMMANDS: list[tuple[str, list[str], str, list[str], dict[str, Any]]] = [
    (
        "studies",
        ["studies", "list"],
        "studies",
        ["-f", "study_name=Demo", "--sort", "studyKey,asc"],
        {"study_name": "Demo", "sort": "studyKey,asc"},
    ),
    (
        "queries",
        ["queries", "list", "STUDY"],
        "queries",
        ["-f", "variable=V_AGE", "--sort", "annotationId,desc"],
        {"variable": "V_AGE", "sort": "annotationId,desc"},
    ),
    (
        "variables",
        ["variables", "list", "STUDY"],
        "variables",
        ["-f", "form_key=F_DM", "-f", "revision>=1", "--sort", "variableName,asc"],
        {"form_key": "F_DM", "revision": (">=", 1), "sort": "variableName,asc"},
    ),
    (
        "record-revisions",
        ["record-revisions", "list", "STUDY"],
        "record_revisions",
        ["-f", "record_id==1", "--sort", "recordRevisionId,desc"],
        {"record_id": ("==", 1), "sort": "recordRevisionId,desc"},
    ),
    (
        "sites",
        ["sites", "list", "STUDY"],
        "sites",
        ["-f", "site_enrollment_status=Active", "--sort", "siteName,asc"],
        {"site_enrollment_status": "Active", "sort": "siteName,asc"},
    ),
    (
        "forms",
        ["forms", "list", "STUDY"],
        "forms",
        ["-f", "form_key=F_DM", "-f", "form_key=F_VS", "--sort", "formId,asc"],
        {"form_key": ["F_DM", "F_VS"], "sort": "formId,asc"},
    ),
    (
        "intervals",
        ["intervals", "list", "STUDY"],
        "intervals",
        ["-f", "interval_name=Baseline", "--sort", "intervalSequence,asc"],
        {"interval_name": "Baseline", "sort": "intervalSequence,asc"},
    ),
    (
        "visits",
        ["visits", "list", "STUDY"],
        "visits",
        ["-f", "interval_name!=Unscheduled", "--sort", "visitId,asc"],
        {"interval_name": ("!=", "Unscheduled"), "sort": "visitId,asc"},
    ),
    (
        "records",
        ["records", "list", "STUDY"],
        "records",
        ["-f", "form_key=F_DM", "--sort", "recordId,desc"],
        {"form_key": "F_DM", "sort": "recordId,desc"},
    ),
    (
        "subjects",
        ["subjects", "list", "STUDY"],
        "subjects",
        [
            "-f",
            "subject_status=Screened",
            "-f",
            "subject_status=Enrolled",
            "--sort",
            "subjectKey,asc",
        ],
        {"subject_status": ["Screened", "Enrolled"], "sort": "subjectKey,asc"},
    ),
    (
        "codings",
        ["codings", "list", "STUDY"],
        "codings",
        ["-f", "dictionary_name=~^Med.*", "--sort", "codingId,asc"],
        {"dictionary_name": ("=~", "^Med.*"), "sort": "codingId,asc"},
    ),
]


@pytest.mark.parametrize(
    ("command_label", "base_cmd", "endpoint_attr", "extra_flags", "expected_kwargs"),
    FILTER_CAPABLE_COMMANDS,
    ids=[entry[0] for entry in FILTER_CAPABLE_COMMANDS],
)
def test_all_11_filter_capable_list_commands_forward_filter_and_sort(
    mock_sdk: MagicMock,
    command_label: str,
    base_cmd: list[str],
    endpoint_attr: str,
    extra_flags: list[str],
    expected_kwargs: dict[str, Any],
) -> None:
    """Verify all 11 filter-capable core list commands expose and forward --filter and --sort."""
    del command_label
    getattr(mock_sdk, endpoint_attr).list.return_value = []

    help_res = invoke_cli([*base_cmd, "--help"])
    assert help_res.exit_code == 0
    assert "--filter" in help_res.stdout
    assert "--sort" in help_res.stdout

    res = invoke_cli([*base_cmd, *extra_flags, "--format", "json"])
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["status"] == "complete"

    expected_args = ("STUDY",) if "STUDY" in base_cmd else ()
    getattr(mock_sdk, endpoint_attr).list.assert_called_once_with(*expected_args, **expected_kwargs)


def test_users_list_supports_sort_and_does_not_expose_filter(mock_sdk: MagicMock) -> None:
    """Verify users list exposes --sort and rejects --filter."""
    mock_sdk.users.list.return_value = []

    help_res = invoke_cli(["users", "list", "--help"])
    assert help_res.exit_code == 0
    assert "--sort" in help_res.stdout
    assert "--filter" not in help_res.stdout

    res = invoke_cli(["users", "list", "STUDY", "--include-inactive", "--sort", "lastName,asc"])
    assert res.exit_code == 0
    mock_sdk.users.list.assert_called_once_with("STUDY", include_inactive=True, sort="lastName,asc")

    bad_res = invoke_cli(["users", "list", "STUDY", "--filter", "user_name=jdoe"])
    assert bad_res.exit_code == 2
    assert bad_res.stdout == ""


def test_records_list_supports_filter_record_data_filter_and_sort(mock_sdk: MagicMock) -> None:
    """Verify records list exposes and forwards --filter, --record-data-filter, and --sort."""
    mock_sdk.records.list.return_value = []

    help_res = invoke_cli(["records", "list", "--help"])
    assert help_res.exit_code == 0
    assert "--filter" in help_res.stdout
    assert "--record-data-filter" in help_res.stdout
    assert "--sort" in help_res.stdout

    res = invoke_cli(
        [
            "records",
            "list",
            "STUDY",
            "--filter",
            "form_key=F_DM",
            "--record-data-filter",
            "V_AGE>=25",
            "--sort",
            "recordId,desc",
            "--format",
            "json",
        ]
    )
    assert res.exit_code == 0, res.stderr
    mock_sdk.records.list.assert_called_once_with(
        "STUDY",
        record_data_filter="V_AGE>=25",
        form_key="F_DM",
        sort="recordId,desc",
    )


def test_records_list_simulation_mode_end_to_end_filter_record_data_filter_and_sort(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify records list works end-to-end in --simulate mode with --filter, --record-data-filter, and --sort."""
    capsys.readouterr()
    cli.app(
        [
            "--simulate",
            "records",
            "list",
            "STUDY",
            "--filter",
            "form_key=F_DM",
            "--record-data-filter",
            "V_AGE>=25",
            "--sort",
            "recordId,desc",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["count"] == 1
    assert payload["items"][0]["formKey"] == "F_DM"
    assert payload["items"][0]["recordData"]["V_AGE"] == 45


def test_list_command_unknown_filter_attribute_writes_stderr_and_exits_1(
    mock_sdk: MagicMock,
) -> None:
    """Verify unknown model filter attributes fail with exit code 1 and clean stdout."""
    del mock_sdk
    res = invoke_cli(["forms", "list", "STUDY", "--filter", "nonexistent_attr=1"])
    assert res.exit_code == 1
    assert res.stdout == ""
    assert "Unknown filter attribute 'nonexistent_attr' for Form" in res.stderr
