"""Unit tests for the core SPI compatibility layer in ``imednet_workflows._spi_compat``."""

from __future__ import annotations

import argparse
import importlib
import io
import json
import sys
import types
from datetime import UTC, date, datetime
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from imednet.spi import cli as real_spi_cli
from imednet.spi import errors as real_spi_errors
from imednet.spi import utils as real_spi_utils
from imednet_workflows import _spi_compat


class _SampleState(Enum):
    RUNNING = "PROCESSING"


class _SampleModel(BaseModel):
    record_id: int = Field(alias="recordId")
    form_key: str = "DEMO"
    active: bool = True
    created_at: datetime = datetime(2026, 1, 10, 14, 30, tzinfo=UTC)
    day: date = date(2026, 1, 10)
    state: _SampleState = _SampleState.RUNNING


class _TtyBuffer(io.StringIO):
    def __init__(self, tty: bool = True) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_fallback_add_output_format_and_emit_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify ``--format`` registration and JSON envelope serialization."""
    parser = argparse.ArgumentParser()
    _spi_compat._fallback_add_output_format(parser)

    assert parser.parse_args([]).output_format == "table"
    assert parser.parse_args(["--format", "json"]).output_format == "json"

    row = _SampleModel(recordId=7)
    _spi_compat._fallback_emit_json([row], "records")
    out = json.loads(capsys.readouterr().out)
    assert out["schemaVersion"] == "1.0"
    assert out["resource"] == "records"
    assert out["status"] == "complete"
    assert out["count"] == 1
    assert out["items"][0]["recordId"] == 7

    assert _spi_compat._encode_json_value(date(2026, 2, 3)) == "2026-02-03"
    assert _spi_compat._encode_json_value(_SampleState.RUNNING) == "PROCESSING"
    with pytest.raises(TypeError, match="Unsupported JSON value type"):
        _spi_compat._encode_json_value(object())


def test_fallback_display_list_modes(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify fallback ``display_list`` across JSON, empty, fields, models, dicts, and scalars."""
    with pytest.raises(ValueError, match="Unsupported output format"):
        _spi_compat._fallback_display_list([], "records", output_format="xml")

    _spi_compat._fallback_display_list([], "records")
    assert "No records found." in capsys.readouterr().out

    _spi_compat._fallback_display_list([], "records", "Custom empty.")
    assert "Custom empty." in capsys.readouterr().out

    model_item = _SampleModel(recordId=11)
    _spi_compat._fallback_display_list([model_item], "matching records", output_format="json")
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["resource"] == "matching_records"
    assert json_out["count"] == 1

    # Model items without explicit fields
    _spi_compat._fallback_display_list([model_item], "models")
    table_out = capsys.readouterr().out
    assert "Found 1 models:" in table_out
    assert "Yes" in table_out
    assert "2026-01-10 14:30" in table_out

    # Dict items without explicit fields (covering cell formatting branches)
    dict_rows: list[dict[str, Any]] = [
        {
            "empty_col": None,
            "flag": False,
            "empty_tags": [],
            "tags": ["alpha", "beta"],
            "long_tags": [f"tag-{idx:02d}" for idx in range(20)],
            "nested": {"key": "x" * 70},
        }
    ]
    _spi_compat._fallback_display_list(dict_rows, "dicts")
    dict_out = capsys.readouterr().out
    assert "Found 1 dicts:" in dict_out
    assert "No" in dict_out
    assert "alpha, beta" in dict_out
    assert "..." in dict_out

    # Explicit fields on objects
    obj_row = SimpleNamespace(subject_key="SUBJ-1", status="")
    _spi_compat._fallback_display_list([obj_row], "subjects", fields=["subject_key", "status"])
    field_out = capsys.readouterr().out
    assert "SUBJ-1" in field_out
    assert "-" in field_out

    # Scalar items fallback
    _spi_compat._fallback_display_list(["item-a", "item-b"], "scalars")
    scalar_out = capsys.readouterr().out
    assert "Found 2 scalars:" in scalar_out
    assert "item-a" in scalar_out


