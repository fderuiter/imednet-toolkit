"""Tests for the imednet_workflows state CLI subcommands."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest


class Result:
    def __init__(self, exit_code, stdout, stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.output = stdout + stderr


class CliRunner:
    def invoke(self, app, args):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                if hasattr(app, "parse_args"):
                    pass
                app(args)
        except SystemExit as e:
            exit_code = e.code or 0
        except Exception:
            import traceback

            err.write(traceback.format_exc())
            exit_code = 1

        # We also need to catch argparse sys.exit(2)
        return Result(exit_code, out.getvalue(), err.getvalue())


from imednet_workflows.cli import state_app
from imednet_workflows.state_ledger import LedgerState, StreamState, StudyState


@pytest.fixture
def runner() -> CliRunner:
    """Helper function to runner."""
    return CliRunner()


def _make_ledger_state(study_key: str, stream_name: str) -> LedgerState:
    """Helper function to  make ledger state."""
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    return LedgerState(
        studies={
            study_key: StudyState(
                streams={
                    stream_name: StreamState(
                        last_timestamp=ts,
                        records_processed=42,
                        last_run_status="success",
                    )
                }
            )
        }
    )


class TestShowState:
    """Test suite for ShowState."""

    def test_show_all_entries(self, runner: CliRunner, tmp_path) -> None:
        """Test that show all entries."""
        ledger_path = str(tmp_path / "ledger.json")
        state = _make_ledger_state("STUDY-01", "records")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.return_value = state
            result = runner.invoke(state_app, ["show", "--ledger-path", ledger_path])

        assert result.exit_code == 0

    def test_show_filters_by_study_key(self, runner: CliRunner, tmp_path) -> None:
        """Test that show filters by study key."""
        ledger_path = str(tmp_path / "ledger.json")
        state = _make_ledger_state("STUDY-01", "records")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.return_value = state
            result = runner.invoke(
                state_app,
                ["show", "--ledger-path", ledger_path, "--study-key", "STUDY-01"],
            )

        assert result.exit_code == 0

    def test_show_no_matching_entries_prints_warning(self, runner: CliRunner, tmp_path) -> None:
        """Test that show no matching entries prints warning."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.return_value = LedgerState()
            result = runner.invoke(state_app, ["show", "--ledger-path", ledger_path])

        assert result.exit_code == 0
        assert "No ledger entries" in result.output

    def test_show_error_on_read_failure(self, runner: CliRunner, tmp_path) -> None:
        """Test that show error on read failure."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.side_effect = OSError("cannot read")
            result = runner.invoke(state_app, ["show", "--ledger-path", ledger_path])

        assert result.exit_code == 1


class TestSetState:
    """Test suite for SetState."""

    def test_set_valid_utc_timestamp(self, runner: CliRunner, tmp_path) -> None:
        """Test that set valid utc timestamp."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            result = runner.invoke(
                state_app,
                [
                    "set",
                    "--study-key",
                    "STUDY-01",
                    "--stream",
                    "records",
                    "--timestamp",
                    "2026-05-22T12:00:00Z",
                    "--ledger-path",
                    ledger_path,
                ],
            )
            mock_ledger.return_value.set_last_timestamp.assert_called_once()

        assert result.exit_code == 0
        assert "Successfully set" in result.output

    def test_set_with_records_processed(self, runner: CliRunner, tmp_path) -> None:
        """Test that set with records processed."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            result = runner.invoke(
                state_app,
                [
                    "set",
                    "--study-key",
                    "STUDY-01",
                    "--stream",
                    "records",
                    "--timestamp",
                    "2026-05-22T12:00:00+00:00",
                    "--records-processed",
                    "100",
                    "--ledger-path",
                    ledger_path,
                ],
            )
            call_kwargs = mock_ledger.return_value.set_last_timestamp.call_args.kwargs
            assert call_kwargs["records_processed"] == 100
            expected_ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
            assert call_kwargs["timestamp"] == expected_ts

        assert result.exit_code == 0

    def test_set_invalid_timestamp_exits_with_error(self, runner: CliRunner, tmp_path) -> None:
        """Test that set invalid timestamp exits with error."""
        ledger_path = str(tmp_path / "ledger.json")
        result = runner.invoke(
            state_app,
            [
                "set",
                "--study-key",
                "STUDY-01",
                "--stream",
                "records",
                "--timestamp",
                "not-a-timestamp",
                "--ledger-path",
                ledger_path,
            ],
        )
        assert result.exit_code == 1
        assert "Invalid ISO timestamp" in result.output

    def test_set_write_failure_exits_with_error(self, runner: CliRunner, tmp_path) -> None:
        """Test that set write failure exits with error."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.set_last_timestamp.side_effect = OSError("disk full")
            result = runner.invoke(
                state_app,
                [
                    "set",
                    "--study-key",
                    "STUDY-01",
                    "--stream",
                    "records",
                    "--timestamp",
                    "2026-05-22T12:00:00Z",
                    "--ledger-path",
                    ledger_path,
                ],
            )

        assert result.exit_code == 1
        assert "Failed to write ledger" in result.output


