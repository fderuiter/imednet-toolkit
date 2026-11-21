"""Unit tests for TUI CLI plugin discovery and fallback behavior."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from imednet.cli import get_parser


def test_tui_subcommand_in_help(capsys):
    """Verify that 'tui' appears in the main CLI help menu."""
    parser = get_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])
    assert exc_info.value.code == 0
    out, _ = capsys.readouterr()
    assert "tui" in out


def test_tui_fallback_when_plugin_missing(monkeypatch, capsys):
    """Verify missing plugin guidance message and exit code 1."""
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])

    parser = get_parser()
    args = parser.parse_args(["tui"])

    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    _, err = capsys.readouterr()
    assert "The terminal interface plugin is not installed" in err
    assert "imednet[tui]" in err


def test_tui_plugin_registered_and_run(monkeypatch):
    """Verify that when plugin is installed, tui subcommand executes run_tui."""
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )

    parser = get_parser()
    args = parser.parse_args(["tui"])

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk,
    ):
        mock_sdk = MagicMock()
        mock_get_sdk.return_value = mock_sdk

        args.func(args)
        mock_run_tui.assert_called_once_with(mock_sdk, force=False)


def test_tui_plugin_high_contrast_flag(monkeypatch):
    """Verify that --high-contrast passes high_contrast=True without mutating os.environ."""
    from imednet import cli as cli_mod
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    env_snapshot = dict(os.environ)

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk,
    ):
        mock_sdk = MagicMock()
        mock_get_sdk.return_value = mock_sdk

        cli_mod.app(["tui", "--high-contrast"])
        mock_run_tui.assert_called_once_with(mock_sdk, force=False, high_contrast=True)
        assert "IMEDNET_HIGH_CONTRAST" not in os.environ
        assert dict(os.environ) == env_snapshot


def test_tui_plugin_non_interactive_exits(monkeypatch, capsys):
    """Verify that tui subcommand exits with code 1 and prints error in non-interactive terminal."""
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )

    parser = get_parser()
    args = parser.parse_args(["tui"])

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=False),
        patch(
            "imednet.cli.utils.context.get_sdk",
            side_effect=RuntimeError("SDK credentials missing in headless environment"),
        ) as mock_get_sdk,
    ):
        with pytest.raises(SystemExit) as exc_info:
            args.func(args)

    assert exc_info.value.code == 1
    _, err = capsys.readouterr()
    assert "Interactive terminal required" in err
    mock_get_sdk.assert_not_called()


def test_tui_plugin_force_flag_bypasses_guard(monkeypatch):
    """Verify that --force flag runs TUI even when non-interactive."""
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )

    parser = get_parser()
    args = parser.parse_args(["tui", "--force"])

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=False),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk,
    ):
        mock_sdk = MagicMock()
        mock_get_sdk.return_value = mock_sdk

        args.func(args)
        mock_run_tui.assert_called_once_with(mock_sdk, force=True)


def test_tui_plugin_study_and_repeatable_batch_options(monkeypatch):
    """Verify --study and repeatable --batch options forward study_key and batch_ids to run_tui."""
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )

    parser = get_parser()
    args = parser.parse_args(
        [
            "tui",
            "--study",
            "STUDY-ONC-01",
            "--batch",
            "BATCH-101",
            "--batch",
            "BATCH-102,BATCH-103",
            "-b",
            "BATCH-101",
        ]
    )

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk,
    ):
        mock_sdk = MagicMock()
        mock_get_sdk.return_value = mock_sdk

        args.func(args)
        mock_run_tui.assert_called_once_with(
            mock_sdk,
            force=False,
            study_key="STUDY-ONC-01",
            batch_ids=["BATCH-101", "BATCH-102", "BATCH-103"],
        )


@pytest.mark.parametrize(
    ("cli_argv", "extra_env"),
    [
        (["tui", "--no-interactive"], {}),
        (["--no-interactive", "tui"], {}),
        (["tui", "--no-interactive", "--force"], {}),
        (["--no-interactive", "tui", "--force"], {}),
        (["tui", "--force"], {"IMEDNET_NON_INTERACTIVE": "1"}),
        (["tui", "--force"], {"IMEDNET_NON_INTERACTIVE": "true"}),
        (["tui", "--force"], {"IMEDNET_NON_INTERACTIVE": "YES"}),
    ],
)
def test_tui_no_interactive_exits_with_code_one_even_when_forced(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_argv: list[str],
    extra_env: dict[str, str],
) -> None:
    """Verify --no-interactive and IMEDNET_NON_INTERACTIVE exit with code 1 even with --force."""
    from imednet import cli as cli_mod
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    for key, val in extra_env.items():
        monkeypatch.setenv(key, val)

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk,
        pytest.raises(SystemExit) as exc_info,
    ):
        cli_mod.app(cli_argv)

    assert exc_info.value.code == 1
    _, err = capsys.readouterr()
    assert "non-interactive" in err.lower()
    mock_run_tui.assert_not_called()
    mock_get_sdk.assert_not_called()


def test_tui_root_high_contrast_and_finally_env_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify root --high-contrast propagates to tui and IMEDNET_HIGH_CONTRAST is restored in finally."""
    from imednet import cli as cli_mod
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)

    def _mutating_run_tui(*_args, **_kwargs):
        os.environ["IMEDNET_HIGH_CONTRAST"] = "1"
        raise RuntimeError("boom inside tui")

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui", side_effect=_mutating_run_tui) as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk", return_value=MagicMock()),
        pytest.raises(SystemExit) as exc_info,
    ):
        cli_mod.app(["--high-contrast", "tui"])

    assert exc_info.value.code == 1
    assert mock_run_tui.call_args.kwargs.get("high_contrast") is True
    assert "IMEDNET_HIGH_CONTRAST" not in os.environ

    # Pre-existing IMEDNET_HIGH_CONTRAST value is restored on exit
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "0")
    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui", side_effect=_mutating_run_tui),
        patch("imednet.cli.utils.context.get_sdk", return_value=MagicMock()),
        pytest.raises(SystemExit),
    ):
        cli_mod.app(["--high-contrast", "tui"])
    assert os.environ.get("IMEDNET_HIGH_CONTRAST") == "0"


