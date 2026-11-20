"""CLI argument parsing utilities."""

from __future__ import annotations

import argparse
import itertools
import sys
import weakref
from collections.abc import Collection, Iterable, Sequence
from typing import Any

from imednet.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from imednet.utils.filters import (
    _parse_clause_operator,
    _split_filter_clauses,
    extract_filter_attributes,
    parse_filter_expressions,
    validate_record_filter,
)

from .output import print_error

STUDY_KEY_ARG = "The key identifying the study."
DEFAULT_INTERACTIVE_LIMIT = DEFAULT_PAGE_SIZE


_STUDY_KEY_FILTER_ATTRS: frozenset[str] = frozenset({"study_key", "studyKey"})


_ENDPOINT_FILTER_ATTRS_CACHE: weakref.WeakKeyDictionary[type[Any], frozenset[str] | None] = (
    weakref.WeakKeyDictionary()
)


def resolve_endpoint_filter_attributes(model: type[Any] | None) -> frozenset[str] | None:
    """Resolve documented endpoint ``FILTER_ATTRIBUTES`` for a registered SDK model."""
    if model is None:
        return None
    if model in _ENDPOINT_FILTER_ATTRS_CACHE:
        return _ENDPOINT_FILTER_ATTRS_CACHE[model]
    from imednet.endpoints.registry import ENDPOINT_REGISTRY

    resolved: frozenset[str] | None = None
    for endpoint_cls in ENDPOINT_REGISTRY.values():
        if getattr(endpoint_cls, "MODEL", None) is model:
            attrs = getattr(endpoint_cls, "FILTER_ATTRIBUTES", None)
            if isinstance(attrs, frozenset):
                resolved = attrs
                break
    _ENDPOINT_FILTER_ATTRS_CACHE[model] = resolved
    return resolved


def _contains_study_key_filter(parsed: dict[str, Any]) -> bool:
    """Return True if parsed filters include ``study_key`` or ``studyKey``."""
    if _STUDY_KEY_FILTER_ATTRS.intersection(parsed):
        return True
    raw_filter = parsed.get("filter")
    if isinstance(raw_filter, str):
        return bool(_STUDY_KEY_FILTER_ATTRS.intersection(extract_filter_attributes(raw_filter)))
    return False


def parse_filter_args(
    filter_args: list[str] | None,
    *,
    model: type[Any] | None = None,
    allowed_attributes: Collection[str] | None = None,
    disallow_study_key: bool = False,
) -> dict[str, Any] | None:
    """Parse repeatable CLI ``--filter`` strings via the shared SPI filter parser.

    Supports legacy ``key=value`` equality, Table 2 iMednet comparison
    operators (``<``, ``<=``, ``>``, ``>=``, ``==``, ``!=``), compound
    expressions joined exclusively by ``;`` (AND) or ``,`` (OR), endpoint
    ``allowed_attributes`` allowlists, and Pydantic ``model`` scalar coercion.
    Exits with status code ``1`` on ``stderr`` if validation fails.
    """
    if not filter_args:
        return None

    effective_allowed: Collection[str] | None = (
        allowed_attributes
        if allowed_attributes is not None
        else resolve_endpoint_filter_attributes(model)
    )
    if disallow_study_key and effective_allowed is not None:
        effective_allowed = frozenset(
            attr for attr in effective_allowed if attr not in _STUDY_KEY_FILTER_ATTRS
        )

    try:
        parsed = parse_filter_expressions(
            filter_args,
            model=model,
            allowed_attributes=effective_allowed,
            allow_regex=False,
        )
    except ValueError as exc:
        print_error(f"Error: {exc}")
        sys.exit(1)

    if disallow_study_key and _contains_study_key_filter(parsed):
        print_error(
            "Error: Invalid filter: 'study_key' and 'studyKey' cannot be used in "
            "--filter when positional study_key is provided."
        )
        sys.exit(1)
    return parsed


