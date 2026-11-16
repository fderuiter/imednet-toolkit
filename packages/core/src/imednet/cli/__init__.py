"""iMednet Command Line Interface."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from ..integrations import SinkConfig
from ..integrations.export import (
    export_to_csv,
    export_to_duckdb,
    export_to_excel,
    export_to_json,
    export_to_long_sql,
    export_to_parquet,
    export_to_sql,
    export_to_sql_by_form,
)
from .decorators import with_sdk
from .utils import get_sdk, parse_filter_args

__all__ = [
    "SinkConfig",
    "app",
    "export_to_csv",
    "export_to_duckdb",
    "export_to_excel",
    "export_to_json",
    "export_to_long_sql",
    "export_to_parquet",
    "export_to_sql",
    "export_to_sql_by_form",
    "get_parser",
    "get_sdk",
    "parse_filter_args",
    "with_sdk",
]


def get_parser() -> argparse.ArgumentParser:
    """Return the main argparse.ArgumentParser for the CLI."""
    parser = argparse.ArgumentParser(description="iMednet SDK Command Line Interface")
    parser.add_argument(
        "--high-contrast", action="store_true", help="Enable high-contrast mode for accessibility."
    )

    subparsers = parser.add_subparsers(dest="command")

    from .datadict import setup_parser as datadict_setup
    from .export import setup_parser as export_setup
    from .intervals import setup_parser as intervals_setup
    from .jobs import setup_parser as jobs_setup
    from .queries import setup_parser as queries_setup
    from .record_revisions import setup_parser as record_revisions_setup
    from .records import setup_parser as records_setup
    from .sites import setup_parser as sites_setup
    from .studies import setup_parser as studies_setup
    from .subjects import setup_parser as subjects_setup
    from .variables import setup_parser as variables_setup

    datadict_setup(subparsers)
    studies_setup(subparsers)
    queries_setup(subparsers)
    variables_setup(subparsers)
    record_revisions_setup(subparsers)
    sites_setup(subparsers)
    export_setup(subparsers)
    subjects_setup(subparsers)
    intervals_setup(subparsers)
    jobs_setup(subparsers)
    records_setup(subparsers)

    import importlib.metadata

    registered_plugins = set()
    # Discover and register CLI plugins using entry points
    for entry_point in importlib.metadata.entry_points(group="imednet.cli_plugins"):
        try:
            plugin_setup = entry_point.load()
            plugin_setup(subparsers)
            registered_plugins.add(entry_point.name)
        except Exception as e:
            print(f"Warning: Failed to load CLI plugin '{entry_point.name}': {e}", file=sys.stderr)

    if "workflows" not in registered_plugins:
        workflows_parser = subparsers.add_parser(
            "workflows", help="Workflows plugin (not installed)"
        )
        workflows_parser.add_argument("args", nargs=argparse.REMAINDER)

        def workflows_stub(args: argparse.Namespace) -> None:
            print("The workflows plugin is not installed.", file=sys.stderr)
            sys.exit(1)

        workflows_parser.set_defaults(func=workflows_stub)

    return parser


def _find_active_subparser(
    parser: argparse.ArgumentParser, args: list[str]
) -> argparse.ArgumentParser:
    """Locate the active subparser context based on the CLI argument list."""
    current_parser = parser
    remaining_args = list(args)

    while True:
        subparser_action: argparse._SubParsersAction[Any] | None = None
        for action in current_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparser_action = action
                break

        if subparser_action is None:
            break

        option_map: dict[str, argparse.Action] = {}
        for action in current_parser._actions:
            for opt_str in action.option_strings:
                option_map[opt_str] = action

        matched_subparser: argparse.ArgumentParser | None = None
        consumed_idx = len(remaining_args)

        i = 0
        while i < len(remaining_args):
            arg = remaining_args[i]

            if arg in subparser_action.choices:
                matched_subparser = subparser_action.choices[arg]
                consumed_idx = i + 1
                break

            if arg.startswith("-"):
                if "=" in arg:
                    opt, _, _ = arg.partition("=")
                    if opt in option_map:
                        i += 1
                        continue
                if arg in option_map:
                    opt_action = option_map[arg]
                    if opt_action.nargs == 0 or isinstance(
                        opt_action,
                        (
                            argparse._StoreTrueAction,
                            argparse._StoreFalseAction,
                            argparse._HelpAction,
                        ),
                    ):
                        i += 1
                    else:
                        n = (
                            1
                            if opt_action.nargs is None
                            else (opt_action.nargs if isinstance(opt_action.nargs, int) else 1)
                        )
                        i += 1 + n
                    continue

            i += 1

        if matched_subparser is None:
            break

        current_parser = matched_subparser
        remaining_args = remaining_args[consumed_idx:]

    return current_parser


def app(args: list[str] | None = None) -> None:
    """Main entrypoint for the CLI."""
    if args is None:
        args = sys.argv[1:]

    parser = get_parser()

    parsed = parser.parse_args(args)

    if parsed.high_contrast:
        os.environ["IMEDNET_HIGH_CONTRAST"] = "1"

    if hasattr(parsed, "func"):
        parsed.func(parsed)
    else:
        active_parser = _find_active_subparser(parser, args)
        active_parser.print_help()


if __name__ == "__main__":
    app()
