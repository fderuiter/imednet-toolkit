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
        assert result.stdout == ""
        assert "Failed to reset ledger state" in result.stderr


class TestStateContractsAndInteractiveShow:
    """Tests for stderr error contracts, JSON envelopes, interactive show, and hybrid Study flags."""

    def test_state_errors_write_only_to_stderr_with_empty_stdout(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """Verify state set, reset, and show write error diagnostics to stderr with empty stdout."""
        ledger_path = str(tmp_path / "ledger.json")

        bad_ts = runner.invoke(
            state_app,
            [
                "set",
                "--study-key",
                "STUDY-01",
                "--stream",
                "records",
                "--timestamp",
                "invalid-ts",
                "--ledger-path",
                ledger_path,
            ],
        )
        assert bad_ts.exit_code == 1
        assert bad_ts.stdout == ""
        assert "Invalid ISO timestamp" in bad_ts.stderr

        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.set_last_timestamp.side_effect = OSError("disk full")
            write_err = runner.invoke(
                state_app,
                [
                    "set",
                    "STUDY-01",
                    "--stream",
                    "records",
                    "--timestamp",
                    "2026-05-22T12:00:00Z",
                    "--ledger-path",
                    ledger_path,
                ],
            )
        assert write_err.exit_code == 1
        assert write_err.stdout == ""
        assert "Failed to write ledger" in write_err.stderr

        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.side_effect = OSError("corrupt ledger")
            show_err = runner.invoke(
                state_app,
                ["show", "--ledger-path", ledger_path, "--format", "json"],
            )
        assert show_err.exit_code == 1
        assert show_err.stdout == ""
        assert "Failed to read ledger" in show_err.stderr

    def test_state_show_format_json_emits_v1_envelope(self, runner: CliRunner, tmp_path) -> None:
        """Verify state show --format json emits a v1 JSON envelope on stdout."""
        import json

        ledger_path = str(tmp_path / "ledger.json")
        state = _make_ledger_state("STUDY-01", "records")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.return_value = state
            result = runner.invoke(
                state_app,
                ["show", "STUDY-01", "--ledger-path", ledger_path, "--format", "json"],
            )

        assert result.exit_code == 0
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["schemaVersion"] == "1.0"
        assert payload["resource"] == "ledger_state"
        assert payload["status"] == "complete"
        assert payload["count"] == 1
        assert payload["items"][0]["studyKey"] == "STUDY-01"
        assert payload["items"][0]["streamName"] == "records"
        assert payload["items"][0]["recordsProcessed"] == 42

    def test_state_show_interactive_prompts_on_tty_and_rejects_piped_or_no_interactive(
        self, runner: CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify state show --interactive prompts on TTY and exits 1 on piped stdin or --no-interactive."""
        import io

        ledger_path = str(tmp_path / "ledger.json")
        state = _make_ledger_state("STUDY-01", "records")
        state.studies["STUDY-02"] = _make_ledger_state("STUDY-02", "visits").studies["STUDY-02"]

        class FakeTtyStdin(io.StringIO):
            def isatty(self) -> bool:
                return True

        monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
        monkeypatch.setattr("sys.stdin", FakeTtyStdin("STUDY-01\n"))
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.return_value = state
            tty_res = runner.invoke(
                state_app,
                ["show", "--ledger-path", ledger_path, "--interactive"],
            )
        assert tty_res.exit_code == 0
        assert "Enter study key to filter" in tty_res.stderr
        assert "STUDY-01" in tty_res.stdout
        assert "STUDY-02" not in tty_res.stdout

        # Piped (non-TTY) stdin exits 1 with stderr diagnostic and empty stdout
        monkeypatch.setattr("sys.stdin", io.StringIO("STUDY-01\n"))
        piped_res = runner.invoke(
            state_app,
            ["show", "--ledger-path", ledger_path, "--interactive"],
        )
        assert piped_res.exit_code == 1
        assert piped_res.stdout == ""
        assert "Interactive prompts require an interactive TTY" in piped_res.stderr

        # --no-interactive exits 1 even when stdin is a TTY
        monkeypatch.setattr("sys.stdin", FakeTtyStdin("STUDY-01\n"))
        no_int_res = runner.invoke(
            state_app,
            ["show", "--ledger-path", ledger_path, "--interactive", "--no-interactive"],
        )
        assert no_int_res.exit_code == 1
        assert no_int_res.stdout == ""
        assert "Interactive prompts require an interactive TTY" in no_int_res.stderr

    def test_state_hybrid_study_key_conflict_exits_2(self, runner: CliRunner, tmp_path) -> None:
        """Verify conflicting positional and --study-key on state commands exits 2."""
        ledger_path = str(tmp_path / "ledger.json")
        res = runner.invoke(
            state_app,
            ["show", "STUDY-01", "--study-key", "STUDY-02", "--ledger-path", ledger_path],
        )
        assert res.exit_code == 2
        assert res.stdout == ""
        assert "conflicting" in res.stderr
