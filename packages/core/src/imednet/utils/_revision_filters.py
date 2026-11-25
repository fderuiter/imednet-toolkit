"""Bounded ``dateCreated`` and ``lastUpdated`` filter helpers for ``RecordRevision`` queries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from .dates import format_iso_datetime, normalize_utc_timestamp_bound
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


def parse_revision_timestamp_utc(raw: Any) -> datetime | None:
    """Parse a revision timestamp or date into a UTC-aware datetime."""
    if raw is None:
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
    if isinstance(bound_val, datetime):
        bound_dt = parse_revision_timestamp_utc(bound_val)
    elif isinstance(bound_val, (str, date)):
        bound_str = bound_val if isinstance(bound_val, str) else bound_val.isoformat()
        try:
            norm_str = normalize_utc_timestamp_bound(bound_str, is_end=op in ("<=", "<"))
            bound_dt = parse_revision_timestamp_utc(norm_str)
        except ValueError:
            bound_dt = parse_revision_timestamp_utc(bound_val)
    else:
        bound_dt = None
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


def _normalize_date_bound_input(raw_bound: Any, *, is_end: bool) -> str:
    """Normalize a ``start_date`` or ``end_date`` bound into an ISO-8601 UTC string."""
    if isinstance(raw_bound, datetime):
        dt = (
            raw_bound.replace(tzinfo=UTC) if raw_bound.tzinfo is None else raw_bound.astimezone(UTC)
        )
        return format_iso_datetime(dt)
    if isinstance(raw_bound, date):
        return normalize_utc_timestamp_bound(raw_bound.isoformat(), is_end=is_end)
    if isinstance(raw_bound, str):
        return normalize_utc_timestamp_bound(raw_bound, is_end=is_end)
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
        return [("kwarg", None, op, val)]
    if isinstance(raw_val, list):
        if not raw_val:
            return []
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
                    entries.append(("kwarg", None, op, val))
                else:
                    _format_filter_value(item)
                    entries.append(("kwarg", None, "==", item))
            return entries
        for item in raw_val:
            _format_filter_value(item)
        return [("scalar_or_list", None, "==", list(raw_val))]
    _format_filter_value(raw_val)
    return [("kwarg", None, "==", raw_val)]


def split_revision_date_bounds(
    filters: Mapping[str, Any],
    *,
    start_date: str | datetime | date | None = None,
    end_date: str | datetime | date | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str, Any]]]:
    """Split secondary ``dateCreated`` and ``lastUpdated`` AND bounds for local evaluation.

    Ensures upstream requests contain at most one ``dateCreated`` clause and at
    most one ``lastUpdated`` clause per ``AND`` filter while evaluating all
    remaining date bounds locally.
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

    start_utc = (
        _normalize_date_bound_input(eff_start, is_end=False) if eff_start is not None else None
    )
    end_utc = _normalize_date_bound_input(eff_end, is_end=True) if eff_end is not None else None

    raw_filter = working.get("filter")
    has_active_raw_filter = (
        isinstance(raw_filter, str)
        and bool(raw_filter.strip())
        and raw_filter.strip().lower() != "none"
    )

    has_dual_kwarg_keys = any(
        camel in working and snake in working for camel, snake in _REVISION_DATE_SPECS
    )
    has_tuple_list_kwarg = any(
        isinstance(working.get(k), list) and any(isinstance(x, tuple) for x in working[k])
        for camel, snake in _REVISION_DATE_SPECS
        for k in (camel, snake)
        if k in working
    )
    has_any_date_kwarg = any(
        k in working for camel, snake in _REVISION_DATE_SPECS for k in (camel, snake)
    )

    raw_has_duplicate_date = False
    if has_active_raw_filter and isinstance(raw_filter, str):
        has_semi, has_comma, has_ws, in_quotes = _scan_filter_string(raw_filter)
        if not in_quotes and not (has_semi and has_comma) and not has_ws and not has_comma:
            clauses_preview, conn_preview = _split_filter_clauses(raw_filter.strip())
            if conn_preview == ";":
                seen_preview: set[str] = set()
                for cl in clauses_preview:
                    parsed_cl = _parse_clause_operator(cl)
                    if parsed_cl is not None:
                        c_attr = _snake_to_camel(parsed_cl[0])
                        if c_attr in REVISION_DATE_ATTRIBUTES and c_attr in seen_preview:
                            raw_has_duplicate_date = True
                            break
                        seen_preview.add(c_attr)

    needs_split = (
        start_utc is not None
        or end_utc is not None
        or has_dual_kwarg_keys
        or has_tuple_list_kwarg
        or raw_has_duplicate_date
        or (has_active_raw_filter and has_any_date_kwarg)
    )
    if not needs_split:
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
                    norm_clause = f"{camel_attr}{op}{raw_rhs}"
                    raw_date_entries[camel_attr].append(("raw", norm_clause, op, unquoted_rhs))
                else:
                    if camel_attr in seen_non_date:
                        raise ValueError(f"Duplicate filter attribute '{camel_attr}' in AND clause")
                    seen_non_date.add(camel_attr)
                    non_date_raw_clauses.append(clause)

    upstream_raw_clauses = list(non_date_raw_clauses)

    for camel_key, snake_key in _REVISION_DATE_SPECS:
        combined_entries: list[tuple[str, str | None, str, Any]] = list(raw_date_entries[camel_key])
        preserved_single_key: str | None = None
        for key_variant in (snake_key, camel_key):
            if key_variant in working:
                preserved_single_key = key_variant
                val = working.pop(key_variant)
                combined_entries.extend(_extract_kwarg_date_bounds(key_variant, val))

        if camel_key == "dateCreated":
            if start_utc is not None:
                combined_entries.append(("kwarg", None, ">=", start_utc))
            if end_utc is not None:
                combined_entries.append(("kwarg", None, "<=", end_utc))

        if not combined_entries:
            continue

        if (
            len(combined_entries) == 1
            and combined_entries[0][0] == "scalar_or_list"
            and not has_active_raw_filter
        ):
            working[preserved_single_key or camel_key] = combined_entries[0][3]
            continue

        if raw_is_or:
            for kind, _, op, val in combined_entries:
                if kind == "scalar_or_list":
                    raise ValueError(
                        f"Cannot combine scalar OR list on '{camel_key}' with an existing OR filter"
                    )
                local_bounds.append((snake_key, op, val))
            continue

        first_kind, first_raw, first_op, first_val = combined_entries[0]
        if first_kind == "scalar_or_list":
            raise ValueError(
                f"Filter expression cannot combine ';' (AND) bounds with ',' (OR) list on '{camel_key}'"
            )

        if has_active_raw_filter:
            clause_str = (
                first_raw
                if first_raw is not None
                else _build_filter_part(camel_key, (first_op, first_val))
            )
            upstream_raw_clauses.append(clause_str)
        else:
            working[camel_key] = (first_op, first_val)

        if len(combined_entries) > 1:
            for kind, _, op, val in combined_entries:
                if kind == "scalar_or_list":
                    raise ValueError(
                        f"Filter expression cannot combine ';' (AND) bounds with ',' (OR) list on '{camel_key}'"
                    )
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
