"""CLI utility functions and classes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .args import (
    STUDY_KEY_ARG,
    UniqueStoreAction,
    add_no_interactive,
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    parse_filter_args,
    parse_record_data_filter,
    positive_int,
    resolve_endpoint_filter_attributes,
    resolve_hybrid_study_and_key,
    resolve_output_format,
    resolve_study_key,
)
from .commands import get_model_value, register_list_command
from .context import bootstrap_cli_dotenv, fetching_status, get_sdk, simulation_mode_context
from .output import (
    _HC_STATUS_COLOR_MAP,
    _STATUS_COLOR_MAP,
    Console,
    Status,
    _format_cell_value,
    _format_header_label,
    _truncate,
    clear_line,
    console,
    display_list,
    emit_json,
    format_job_progress,
    print_error,
)
from .terminal import (
    NO_COLOR_ENV,
    NON_INTERACTIVE_ENV,
    OUTPUT_FORMAT_ENV,
    SIMULATION_MODE_ENV,
    is_dumb_terminal,
    is_json_output_mode,
    is_non_interactive,
    is_tty,
    supports_ansi_color,
    supports_fullscreen_ui,
    supports_interactive_stdin,
    supports_live_status,
)
from .write_helpers import load_offline_schema, parse_email_notify, resolve_cli_schema

if TYPE_CHECKING:
    from .export import sanitize_cli_phi, sanitize_cli_records
    from .grid import InteractiveDataGrid, is_interactive_terminal, run_interactive_grid

__all__ = [
    "NON_INTERACTIVE_ENV",
    "NO_COLOR_ENV",
    "OUTPUT_FORMAT_ENV",
    "SIMULATION_MODE_ENV",
    "STUDY_KEY_ARG",
    "_HC_STATUS_COLOR_MAP",
    "_STATUS_COLOR_MAP",
    "Console",
    "InteractiveDataGrid",
    "Status",
    "UniqueStoreAction",
    "_format_cell_value",
    "_format_header_label",
    "_truncate",
    "add_no_interactive",
    "add_output_format",
    "add_simulation_arg",
    "add_study_key_args",
    "bootstrap_cli_dotenv",
    "clear_line",
    "console",
    "display_list",
    "emit_json",
    "fetching_status",
    "format_job_progress",
    "get_model_value",
    "get_sdk",
    "is_dumb_terminal",
    "is_interactive_terminal",
    "is_json_output_mode",
    "is_non_interactive",
    "is_tty",
    "load_offline_schema",
    "parse_email_notify",
    "parse_filter_args",
    "parse_record_data_filter",
    "positive_int",
    "print_error",
    "register_list_command",
    "resolve_cli_schema",
    "resolve_endpoint_filter_attributes",
    "resolve_hybrid_study_and_key",
    "resolve_output_format",
    "resolve_study_key",
    "run_interactive_grid",
    "sanitize_cli_phi",
    "sanitize_cli_records",
    "simulation_mode_context",
    "supports_ansi_color",
    "supports_fullscreen_ui",
    "supports_interactive_stdin",
    "supports_live_status",
]


def __getattr__(name: str) -> Any:
    """Lazily load interactive grid symbols so standard CLI startup avoids importing curses."""
    if name == "grid":
        import importlib
        import sys

        mod = sys.modules.get(f"{__name__}.grid")
        if mod is None:
            mod = importlib.import_module(".grid", __name__)
        globals().pop("grid", None)
        return mod
    if name in ("InteractiveDataGrid", "is_interactive_terminal", "run_interactive_grid"):
        try:
            import importlib

            grid_mod = importlib.import_module(".grid", __name__)
            globals().pop("grid", None)
            return getattr(grid_mod, name)
        except (ImportError, AttributeError):
            if name == "is_interactive_terminal":
                return lambda: False
            if name == "run_interactive_grid":
                return lambda *args, **kwargs: False
            raise
    if name in ("sanitize_cli_phi", "sanitize_cli_records"):
        from . import export as _export_mod

        val = getattr(_export_mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