class TestResetState:
    """Test suite for ResetState."""

    def test_reset_whole_study_when_found(self, runner: CliRunner, tmp_path) -> None:
        """Test that reset whole study when found."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.delete_entry.return_value = True
            result = runner.invoke(
                state_app,
                ["reset", "--study-key", "STUDY-01", "--ledger-path", ledger_path],
            )
            mock_ledger.return_value.delete_entry.assert_called_once_with("STUDY-01")

        assert result.exit_code == 0
        assert "Successfully reset all streams" in result.output

    def test_reset_specific_stream_when_found(self, runner: CliRunner, tmp_path) -> None:
        """Test that reset specific stream when found."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.delete_entry.return_value = True
            result = runner.invoke(
                state_app,
                [
                    "reset",
                    "--study-key",
                    "STUDY-01",
                    "--stream",
                    "records",
                    "--ledger-path",
                    ledger_path,
                ],
            )
            mock_ledger.return_value.delete_entry.assert_called_once_with("STUDY-01", "records")

        assert result.exit_code == 0
        assert "Successfully reset stream" in result.output

    def test_reset_study_not_found_prints_warning(self, runner: CliRunner, tmp_path) -> None:
        """Test that reset study not found prints warning."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.delete_entry.return_value = False
            result = runner.invoke(
                state_app,
                ["reset", "--study-key", "NONEXISTENT", "--ledger-path", ledger_path],
            )

        assert result.exit_code == 0
        assert "No state found" in result.output

    def test_reset_stream_not_found_prints_warning(self, runner: CliRunner, tmp_path) -> None:
        """Test that reset stream not found prints warning."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.delete_entry.return_value = False
            result = runner.invoke(
                state_app,
                [
                    "reset",
                    "--study-key",
                    "STUDY-01",
                    "--stream",
                    "nonexistent",
                    "--ledger-path",
                    ledger_path,
                ],
            )

        assert result.exit_code == 0
        assert "No stream" in result.output

    def test_reset_exception_exits_with_error(self, runner: CliRunner, tmp_path) -> None:
        """Test that reset exception exits with error."""
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.delete_entry.side_effect = OSError("locked")
            result = runner.invoke(
                state_app,
                ["reset", "--study-key", "STUDY-01", "--ledger-path", ledger_path],
            )

        assert result.exit_code == 1
        assert "Failed to reset ledger state" in result.output


def test_show_state_handles_none_timestamp(runner: CliRunner, tmp_path) -> None:
    """state show renders None without AttributeError when last_timestamp is uncommitted."""
    import argparse

    from imednet_workflows.cli import setup_parser

    ledger_path = str(tmp_path / "ledger.json")
    state = LedgerState(
        studies={
            "STUDY-NULL": StudyState(
                streams={
                    "records": StreamState(
                        last_timestamp=None,
                        records_processed=0,
                        last_run_status="failed",
                        error_message="initial failure",
                    )
                }
            )
        }
    )
    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        mock_ledger.return_value.read_state.return_value = state
        res_app = runner.invoke(state_app, ["show", "--ledger-path", ledger_path])

        root = argparse.ArgumentParser()
        setup_parser(root.add_subparsers(dest="sub"))
        res_main = runner.invoke(
            lambda argv: (parsed := root.parse_args(argv)).func(parsed),
            ["workflows", "state", "show", "--ledger-path", ledger_path],
        )

    assert res_app.exit_code == 0
    assert "STUDY-NULL | records | None | 0 | failed | initial failure" in res_app.stdout
    assert res_main.exit_code == 0
    assert "STUDY-NULL | records | None | 0 | failed | initial failure" in res_main.stdout


def test_set_state_force_flag_overrides_stuck_stream(runner: CliRunner, tmp_path) -> None:
    """workflows state set --force overrides invalid state transitions such as PENDING -> SUCCESS."""
    from imednet_workflows.state_ledger import ExtractionStateLedger, StreamExecutionStatus

    ledger_path = str(tmp_path / "ledger.json")
    ledger = ExtractionStateLedger(ledger_path)
    ledger.set_last_timestamp("STUDY-STUCK", "records", None, status=StreamExecutionStatus.PENDING)

    # Without --force, PENDING -> SUCCESS is rejected
    rejected = runner.invoke(
        state_app,
        [
            "set",
            "-s",
            "STUDY-STUCK",
            "-m",
            "records",
            "-t",
            "2026-05-22T12:00:00Z",
            "-l",
            ledger_path,
        ],
    )
    assert rejected.exit_code == 1
    assert "Failed to write ledger" in rejected.output

    # With --force, manual override succeeds
    forced = runner.invoke(
        state_app,
        [
            "set",
            "-s",
            "STUDY-STUCK",
            "-m",
            "records",
            "-t",
            "2026-05-22T12:00:00Z",
            "-l",
            ledger_path,
            "--force",
        ],
    )
    assert forced.exit_code == 0
    assert ledger.get_last_timestamp("STUDY-STUCK", "records") == datetime(
        2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc
    )
    assert ledger.get_last_status("STUDY-STUCK", "records") == StreamExecutionStatus.SUCCESS
