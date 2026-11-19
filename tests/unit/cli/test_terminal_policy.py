"""Unit and PTY integration tests for centralized CLI terminal policy."""

from __future__ import annotations

import errno
import importlib
import io
import json
import os
import pty
import re
import sys
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

import imednet.cli.utils.grid as grid_mod
import imednet.cli.utils.output as output_mod
import imednet.cli.utils.terminal as terminal_mod
import imednet.spi.cli as spi_cli
from imednet import cli
from imednet.models.jobs import JobStatus
from imednet.models.studies import Study
from imednet_workflows.cli import state_app
from imednet_workflows.wizard import prompt_confirm, prompt_input


class _FakeTTY(io.StringIO):
    """In-memory stream reporting isatty() == True."""

    def isatty(self) -> bool:
        return True


class _BrokenStream:
    """Stream raising ValueError on isatty() as closed files do."""

    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")


def _drain_fd(fd: int, chunks: list[bytes]) -> None:
    """Read from a PTY master fd until EOF (EIO on Linux)."""
    while True:
        try:
            data = os.read(fd, 4096)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                break
            raise
        if not data:
            break
        chunks.append(data)


def _run_on_combined_pty(fn: Callable[[], None]) -> tuple[int, str]:
    """Execute ``fn`` with stdin, stdout, and stderr bound to one real PTY."""
    master_fd, slave_fd = pty.openpty()
    chunks: list[bytes] = []
    reader = threading.Thread(target=_drain_fd, args=(master_fd, chunks), daemon=True)
    reader.start()

    stdin_fd = os.dup(slave_fd)
    stdout_fd = os.dup(slave_fd)
    stderr_fd = os.dup(slave_fd)
    os.close(slave_fd)

    pty_in = os.fdopen(stdin_fd, "r", encoding="utf-8", closefd=True)
    pty_out = os.fdopen(stdout_fd, "w", encoding="utf-8", buffering=1, closefd=True)
    pty_err = os.fdopen(stderr_fd, "w", encoding="utf-8", buffering=1, closefd=True)

    old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
    exit_code = 0
    try:
        sys.stdin = pty_in
        sys.stdout = pty_out
        sys.stderr = pty_err
        try:
            fn()
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err
        pty_out.close()
        pty_err.close()
        pty_in.close()
        reader.join(timeout=2.0)
        os.close(master_fd)

    return exit_code, b"".join(chunks).decode("utf-8", errors="replace")


def _run_on_split_ptys(fn: Callable[[], None]) -> tuple[int, str, str]:
    """Execute ``fn`` with interactive PTYs separated between stdout and stderr."""
    out_master, out_slave = pty.openpty()
    err_master, err_slave = pty.openpty()
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    out_reader = threading.Thread(target=_drain_fd, args=(out_master, out_chunks), daemon=True)
    err_reader = threading.Thread(target=_drain_fd, args=(err_master, err_chunks), daemon=True)
    out_reader.start()
    err_reader.start()

    stdin_fd = os.dup(out_slave)
    pty_in = os.fdopen(stdin_fd, "r", encoding="utf-8", closefd=True)
    pty_out = os.fdopen(out_slave, "w", encoding="utf-8", buffering=1, closefd=True)
    pty_err = os.fdopen(err_slave, "w", encoding="utf-8", buffering=1, closefd=True)

    old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
    exit_code = 0
    try:
        sys.stdin = pty_in
        sys.stdout = pty_out
        sys.stderr = pty_err
        try:
            fn()
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err
        pty_out.close()
        pty_err.close()
        pty_in.close()
        out_reader.join(timeout=2.0)
        err_reader.join(timeout=2.0)
        os.close(out_master)
        os.close(err_master)

    return (
        exit_code,
        b"".join(out_chunks).decode("utf-8", errors="replace"),
        b"".join(err_chunks).decode("utf-8", errors="replace"),
    )


def test_spi_cli_reexports_terminal_policy_symbols() -> None:
    """Verify imednet.spi.cli re-exports the centralized terminal policy surface."""
    expected = (
        "NON_INTERACTIVE_ENV",
        "NO_COLOR_ENV",
        "OUTPUT_FORMAT_ENV",
        "add_no_interactive",
        "is_dumb_terminal",
        "is_interactive_terminal",
        "is_json_output_mode",
        "is_non_interactive",
        "is_tty",
        "supports_ansi_color",
        "supports_fullscreen_ui",
        "supports_interactive_stdin",
        "supports_live_status",
    )
    for name in expected:
        assert hasattr(spi_cli, name), f"Missing {name} on imednet.spi.cli"
    assert spi_cli.is_interactive_terminal is grid_mod.is_interactive_terminal
    assert spi_cli.supports_fullscreen_ui is terminal_mod.supports_fullscreen_ui
    assert spi_cli.supports_live_status is terminal_mod.supports_live_status
    assert spi_cli.supports_ansi_color is terminal_mod.supports_ansi_color
    assert spi_cli.supports_interactive_stdin is terminal_mod.supports_interactive_stdin


