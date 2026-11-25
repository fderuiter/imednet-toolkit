"""Record export timestamp normalization, multi-bound reduction, and post-predicates."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from imednet.utils.dates import (
    format_iso_datetime,
    normalize_utc_timestamp_bound,
    parse_iso_datetime,
)
from imednet.utils.filters import _snake_to_camel, validate_filter_operator

RECORD_TIMESTAMP_ATTRIBUTES: frozenset[str] = frozenset(
    {"dateCreated", "dateModified", "lastUpdated"}
)

_RECORD_TIMESTAMP_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "dateModified": ("date_modified", "dateModified", "last_updated", "lastUpdated"),
    "dateCreated": ("date_created", "dateCreated"),
    "lastUpdated": ("last_updated", "lastUpdated"),
}


def normalize_watermark_datetime(value: datetime | str | None) -> datetime | None:
    """Normalize a datetime or ISO-8601 date/timestamp string into UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = parse_iso_datetime(stripped)
        except ValueError:
            normalized_iso = normalize_utc_timestamp_bound(stripped, is_end=False)
            parsed = parse_iso_datetime(normalized_iso)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise ValueError(f"Watermark must be a datetime or ISO-8601 string, got {type(value).__name__}")


def _as_condition_tuples(value: Any) -> list[tuple[str, Any]]:
    """Convert scalar, 2-tuple, or list filter values into ``[(operator, value), ...]``."""
    if isinstance(value, list):
        items: list[tuple[str, Any]] = []
        for item in value:
            if isinstance(item, tuple):
                if len(item) != 2:
                    raise ValueError(f"Filter tuple must have 2 elements, got {len(item)}")
                items.append((item[0], item[1]))
            else:
                items.append(("==", item))
        return items
    if isinstance(value, tuple):
        if len(value) != 2:
            raise ValueError(f"Filter tuple must have 2 elements, got {len(value)}")
        return [(value[0], value[1])]
    return [("==", value)]


def _merge_and_condition(target: dict[str, Any], key: str, condition: Any) -> None:
    """Merge an AND filter condition onto ``target``, combining timestamp multi-bounds."""
    camel_key = _snake_to_camel(key)
    for existing in list(target):
        if _snake_to_camel(existing) == camel_key:
            if camel_key in RECORD_TIMESTAMP_ATTRIBUTES:
                target[camel_key] = _as_condition_tuples(target.pop(existing)) + (
                    _as_condition_tuples(condition)
                )
                return
            raise ValueError(f"Duplicate filter attribute '{camel_key}' in AND clause")
    target[key] = condition


def _extract_record_attribute_timestamp(
    record: Any, attr_candidates: tuple[str, ...]
) -> datetime | None:
    """Extract a whole-second UTC timestamp for the first matching attribute on ``record``."""
    for attr in attr_candidates:
        raw = record.get(attr) if isinstance(record, Mapping) else getattr(record, attr, None)
        if isinstance(raw, bool) or raw is None:
            continue
        if isinstance(raw, (datetime, str)):
            try:
                ts = normalize_watermark_datetime(raw)
            except ValueError:
                continue
            if ts is not None:
                return ts.replace(microsecond=0)
        elif isinstance(raw, date):
            return datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    return None


def _coerce_record_timestamp_clauses(
    raw_cond: Any, *, attr_name: str = "dateModified"
) -> list[tuple[str, datetime]]:
    """Normalize a branch timestamp condition into whole-second ``[(operator, utc_datetime), ...]``."""
    raw_items: list[tuple[str, Any]]
    if isinstance(raw_cond, list):
        raw_items = []
        for item in raw_cond:
            if isinstance(item, tuple):
                if len(item) != 2:
                    raise ValueError(
                        f"Filter tuple for '{attr_name}' must have 2 elements, got {len(item)}"
                    )
                raw_items.append((item[0], item[1]))
            else:
                raw_items.append(("==", item))
    elif isinstance(raw_cond, tuple):
        if len(raw_cond) != 2:
            raise ValueError(
                f"Filter tuple for '{attr_name}' must have 2 elements, got {len(raw_cond)}"
            )
        raw_items = [(raw_cond[0], raw_cond[1])]
    else:
        raw_items = [("==", raw_cond)]

    parsed: list[tuple[str, datetime]] = []
    for op, val in raw_items:
        validate_filter_operator(op, allow_regex=False)
        is_end = op in ("<", "<=")
        if isinstance(val, bool) or val is None:
            raise ValueError(f"Invalid {attr_name} filter value: {val!r}")
        if isinstance(val, datetime):
            dt = parse_iso_datetime(format_iso_datetime(val))
        elif isinstance(val, date):
            dt = parse_iso_datetime(normalize_utc_timestamp_bound(val.isoformat(), is_end=is_end))
        elif isinstance(val, str):
            if not val.strip():
                raise ValueError(f"Invalid {attr_name} filter value: {val!r}")
            dt = parse_iso_datetime(normalize_utc_timestamp_bound(val, is_end=is_end))
        else:
            raise ValueError(f"Invalid {attr_name} filter value: {val!r}")
        parsed.append((op, dt))
    return parsed


