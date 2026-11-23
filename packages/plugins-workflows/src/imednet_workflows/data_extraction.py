"""Provides workflows for extracting specific datasets from iMednet studies."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from imednet.spi.models import Record, RecordRevision
from imednet.spi.sink_base import is_record_deleted
from imednet.spi.utils import parse_iso_datetime, resolve_utc_timestamp_bound

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


def _to_utc_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 string, date, or datetime into a timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            dt = parse_iso_datetime(stripped)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _matches_date_bound(actual_raw: Any, op: str, expected_raw: Any) -> bool:
    """Evaluate a UTC dateCreated comparison predicate locally."""
    if op not in ("==", "=", "!=", "<>", "<", "<=", ">", ">="):
        raise ValueError(f"Unsupported dateCreated filter operator: {op!r}")

    actual_dt = _to_utc_datetime(actual_raw)
    if actual_dt is None:
        return op in ("!=", "<>")

    expected_dt = _to_utc_datetime(expected_raw)
    if expected_dt is None:
        actual_text = str(actual_raw)
        expected_text = str(expected_raw)
        if op in ("==", "="):
            return actual_text == expected_text
        if op in ("!=", "<>"):
            return actual_text != expected_text
        if op == "<":
            return actual_text < expected_text
        if op == "<=":
            return actual_text <= expected_text
        if op == ">":
            return actual_text > expected_text
        return actual_text >= expected_text

    if op in ("==", "="):
        return actual_dt == expected_dt
    if op in ("!=", "<>"):
        return actual_dt != expected_dt
    if op == "<":
        return actual_dt < expected_dt
    if op == "<=":
        return actual_dt <= expected_dt
    if op == ">":
        return actual_dt > expected_dt
    return actual_dt >= expected_dt


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
        *,
        include_deleted: bool = False,
        **other_filters: Any,
    ) -> list[Record]:
        """Extracts records based on criteria spanning subjects, visits, and records.

        Args:
            study_key: The key identifying the study.
            record_filter: Dictionary of conditions for the records endpoint.
            subject_filter: Dictionary of conditions for the subjects endpoint.
            visit_filter: Dictionary of conditions for the visits endpoint.
            on_progress: Optional callback invoked with progress status strings.
            include_deleted: When True, include voided (deleted=True) records.
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

        if not include_deleted:
            records = [r for r in records if not is_record_deleted(r)]

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

        Pushes a single ``dateCreated`` predicate to the iMednet REST endpoint to
        satisfy Common API unique AND-attribute rules and evaluates any additional
        UTC bounds (including half-open ``< next_day_00:00:00Z`` bounds for
        date-only ``end_date`` inputs) locally in Python.

        Args:
            study_key: The key identifying the study.
            start_date: Optional inclusive lower UTC date/timestamp bound for ``dateCreated``.
            end_date: Optional upper UTC date/timestamp bound for ``dateCreated``.
            user_filter: Optional dictionary of base filter conditions.
            **filters: Additional key-value pairs to be added as equality filters.

        Returns:
            A list of RecordRevision objects matching the criteria.
        """
        final_filter_dict: dict[str, Any] = dict(user_filter) if user_filter else {}
        final_filter_dict.update(filters)

        extra_bounds: list[tuple[str, Any]] = []
        has_explicit_date_key = (
            "date_created" in final_filter_dict or "dateCreated" in final_filter_dict
        )
        if start_date is not None or end_date is not None or has_explicit_date_key:
            explicit_values: list[Any] = []
            for key_variant in ("date_created", "dateCreated"):
                if key_variant in final_filter_dict:
                    explicit_values.append(final_filter_dict.pop(key_variant))

            # Preserve pure OR scalar lists when no range bounds or duplicate keys exist
            if (
                start_date is None
                and end_date is None
                and len(explicit_values) == 1
                and isinstance(explicit_values[0], list)
                and not any(isinstance(item, tuple) for item in explicit_values[0])
            ):
                final_filter_dict["dateCreated"] = explicit_values[0]
            else:
                date_bounds: list[tuple[str, Any]] = []
                if start_date is not None:
                    date_bounds.append(resolve_utc_timestamp_bound(start_date, is_end=False))
                if end_date is not None:
                    date_bounds.append(resolve_utc_timestamp_bound(end_date, is_end=True))

                for existing in explicit_values:
                    if isinstance(existing, list):
                        for item in existing:
                            date_bounds.append(item if isinstance(item, tuple) else ("==", item))
                    elif isinstance(existing, tuple):
                        date_bounds.append(existing)
                    else:
                        date_bounds.append(("==", existing))

                if date_bounds:
                    final_filter_dict["dateCreated"] = date_bounds[0]
                    extra_bounds = date_bounds[1:]

        revisions = list(
            self._sdk.get_record_revisions(
                study_key,
                **final_filter_dict,
            )
        )
        if extra_bounds:
            revisions = [
                rev
                for rev in revisions
                if all(
                    _matches_date_bound(getattr(rev, "date_created", None), op, bound_val)
                    for op, bound_val in extra_bounds
                )
            ]
        return revisions


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
