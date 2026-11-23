"""Service Provider Interface (SPI) for CLI extensions."""

from typing import TYPE_CHECKING, Any

from imednet.cli.decorators import (
    cli_execution_guard,
    redact_cli_secrets,
    validate_cli_path_segment,
    with_sdk,
)
from imednet.cli.utils import (
    NO_COLOR_ENV,
    NON_INTERACTIVE_ENV,
    OUTPUT_FORMAT_ENV,
    SIMULATION_MODE_ENV,
    STUDY_KEY_ARG,
    Console,
    Status,
    UniqueStoreAction,
    add_no_interactive,
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    clear_line,
    console,
    display_list,
    emit_json,
    fetching_status,
    format_job_progress,
    is_dumb_terminal,
    is_json_output_mode,
    is_non_interactive,
    is_tty,
    load_offline_schema,
    parse_email_notify,
    parse_filter_args,
    parse_record_data_filter,
    positive_int,
    print_error,
    resolve_cli_schema,
    resolve_endpoint_filter_attributes,
    resolve_hybrid_study_and_key,
    resolve_output_format,
    resolve_study_key,
    simulation_mode_context,
    supports_ansi_color,
    supports_fullscreen_ui,
    supports_interactive_stdin,
    supports_live_status,
)

if TYPE_CHECKING:
    from imednet.cli.utils import is_interactive_terminal

__all__ = [
    "NON_INTERACTIVE_ENV",
    "NO_COLOR_ENV",
    "OUTPUT_FORMAT_ENV",
    "SIMULATION_MODE_ENV",
    "STUDY_KEY_ARG",
    "Console",
    "Status",
    "UniqueStoreAction",
    "add_no_interactive",
    "add_output_format",
    "add_simulation_arg",
    "add_study_key_args",
    "clear_line",
    "cli_execution_guard",
    "console",
    "display_list",
    "emit_json",
    "fetching_status",
    "format_job_progress",
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
    "redact_cli_secrets",
    "resolve_cli_schema",
    "resolve_endpoint_filter_attributes",
    "resolve_hybrid_study_and_key",
    "resolve_output_format",
    "resolve_study_key",
    "simulation_mode_context",
    "supports_ansi_color",
    "supports_fullscreen_ui",
    "supports_interactive_stdin",
    "supports_live_status",
    "validate_cli_path_segment",
    "with_sdk",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve interactive terminal helpers from ``imednet.cli.utils``."""
    if name == "is_interactive_terminal":
        from imednet.cli.utils import is_interactive_terminal

        return is_interactive_terminal
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return exported module attributes including lazy SPI helpers."""
    return sorted(set(globals()) | set(__all__))