def test_capability_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test separated capability predicates across TTY, TERM, NO_COLOR, JSON, and non-interactive."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    monkeypatch.delenv("IMEDNET_CLI_OUTPUT_FORMAT", raising=False)
    monkeypatch.setattr(sys, "argv", ["imednet"])

    tty_in = _FakeTTY()
    tty_out = _FakeTTY()
    tty_err = _FakeTTY()
    pipe_stream = io.StringIO()

    assert terminal_mod.is_tty(None) is False
    assert terminal_mod.is_tty(_BrokenStream()) is False
    assert terminal_mod.is_tty(pipe_stream) is False
    assert terminal_mod.is_tty(tty_out) is True

    # Baseline interactive TTY
    assert terminal_mod.supports_fullscreen_ui(tty_in, tty_out) is True
    assert terminal_mod.supports_live_status(tty_err) is True
    assert terminal_mod.supports_ansi_color(tty_out) is True
    assert terminal_mod.supports_interactive_stdin(tty_in) is True

    # Piped stdin disables fullscreen UI and stdin prompts, while stderr status & stdout color work
    assert terminal_mod.supports_fullscreen_ui(pipe_stream, tty_out) is False
    assert terminal_mod.supports_interactive_stdin(pipe_stream) is False
    assert terminal_mod.supports_live_status(tty_err) is True
    assert terminal_mod.supports_ansi_color(tty_out) is True

    # NO_COLOR disables ANSI SGR color ONLY; fullscreen UI, live status, and stdin remain enabled
    monkeypatch.setenv("NO_COLOR", "1")
    assert terminal_mod.supports_ansi_color(tty_out) is False
    assert terminal_mod.supports_live_status(tty_err) is True
    assert terminal_mod.supports_fullscreen_ui(tty_in, tty_out) is True
    assert terminal_mod.supports_interactive_stdin(tty_in) is True
    monkeypatch.delenv("NO_COLOR")

    # IMEDNET_NON_INTERACTIVE=1 disables fullscreen UI, live status, and prompts, NOT static color
    monkeypatch.setenv("IMEDNET_NON_INTERACTIVE", "true")
    assert terminal_mod.supports_fullscreen_ui(tty_in, tty_out) is False
    assert terminal_mod.supports_live_status(tty_err) is False
    assert terminal_mod.supports_interactive_stdin(tty_in) is False
    assert terminal_mod.supports_ansi_color(tty_out) is True
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE")

    # TERM=dumb disables fullscreen UI, live status, and ANSI color
    monkeypatch.setenv("TERM", "dumb")
    assert terminal_mod.supports_fullscreen_ui(tty_in, tty_out) is False
    assert terminal_mod.supports_live_status(tty_err) is False
    assert terminal_mod.supports_ansi_color(tty_out) is False

    # Empty TERM disables fullscreen UI
    monkeypatch.setenv("TERM", "")
    assert terminal_mod.supports_fullscreen_ui(tty_in, tty_out) is False
    monkeypatch.setenv("TERM", "xterm-256color")

    # JSON output mode via env or argv disables fullscreen UI and live status
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    assert terminal_mod.is_json_output_mode() is True
    assert terminal_mod.supports_fullscreen_ui(tty_in, tty_out) is False
    assert terminal_mod.supports_live_status(tty_err) is False
    monkeypatch.delenv("IMEDNET_CLI_OUTPUT_FORMAT")

    monkeypatch.setattr(sys, "argv", ["imednet", "jobs", "status", "--format", "json"])
    assert terminal_mod.is_json_output_mode() is True
    monkeypatch.setattr(sys, "argv", ["imednet", "jobs", "status", "--format=json"])
    assert terminal_mod.is_json_output_mode() is True