@pytest.mark.parametrize(
    "cli_argv",
    [
        ["tui", "--study", "../admin"],
        ["tui", "--study", "STUDY/../../etc"],
        ["tui", "--study", "STUDY-1", "--batch", "../secret"],
        ["tui", "--study", "STUDY-1", "--batch", "BATCH-1,BATCH?token=abc"],
    ],
)
def test_tui_rejects_invalid_path_segments_before_sdk_init(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_argv: list[str],
) -> None:
    """Verify imednet tui validates --study and --batch path segments via SPI before SDK init."""
    from imednet import cli as cli_mod
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk,
        pytest.raises(SystemExit) as exc_info,
    ):
        cli_mod.app(cli_argv)

    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "Error:" in err
    mock_get_sdk.assert_not_called()
    mock_run_tui.assert_not_called()


def test_tui_positional_study_and_flag_conflict_exits_two(monkeypatch, capsys):
    """Verify tui accepts positional STUDY and exits 2 on conflicting --study-key."""
    from imednet import cli as cli_mod
    from imednet_tui.cli import setup_parser

    class DummyEntryPoint:
        name = "tui"

        def load(self):
            return setup_parser

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda **kwargs: (
            [DummyEntryPoint()] if kwargs.get("group") == "imednet.cli_plugins" else []
        ),
    )

    with (
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
    ):
        cli_mod.app(["tui", "STUDY", "--simulation"])
        assert mock_run_tui.call_count == 1
        assert mock_run_tui.call_args.kwargs["study_key"] == "STUDY"
        assert "IMEDNET_SIMULATION_MODE" not in os.environ

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.app(["tui", "STUDY-A", "--study-key", "STUDY-B"])
    assert exc_info.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "conflicting values provided for study_key" in err
