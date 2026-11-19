"""CLI entry point for the iMednet TUI plugin."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from typing import Any

from imednet.spi.cli import STUDY_KEY_ARG, is_interactive_terminal, print_error, with_sdk
from imednet.spi.facade import ImednetFacade

_BATCH_SPLIT_RE = re.compile(r"[\s,]+")


def _parse_batch_args(raw_batches: Sequence[str] | str | None) -> list[str]:
    """Normalize repeatable or comma-separated CLI batch ID values."""
    if raw_batches is None:
        return []
    values = [raw_batches] if isinstance(raw_batches, str) else list(raw_batches)
    parsed: list[str] = []
    seen: set[str] = set()
    for entry in values:
        for token in _BATCH_SPLIT_RE.split(str(entry)):
            cleaned = token.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                parsed.append(cleaned)
    return parsed


def setup_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Register the 'tui' subparser."""
    tui_parser = subparsers.add_parser(
        "tui",
        help="Launch interactive terminal interface.",
        description="Interactive terminal interface for browsing clinical studies, sites, subjects, and jobs.",
    )
    tui_parser.add_argument(
        "-s",
        "--study",
        "--study-key",
        dest="study_key",
        default=None,
        help=STUDY_KEY_ARG,
    )
    tui_parser.add_argument(
        "-b",
        "--batch",
        "--batch-id",
        dest="batch_ids",
        action="append",
        default=None,
        help="Job batch ID to monitor in the Jobs tab (repeatable or comma-separated).",
    )
    tui_parser.add_argument(
        "--high-contrast",
        action="store_true",
        help="Enable high-contrast mode for accessibility.",
    )
    tui_parser.add_argument(
        "--force",
        action="store_true",
        help="Force launch even if terminal streams appear non-interactive.",
    )

    @with_sdk
    def _launch(sdk: ImednetFacade, args: argparse.Namespace | None = None) -> None:
        from .app import run_tui

        force = bool(args and getattr(args, "force", False))
        raw_study = None
        raw_batches = None
        high_contrast = False
        if args is not None:
            raw_study = getattr(args, "study_key", None) or getattr(args, "study", None)
            raw_batches = getattr(args, "batch_ids", None) or getattr(args, "batch", None)
            high_contrast = bool(getattr(args, "high_contrast", False))

        study_key = str(raw_study).strip() if raw_study and str(raw_study).strip() else None
        batch_ids = _parse_batch_args(raw_batches)

        run_kwargs: dict[str, Any] = {"force": force}
        if high_contrast:
            run_kwargs["high_contrast"] = True
        if study_key is not None:
            run_kwargs["study_key"] = study_key
        if batch_ids:
            run_kwargs["batch_ids"] = batch_ids
        run_tui(sdk, **run_kwargs)

    def run_tui_command(args: argparse.Namespace | None = None) -> None:
        force = bool(args and getattr(args, "force", False))
        if not force and not is_interactive_terminal():
            print_error("Error: Interactive terminal required to run the TUI interface.")
            raise SystemExit(1)

        _launch(args)

    tui_parser.set_defaults(func=run_tui_command)
