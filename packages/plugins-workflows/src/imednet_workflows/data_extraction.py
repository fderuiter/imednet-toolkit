"""Provides workflows for extracting specific datasets from iMednet studies."""

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from imednet.spi.models import Record, RecordRevision
from imednet.spi.utils import (
    ALLOWED_OPERATORS,
    format_iso_datetime,
    normalize_utc_timestamp_bound,
    parse_iso_datetime,
)

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


def _parse_revision_utc_datetime(raw_value: Any) -> datetime | None:
    """Parse a RecordRevision ``date_created`` value into a UTC-aware datetime."""
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        return parse_iso_datetime(format_iso_datetime(raw_value))
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped:
            return None
        try:
            normalized = normalize_utc_timestamp_bound(stripped, is_end=False)
            return parse_iso_datetime(normalized)
        except ValueError:
            return None
    return None


def _normalize_date_predicate_tuple(
    operator: str,
    raw_value: Any,
) -> tuple[tuple[str, str], Callable[[RecordRevision, datetime | None], bool]]:
    """Normalize one ``(operator, value)`` predicate on ``date_created``."""
    if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
        raise ValueError(
            f"Invalid filter operator '{operator}'. Allowed operators: {sorted(ALLOWED_OPERATORS)}"
        )

    if operator == "=~":
        pattern = str(raw_value)

        def _matches_contains(rev: RecordRevision, rev_dt: datetime | None) -> bool:
            raw_rev = getattr(rev, "date_created", None)
            if rev_dt is None or raw_rev is None:
                return False
            return pattern in str(raw_rev)

        return ("=~", pattern), _matches_contains

    if isinstance(raw_value, datetime):
        utc_str = format_iso_datetime(raw_value)
    else:
        is_end_bound = operator in {"<=", "<"}
        utc_str = normalize_utc_timestamp_bound(str(raw_value), is_end=is_end_bound)
    bound_dt = parse_iso_datetime(utc_str)

    def _matches_comparison(_rev: RecordRevision, rev_dt: datetime | None) -> bool:
        if rev_dt is None:
            return False
        if operator == "==":
            return rev_dt == bound_dt
        if operator == "!=":
            return rev_dt != bound_dt
        if operator == ">=":
            return rev_dt >= bound_dt
        if operator == ">":
            return rev_dt > bound_dt
        if operator == "<=":
            return rev_dt <= bound_dt
        return rev_dt < bound_dt

    return (operator, utc_str), _matches_comparison


def _select_single_date_filter_tuple(
    candidates: list[tuple[str, str]],
) -> tuple[str, str] | None:
    """Choose at most one ``date_created`` operator tuple for the API request."""
    if not candidates:
        return None

    for op, val in candidates:
        if op == "==":
            return (op, val)

    lower_bounds = [(op, val) for op, val in candidates if op in {">=", ">"}]
    if lower_bounds:
        return max(
            lower_bounds,
            key=lambda item: (parse_iso_datetime(item[1]), 1 if item[0] == ">" else 0),
        )

    upper_bounds = [(op, val) for op, val in candidates if op in {"<=", "<"}]
    if upper_bounds:
        return min(
            upper_bounds,
            key=lambda item: (parse_iso_datetime(item[1]), 0 if item[0] == "<" else 1),
        )

    return candidates[0]


