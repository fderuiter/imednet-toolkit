"""Service Provider Interface (SPI) for CLI extensions."""

from imednet.cli.decorators import with_sdk
from imednet.cli.utils import (
    STUDY_KEY_ARG,
    Console,
    Status,
    add_filter_arg,
    add_list_query_args,
    add_output_format,
    add_record_data_filter_arg,
    add_sort_arg,
    build_list_query_kwargs,
    clear_line,
    console,
    display_list,
    emit_json,
    fetching_status,
    format_job_progress,
    parse_filter_args,
    print_error,
)

__all__ = [
    "STUDY_KEY_ARG",
    "Console",
    "Status",
    "add_filter_arg",
    "add_list_query_args",
    "add_output_format",
    "add_record_data_filter_arg",
    "add_sort_arg",
    "build_list_query_kwargs",
    "clear_line",
    "console",
    "display_list",
    "emit_json",
    "fetching_status",
    "format_job_progress",
    "parse_filter_args",
    "print_error",
    "with_sdk",
]