def test_fallback_terminal_and_progress_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify error printing, line clearing, status context, and progress formatting."""
    pipe_buf = _TtyBuffer(tty=False)
    _spi_compat._fallback_print_error("\x1b[31mBoom\x1b[0m", file=pipe_buf)
    assert pipe_buf.getvalue() == "Boom\n"

    tty_buf = _TtyBuffer(tty=True)
    _spi_compat._fallback_print_error("\x1b[31mBoom\x1b[0m", file=tty_buf)
    assert "\x1b[31mBoom\x1b[0m" in tty_buf.getvalue()

    clear_buf = _TtyBuffer(tty=True)
    _spi_compat._fallback_clear_line(stream=clear_buf, lines=2)
    assert "\033[K" in clear_buf.getvalue()

    # Job progress formatting via event object and positional args
    event = SimpleNamespace(
        batch_id="B-1",
        status=SimpleNamespace(progress=50, state=_SampleState.RUNNING),
        elapsed=2.5,
    )
    line1 = _spi_compat._fallback_format_job_progress(event)
    assert "Job B-1:" in line1 and "50%" in line1 and "PROCESSING (2.5s)" in line1

    bad_status = SimpleNamespace(progress="not-an-int", state="PENDING")
    line2 = _spi_compat._fallback_format_job_progress("B-2", bad_status, 1.0)
    assert "Job B-2:" in line2 and "0%" in line2 and "PENDING (1.0s)" in line2

    # Fallback console & status on TTY and non-TTY streams
    tty_status_stream = _TtyBuffer(tty=True)
    fb_console = _spi_compat._FallbackConsole(stream=tty_status_stream)
    with fb_console.status("Starting...") as status_ctx:
        status_ctx.update("Halfway...")
    assert "Starting..." in tty_status_stream.getvalue()
    assert "Halfway..." in tty_status_stream.getvalue()

    pipe_status_stream = _TtyBuffer(tty=False)
    monkeypatch.delenv("IMEDNET_CLI_OUTPUT_FORMAT", raising=False)
    with _spi_compat._FallbackConsole(stream=pipe_status_stream).status("\x1b[32mWorking\x1b[0m"):
        pass
    assert "Working" in pipe_status_stream.getvalue()

    # Suppressed when IMEDNET_CLI_OUTPUT_FORMAT=json
    json_status_stream = _TtyBuffer(tty=False)
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    with _spi_compat._FallbackConsole(stream=json_status_stream).status("Quiet"):
        pass
    assert json_status_stream.getvalue() == ""


def test_fallback_filter_date_and_partition_helpers() -> None:
    """Verify filter expression parsing, UTC bound normalization, and partition validation."""
    assert _spi_compat._fallback_parse_filter_expression("form_key=DEMO", model=_SampleModel) == (
        "form_key",
        "DEMO",
    )
    assert _spi_compat._fallback_parse_filter_expression("recordId>=10", model=_SampleModel) == (
        "recordId",
        (">=", 10),
    )
    assert _spi_compat._fallback_parse_filter_expression("active=true") == ("active", True)
    assert _spi_compat._fallback_parse_filter_expression("active=false") == ("active", False)

    for bad_expr in ("", "no_operator", "=missing_key", "key=", "a===b", "a<>b", "bad key=1"):
        with pytest.raises(ValueError):
            _spi_compat._fallback_parse_filter_expression(bad_expr)

    with pytest.raises(ValueError, match="Unknown filter attribute"):
        _spi_compat._fallback_parse_filter_expression("unknown_col=1", model=_SampleModel)

    assert _spi_compat._fallback_parse_filter_args(None) is None
    grouped = _spi_compat._fallback_parse_filter_args(
        ["record_id>=1", "record_id<=10", "form_key=DEMO", "form_key=AE"],
        model=_SampleModel,
    )
    assert grouped == {
        "record_id": [(">=", 1), ("<=", 10)],
        "form_key": [("==", "DEMO"), ("==", "AE")],
    }

    with pytest.raises(SystemExit) as exc_info:
        _spi_compat._fallback_parse_filter_args(["invalid_col=1"], model=_SampleModel)
    assert exc_info.value.code == 1

    # Date bound normalization
    assert (
        _spi_compat._fallback_normalize_utc_timestamp_bound("2026-03-01", is_end=False)
        == "2026-03-01T00:00:00Z"
    )
    assert (
        _spi_compat._fallback_normalize_utc_timestamp_bound("2026-03-01", is_end=True)
        == "2026-03-01T23:59:59Z"
    )
    assert (
        _spi_compat._fallback_normalize_utc_timestamp_bound("2026-03-01T12:30:00Z")
        == "2026-03-01T12:30:00Z"
    )
    with pytest.raises(ValueError, match="non-empty ISO 8601"):
        _spi_compat._fallback_normalize_utc_timestamp_bound("   ")

    # Partition key validation
    _spi_compat._fallback_validate_partition_key("STUDY_01")
    for bad_key in ("..", ".", "../escape", "a/b", "a\\b", "C:/foo", "bad\x00key"):
        with pytest.raises(_spi_compat.PathTraversalValidationError):
            _spi_compat._fallback_validate_partition_key(bad_key)


def test_spi_compat_reload_against_core_1_1_0_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate ``imednet==1.1.0`` SPI modules and verify clean plugin reload and fallback wiring."""
    legacy_cli = types.ModuleType("imednet.spi.cli")
    legacy_cli.STUDY_KEY_ARG = real_spi_cli.STUDY_KEY_ARG  # type: ignore[attr-defined]
    legacy_cli.with_sdk = real_spi_cli.with_sdk  # type: ignore[attr-defined]
    legacy_cli.parse_filter_args = lambda filter_args: {"raw": filter_args}  # type: ignore[attr-defined]

    legacy_utils = types.ModuleType("imednet.spi.utils")
    for name in dir(real_spi_utils):
        if name not in {
            "normalize_utc_timestamp_bound",
            "parse_filter_expression",
            "parse_filter_expressions",
            "validate_filter_key",
            "validate_partition_key",
            "PathTraversalValidationError",
        }:
            setattr(legacy_utils, name, getattr(real_spi_utils, name))

    import imednet.spi as spi_pkg
    import imednet_workflows.cli as wf_cli

    try:
        monkeypatch.setattr(spi_pkg, "cli", legacy_cli)
        monkeypatch.setattr(spi_pkg, "utils", legacy_utils)
        monkeypatch.setitem(sys.modules, "imednet.spi.cli", legacy_cli)
        monkeypatch.setitem(sys.modules, "imednet.spi.utils", legacy_utils)

        reloaded_compat = importlib.reload(_spi_compat)
        reloaded_wf_cli = importlib.reload(wf_cli)

        assert reloaded_compat.add_output_format is reloaded_compat._fallback_add_output_format
        assert reloaded_compat.display_list is reloaded_compat._fallback_display_list
        assert reloaded_compat.emit_json is reloaded_compat._fallback_emit_json
        assert (
            reloaded_compat.PathTraversalValidationError
            is real_spi_errors.PathTraversalValidationError
        )
        assert reloaded_compat.is_interactive_terminal() is False
        assert reloaded_compat.parse_filter_args(["form_key=DEMO"], model=_SampleModel) == {
            "form_key": "DEMO"
        }

        root_parser = argparse.ArgumentParser()
        sub = root_parser.add_subparsers(dest="cmd")
        reloaded_wf_cli.setup_parser(sub)
        reloaded_wf_cli.setup_subject_parser(sub)
        parsed = root_parser.parse_args(
            ["workflows", "extract-records", "STUDY1", "--format", "json"]
        )
        assert parsed.output_format == "json"
    finally:
        monkeypatch.undo()
        importlib.reload(_spi_compat)
        importlib.reload(wf_cli)
