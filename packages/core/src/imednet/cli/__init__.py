"""iMednet Command Line Interface."""

# pylint: disable=duplicate-code

from __future__ import annotations

import argparse
import os
import sys
from importlib import import_module
from typing import TYPE_CHECKING, Any

from .decorators import _redact_secrets, cli_execution_guard, with_sdk
from .utils import (
    NON_INTERACTIVE_ENV,
    OUTPUT_FORMAT_ENV,
    SIMULATION_MODE_ENV,
    add_no_interactive,
    add_simulation_arg,
    get_sdk,
    parse_filter_args,
    parse_record_data_filter,
)

if TYPE_CHECKING:
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

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "SinkConfig": ("imednet.integrations.sink_base", "SinkConfig"),
    "export_to_csv": ("imednet.integrations.export", "export_to_csv"),
    "export_to_duckdb": ("imednet.integrations.export", "export_to_duckdb"),
    "export_to_excel": ("imednet.integrations.export", "export_to_excel"),
    "export_to_json": ("imednet.integrations.export", "export_to_json"),
    "export_to_long_sql": ("imednet.integrations.export", "export_to_long_sql"),
    "export_to_parquet": ("imednet.integrations.export", "export_to_parquet"),
    "export_to_sql": ("imednet.integrations.export", "export_to_sql"),
    "export_to_sql_by_form": ("imednet.integrations.export", "export_to_sql_by_form"),
}

for _lazy_name in _LAZY_ATTRS:
    globals().pop(_lazy_name, None)

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
    "parse_record_data_filter",
    "with_sdk",
]

_CORE_COMMAND_SPECS: tuple[tuple[str, str, str], ...] = (
    ("datadict", ".datadict", "Manage, transform, and validate study data dictionaries."),
    ("studies", ".studies", "Manage studies."),
    ("queries", ".queries", "Manage queries."),
    ("variables", ".variables", "Manage variables."),
    ("record-revisions", ".record_revisions", "Manage record revisions."),
    ("sites", ".sites", "Manage sites."),
    ("export", ".export", "Export study data to various formats."),
    ("subjects", ".subjects", "Manage subjects within a study."),
    ("intervals", ".intervals", "Manage intervals."),
    ("jobs", ".jobs", "Manage background jobs."),
    ("records", ".records", "Manage records within a study."),
    ("forms", ".forms", "Manage forms."),
    ("visits", ".visits", "Manage visits."),
    ("codings", ".codings", "Manage medical codings within a study."),
    ("users", ".users", "Manage users within a study."),
)

