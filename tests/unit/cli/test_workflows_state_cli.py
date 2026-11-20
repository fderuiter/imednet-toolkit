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
        assert result.stdout == ""
        assert "Failed to reset ledger state" in result.stderr


class TestHeadlessStateRecoveryAndContracts:
    """Verify headless state recovery, stderr diagnostics, and interrupt exit codes."""

    @pytest.mark.parametrize("initial_status", ["failed", "pending"])
    def test_headless_state_set_recovers_failed_and_pending_streams(
        self, runner: CliRunner, tmp_path, initial_status: str
    ) -> None:
        """Headless workflows state set recovers failed and pending streams to success."""
        from imednet.cli import app as main_app
        from imednet_workflows.state_ledger import FileStateProvider, InvalidStateTransitionError

        ledger_path = tmp_path / "ledger.json"
        provider = FileStateProvider(ledger_path)
        ts_old = datetime(2026, 5, 20, 8, 0, 0, tzinfo=timezone.utc)
        ts_new = "2026-05-22T12:00:00Z"

        provider.set_last_timestamp(
            study_key="STUDY-REC",
            stream_name="records",
            timestamp=ts_old,
            records_processed=5,
            status=initial_status,  # type: ignore[arg-type]
            error_message="boom" if initial_status == "failed" else None,
            force=True,
        )

        # Programmatic non-forced update must still enforce strict transition matrix
        with pytest.raises(InvalidStateTransitionError):
            provider.set_last_timestamp(
                study_key="STUDY-REC",
                stream_name="records",
                timestamp=datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc),
                status="success",
                force=False,
            )

        # Headless CLI recovery via top-level workflows state set succeeds with exit code 0
        result = runner.invoke(
            main_app,
            [
                "workflows",
                "state",
                "set",
                "--study-key",
                "STUDY-REC",
                "--stream",
                "records",
                "--timestamp",
                ts_new,
                "--records-processed",
                "12",
                "--ledger-path",
                str(ledger_path),
            ],
        )
        assert result.exit_code == 0
        assert "Successfully set high-water mark" in result.stdout
        assert result.stderr == ""

        updated = provider.read_state().studies["STUDY-REC"].streams["records"]
        assert updated.last_run_status == "success"
        assert updated.records_processed == 12
        assert updated.error_message is None

    def test_state_diagnostics_route_exclusively_to_stderr(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """State command errors write only to stderr in both main app and state_app."""
        from imednet.cli import app as main_app

        ledger_path = str(tmp_path / "ledger.json")

        res_bad_ts = runner.invoke(
            main_app,
            [
                "workflows",
                "state",
                "set",
                "-s",
                "S1",
                "-m",
                "records",
                "-t",
                "invalid-ts",
                "-l",
                ledger_path,
            ],
        )
        assert res_bad_ts.exit_code == 1
        assert res_bad_ts.stdout == ""
        assert "Invalid ISO timestamp format" in res_bad_ts.stderr

        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.side_effect = OSError("corrupt ledger")
            res_show = runner.invoke(main_app, ["workflows", "state", "show", "-l", ledger_path])
        assert res_show.exit_code == 1
        assert res_show.stdout == ""
        assert "Failed to read ledger" in res_show.stderr

    def test_state_commands_keyboard_interrupt_exits_130(self, runner: CliRunner, tmp_path) -> None:
        """Ctrl+C in state_app and workflows state exits 130 with Aborted by user. on stderr."""
        from imednet.cli import app as main_app

        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.side_effect = KeyboardInterrupt()
            res_standalone = runner.invoke(state_app, ["show", "-l", ledger_path])
            res_main = runner.invoke(main_app, ["workflows", "state", "show", "-l", ledger_path])

        for res in (res_standalone, res_main):
            assert res.exit_code == 130
            assert res.stdout == ""
            assert "Aborted by user." in res.stderr
            assert "Traceback" not in res.stderr
