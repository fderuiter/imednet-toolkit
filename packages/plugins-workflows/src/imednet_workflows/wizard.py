"""Interactive console wizard and parameter validator for iMednet workflows.

Provides step-by-step interactive prompts, live parameter validation,
and stderr progress reporting to preserve clean stdout stream outputs.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from ._spi_compat import print_error, supports_interactive_stdin
from .state_ledger import DEFAULT_LEDGER_PATH, get_state_provider


def ensure_interactive_stdin(*, force_non_interactive: bool = False) -> None:
    """Ensure interactive stdin prompts are permitted, exiting with code 1 otherwise."""
    if force_non_interactive or not supports_interactive_stdin():
        print_error(
            "Error: Interactive prompts require an interactive TTY on stdin and cannot run "
            "when stdin is piped or non-interactive mode is active. "
            "Pass required options via CLI flags."
        )
        sys.exit(1)


def print_progress(message: str) -> None:
    """Print a progress or status message to stderr and flush.

    This ensures stdout remains clean for structured data piping.
    """
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def prompt_input(prompt_text: str, default: str | None = None) -> str:
    """Prompt the user for input on stderr and read from stdin.

    Args:
        prompt_text: Prompt message describing the expected input.
        default: Optional default value if the user provides an empty line.

    Returns:
        The stripped string provided by the user, or the default.

    Raises:
        SystemExit: If stdin is non-interactive or end-of-file (EOF) is encountered.
    """
    ensure_interactive_stdin()
    if default is not None:
        sys.stderr.write(f"{prompt_text} [{default}]: ")
    else:
        sys.stderr.write(f"{prompt_text}: ")
    sys.stderr.flush()

    line = sys.stdin.readline()
    if line == "":
        print_progress("Error: Input stream closed (EOF). Operation cancelled.")
        sys.exit(1)
    stripped = line.strip()
    if not stripped and default is not None:
        return default
    return stripped


def prompt_confirm(prompt_text: str, default: bool = False) -> bool:
    """Prompt the user for a boolean confirmation on stderr.

    Args:
        prompt_text: Description of the confirmation action.
        default: Default boolean choice if user presses Enter.

    Returns:
        True if confirmed, False otherwise.

    Raises:
        SystemExit: If stdin is non-interactive or end-of-file (EOF) is encountered.
    """
    ensure_interactive_stdin()
    suffix = " [Y/n]: " if default else " [y/N]: "
    sys.stderr.write(f"{prompt_text}{suffix}")
    sys.stderr.flush()

    line = sys.stdin.readline()
    if line == "":
        print_progress("Error: Input stream closed (EOF). Operation cancelled.")
        sys.exit(1)
    choice = line.strip().lower()
    if not choice:
        return default
    return choice in ("y", "yes")


def validate_iso8601_timestamp(ts: str) -> tuple[bool, datetime | None, str | None]:
    """Validate that a string is a valid ISO-8601 UTC timestamp.

    Args:
        ts: Input timestamp string (e.g. '2026-05-22T12:00:00Z').

    Returns:
        A tuple of (is_valid, parsed_datetime_utc, error_message).
    """
    if not ts or not ts.strip():
        return False, None, "Timestamp cannot be empty. Example: '2026-05-22T12:00:00Z'."

    raw = ts.strip()
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as err:
        return (
            False,
            None,
            f"Invalid ISO-8601 timestamp format: '{raw}'. Expected 'YYYY-MM-DDTHH:MM:SSZ' (e.g., '2026-05-22T12:00:00Z'). Details: {err}",
        )

    # Normalize to UTC
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

    return True, dt, None


def prompt_iso8601_timestamp(
    prompt_text: str = "Enter ISO-8601 UTC timestamp (e.g., '2026-05-22T12:00:00Z')",
    default: str | None = None,
) -> tuple[str, datetime]:
    """Interactively prompt for and validate an ISO-8601 UTC timestamp until valid.

    Args:
        prompt_text: Message describing the timestamp prompt.
        default: Optional default timestamp string.

    Returns:
        A tuple of (raw_timestamp_string, parsed_datetime_utc).
    """
    while True:
        value = prompt_input(prompt_text, default=default)
        is_valid, dt, err = validate_iso8601_timestamp(value)
        if is_valid and dt is not None:
            return value, dt
        print_progress(f"Validation Error: {err}")


def validate_filter_syntax(
    filter_str: str,
    *,
    model: type[Any] | None = None,
) -> tuple[bool, str | None, Any | None, str | None]:
    """Validate a single filter parameter string via the shared SPI parser.

    Args:
        filter_str: Raw filter string.
        model: Optional endpoint resource model class used to validate filter keys.

    Returns:
        A tuple of (is_valid, key, parsed_value, error_message).
    """
    from ._spi_compat import parse_filter_expression

    try:
        key, parsed_val = parse_filter_expression(filter_str, model=model)
    except ValueError as exc:
        return False, None, None, str(exc)
    return True, key, parsed_val, None


def prompt_filters(
    category_name: str,
    example: str,
    existing_filters: list[str] | None = None,
    *,
    model: type[Any] | None = None,
) -> list[str]:
    """Interactively prompt and validate key-value filters for a category.

    Args:
        category_name: Name of the filter category (e.g., 'subject filter').
        example: An illustrative example string (e.g., 'subject_status=Screened').
        existing_filters: Filters already supplied on the command line.
        model: Optional endpoint resource model class used to validate filter keys.

    Returns:
        A list of validated filter strings.
    """
    filters: list[str] = []
    if existing_filters:
        for f in existing_filters:
            is_valid, _, _, err = validate_filter_syntax(f, model=model)
            if is_valid:
                filters.append(f)
            else:
                print_progress(f"Validation Error in provided filter: {err}")
                corrected = prompt_input(
                    f"Enter corrected {category_name} to replace invalid '{f}' (e.g., '{example}')"
                )
                if not corrected:
                    print_progress(
                        f"Error: Malformed filter '{f}' was not corrected. Aborting to prevent unfiltered extraction."
                    )
                    sys.exit(1)
                while True:
                    c_valid, _, _, c_err = validate_filter_syntax(corrected, model=model)
                    if c_valid:
                        filters.append(corrected)
                        break
                    print_progress(f"Validation Error: {c_err}")
                    corrected = prompt_input(
                        f"Enter corrected {category_name} to replace invalid '{f}' (e.g., '{example}')"
                    )
                    if not corrected:
                        print_progress(
                            f"Error: Malformed filter '{f}' was not corrected. Aborting to prevent unfiltered extraction."
                        )
                        sys.exit(1)

    # If filters were already provided, confirm or add more
    prompt_msg = (
        f"Enter {category_name} (e.g., '{example}', or press Enter to skip/finish)"
        if not filters
        else f"Enter additional {category_name} (e.g., '{example}', or press Enter to finish)"
    )

    while True:
        entry = prompt_input(prompt_msg)
        if not entry:
            break
        is_valid, _, _, err = validate_filter_syntax(entry, model=model)
        if is_valid:
            filters.append(entry)
            prompt_msg = f"Enter additional {category_name} (or press Enter to finish)"
        else:
            print_progress(f"Validation Error: {err}")

    return filters


def prompt_study_key(study_key: str | None = None) -> str:
    """Ensure a valid non-empty study key, prompting if needed.

    Args:
        study_key: Existing study key from CLI arguments.

    Returns:
        Non-empty study key string.
    """
    if study_key and study_key.strip():
        return study_key.strip()

    while True:
        val = prompt_input("Enter study key")
        if val:
            return val
        print_progress(
            "Validation Error: Study key cannot be empty. Please enter a valid study key."
        )


def prompt_subject_key(subject_key: str | None = None) -> str:
    """Ensure a valid non-empty subject key, prompting if needed.

    Args:
        subject_key: Existing subject key from CLI arguments.

    Returns:
        Non-empty subject key string.
    """
    if subject_key and subject_key.strip():
        return subject_key.strip()

    while True:
        val = prompt_input("Enter subject key")
        if val:
            return val
        print_progress(
            "Validation Error: Subject key cannot be empty. Please enter a valid subject key."
        )


_SUBJECT_SECTION_ALIASES: dict[str, str] = {
    "1": "subject",
    "subject": "subject",
    "subject_details": "subject",
    "details": "subject",
    "2": "visits",
    "visit": "visits",
    "visits": "visits",
    "3": "records",
    "record": "records",
    "records": "records",
    "4": "queries",
    "query": "queries",
    "queries": "queries",
    "5": "all",
    "all": "all",
}


def prompt_subject_section(default: str = "all") -> str:
    """Prompt the user on stderr to choose a subject-data section to inspect.

    Args:
        default: Default section key if the user presses Enter.

    Returns:
        Canonical section key: ``'subject'``, ``'visits'``, ``'records'``,
        ``'queries'``, or ``'all'``.
    """
    print_progress("Select subject data section (1=subject, 2=visits, 3=records, 4=queries, 5=all)")
    while True:
        raw = prompt_input("Section", default=default)
        normalized = _SUBJECT_SECTION_ALIASES.get(raw.strip().lower())
        if normalized is not None:
            return normalized
        print_progress(
            f"Validation Error: Invalid section '{raw}'. "
            "Choose 1-5 or one of: subject, visits, records, queries, all."
        )


def prompt_stream_name(stream: str | None = None) -> str:
    """Ensure a valid non-empty stream name, prompting if needed.

    Args:
        stream: Existing stream name from CLI arguments.

    Returns:
        Non-empty stream name string.
    """
    if stream and stream.strip():
        return stream.strip()

    while True:
        val = prompt_input("Enter stream name (e.g., 'records')")
        if val:
            return val
        print_progress(
            "Validation Error: Stream name cannot be empty. Please enter a valid stream name."
        )


def prompt_positive_int(prompt_text: str, default: int = 1) -> int:
    """Prompt for a positive integer with immediate validation.

    Args:
        prompt_text: Message describing the expected number.
        default: Default positive integer value.

    Returns:
        Validated positive integer (> 0).
    """
    while True:
        raw = prompt_input(prompt_text, default=str(default))
        try:
            val = int(raw)
            if val > 0:
                return val
            print_progress("Validation Error: Value must be a positive integer (> 0).")
        except ValueError:
            print_progress(f"Validation Error: '{raw}' is not a valid integer.")


def prompt_non_negative_int(prompt_text: str, default: int = 0) -> int:
    """Prompt for a non-negative integer with immediate validation.

    Args:
        prompt_text: Message describing the expected number.
        default: Default non-negative integer value.

    Returns:
        Validated non-negative integer (>= 0).
    """
    while True:
        raw = prompt_input(prompt_text, default=str(default))
        try:
            val = int(raw)
            if val >= 0:
                return val
            print_progress("Validation Error: Value must be a non-negative integer (>= 0).")
        except ValueError:
            print_progress(f"Validation Error: '{raw}' is not a valid integer.")


def display_ledger_streams(
    ledger_path: str = DEFAULT_LEDGER_PATH, study_key: str | None = None
) -> None:
    """Display active ledger streams to stderr for interactive inspection.

    Args:
        ledger_path: Path to the pipeline ledger JSON file.
        study_key: Optional study key to filter displayed streams.
    """
    ledger = get_state_provider(ledger_path)
    try:
        state = ledger.read_state()
    except Exception as err:
        print_progress(f"Notice: Could not read existing ledger at {ledger_path}: {err}")
        return

    print_progress(f"\n--- Current Ledger Streams ({ledger_path}) ---")
    header = "Study Key | Stream Name | Last Timestamp (UTC) | Records Processed | Status | Error Message"
    print_progress(header)
    print_progress("-" * len(header))

    found = False
    for s_key, study_state in state.studies.items():
        if study_key and s_key != study_key:
            continue
        for stream_name, stream_state in study_state.streams.items():
            found = True
            ts_str = (
                stream_state.last_timestamp.isoformat() if stream_state.last_timestamp else "None"
            )
            print_progress(
                f"{s_key} | {stream_name} | {ts_str} | {stream_state.records_processed} | {stream_state.last_run_status} | {stream_state.error_message or ''}"
            )

    if not found:
        print_progress("No active stream records found in ledger.")
    print_progress("")
