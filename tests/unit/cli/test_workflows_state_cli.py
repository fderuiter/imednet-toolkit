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
        """Headless workflows state set --force recovers failed and pending streams to success."""
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

        # Non-forced CLI set routes diagnostic to stderr and exits 1
        rejected = runner.invoke(
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
        assert rejected.exit_code == 1
        assert rejected.stdout == ""
        assert "Failed to write ledger" in rejected.stderr

        # Headless CLI recovery via top-level workflows state set --force succeeds with exit code 0
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
                "--force",
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


class _InteractiveStringIO:
    """In-memory TTY-like stdin stream for interactive wizard tests."""

    def __init__(self, text: str) -> None:
        import io

        self._buf = io.StringIO(text)

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self._buf.readline()


@pytest.mark.parametrize("via_root_cli", [False, True])
class TestStateStreamContractsAndParity:
    """Verify cli.app and state_app share parser parity, TTY guards, and stderr-only errors."""

    @staticmethod
    def _invoke(runner: CliRunner, via_root_cli: bool, state_args: list[str]) -> Result:
        from imednet import cli

        if via_root_cli:
            return runner.invoke(cli.app, ["workflows", "state", *state_args])
        return runner.invoke(state_app, state_args)

    def test_error_1_show_read_failure_writes_only_to_stderr(
        self, runner: CliRunner, tmp_path, via_root_cli: bool
    ) -> None:
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.side_effect = OSError("cannot read")
            result = self._invoke(runner, via_root_cli, ["show", "--ledger-path", ledger_path])

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Failed to read ledger" in result.stderr

    def test_error_2_show_interactive_piped_stdin_exits_one_on_stderr(
        self, runner: CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch, via_root_cli: bool
    ) -> None:
        import io
        import sys

        ledger_path = str(tmp_path / "ledger.json")
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        result = self._invoke(
            runner, via_root_cli, ["show", "--interactive", "--ledger-path", ledger_path]
        )
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Interactive prompts require an interactive TTY on stdin" in result.stderr

    def test_error_2b_show_interactive_with_no_interactive_exits_one_on_stderr(
        self, runner: CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch, via_root_cli: bool
    ) -> None:
        import sys

        ledger_path = str(tmp_path / "ledger.json")
        monkeypatch.setattr(sys, "stdin", _InteractiveStringIO("STUDY-01\n"))
        result = self._invoke(
            runner,
            via_root_cli,
            ["show", "--interactive", "--no-interactive", "--ledger-path", ledger_path],
        )
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Interactive prompts require an interactive TTY on stdin" in result.stderr

    def test_show_interactive_tty_prompts_and_filters_study(
        self, runner: CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch, via_root_cli: bool
    ) -> None:
        import sys

        ledger_path = str(tmp_path / "ledger.json")
        state = _make_ledger_state("STUDY-01", "records")
        state.studies["STUDY-02"] = _make_ledger_state("STUDY-02", "visits").studies["STUDY-02"]
        monkeypatch.setattr(sys, "stdin", _InteractiveStringIO("STUDY-01\n"))
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.return_value = state
            result = self._invoke(
                runner, via_root_cli, ["show", "--interactive", "--ledger-path", ledger_path]
            )
        assert result.exit_code == 0
        assert "STUDY-01 | records" in result.stdout
        assert "STUDY-02" not in result.stdout

    def test_error_3_set_invalid_timestamp_writes_only_to_stderr(
        self, runner: CliRunner, tmp_path, via_root_cli: bool
    ) -> None:
        ledger_path = str(tmp_path / "ledger.json")
        result = self._invoke(
            runner,
            via_root_cli,
            [
                "set",
                "-s",
                "STUDY-01",
                "-m",
                "records",
                "-t",
                "bad-timestamp",
                "-l",
                ledger_path,
            ],
        )
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Invalid ISO timestamp format" in result.stderr

    def test_error_4_set_headless_write_failure_writes_only_to_stderr(
        self, runner: CliRunner, tmp_path, via_root_cli: bool
    ) -> None:
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.set_last_timestamp.side_effect = OSError("disk full")
            result = self._invoke(
                runner,
                via_root_cli,
                [
                    "set",
                    "-s",
                    "STUDY-01",
                    "-m",
                    "records",
                    "-t",
                    "2026-05-22T12:00:00Z",
                    "-l",
                    ledger_path,
                ],
            )
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Failed to write ledger" in result.stderr

    def test_error_5_set_interactive_write_failure_writes_only_to_stderr(
        self, runner: CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch, via_root_cli: bool
    ) -> None:
        import sys

        ledger_path = str(tmp_path / "ledger.json")
        monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
        monkeypatch.setattr(sys, "stdin", _InteractiveStringIO("5\ny\n"))
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.read_state.return_value = LedgerState()
            mock_ledger.return_value.set_last_timestamp.side_effect = OSError("readonly fs")
            result = self._invoke(
                runner,
                via_root_cli,
                [
                    "set",
                    "--interactive",
                    "-s",
                    "STUDY-01",
                    "-m",
                    "records",
                    "-t",
                    "2026-05-22T12:00:00Z",
                    "-l",
                    ledger_path,
                ],
            )
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Failed to write ledger" in result.stderr

    def test_error_6_reset_exception_writes_only_to_stderr(
        self, runner: CliRunner, tmp_path, via_root_cli: bool
    ) -> None:
        ledger_path = str(tmp_path / "ledger.json")
        with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
            mock_ledger.return_value.delete_entry.side_effect = OSError("locked")
            result = self._invoke(
                runner,
                via_root_cli,
                ["reset", "--study-key", "STUDY-01", "--ledger-path", ledger_path],
            )
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Failed to reset ledger state" in result.stderr


def test_direct_state_helpers_missing_args_write_only_to_stderr(tmp_path, capsys) -> None:
    """Verify _execute_set_state and _execute_reset_state fallback error paths write to stderr."""
    from imednet_workflows.cli import _execute_reset_state, _execute_set_state

    ledger_path = str(tmp_path / "ledger.json")
    with pytest.raises(SystemExit) as set_exc:
        _execute_set_state(
            study_key=None,
            stream=None,
            timestamp=None,
            records_processed=0,
            ledger_path=ledger_path,
            interactive=False,
            parser=None,
        )
    set_cap = capsys.readouterr()
    assert set_exc.value.code == 2
    assert set_cap.out == ""
    assert "the following arguments are required" in set_cap.err

    with pytest.raises(SystemExit) as reset_exc:
        _execute_reset_state(
            study_key=None,
            stream=None,
            ledger_path=ledger_path,
            interactive=False,
            parser=None,
        )
    reset_cap = capsys.readouterr()
    assert reset_exc.value.code == 2
    assert reset_cap.out == ""
    assert "the following arguments are required" in reset_cap.err


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


def test_show_state_json_envelope_and_hybrid_study_key(runner: CliRunner, tmp_path) -> None:
    """workflows state show --format json emits v1 ledger_state envelope and supports positional study_key."""
    import json

    from imednet.cli import app as main_app

    ledger_path = str(tmp_path / "ledger.json")
    state = _make_ledger_state("STUDY-01", "records")
    state.studies["STUDY-02"] = _make_ledger_state("STUDY-02", "visits").studies["STUDY-02"]

    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        mock_ledger.return_value.read_state.return_value = state
        res_standalone = runner.invoke(
            state_app, ["show", "STUDY-01", "--ledger-path", ledger_path, "--format", "json"]
        )
        res_main = runner.invoke(
            main_app,
            [
                "workflows",
                "state",
                "show",
                "--study-key",
                "STUDY-01",
                "--ledger-path",
                ledger_path,
                "--format",
                "json",
            ],
        )

    for res in (res_standalone, res_main):
        assert res.exit_code == 0
        assert res.stderr == ""
        payload = json.loads(res.stdout)
        assert payload["schemaVersion"] == "1.0"
        assert payload["resource"] == "ledger_state"
        assert payload["status"] == "complete"
        assert payload["count"] == 1
        assert payload["items"] == [
            {
                "studyKey": "STUDY-01",
                "streamName": "records",
                "lastTimestamp": "2026-05-22T12:00:00+00:00",
                "recordsProcessed": 42,
                "status": "success",
                "errorMessage": None,
            }
        ]


def test_show_state_rejects_interactive_json_and_conflicting_study_keys(
    runner: CliRunner, tmp_path
) -> None:
    """workflows state show rejects --interactive --format json (exit 1) and conflicting study keys (exit 2)."""
    from imednet.cli import app as main_app

    ledger_path = str(tmp_path / "ledger.json")
    conflict_fmt = runner.invoke(
        main_app,
        ["workflows", "state", "show", "--interactive", "--format", "json", "-l", ledger_path],
    )
    assert conflict_fmt.exit_code == 1
    assert conflict_fmt.stdout == ""
    assert "Cannot combine --interactive with --format json" in conflict_fmt.stderr

    conflict_study = runner.invoke(
        main_app,
        ["workflows", "state", "show", "STUDY-A", "--study-key", "STUDY-B", "-l", ledger_path],
    )
    assert conflict_study.exit_code == 2
    assert conflict_study.stdout == ""
    assert "conflicting values provided for study_key" in conflict_study.stderr


def test_show_state_output_format_env_and_explicit_table_override(
    runner: CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMEDNET_CLI_OUTPUT_FORMAT=json selects JSON unless --format table explicitly overrides it."""
    import json
    import os

    from imednet.cli import app as main_app

    ledger_path = str(tmp_path / "ledger.json")
    state = _make_ledger_state("STUDY-01", "records")
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")

    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        mock_ledger.return_value.read_state.return_value = state
        env_json_standalone = runner.invoke(state_app, ["show", "-l", ledger_path])
        env_json_main = runner.invoke(main_app, ["workflows", "state", "show", "-l", ledger_path])
        override_standalone = runner.invoke(
            state_app, ["show", "-l", ledger_path, "--format", "table"]
        )
        override_main = runner.invoke(
            main_app, ["workflows", "state", "show", "-l", ledger_path, "--format", "table"]
        )

    for res in (env_json_standalone, env_json_main):
        assert res.exit_code == 0
        payload = json.loads(res.stdout)
        assert payload["resource"] == "ledger_state"

    for res in (override_standalone, override_main):
        assert res.exit_code == 0
        assert "STUDY-01 | records" in res.stdout
    assert os.environ.get("IMEDNET_CLI_OUTPUT_FORMAT") == "json"


@pytest.mark.parametrize("initial_status", ["failed", "pending"])
def test_headless_state_set_recovers_airflow_variable_streams(
    runner: CliRunner,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    initial_status: str,
) -> None:
    """Headless workflows state set --force recovers failed/pending Airflow 3 Variable checkpoints."""
    import logging
    import sys

    if (
        "airflow" not in sys.modules
        or getattr(sys.modules.get("airflow"), "__file__", None) is None
    ):
        for mod in list(sys.modules):
            if mod == "airflow" or mod.startswith("airflow."):
                sys.modules.pop(mod, None)

    pytest.importorskip("airflow")
    import airflow.settings
    from airflow.configuration import conf
    from airflow.utils.db import initdb

    from imednet.cli import app as main_app
    from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    db_file = tmp_path / f"airflow_cli_{initial_status}.db"
    db_url = f"sqlite:///{db_file}"
    async_db_url = f"sqlite+aiosqlite:///{db_file}"
    prev_conn = conf.get("database", "sql_alchemy_conn", fallback=None)
    prev_async_conn = conf.get("database", "sql_alchemy_conn_async", fallback=None)
    prev_pool_enabled = conf.get("database", "sql_alchemy_pool_enabled", fallback=None)
    prev_settings_conn = airflow.settings.SQL_ALCHEMY_CONN
    has_async_conn_attr = hasattr(airflow.settings, "SQL_ALCHEMY_CONN_ASYNC")
    prev_settings_async_conn = getattr(airflow.settings, "SQL_ALCHEMY_CONN_ASYNC", None)
    conf.set("database", "sql_alchemy_conn", db_url)
    conf.set("database", "sql_alchemy_conn_async", async_db_url)
    conf.set("database", "sql_alchemy_pool_enabled", "False")
    airflow.settings.SQL_ALCHEMY_CONN = db_url
    if has_async_conn_attr:
        airflow.settings.SQL_ALCHEMY_CONN_ASYNC = async_db_url
    airflow.settings.dispose_orm()
    airflow.settings.configure_orm()
    initdb()
    monkeypatch.setenv("USE_AIRFLOW_STATE_PROVIDER", "1")
    monkeypatch.delenv("AIRFLOW_CTX_TASK_ID", raising=False)

    try:
        provider = AirflowStateProvider()
        provider.set_last_timestamp(
            study_key="STUDY-AF-CLI",
            stream_name="records",
            timestamp=datetime(2026, 5, 20, 8, 0, 0, tzinfo=timezone.utc),
            records_processed=5,
            status=initial_status,  # type: ignore[arg-type]
            error_message="worker crash" if initial_status == "failed" else None,
            force=True,
        )

        # Non-forced CLI set rejects invalid transition on stderr with exit code 1
        rejected = runner.invoke(
            main_app,
            [
                "workflows",
                "state",
                "set",
                "--study-key",
                "STUDY-AF-CLI",
                "--stream",
                "records",
                "--timestamp",
                "2026-05-22T12:00:00Z",
                "--records-processed",
                "12",
            ],
        )
        assert rejected.exit_code == 1
        assert rejected.stdout == ""
        assert "Failed to write ledger" in rejected.stderr

        # Forced CLI set recovers Airflow Variable state to success and clears error_message
        recovered = runner.invoke(
            main_app,
            [
                "workflows",
                "state",
                "set",
                "--study-key",
                "STUDY-AF-CLI",
                "--stream",
                "records",
                "--timestamp",
                "2026-05-22T12:00:00Z",
                "--records-processed",
                "12",
                "--force",
            ],
        )
        assert recovered.exit_code == 0
        assert "Successfully set high-water mark" in recovered.stdout
        assert recovered.stderr == ""
        assert provider.get_last_status("STUDY-AF-CLI", "records") == StreamExecutionStatus.SUCCESS
        updated = provider.read_state().studies["STUDY-AF-CLI"].streams["records"]
        assert updated.records_processed == 12
        assert updated.error_message is None
    finally:
        airflow.settings.dispose_orm()
        airflow.settings.SQL_ALCHEMY_CONN = prev_settings_conn
        if has_async_conn_attr:
            airflow.settings.SQL_ALCHEMY_CONN_ASYNC = prev_settings_async_conn
        if prev_conn is not None:
            conf.set("database", "sql_alchemy_conn", prev_conn)
        if prev_async_conn is not None:
            conf.set("database", "sql_alchemy_conn_async", prev_async_conn)
        if prev_pool_enabled is not None:
            conf.set("database", "sql_alchemy_pool_enabled", prev_pool_enabled)
        airflow.settings.configure_orm()
        root_logger.handlers = saved_handlers
        root_logger.setLevel(saved_level)


def test_interactive_state_set_reads_ledger_once_and_reuses_wizard_state(
    runner: CliRunner,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive workflows state set reuses wizard LedgerState instead of calling read_state twice."""
    import sys

    ledger_path = str(tmp_path / "ledger.json")
    existing_state = _make_ledger_state("STUDY-ONCE", "records")
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _InteractiveStringIO("15\ny\n"))

    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        mock_ledger.return_value.read_state.return_value = existing_state
        res = runner.invoke(
            state_app,
            [
                "set",
                "--interactive",
                "-s",
                "STUDY-ONCE",
                "-m",
                "records",
                "-t",
                "2026-06-01T00:00:00Z",
                "-l",
                ledger_path,
            ],
        )

    assert res.exit_code == 0
    assert mock_ledger.call_count == 1
    assert mock_ledger.return_value.read_state.call_count == 1
    assert "Current Status:    success (timestamp: 2026-05-22T12:00:00+00:00)" in res.stderr
    mock_ledger.return_value.set_last_timestamp.assert_called_once()
