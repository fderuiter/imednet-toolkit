"""Unit tests for CLI `imednet export sql --long-format` whitelist forwarding."""

from __future__ import annotations

import datetime as dt
import importlib
import io
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable


def _invoke_cli(args: list[str]) -> tuple[int, str, str]:
    """Invoke cli.app with captured stdout/stderr and return (exit_code, stdout, stderr)."""
    out = io.StringIO()
    err = io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli.app(args)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reload CLI commands and inject synthetic API credentials."""
    importlib.reload(cli)
    monkeypatch.setenv("IMEDNET_API_KEY", "test-key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-secret")


def test_cli_export_sql_long_format_forwards_forms_and_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify --forms and --vars are parsed and forwarded to export_to_long_sql."""
    mock_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))
    mock_long_sql = MagicMock()
    monkeypatch.setattr(cli, "export_to_long_sql", mock_long_sql)

    code, _stdout, stderr = _invoke_cli(
        [
            "export",
            "sql",
            "STUDY_CLI",
            "long_tbl",
            "sqlite://",
            "--long-format",
            "--forms",
            "10,20",
            "--vars",
            "AGE,HR",
        ]
    )
    assert code == 0, stderr
    mock_long_sql.assert_called_once()
    call_args, call_kwargs = mock_long_sql.call_args
    assert call_args[:4] == (mock_sdk, "STUDY_CLI", "long_tbl", "sqlite://")
    assert call_kwargs["form_whitelist"] == [10, 20]
    assert call_kwargs["variable_whitelist"] == ["AGE", "HR"]


def test_cli_export_sql_long_format_omits_unset_whitelists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify omitted --forms and --vars do not inject non-None whitelists."""
    mock_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))
    mock_long_sql = MagicMock()
    monkeypatch.setattr(cli, "export_to_long_sql", mock_long_sql)

    code, _stdout, stderr = _invoke_cli(
        ["export", "sql", "STUDY_CLI", "long_tbl", "sqlite://", "--long-format"]
    )
    assert code == 0, stderr
    mock_long_sql.assert_called_once()
    _, call_kwargs = mock_long_sql.call_args
    assert "form_whitelist" not in call_kwargs
    assert "variable_whitelist" not in call_kwargs


def test_cli_export_sql_long_format_end_to_end_sqlite_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify end-to-end CLI long-format SQLite export filters forms and variables accurately."""
    study_key = "STUDY_E2E_LONG"
    forms = [
        Form(form_id=10, form_key="DM", form_name="Demographics"),
        Form(form_id=20, form_key="VS", form_name="Vitals"),
    ]
    variables = [
        Variable(
            variable_id=1, variable_name="AGE", variable_type="integer", form_id=10, form_key="DM"
        ),
        Variable(
            variable_id=2, variable_name="SEX", variable_type="text", form_id=10, form_key="DM"
        ),
        Variable(
            variable_id=3, variable_name="HR", variable_type="integer", form_id=20, form_key="VS"
        ),
    ]
    records = [
        Record(
            record_id=1,
            study_key=study_key,
            subject_key="S01",
            form_id=10,
            form_key="DM",
            deleted=False,
            date_modified=dt.datetime(2026, 6, 1, 10, 0, tzinfo=dt.UTC),
            record_data={"AGE": 47, "SEX": "F"},
        ),
        Record(
            record_id=2,
            study_key=study_key,
            subject_key="S01",
            form_id=20,
            form_key="VS",
            deleted=False,
            date_modified=dt.datetime(2026, 6, 1, 10, 5, tzinfo=dt.UTC),
            record_data={"HR": 70},
        ),
    ]

    sdk = MagicMock(sinks=None)
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.get_intervals.return_value = []
    sdk.records.list.side_effect = lambda **_kw: iter([r.model_copy(deep=True) for r in records])
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=sdk))

    db_file = tmp_path / "cli_long.sqlite"
    code, _stdout, stderr = _invoke_cli(
        [
            "export",
            "sql",
            study_key,
            "clinical_eav",
            f"sqlite:///{db_file}",
            "--long-format",
            "--forms",
            "10",
            "--vars",
            "AGE",
        ]
    )
    assert code == 0, stderr

    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            "SELECT record_id, form_id, variable_name, value FROM clinical_eav"
        ).fetchall()
    assert rows == [(1, 10, "AGE", 47)]