def parse_record_data_filter(record_data_filter: str | None) -> str | None:
    """Validate a CLI ``--record-data-filter`` string via ``validate_record_filter``.

    Supports comparison operators including ``=~``, exclusive ``;`` (AND) or
    ``,`` (OR) connectors, and literal ``none`` (case-insensitive) bypass.
    Exits with status code ``1`` on ``stderr`` if validation fails.
    """
    if record_data_filter is None:
        return None
    stripped = record_data_filter.strip()
    if not stripped:
        print_error("Error: Invalid record data filter: cannot be empty.")
        sys.exit(1)
    try:
        validate_record_filter(record_data_filter)
        if stripped.lower() != "none":
            clauses, _ = _split_filter_clauses(stripped)
            for clause in clauses:
                if _parse_clause_operator(clause) is None:
                    raise ValueError(
                        f"Malformed filter clause '{clause}': missing comparison operator"
                    )
    except ValueError as exc:
        print_error(f"Error: Invalid record data filter: {exc}")
        sys.exit(1)
    return stripped


class UniqueStoreAction(argparse.Action):
    """Store action that rejects conflicting duplicate flag values."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        """Store flag value on namespace or abort with exit code 2 on conflicting values."""
        del option_string
        existing = getattr(namespace, self.dest, None)
        if existing is not None and existing != values:
            key_name = self.dest.removesuffix("_flag")
            parser.error(f"conflicting values provided for {key_name}: '{existing}' and '{values}'")
        setattr(namespace, self.dest, values)


def _is_blank_arg(val: str | None) -> bool:
    return val is None or not str(val).strip()


def add_study_key_args(
    parser: argparse.ArgumentParser,
    *,
    flag_names: Sequence[str] = ("-s", "--study-key"),
    include_positional: bool = True,
    positional_help: str = STUDY_KEY_ARG,
    flag_help: str = STUDY_KEY_ARG,
) -> None:
    """Register hybrid ``--study-key`` flag and optional positional ``study_key``."""
    available_flags = [f for f in flag_names if f not in parser._option_string_actions]
    if available_flags:
        parser.add_argument(
            *available_flags,
            dest="study_key_flag",
            default=None,
            action=UniqueStoreAction,
            help=flag_help,
        )
    if include_positional:
        has_pos = any(
            not action.option_strings and action.dest == "study_key" for action in parser._actions
        )
        if not has_pos:
            parser.add_argument("study_key", nargs="?", default=None, help=positional_help)


def resolve_study_key(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    required: bool = True,
    missing_error: str = "the following arguments are required: study_key",
) -> str | None:
    """Resolve hybrid positional ``study_key`` and ``--study-key`` flag, exiting 2 on conflict."""
    flag_val: str | None = getattr(args, "study_key_flag", None)
    pos_val: str | None = getattr(args, "study_key", None)

    flag_present = not _is_blank_arg(flag_val)
    pos_present = not _is_blank_arg(pos_val)

    if flag_present and pos_present:
        if flag_val != pos_val:
            parser.error(
                f"conflicting values provided for study_key: '{flag_val}' (flag) and '{pos_val}' (positional)"
            )
        resolved: str | None = flag_val
    elif flag_present:
        resolved = flag_val
    elif pos_present:
        resolved = pos_val
    else:
        resolved = None

    if resolved is None and required:
        parser.error(missing_error)

    if resolved is not None or hasattr(args, "study_key"):
        args.study_key = resolved
    return resolved


def resolve_hybrid_study_and_key(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    secondary_dest: str = "subject_key",
    secondary_flag_dest: str = "subject_key_flag",
) -> tuple[str, str]:
    """Resolve hybrid positional and flag pairs for ``study_key`` and a secondary key."""
    flag_study: str | None = getattr(args, "study_key_flag", None)
    flag_secondary: str | None = getattr(args, secondary_flag_dest, None)
    pos_study: str | None = getattr(args, "study_key", None)
    pos_secondary: str | None = getattr(args, secondary_dest, None)

    if flag_study is not None and flag_secondary is not None:
        study_key: str | None = flag_study
        secondary_key: str | None = flag_secondary
    elif flag_study is not None and flag_secondary is None:
        study_key = flag_study
        if pos_secondary is not None and pos_study is not None:
            if (
                not _is_blank_arg(pos_study)
                and not _is_blank_arg(flag_study)
                and pos_study != flag_study
            ):
                parser.error(
                    f"conflicting values provided for study_key: '{flag_study}' (flag) and '{pos_study}' (positional)"
                )
            secondary_key = pos_secondary
        elif pos_study is not None:
            secondary_key = pos_study
        else:
            secondary_key = None
    elif flag_study is None and flag_secondary is not None:
        secondary_key = flag_secondary
        if pos_study is not None and pos_secondary is not None:
            if (
                not _is_blank_arg(pos_secondary)
                and not _is_blank_arg(flag_secondary)
                and pos_secondary != flag_secondary
            ):
                parser.error(
                    f"conflicting values provided for {secondary_dest}: '{flag_secondary}' (flag) and '{pos_secondary}' (positional)"
                )
            study_key = pos_study
        elif pos_study is not None:
            study_key = pos_study
        else:
            study_key = None
    else:
        study_key = pos_study
        secondary_key = pos_secondary

    missing_study = _is_blank_arg(study_key)
    missing_secondary = _is_blank_arg(secondary_key)
    if missing_study and missing_secondary:
        parser.error(f"the following arguments are required: study_key, {secondary_dest}")
    elif missing_study:
        parser.error("the following arguments are required: study_key")
    elif missing_secondary:
        parser.error(f"the following arguments are required: {secondary_dest}")

    assert study_key is not None
    assert secondary_key is not None
    return study_key, secondary_key


def add_simulation_arg(parser: argparse.ArgumentParser, *, suppress_default: bool = True) -> None:
    """Add ``--simulation`` option to run against offline ``SimulationTransport`` fixtures."""
    if "--simulation" in parser._option_string_actions:
        return
    kwargs: dict[str, Any] = {
        "dest": "simulation",
        "action": "store_true",
        "help": "Run offline against deterministic synthetic simulation data without live API credentials.",
    }
    if suppress_default:
        kwargs["default"] = argparse.SUPPRESS
    parser.add_argument("--simulation", **kwargs)


def add_no_interactive(parser: argparse.ArgumentParser, *, suppress_default: bool = True) -> None:
    """Add ``--no-interactive`` option to disable interactive UI, spinners, and prompts."""
    if "--no-interactive" in parser._option_string_actions:
        return
    kwargs: dict[str, Any] = {
        "action": "store_true",
        "help": "Disable interactive terminal UI, live spinners, and stdin prompts.",
    }
    if suppress_default:
        kwargs["default"] = argparse.SUPPRESS
    parser.add_argument("--no-interactive", **kwargs)


def add_output_format(parser: argparse.ArgumentParser) -> None:
    """Add common machine response and non-interactive flags to a leaf read command."""
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("table", "json"),
        default="table",
        help="Response format: human table (default) or versioned JSON on stdout.",
    )
    add_no_interactive(parser)


def add_list_query_args(
    parser: argparse.ArgumentParser,
    *,
    include_filter: bool = True,
    filter_dest: str = "filter_args",
    filter_help: str | None = None,
    include_record_data_filter: bool = False,
) -> None:
    """Attach server-side filter, sort, limit, and unbounded flags to a list subparser."""
    if include_filter:
        parser.add_argument(
            "-f",
            "--filter",
            action="append",
            dest=filter_dest,
            default=None,
            help=filter_help
            or (
                "Server filter expression using =, ==, !=, <, <=, >, >= and "
                "';' (AND) or ',' (OR) connectors. Repeat for multiple AND filters."
            ),
        )
    if include_record_data_filter:
        parser.add_argument(
            "--record-data-filter",
            dest="record_data_filter",
            default=None,
            help=(
                "Server recordDataFilter expression for Form Variable values using "
                "==, !=, <, <=, >, >=, =~ (e.g., 'AESER==Serious;AGE>=18' or 'AETERM=~^Head')."
            ),
        )
    parser.add_argument(
        "--sort",
        dest="sort",
        default=None,
        help="Server sort specification (e.g., 'dateCreated,desc' or 'recordId,asc').",
    )
    parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=None,
        help="Maximum number of items to fetch before stopping.",
    )
    parser.add_argument(
        "--all",
        dest="fetch_all",
        action="store_true",
        default=False,
        help="Fetch all matching pages even in interactive TTY sessions.",
    )


def prepare_list_query(
    *,
    filter_args: list[str] | None = None,
    record_data_filter: str | None = None,
    sort: str | None = None,
    limit: int | None = None,
    fetch_all: bool = False,
    output_format: str = "table",
    has_output_file: bool = False,
    no_interactive: bool = False,
    model: type[Any] | None = None,
    allowed_attributes: Collection[str] | None = None,
    disallow_study_key: bool = False,
) -> tuple[dict[str, Any], int | None]:
    """Validate CLI query options and resolve SDK ``list()`` kwargs and item bound.

    Returns:
        Tuple of ``(request_kwargs, effective_limit)``.
    """
    if limit is not None and fetch_all:
        print_error("Error: Cannot combine --limit and --all.")
        sys.exit(1)

    if limit is not None and limit <= 0:
        print_error("Error: --limit must be a positive integer.")
        sys.exit(1)

    if sort is not None and not sort.strip():
        print_error("Error: --sort cannot be empty.")
        sys.exit(1)

    request_kwargs: dict[str, Any] = {}
    parsed_filters = parse_filter_args(
        filter_args,
        model=model,
        allowed_attributes=allowed_attributes,
        disallow_study_key=disallow_study_key,
    )
    if parsed_filters:
        request_kwargs.update(parsed_filters)

    parsed_record_data_filter = parse_record_data_filter(record_data_filter)
    if parsed_record_data_filter is not None:
        request_kwargs["record_data_filter"] = parsed_record_data_filter

    if sort is not None:
        request_kwargs["sort"] = sort.strip()

    effective_limit: int | None = None
    if limit is not None:
        effective_limit = limit
    elif not fetch_all and not no_interactive and output_format == "table" and not has_output_file:
        try:
            from .grid import is_interactive_terminal

            is_tty = is_interactive_terminal()
        except ImportError:
            is_tty = False
        if is_tty:
            effective_limit = DEFAULT_INTERACTIVE_LIMIT

    if effective_limit is not None:
        request_kwargs["size"] = min(effective_limit, MAX_PAGE_SIZE)

    return request_kwargs, effective_limit


def collect_list_items(
    source: Iterable[Any],
    effective_limit: int | None,
) -> tuple[list[Any], bool]:
    """Materialize items from an SDK ``list()`` iterable up to ``effective_limit``.

    Detects server-side truncation without issuing an extra page request when
    backed by ``_PaginatedIterator``.

    Returns:
        Tuple of ``(items, truncated)``.
    """
    if effective_limit is None:
        return list(source), False

    if isinstance(source, Sequence):
        truncated = len(source) > effective_limit
        return list(source[:effective_limit]), truncated

    if hasattr(source, "has_more"):
        items = list(itertools.islice(source, effective_limit))
        return items, bool(getattr(source, "has_more", False))

    iterator = iter(source)
    batch = list(itertools.islice(iterator, effective_limit + 1))
    truncated = len(batch) > effective_limit
    return batch[:effective_limit], truncated