_KNOWN_PLUGIN_SPECS: dict[str, tuple[str, str]] = {
    "imednet_tui.cli:setup_parser": ("tui", "Launch interactive terminal interface."),
    "imednet_workflows.cli:setup_subject_parser": (
        "subject-data",
        "Retrieve all data for a single subject.",
    ),
    "imednet_workflows.cli:setup_parser": ("workflows", "Execute common data workflows."),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve heavy export and sink helpers on attribute access (PEP 562)."""
        if name in _LAZY_ATTRS:
            mod_name, attr_name = _LAZY_ATTRS[name]
            mod = import_module(mod_name)  # nosem
            unpatched = getattr(mod, "_UNPATCHED_EXPORTS", None)
            val = (
                unpatched[attr_name]
                if isinstance(unpatched, dict) and attr_name in unpatched
                else getattr(mod, attr_name)
            )
            globals()[name] = val
            return val
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return exported module attributes including lazy CLI helpers."""
    return sorted(set(globals()) | set(__all__))


def _create_root_parser() -> argparse.ArgumentParser:
    """Create the root CLI ArgumentParser with top-level flags."""
    parser = argparse.ArgumentParser(
        description="iMednet SDK Command Line Interface",
        epilog="Install optional CLI features: pip install 'imednet[cli]'",
    )
    parser.add_argument(
        "--high-contrast", action="store_true", help="Enable high-contrast mode for accessibility."
    )
    add_no_interactive(parser, suppress_default=False)
    add_simulation_arg(parser, suppress_default=False)
    return parser


def _extract_root_command(parser: argparse.ArgumentParser, args: list[str]) -> str | None:
    """Extract the first top-level command token from *args*, skipping root flags."""
    option_map: dict[str, argparse.Action] = {}
    for action in parser._actions:
        for opt_str in action.option_strings:
            option_map[opt_str] = action

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if arg.startswith("-") and arg != "-":
            if "=" in arg:
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
            continue
        return arg
    return None


def _populate_subparsers(
    parser: argparse.ArgumentParser,
    *,
    active_command: str | None = None,
    load_all: bool = False,
) -> argparse.ArgumentParser:
    """Register core and plugin subparsers, loading only *active_command* unless *load_all*."""
    import importlib.metadata

    subparsers = parser.add_subparsers(dest="command")

    for cmd_name, mod_name, cmd_help in _CORE_COMMAND_SPECS:
        if load_all or active_command == cmd_name:
            mod = import_module(mod_name, __name__)  # nosem
            mod.setup_parser(subparsers)
        else:
            subparsers.add_parser(cmd_name, help=cmd_help)

    registered_plugins: set[str] = set()
    for entry_point in importlib.metadata.entry_points(group="imednet.cli_plugins"):
        ep_value = getattr(entry_point, "value", None)
        known_spec = _KNOWN_PLUGIN_SPECS.get(ep_value) if isinstance(ep_value, str) else None
        if not load_all and known_spec is not None and active_command != known_spec[0]:
            subparsers.add_parser(known_spec[0], help=known_spec[1])
            registered_plugins.add(entry_point.name)
            continue
        try:
            plugin_setup = entry_point.load()
            plugin_setup(subparsers)
            registered_plugins.add(entry_point.name)
        except Exception as e:
            print(
                f"Warning: Failed to load CLI plugin '{entry_point.name}': {_redact_secrets(str(e))}",
                file=sys.stderr,
            )

    if "workflows" not in registered_plugins:
        workflows_parser = subparsers.add_parser(
            "workflows", help="Workflows plugin (not installed)"
        )
        workflows_parser.add_argument("args", nargs=argparse.REMAINDER)

        def workflows_stub(args: argparse.Namespace) -> None:
            print("The workflows plugin is not installed.", file=sys.stderr)
            sys.exit(1)

        workflows_parser.set_defaults(func=workflows_stub)

    if "tui" not in registered_plugins:
        tui_parser = subparsers.add_parser(
            "tui", help="Interactive terminal interface (not installed)"
        )
        tui_parser.add_argument("args", nargs=argparse.REMAINDER)

        def tui_stub(args: argparse.Namespace) -> None:
            print(
                "The terminal interface plugin is not installed. To install it, run: pip install 'imednet[tui]'",
                file=sys.stderr,
            )
            sys.exit(1)

        tui_parser.set_defaults(func=tui_stub)

    return parser


def get_parser() -> argparse.ArgumentParser:
    """Return the main argparse.ArgumentParser for the CLI."""
    parser = _create_root_parser()
    return _populate_subparsers(parser, load_all=True)


def _build_scoped_parser(args: list[str]) -> argparse.ArgumentParser:
    """Build a CLI parser loading only the subcommand targeted by *args*."""
    parser = _create_root_parser()
    active_command = _extract_root_command(parser, args)
    return _populate_subparsers(parser, active_command=active_command, load_all=False)


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

    parser = _build_scoped_parser(args)

    parsed = parser.parse_args(args)

    old_high_contrast = os.environ.get("IMEDNET_HIGH_CONTRAST")
    old_output_format = os.environ.get(OUTPUT_FORMAT_ENV)
    old_non_interactive = os.environ.get(NON_INTERACTIVE_ENV)
    old_simulation_mode = os.environ.get(SIMULATION_MODE_ENV)
    try:
        if parsed.high_contrast and getattr(parsed, "command", None) != "tui":
            os.environ["IMEDNET_HIGH_CONTRAST"] = "1"

        if getattr(parsed, "no_interactive", False):
            os.environ[NON_INTERACTIVE_ENV] = "1"

        if getattr(parsed, "simulation", False):
            os.environ[SIMULATION_MODE_ENV] = "1"

        explicit_format = getattr(parsed, "output_format", None)
        if explicit_format is None:
            explicit_format = getattr(parsed, "format", None)

        if explicit_format == "json":
            os.environ[OUTPUT_FORMAT_ENV] = "json"
        elif explicit_format in {"table", "text", "md"}:
            os.environ.pop(OUTPUT_FORMAT_ENV, None)
        elif old_output_format is not None and old_output_format.strip().lower() == "json":
            os.environ[OUTPUT_FORMAT_ENV] = "json"

        with cli_execution_guard():
            if hasattr(parsed, "func"):
                parsed.func(parsed)
            else:
                active_parser = _find_active_subparser(parser, args)
                active_parser.print_help()
    finally:
        if old_high_contrast is not None:
            os.environ["IMEDNET_HIGH_CONTRAST"] = old_high_contrast
        else:
            os.environ.pop("IMEDNET_HIGH_CONTRAST", None)
        if old_output_format is not None:
            os.environ[OUTPUT_FORMAT_ENV] = old_output_format
        else:
            os.environ.pop(OUTPUT_FORMAT_ENV, None)
        if old_non_interactive is not None:
            os.environ[NON_INTERACTIVE_ENV] = old_non_interactive
        else:
            os.environ.pop(NON_INTERACTIVE_ENV, None)
        if old_simulation_mode is not None:
            os.environ[SIMULATION_MODE_ENV] = old_simulation_mode
        else:
            os.environ.pop(SIMULATION_MODE_ENV, None)


if __name__ == "__main__":
    app()
