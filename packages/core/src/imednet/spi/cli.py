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
    from imednet.cli.utils.args import collect_list_items_with_remainder, prepare_list_query
    from imednet.cli.utils.export import (
        export_list_to_file,
        mask_cli_models,
        mask_cli_records,
        resolve_cli_phi_fields,
        resolve_records_export_target,
    )

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
    "collect_list_items_with_remainder",
    "console",
    "display_list",
    "emit_json",
    "export_list_to_file",
    "fetching_status",
    "format_job_progress",
    "is_dumb_terminal",
    "is_interactive_terminal",
    "is_json_output_mode",
    "is_non_interactive",
    "is_tty",
    "load_offline_schema",
    "mask_cli_models",
    "mask_cli_records",
    "parse_email_notify",
    "parse_filter_args",
    "parse_record_data_filter",
    "positive_int",
    "prepare_list_query",
    "print_error",
    "redact_cli_secrets",
    "resolve_cli_phi_fields",
    "resolve_cli_schema",
    "resolve_endpoint_filter_attributes",
    "resolve_hybrid_study_and_key",
    "resolve_output_format",
    "resolve_records_export_target",
    "resolve_study_key",
    "simulation_mode_context",
    "supports_ansi_color",
    "supports_fullscreen_ui",
    "supports_interactive_stdin",
    "supports_live_status",
    "validate_cli_path_segment",
    "with_sdk",
]

_LAZY_EXPORT_NAMES = frozenset(
    {
        "export_list_to_file",
        "mask_cli_models",
        "mask_cli_records",
        "resolve_cli_phi_fields",
        "resolve_records_export_target",
    }
)
_LAZY_ARGS_NAMES = frozenset(
    {
        "collect_list_items_with_remainder",
        "prepare_list_query",
    }
)


def __getattr__(name: str) -> Any:
    """Lazily resolve interactive terminal, pagination, and PHI helpers from ``imednet.cli.utils``."""
    if name == "is_interactive_terminal":
        from imednet.cli.utils import is_interactive_terminal

        return is_interactive_terminal
    if name in _LAZY_EXPORT_NAMES:
        from imednet.cli.utils import export as _cli_export

        return getattr(_cli_export, name)
    if name in _LAZY_ARGS_NAMES:
        from imednet.cli.utils import args as _cli_args

        return getattr(_cli_args, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return exported module attributes including lazy SPI helpers."""
    return sorted(set(globals()) | set(__all__))