class DataExtractionWorkflow:
    """Provides methods for complex data extraction tasks involving multiple iMednet endpoints.

    Args:
        sdk: An instance of the ImednetSDK.
    """

    def __init__(self, sdk: "ImednetFacade"):
        """Initialize the data extraction workflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
        """
        self._sdk = sdk

    def extract_records_by_criteria(
        self,
        study_key: str,
        record_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        subject_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        visit_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        on_progress: Callable[[str], None] | None = None,
        **other_filters: Any,
    ) -> list[Record]:
        """Extracts records based on criteria spanning subjects, visits, and records.

        Args:
            study_key: The key identifying the study.
            record_filter: Dictionary of conditions for the records endpoint.
            subject_filter: Dictionary of conditions for the subjects endpoint.
            visit_filter: Dictionary of conditions for the visits endpoint.
            on_progress: Optional callback invoked with progress status strings.
            **other_filters: Additional keyword arguments passed as filters to the
                             records endpoint `list` method.

        Returns:
            A list of Record objects matching all specified criteria.
        """
        matching_subject_keys: set[str] | None = None
        if subject_filter:
            if on_progress:
                on_progress("Querying subjects matching filter criteria...")
            subjects = self._sdk.get_subjects(study_key, **subject_filter)
            matching_subject_keys = {s.subject_key for s in subjects}  # type: ignore
            if on_progress:
                on_progress(f"Retrieved {len(matching_subject_keys)} matching subjects.")
            if not matching_subject_keys:
                if on_progress:
                    on_progress("No subjects matched criteria; returning 0 records.")
                return []

        matching_visit_ids: set[int] | None = None
        if visit_filter:
            # Client-side filtering for subject_key on visits is still needed
            # as build_filter_string doesn't handle complex AND/OR structures easily
            # from separate filter dictionaries.
            if on_progress:
                on_progress("Querying visits matching filter criteria...")
            visits = self._sdk.get_visits(study_key, **visit_filter)

            if matching_subject_keys:
                visits = [v for v in visits if v.subject_key in matching_subject_keys]

            matching_visit_ids = {v.visit_id for v in visits}  # type: ignore
            if on_progress:
                on_progress(f"Retrieved {len(matching_visit_ids)} matching visits.")
            if not matching_visit_ids:
                if on_progress:
                    on_progress("No visits matched criteria; returning 0 records.")
                return []

        # Build the final record filter dictionary
        final_record_filter_dict = dict(record_filter) if record_filter else {}
        final_record_filter_dict.update(other_filters)  # Add other_filters here

        # Client-side filtering is used below for subject/visit matching,
        # so no need to add complex 'in' clauses here even if build_filter_string supported it.

        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")
        records = self._sdk.get_records(
            study_key=study_key,
            record_data_filter=None,
            **final_record_filter_dict,
        )

        # Client-side filtering fallback
        if matching_subject_keys:
            if on_progress:
                on_progress("Filtering records by matched subject keys...")
            records = [r for r in records if r.subject_key in matching_subject_keys]
        # Corrected attribute from visit_oid to visit_id and variable name
        if matching_visit_ids:
            if on_progress:
                on_progress("Filtering records by matched visit IDs...")
            records = [r for r in records if r.visit_id in matching_visit_ids]

        result_records = list(records)
        if on_progress:
            on_progress(f"Extraction complete: found {len(result_records)} matching records.")

        return result_records

    def extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **filters: Any,
    ) -> list[RecordRevision]:
        """Extracts the audit trail (record revisions) based on specified filters.

        Args:
            study_key: The key identifying the study.
            start_date: Optional inclusive lower UTC date/timestamp bound for ``dateCreated``.
            end_date: Optional inclusive upper UTC date/timestamp bound for ``dateCreated``.
            user_filter: Optional dictionary of base filter conditions.
            **filters: Additional key-value pairs to be added as equality filters.

        Returns:
            A list of RecordRevision objects matching the criteria.
        """
        final_filter_dict: dict[str, Any] = {}
        caller_date_entries: list[Any] = []

        for source_dict in (user_filter, filters):
            if not source_dict:
                continue
            for key, val in source_dict.items():
                if key in {"date_created", "dateCreated"}:
                    caller_date_entries.append(val)
                else:
                    final_filter_dict[key] = val

        server_candidates: list[tuple[str, str]] = []
        python_predicates: list[Callable[[RecordRevision, datetime | None], bool]] = []

        for entry in caller_date_entries:
            if isinstance(entry, tuple):
                if len(entry) != 2:
                    raise ValueError(
                        f"Filter tuple for 'date_created' must have exactly 2 elements "
                        f"(operator, value), got {len(entry)}"
                    )
                cand, pred = _normalize_date_predicate_tuple(entry[0], entry[1])
                server_candidates.append(cand)
                python_predicates.append(pred)
            elif isinstance(entry, list):
                if not entry:
                    continue
                if any(isinstance(item, tuple) for item in entry):
                    if not all(isinstance(item, tuple) for item in entry):
                        raise ValueError(
                            "Filter list for 'date_created' cannot mix operator tuples and scalar values"
                        )
                    for tup in entry:
                        if len(tup) != 2:
                            raise ValueError(
                                f"Filter tuple for 'date_created' must have exactly 2 elements "
                                f"(operator, value), got {len(tup)}"
                            )
                        cand, pred = _normalize_date_predicate_tuple(tup[0], tup[1])
                        server_candidates.append(cand)
                        python_predicates.append(pred)
                else:
                    normalized_scalars: list[str] = []
                    target_dts: set[datetime] = set()
                    for scalar in entry:
                        utc_val = (
                            format_iso_datetime(scalar)
                            if isinstance(scalar, datetime)
                            else normalize_utc_timestamp_bound(str(scalar), is_end=False)
                        )
                        normalized_scalars.append(utc_val)
                        target_dts.add(parse_iso_datetime(utc_val))
                    if len(normalized_scalars) == 1:
                        server_candidates.append(("==", normalized_scalars[0]))

                    def _matches_any_scalar(
                        _rev: RecordRevision,
                        rev_dt: datetime | None,
                        allowed: set[datetime] = target_dts,
                    ) -> bool:
                        return rev_dt is not None and rev_dt in allowed

                    python_predicates.append(_matches_any_scalar)
            else:
                cand, pred = _normalize_date_predicate_tuple("==", entry)
                server_candidates.append(cand)
                python_predicates.append(pred)

        if start_date is not None:
            cand, pred = _normalize_date_predicate_tuple(">=", start_date)
            server_candidates.append(cand)
            python_predicates.append(pred)

        if end_date is not None:
            cand, pred = _normalize_date_predicate_tuple("<=", end_date)
            server_candidates.append(cand)
            python_predicates.append(pred)

        selected_tuple = _select_single_date_filter_tuple(server_candidates)
        if selected_tuple is not None:
            final_filter_dict["date_created"] = selected_tuple

        revisions = list(
            self._sdk.get_record_revisions(
                study_key,
                **final_filter_dict,
            )
        )

        if not python_predicates:
            return revisions

        filtered_revisions: list[RecordRevision] = []
        for revision in revisions:
            rev_dt = _parse_revision_utc_datetime(getattr(revision, "date_created", None))
            if rev_dt is None:
                continue
            if all(predicate(revision, rev_dt) for predicate in python_predicates):
                filtered_revisions.append(revision)

        return filtered_revisions


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