def _reduce_timestamp_clauses(
    clauses: Sequence[tuple[str, datetime]], *, attr_name: str
) -> tuple[tuple[str, str], Callable[[Any], bool] | None] | None:
    """Reduce timestamp clauses to one server comparison and an optional local post-predicate."""
    lower: tuple[datetime, bool] | None = None
    upper: tuple[datetime, bool] | None = None
    exact: datetime | None = None
    excluded: set[datetime] = set()

    for op, dt in clauses:
        if op == "==":
            if exact is not None and exact != dt:
                return None
            exact = dt
        elif op in (">=", ">"):
            strict = op == ">"
            if lower is None or dt > lower[0] or (dt == lower[0] and strict and not lower[1]):
                lower = (dt, strict)
        elif op in ("<=", "<"):
            strict = op == "<"
            if upper is None or dt < upper[0] or (dt == upper[0] and strict and not upper[1]):
                upper = (dt, strict)
        elif op == "!=":
            excluded.add(dt)

    if lower is not None:
        for ex in list(excluded):
            if ex < lower[0] or (ex == lower[0] and lower[1]):
                excluded.discard(ex)
            elif ex == lower[0] and not lower[1]:
                lower = (lower[0], True)
                excluded.discard(ex)

    if upper is not None:
        for ex in list(excluded):
            if ex > upper[0] or (ex == upper[0] and upper[1]):
                excluded.discard(ex)
            elif ex == upper[0] and not upper[1]:
                upper = (upper[0], True)
                excluded.discard(ex)

    if exact is not None:
        if exact in excluded:
            return None
        if lower is not None and (exact < lower[0] or (exact == lower[0] and lower[1])):
            return None
        if upper is not None and (exact > upper[0] or (exact == upper[0] and upper[1])):
            return None
        return ("==", format_iso_datetime(exact)), None

    if lower is not None and upper is not None:
        if lower[0] > upper[0]:
            return None
        if lower[0] == upper[0]:
            if lower[1] or upper[1] or lower[0] in excluded:
                return None
            return ("==", format_iso_datetime(lower[0])), None

    residual_upper: tuple[datetime, bool] | None = None
    if lower is not None:
        server_cond = (">" if lower[1] else ">=", format_iso_datetime(lower[0]))
        residual_upper = upper
    elif upper is not None:
        server_cond = ("<" if upper[1] else "<=", format_iso_datetime(upper[0]))
    elif excluded:
        first_ex = min(excluded)
        excluded.discard(first_ex)
        server_cond = ("!=", format_iso_datetime(first_ex))
    else:
        return None

    if residual_upper is None and not excluded:
        return server_cond, None

    attr_candidates = _RECORD_TIMESTAMP_FIELD_CANDIDATES[attr_name]
    frozen_excluded = frozenset(excluded)

    def _attr_post_predicate(record: Any) -> bool:
        rec_ts = _extract_record_attribute_timestamp(record, attr_candidates)
        if rec_ts is None:
            return False
        if residual_upper is not None:
            bound_dt, is_strict = residual_upper
            if is_strict and not (rec_ts < bound_dt):
                return False
            if not is_strict and not (rec_ts <= bound_dt):
                return False
        return rec_ts not in frozen_excluded

    return server_cond, _attr_post_predicate


def _combine_branch_with_watermark(
    branch: dict[str, Any],
    norm_wm: datetime | None,
) -> tuple[dict[str, Any], Callable[[Any], bool] | None] | None:
    """Normalize Record timestamp bounds and combine with ``norm_wm``.

    Normalizes ``dateModified``, ``dateCreated``, and ``lastUpdated`` clauses
    whether ``norm_wm`` is present or absent, emits at most one server
    comparison per attribute, returns ``None`` when any attribute's bounds are
    unsatisfiable, and returns a post-predicate for residual upper or ``!=``
    clauses.
    """
    out = dict(branch)
    wm_floor = parse_iso_datetime(format_iso_datetime(norm_wm)) if norm_wm is not None else None
    predicates: list[Callable[[Any], bool]] = []

    for camel_attr in ("dateModified", "dateCreated", "lastUpdated"):
        matching_keys = [k for k in out if _snake_to_camel(k) == camel_attr]
        if len(matching_keys) > 1:
            raise ValueError(f"Duplicate filter attribute '{camel_attr}' in AND clause")

        clauses: list[tuple[str, datetime]] = []
        if matching_keys:
            raw_existing = out.pop(matching_keys[0])
            clauses.extend(_coerce_record_timestamp_clauses(raw_existing, attr_name=camel_attr))
        if camel_attr == "dateModified" and wm_floor is not None:
            clauses.append((">=", wm_floor))

        if not clauses:
            continue

        reduced = _reduce_timestamp_clauses(clauses, attr_name=camel_attr)
        if reduced is None:
            return None
        server_cond, attr_pred = reduced
        out[camel_attr] = server_cond
        if attr_pred is not None:
            predicates.append(attr_pred)

    if not predicates:
        return out, None
    if len(predicates) == 1:
        return out, predicates[0]

    def _combined_post_predicate(record: Any) -> bool:
        return all(pred(record) for pred in predicates)

    return out, _combined_post_predicate