def test_lazy_curses_grid_import_and_missing_curses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify imednet.cli.utils keeps grid/curses lazy and handles curses=None gracefully."""
    saved_grid = sys.modules.pop("imednet.cli.utils.grid", None)
    try:
        import imednet.cli.utils as cli_utils

        cli_utils.__dict__.pop("grid", None)
        importlib.reload(cli_utils)
        assert "imednet.cli.utils.grid" not in sys.modules
        with pytest.raises(AttributeError):
            _ = cli_utils.non_existent_symbol  # noqa: B018
        assert callable(cli_utils.is_interactive_terminal)
        assert "imednet.cli.utils.grid" in sys.modules
    finally:
        if saved_grid is not None:
            sys.modules["imednet.cli.utils.grid"] = saved_grid

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr(sys, "stdout", _FakeTTY())
    monkeypatch.setattr(grid_mod, "curses", None)
    assert grid_mod.is_interactive_terminal() is False


def test_non_interactive_env_restored_after_in_memory_cli_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Verify --no-interactive restores IMEDNET_NON_INTERACTIVE even after errors."""
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)

    mock_sdk = MagicMock()
    mock_sdk.studies.list.return_value = []
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    cli.app(["--no-interactive", "studies", "list"])
    assert "IMEDNET_NON_INTERACTIVE" not in os.environ

    monkeypatch.setenv("IMEDNET_NON_INTERACTIVE", "0")
    mock_sdk.studies.list.side_effect = RuntimeError("boom")
    with pytest.raises(SystemExit) as exc:
        cli.app(["studies", "list", "--no-interactive"])
    assert exc.value.code == 1
    assert os.environ.get("IMEDNET_NON_INTERACTIVE") == "0"

    # Also verify state_app restores IMEDNET_NON_INTERACTIVE
    ledger_file = tmp_path / "ledger.json"
    state_app(["--no-interactive", "show", "--ledger-path", str(ledger_file)])
    assert os.environ.get("IMEDNET_NON_INTERACTIVE") == "0"


def test_pty_single_item_job_status_and_wait_print_static_table_and_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify jobs status and jobs wait never launch curses on a PTY and exit 0 immediately."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")

    job = JobStatus(batch_id="BATCH-42", state="COMPLETED", progress=100)
    mock_sdk = MagicMock()
    mock_sdk.get_job.return_value = job
    mock_sdk.poll_job.return_value = job
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    grid_spy = MagicMock(return_value=True)
    monkeypatch.setattr(grid_mod, "run_interactive_grid", grid_spy)

    code_status, out_status, _ = _run_on_split_ptys(
        lambda: cli.app(["jobs", "status", "STUDY-1", "BATCH-42"])
    )
    assert code_status == 0
    assert "BATCH-42" in out_status
    assert "COMPLETED" in out_status
    grid_spy.assert_not_called()

    code_wait, out_wait, _ = _run_on_split_ptys(
        lambda: cli.app(["jobs", "wait", "STUDY-1", "BATCH-42"])
    )
    assert code_wait == 0
    assert "BATCH-42" in out_wait
    assert "COMPLETED" in out_wait
    grid_spy.assert_not_called()


def test_pty_list_commands_preserve_grid_by_default_and_honor_no_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify multi-item list commands use curses on PTY unless --no-interactive is active."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")

    study = Study(study_key="S1", study_name="Trial One", study_description="Desc")
    mock_sdk = MagicMock()
    mock_sdk.studies.list.return_value = [study]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    grid_spy = MagicMock(return_value=True)
    monkeypatch.setattr(grid_mod, "run_interactive_grid", grid_spy)

    # Default on interactive PTY launches curses grid
    code_default, _, _ = _run_on_split_ptys(lambda: cli.app(["studies", "list"]))
    assert code_default == 0
    assert grid_spy.call_count == 1

    # Root --no-interactive bypasses curses grid and emits static table
    grid_spy.reset_mock()
    code_root_opt, out_root_opt, _ = _run_on_split_ptys(
        lambda: cli.app(["--no-interactive", "studies", "list"])
    )
    assert code_root_opt == 0
    assert "Trial One" in out_root_opt
    grid_spy.assert_not_called()

    # Subcommand --no-interactive bypasses curses grid and emits static table
    code_sub_opt, out_sub_opt, _ = _run_on_split_ptys(
        lambda: cli.app(["studies", "list", "--no-interactive"])
    )
    assert code_sub_opt == 0
    assert "Trial One" in out_sub_opt
    grid_spy.assert_not_called()


def test_combined_pty_json_output_is_pure_parseable_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify --format json on a combined PTY emits zero VT100/spinner bytes and parses cleanly."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")

    job = JobStatus(batch_id="BATCH-JSON", state="COMPLETED", progress=100)
    mock_sdk = MagicMock()
    mock_sdk.get_job.return_value = job

    def _poll_job(study_key: str, batch_id: str, **kwargs: Any) -> JobStatus:
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress(MagicMock(batch_id=batch_id, status=job, elapsed=0.2))
        return job

    mock_sdk.poll_job.side_effect = _poll_job
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    code_status, raw_status = _run_on_combined_pty(
        lambda: cli.app(["jobs", "status", "STUDY-1", "BATCH-JSON", "--format", "json"])
    )
    assert code_status == 0
    assert "\x1b" not in raw_status
    parsed_status = json.loads(raw_status)
    assert parsed_status["schemaVersion"] == "1.0"
    assert parsed_status["resource"] == "jobs"
    assert parsed_status["count"] == 1
    assert parsed_status["items"][0]["batchId"] == "BATCH-JSON"

    code_wait, raw_wait = _run_on_combined_pty(
        lambda: cli.app(["jobs", "wait", "STUDY-1", "BATCH-JSON", "--format", "json"])
    )
    assert code_wait == 0
    assert "\x1b" not in raw_wait
    parsed_wait = json.loads(raw_wait)
    assert parsed_wait["schemaVersion"] == "1.0"
    assert parsed_wait["items"][0]["batchId"] == "BATCH-JSON"


