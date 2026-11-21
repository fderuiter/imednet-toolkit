"""CLI entry point for the iMednet TUI plugin."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Sequence
from typing import Any

from imednet.spi.cli import (
    add_no_interactive,
    add_simulation_arg,
    add_study_key_args,
    is_non_interactive,
    print_error,
    resolve_study_key,
    simulation_mode_context,
    validate_cli_path_segment,
    with_sdk,
)
from imednet.spi.facade import ImednetFacade

from .terminal import is_interactive_terminal

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
    add_study_key_args(tui_parser, flag_names=("-s", "--study", "--study-key"))
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
        default=argparse.SUPPRESS,
        help="Enable high-contrast mode for accessibility.",
    )
    tui_parser.add_argument(
        "--force",
        action="store_true",
        help="Force launch even if terminal streams appear non-interactive.",
    )
    add_no_interactive(tui_parser)
    add_simulation_arg(tui_parser)

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
        old_high_contrast = os.environ.get("IMEDNET_HIGH_CONTRAST")
        try:
            resolved_study: str | None = None
            if args is not None:
                resolved_study = resolve_study_key(tui_parser, args, required=False)
                if resolved_study is None:
                    legacy_study = getattr(args, "study", None)
                    if legacy_study and str(legacy_study).strip():
                        resolved_study = str(legacy_study).strip()
                        args.study_key = resolved_study

            no_interactive = bool(args and getattr(args, "no_interactive", False))
            if no_interactive or is_non_interactive():
                print_error(
                    "Error: Interactive terminal required; cannot run the TUI in non-interactive mode."
                )
                raise SystemExit(1)

            force = bool(args and getattr(args, "force", False))
            if not force and not is_interactive_terminal():
                print_error("Error: Interactive terminal required to run the TUI interface.")
                raise SystemExit(1)

            if args is not None:
                if resolved_study is not None:
                    validate_cli_path_segment(resolved_study, name="study_key")
                raw_batches = getattr(args, "batch_ids", None) or getattr(args, "batch", None)
                for batch_id in _parse_batch_args(raw_batches):
                    validate_cli_path_segment(batch_id, name="batch_id")

            with simulation_mode_context(bool(args and getattr(args, "simulation", False))):
                _launch(args)
        finally:
            if old_high_contrast is not None:
                os.environ["IMEDNET_HIGH_CONTRAST"] = old_high_contrast
            else:
                os.environ.pop("IMEDNET_HIGH_CONTRAST", None)

    tui_parser.set_defaults(func=run_tui_command)
