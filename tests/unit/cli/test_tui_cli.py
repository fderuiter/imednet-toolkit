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
        patch("imednet_tui.app.run_tui") as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk,
    ):
        mock_sdk = MagicMock()
        mock_get_sdk.return_value = mock_sdk

        args.func(args)
        mock_run_tui.assert_called_once_with(mock_sdk)


def test_tui_plugin_high_contrast_flag(monkeypatch):
    """Verify that --high-contrast flag sets IMEDNET_HIGH_CONTRAST=1."""
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

    parser = get_parser()
    args = parser.parse_args(["tui", "--high-contrast"])

    with (
        patch("imednet_tui.app.run_tui") as mock_run_tui,
        patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk,
    ):
        mock_sdk = MagicMock()
        mock_get_sdk.return_value = mock_sdk

        args.func(args)
        mock_run_tui.assert_called_once()
        assert os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"
