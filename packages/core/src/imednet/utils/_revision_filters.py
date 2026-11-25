"""Bounded ``dateCreated`` and ``lastUpdated`` filter helpers for ``RecordRevision`` queries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .dates import (
    format_iso_datetime,
    is_date_only_bound,
    parse_utc_timestamp_bound,
)
from .filters import (
    ALLOWED_OPERATORS,
    _build_filter_part,
    _format_filter_value,
    _parse_clause_operator,
    _scan_filter_string,
    _snake_to_camel,
    _split_filter_clauses,
    _unquote_filter_scalar,
    validate_filter_operator,
    validate_filter_syntax,
)

REVISION_DATE_ATTRIBUTES: frozenset[str] = frozenset({"dateCreated", "lastUpdated"})
_REVISION_DATE_SPECS: tuple[tuple[str, str], ...] = (
    ("dateCreated", "date_created"),
    ("lastUpdated", "last_updated"),
)
_DATETIME_MAX_UTC = datetime.max.replace(tzinfo=UTC)


def parse_revision_timestamp_utc(raw: Any) -> datetime | None:
    """Parse a revision timestamp or date into a UTC-aware datetime."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw.astimezone(UTC)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def matches_revision_date_bound(
    rev_dt: datetime | None,
    op: str,
    bound_val: Any,
    *,
    attr_name: str = "dateCreated",
) -> bool:
    """Evaluate a local date comparison operator against a parsed UTC timestamp."""
    if rev_dt is None:
        return False
    if op == "in":
        if not isinstance(bound_val, (list, tuple, set, frozenset)):
            return False
        return any(
            matches_revision_date_bound(rev_dt, "==", candidate, attr_name=attr_name)
            for candidate in bound_val
        )
    if isinstance(bound_val, bool):
        return False
    bound_dt: datetime | None = None
    if isinstance(bound_val, (datetime, date, str)):
        try:
            bound_dt = parse_utc_timestamp_bound(bound_val, is_end=(op == "<="))
        except ValueError:
            bound_dt = parse_revision_timestamp_utc(bound_val)
    if bound_dt is None:
        return False
    if op in ("==", "="):
        return rev_dt == bound_dt
    if op in ("!=", "<>"):
        return rev_dt != bound_dt
    if op == "<":
        return rev_dt < bound_dt
    if op == "<=":
        return rev_dt <= bound_dt
    if op == ">":
        return rev_dt > bound_dt
    if op == ">=":
        return rev_dt >= bound_dt
    raise ValueError(f"Unsupported {attr_name} filter operator: {op!r}")


def matches_revision_date_bounds(
    item: Any,
    local_bounds: Sequence[tuple[str, str, Any]],
) -> bool:
    """Return True if *item* satisfies all ``(snake_attr, op, bound_val)`` local date bounds."""
    for snake_attr, op, bound_val in local_bounds:
        if snake_attr == "__or__" and op == "or":
            if not any(matches_revision_date_bounds(item, [sub]) for sub in bound_val):
                return False
            continue
        camel_attr = _snake_to_camel(snake_attr)
        if isinstance(item, Mapping):
            raw_val = item.get(snake_attr, item.get(camel_attr))
        else:
            raw_val = getattr(item, snake_attr, getattr(item, camel_attr, None))
        if not matches_revision_date_bound(
            parse_revision_timestamp_utc(raw_val),
            op,
            bound_val,
            attr_name=camel_attr,
        ):
            return False
    return True


def _plan_upstream_date_clause(op: str, raw_bound: Any) -> tuple[str, str, bool]:
    """Return ``(upstream_op, upstream_utc_str, requires_local_check)`` for one date bound."""
    if isinstance(raw_bound, bool) or raw_bound is None:
        raise ValueError(f"Invalid date bound value: {raw_bound!r}")

    if op == "<=" and is_date_only_bound(raw_bound):
        bound_dt = parse_utc_timestamp_bound(raw_bound, is_end=False)
        bound_date = bound_dt.date()
        if bound_date < date.max:
            next_day = bound_date + timedelta(days=1)
            return "<", f"{next_day.isoformat()}T00:00:00Z", False
        return "<=", "9999-12-31T23:59:59Z", True

    bound_dt = parse_utc_timestamp_bound(raw_bound, is_end=(op == "<="))
    if bound_dt.microsecond == 0:
        return op, format_iso_datetime(bound_dt), False

    # Subsecond bound: widen whole-second upstream filter so local check is exact.
    floor_str = format_iso_datetime(bound_dt)
    if op in (">=", ">"):
        return ">=", floor_str, True
    if op in ("<=", "<"):
        if bound_dt <= _DATETIME_MAX_UTC - timedelta(seconds=1):
            ceil_dt = bound_dt.replace(microsecond=0) + timedelta(seconds=1)
            return "<", format_iso_datetime(ceil_dt), True
        return "<=", floor_str, True
    if op == "==":
        return ">=", floor_str, True
    return "!=", floor_str, True