@pytest.mark.parametrize(
    ("env_key", "env_val"),
    [
        ("IMEDNET_NON_INTERACTIVE", "1"),
        ("TERM", "dumb"),
    ],
)
def test_pty_suppresses_vt100_codes_in_non_interactive_or_dumb_term(
    monkeypatch: pytest.MonkeyPatch, env_key: str, env_val: str
) -> None:
    """Verify Status, clear_line, and job_wait emit zero VT100 control sequences when suppressed."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    monkeypatch.setenv(env_key, env_val)

    def _exercise_live_helpers() -> None:
        with output_mod.console.status("Working...") as st:
            st.update("Still working...")
        output_mod.clear_line()

    code, _, err_out = _run_on_split_ptys(_exercise_live_helpers)
    assert code == 0
    assert "\x1b[?25l" not in err_out
    assert "\x1b[?25h" not in err_out
    assert "\x1b[K" not in err_out
    assert "Working..." in err_out


def test_pty_ansi_color_and_high_contrast_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify NO_COLOR and TERM=dumb strip ANSI colors while preserving --high-contrast on TTY."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")

    job = JobStatus(batch_id="B-COLOR", state="COMPLETED", progress=100)
    mock_sdk = MagicMock()
    mock_sdk.get_job.return_value = job
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    # Standard TTY color: green (\x1b[32m)
    _, out_std, _ = _run_on_split_ptys(lambda: cli.app(["jobs", "status", "S1", "B-COLOR"]))
    assert "\x1b[32mCOMPLETED\x1b[0m" in out_std

    # High-contrast TTY color (even with --no-interactive): blue (\x1b[34m)
    _, out_hc, _ = _run_on_split_ptys(
        lambda: cli.app(["--high-contrast", "--no-interactive", "jobs", "status", "S1", "B-COLOR"])
    )
    assert "\x1b[34mCOMPLETED\x1b[0m" in out_hc
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)

    # NO_COLOR=1 strips all SGR colors on both stdout tables and stderr print_error
    monkeypatch.setenv("NO_COLOR", "1")
    _, out_nc, err_nc = _run_on_split_ptys(
        lambda: (
            cli.app(["jobs", "status", "S1", "B-COLOR"]),
            output_mod.print_error("\033[31mColored Error\033[0m"),
        )  # type: ignore[func-returns-value]
    )
    assert "\x1b[" not in out_nc
    assert re.search(r"\x1b\[[0-9;]*m", err_nc) is None
    assert "Colored Error" in err_nc

    # Live status frame with NO_COLOR=1 strips SGR sequences from status message
    buf_tty = _FakeTTY()
    con = output_mod.Console(stream=buf_tty, width=80)
    st = output_mod.Status("\033[31mAlert\033[0m", console=con)
    st._start_time = 0.0
    st._render_frame(0)
    assert "\x1b[31m" not in buf_tty.getvalue()
    assert "Alert" in buf_tty.getvalue()

    # TERM=dumb strips all ANSI colors on PTY
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    _, out_dumb, err_dumb = _run_on_split_ptys(
        lambda: (
            cli.app(["jobs", "status", "S1", "B-COLOR"]),
            output_mod.print_error("\033[31mDumb Error\033[0m"),
        )  # type: ignore[func-returns-value]
    )
    assert "\x1b[" not in out_dumb
    assert "\x1b[" not in err_dumb


def test_open_pipe_stdin_exits_immediately_without_hanging_on_pty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify an open pipe on stdin (no EOF written) exits immediately with code 1."""
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    r_fd, w_fd = os.pipe()
    pipe_in = os.fdopen(r_fd, "r", encoding="utf-8", closefd=True)
    err_tty = _FakeTTY()
    try:
        monkeypatch.setattr(sys, "stdin", pipe_in)
        monkeypatch.setattr(sys, "stderr", err_tty)
        with pytest.raises(SystemExit) as exc1:
            prompt_input("Study key")
        assert exc1.value.code == 1

        with pytest.raises(SystemExit) as exc2:
            prompt_confirm("Confirm?")
        assert exc2.value.code == 1
        assert "Interactive prompts require an interactive TTY on stdin" in err_tty.getvalue()
    finally:
        pipe_in.close()
        os.close(w_fd)
