"""Stateless filter-bound and local set-filtering helpers for read workflows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from imednet.spi.models import Query
from imednet.spi.utils import (
    build_filter_string,
    normalize_utc_timestamp_bound,
    parse_filter_expressions,
)


def build_delta_watermark_filter(high_water_mark: str) -> str:
    """Build a whole-second UTC ``dateModified>=...`` filter string for incremental sync."""
    normalized_hwm = normalize_utc_timestamp_bound(high_water_mark, is_end=False)
    return build_filter_string({"date_modified": (">=", normalized_hwm)})


def plan_missing_record_id_bound(missing_ids: set[int]) -> int | tuple[str, int]:
    """Plan a single-attribute ``recordId`` filter (exact ``==`` or ``>= min``) for backfill."""
    if len(missing_ids) == 1:
        return next(iter(missing_ids))
    return (">=", min(missing_ids))


def _can_push_down_single_subject(
    subject_key: str,
    final_filter_dict: Mapping[str, Any],
    extra_kwargs: Mapping[str, Any],
) -> bool:
    """Return True when ``subject_key`` can be added upstream without connector/key conflicts."""
    combined_items = (*final_filter_dict.items(), *extra_kwargs.items())
    for key, val in combined_items:
        if key in ("subject_key", "subjectKey"):
            return False
        if (
            key != "filter"
            and isinstance(val, (list, set))
            and len(val) > 1
            and not any(isinstance(item, tuple) for item in val)
        ):
            return False

    subj_clause = build_filter_string({"subject_key": subject_key})
    for raw_flt in (final_filter_dict.get("filter"), extra_kwargs.get("filter")):
        if isinstance(raw_flt, str) and raw_flt.strip() and raw_flt.strip().lower() != "none":
            try:
                parse_filter_expressions([raw_flt, subj_clause])
            except ValueError:
                return False
    return True


def plan_site_subject_query_filters(
    subjects: Iterable[Any],
    additional_filter: Mapping[str, Any] | None,
    extra_kwargs: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any], bool]:
    """Plan upstream query filters and whether ``subjectKey`` must be filtered locally."""
    subject_keys = list(
        dict.fromkeys(
            key
            for s in subjects
            if getattr(s, "is_deleted", False) is not True
            and isinstance(s.subject_key, str)
            and (key := s.subject_key.strip())
        )
    )
    final_filter_dict: dict[str, Any] = dict(additional_filter) if additional_filter else {}
    if not subject_keys:
        return [], final_filter_dict, False

    if len(subject_keys) == 1 and _can_push_down_single_subject(
        subject_keys[0],
        final_filter_dict,
        extra_kwargs,
    ):
        final_filter_dict["subject_key"] = subject_keys[0]
        return subject_keys, final_filter_dict, False

    return subject_keys, final_filter_dict, True


def filter_queries_by_subject_keys(
    queries: Iterable[Query],
    subject_keys: Iterable[str],
) -> list[Query]:
    """Filter ``queries`` in memory to those matching trimmed ``subject_keys``."""
    allowed = set(subject_keys)
    return [
        q
        for q in queries
        if isinstance(q_sub := getattr(q, "subject_key", None), str) and q_sub.strip() in allowed
    ]


def build_single_item_variable_filters(
    form_whitelist: Sequence[int] | None,
    variable_whitelist: Sequence[str] | None,
) -> dict[str, Any]:
    """Emit scalar ``formId`` / ``variableName`` filters only for single-item whitelists."""
    filters: dict[str, Any] = {}
    if form_whitelist is not None:
        unique_forms = list(dict.fromkeys(form_whitelist))
        if len(unique_forms) == 1:
            filters["formId"] = unique_forms[0]
    if variable_whitelist is not None:
        unique_vars = list(dict.fromkeys(variable_whitelist))
        if len(unique_vars) == 1:
            filters["variableName"] = unique_vars[0]
    return filters