def _validate_date_bound_scalar(raw_bound: Any) -> datetime:
    """Validate and parse a date or timestamp bound scalar into UTC."""
    if isinstance(raw_bound, bool) or raw_bound is None:
        raise ValueError(f"Invalid date bound value: {raw_bound!r}")
    if isinstance(raw_bound, (datetime, date, str)):
        return parse_utc_timestamp_bound(raw_bound, is_end=False)
    raise ValueError(f"Invalid date bound value: {raw_bound!r}")


def _extract_kwarg_date_bounds(
    key: str,
    raw_val: Any,
) -> list[tuple[str, str | None, str, Any]]:
    """Convert a filter kwarg value for a date attribute into validated operator-bound entries."""
    if isinstance(raw_val, tuple):
        if len(raw_val) != 2:
            raise ValueError(
                f"Filter tuple for '{key}' must have exactly 2 elements "
                f"(operator, value), got {len(raw_val)}"
            )
        op, val = raw_val
        validate_filter_operator(op)
        _format_filter_value(val)
        _validate_date_bound_scalar(val)
        return [("kwarg", None, op, val)]
    if isinstance(raw_val, list):
        if not raw_val:
            raise ValueError(f"Filter list for '{key}' cannot be empty")
        if any(isinstance(item, tuple) for item in raw_val):
            entries: list[tuple[str, str | None, str, Any]] = []
            for item in raw_val:
                if isinstance(item, tuple):
                    if len(item) != 2:
                        raise ValueError(
                            f"Filter tuple for '{key}' must have exactly 2 elements "
                            f"(operator, value), got {len(item)}"
                        )
                    op, val = item
                    validate_filter_operator(op)
                    _format_filter_value(val)
                    _validate_date_bound_scalar(val)
                    entries.append(("kwarg", None, op, val))
                else:
                    _format_filter_value(item)
                    _validate_date_bound_scalar(item)
                    entries.append(("kwarg", None, "==", item))
            return entries
        for item in raw_val:
            _format_filter_value(item)
            _validate_date_bound_scalar(item)
        return [("scalar_or_list", None, "in", list(raw_val))]
    _format_filter_value(raw_val)
    _validate_date_bound_scalar(raw_val)
    return [("kwarg", None, "==", raw_val)]


def split_revision_date_bounds(
    filters: Mapping[str, Any],
    *,
    start_date: str | datetime | date | None = None,
    end_date: str | datetime | date | None = None,
    always_evaluate_locally: bool = False,
) -> tuple[dict[str, Any], list[tuple[str, str, Any]]]:
    """Split secondary ``dateCreated`` and ``lastUpdated`` bounds for local evaluation.

    Ensures upstream requests contain canonical whole-second UTC timestamps with at
    most one ``dateCreated`` clause and at most one ``lastUpdated`` clause per
    ``AND`` filter while evaluating secondary, fractional-second, and disjunctive
    OR-list bounds locally.
    """
    working = dict(filters)
    kw_start = working.pop("start_date", None)
    kw_start_camel = working.pop("startDate", None)
    kw_end = working.pop("end_date", None)
    kw_end_camel = working.pop("endDate", None)

    eff_start = (
        start_date
        if start_date is not None
        else (kw_start if kw_start is not None else kw_start_camel)
    )
    eff_end = end_date if end_date is not None else (kw_end if kw_end is not None else kw_end_camel)

    if eff_start is not None:
        _validate_date_bound_scalar(eff_start)
    if eff_end is not None:
        _validate_date_bound_scalar(eff_end)

    raw_filter = working.get("filter")
    has_active_raw_filter = (
        isinstance(raw_filter, str)
        and bool(raw_filter.strip())
        and raw_filter.strip().lower() != "none"
    )

    has_any_date_kwarg = any(
        k in working for camel, snake in _REVISION_DATE_SPECS for k in (camel, snake)
    )

    raw_has_date_attr = False
    raw_has_duplicate_date = False
    if has_active_raw_filter and isinstance(raw_filter, str):
        has_semi, has_comma, has_ws, in_quotes = _scan_filter_string(raw_filter)
        if not in_quotes and not (has_semi and has_comma) and not has_ws:
            clauses_preview, conn_preview = _split_filter_clauses(raw_filter.strip())
            seen_date_preview: set[str] = set()
            for cl in clauses_preview:
                if not cl:
                    continue
                try:
                    parsed_cl = _parse_clause_operator(cl)
                except ValueError:
                    parsed_cl = None
                if parsed_cl is None:
                    continue
                c_attr = _snake_to_camel(parsed_cl[0])
                if c_attr in REVISION_DATE_ATTRIBUTES:
                    raw_has_date_attr = True
                    if conn_preview == ";" and c_attr in seen_date_preview:
                        raw_has_duplicate_date = True
                    seen_date_preview.add(c_attr)

    needs_processing = (
        eff_start is not None
        or eff_end is not None
        or has_any_date_kwarg
        or raw_has_duplicate_date
        or (always_evaluate_locally and raw_has_date_attr)
    )
    if not needs_processing:
        return working, []

    local_bounds: list[tuple[str, str, Any]] = []
    raw_date_entries: dict[str, list[tuple[str, str | None, str, Any]]] = {
        "dateCreated": [],
        "lastUpdated": [],
    }
    non_date_raw_clauses: list[str] = []
    raw_is_or = False

    if has_active_raw_filter and isinstance(raw_filter, str):
        has_semi, has_comma, has_ws, in_quotes = _scan_filter_string(raw_filter)
        if in_quotes:
            raise ValueError("Filter expression contains unclosed quotation marks")
        if has_semi and has_comma:
            raise ValueError(
                "Filter expression cannot combine both ';' (AND) and ',' (OR) connectors"
            )
        if has_ws:
            raise ValueError(
                "Filter expression contains whitespace between attribute, operator, and value"
            )
        if has_comma:
            validate_filter_syntax(raw_filter)
            raw_is_or = True
            if always_evaluate_locally:
                or_clauses, _ = _split_filter_clauses(raw_filter.strip())
                normalized_or_clauses: list[str] = []
                or_attrs: set[str] = set()
                or_ops: set[str] = set()
                or_vals: list[str] = []
                or_date_clauses: list[tuple[str, str, Any]] = []
                or_all_date = True
                for clause in or_clauses:
                    parsed = _parse_clause_operator(clause)
                    if parsed is None:
                        normalized_or_clauses.append(clause)
                        or_attrs.add(clause)
                        or_all_date = False
                        continue
                    attr, op, raw_rhs = parsed
                    camel_attr = _snake_to_camel(attr)
                    or_attrs.add(camel_attr)
                    or_ops.add(op)
                    if camel_attr in REVISION_DATE_ATTRIBUTES:
                        unquoted_rhs, _ = _unquote_filter_scalar(raw_rhs, clause)
                        if not unquoted_rhs:
                            raise ValueError(
                                f"Invalid filter format: '{clause}'. Empty filter value."
                            )
                        up_op, up_val, _ = _plan_upstream_date_clause(op, unquoted_rhs)
                        normalized_or_clauses.append(
                            _build_filter_part(camel_attr, (up_op, up_val))
                        )
                        or_vals.append(unquoted_rhs)
                        snake_or_attr = (
                            "date_created" if camel_attr == "dateCreated" else "last_updated"
                        )
                        or_date_clauses.append((snake_or_attr, op, unquoted_rhs))
                    else:
                        normalized_or_clauses.append(clause)
                        or_all_date = False
                working["filter"] = ",".join(normalized_or_clauses)
                if or_all_date and or_date_clauses:
                    if len(or_attrs) == 1 and or_ops == {"=="}:
                        local_bounds.append((or_date_clauses[0][0], "in", list(or_vals)))
                    else:
                        local_bounds.append(("__or__", "or", or_date_clauses))
        else:
            clauses, _ = _split_filter_clauses(raw_filter.strip())
            seen_non_date: set[str] = set()
            for clause in clauses:
                if not clause:
                    raise ValueError("Filter expression contains an empty clause")
                parsed = _parse_clause_operator(clause)
                if parsed is None:
                    non_date_raw_clauses.append(clause)
                    continue
                attr, op, raw_rhs = parsed
                if op not in ALLOWED_OPERATORS:
                    raise ValueError(
                        f"Invalid filter operator '{op}'. "
                        f"Allowed operators: {sorted(ALLOWED_OPERATORS)}"
                    )
                camel_attr = _snake_to_camel(attr)
                if camel_attr in REVISION_DATE_ATTRIBUTES:
                    unquoted_rhs, _ = _unquote_filter_scalar(raw_rhs, clause)
                    if not unquoted_rhs:
                        raise ValueError(f"Invalid filter format: '{clause}'. Empty filter value.")
                    _validate_date_bound_scalar(unquoted_rhs)
                    raw_date_entries[camel_attr].append(("raw", None, op, unquoted_rhs))
                else:
                    if camel_attr in seen_non_date:
                        raise ValueError(f"Duplicate filter attribute '{camel_attr}' in AND clause")
                    seen_non_date.add(camel_attr)
                    non_date_raw_clauses.append(clause)

    # Extract date kwargs upfront so we know whether any non-date kwargs remain.
    extracted_kwarg_entries: dict[
        str, tuple[str | None, list[tuple[str, str | None, str, Any]]]
    ] = {}
    for camel_key, snake_key in _REVISION_DATE_SPECS:
        kw_entries: list[tuple[str, str | None, str, Any]] = []
        preserved_single_key: str | None = None
        present_count = 0
        for key_variant in (snake_key, camel_key):
            if key_variant in working:
                present_count += 1
                preserved_single_key = key_variant
                val = working.pop(key_variant)
                kw_entries.extend(_extract_kwarg_date_bounds(key_variant, val))
        if present_count > 1:
            preserved_single_key = None
        extracted_kwarg_entries[camel_key] = (preserved_single_key, kw_entries)

    non_filter_kwargs_present = any(
        k not in ("filter", "page", "size", "sort", "limit") for k in working
    )
    active_date_attr_count = sum(
        1
        for camel_key, _ in _REVISION_DATE_SPECS
        if raw_date_entries[camel_key]
        or extracted_kwarg_entries[camel_key][1]
        or (camel_key == "dateCreated" and (eff_start is not None or eff_end is not None))
    )

    upstream_raw_clauses = list(non_date_raw_clauses)

    for camel_key, snake_key in _REVISION_DATE_SPECS:
        preserved_single_key, kw_entries = extracted_kwarg_entries[camel_key]
        combined_entries: list[tuple[str, str | None, str, Any]] = [
            *raw_date_entries[camel_key],
            *kw_entries,
        ]

        if camel_key == "dateCreated":
            if eff_start is not None:
                combined_entries.append(("kwarg", None, ">=", eff_start))
            if eff_end is not None:
                combined_entries.append(("kwarg", None, "<=", eff_end))

        if not combined_entries:
            continue

        if raw_is_or:
            for _, _, op, val in combined_entries:
                local_bounds.append((snake_key, op, val))
            continue

        can_emit_pure_or_upstream = (
            len(combined_entries) == 1
            and combined_entries[0][0] == "scalar_or_list"
            and not has_active_raw_filter
            and not non_filter_kwargs_present
            and active_date_attr_count == 1
        )
        if can_emit_pure_or_upstream:
            or_items = combined_entries[0][3]
            planned_or = [_plan_upstream_date_clause("==", item) for item in or_items]
            if all(up_op == "==" and not req_local for up_op, _, req_local in planned_or):
                working[preserved_single_key or camel_key] = [up_val for _, up_val, _ in planned_or]
                if always_evaluate_locally:
                    local_bounds.append((snake_key, "in", list(or_items)))
                continue

        # Choose one conjunctive clause to dispatch upstream: prefer explicit non-OR bounds,
        # otherwise derive a lower-bound floor (>= min) from the first scalar OR list.
        non_or_entries = [entry for entry in combined_entries if entry[0] != "scalar_or_list"]
        if non_or_entries:
            _, _, first_op, first_val = non_or_entries[0]
            up_op, up_val, first_needs_local = _plan_upstream_date_clause(first_op, first_val)
        else:
            first_or_items = combined_entries[0][3]
            min_dt = min(parse_utc_timestamp_bound(item, is_end=False) for item in first_or_items)
            up_op = "==" if len(first_or_items) == 1 and min_dt.microsecond == 0 else ">="
            up_val = format_iso_datetime(min_dt)
            first_needs_local = True

        if has_active_raw_filter:
            upstream_raw_clauses.append(_build_filter_part(camel_key, (up_op, up_val)))
        else:
            target_key = (
                preserved_single_key
                if (
                    preserved_single_key is not None
                    and len(combined_entries) == 1
                    and combined_entries[0][0] == "kwarg"
                )
                else camel_key
            )
            working[target_key] = (up_op, up_val)

        need_local_for_attr = (
            always_evaluate_locally
            or len(combined_entries) > 1
            or first_needs_local
            or any(entry[0] == "scalar_or_list" for entry in combined_entries)
        )
        if need_local_for_attr:
            for _, _, op, val in combined_entries:
                local_bounds.append((snake_key, op, val))

    if has_active_raw_filter and not raw_is_or:
        working["filter"] = ";".join(upstream_raw_clauses)

    return working, local_bounds


__all__ = [
    "REVISION_DATE_ATTRIBUTES",
    "matches_revision_date_bound",
    "matches_revision_date_bounds",
    "parse_revision_timestamp_utc",
    "split_revision_date_bounds",
]
