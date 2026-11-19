"""Provides workflows for extracting specific datasets from iMednet studies."""

import asyncio
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from imednet.spi.models import Record, RecordRevision, Subject, Visit
from imednet.spi.utils import normalize_utc_timestamp_bound

from ._guards import ensure_async_sdk, ensure_sync_sdk

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

_OWNER = "DataExtractionWorkflow"


def _extract_subject_keys(
    subjects: Iterable[Subject],
    on_progress: Callable[[str], None] | None,
) -> set[str]:
    """Collect matching subject keys and report progress."""
    keys = {s.subject_key for s in subjects if s.subject_key is not None}
    if on_progress:
        on_progress(f"Retrieved {len(keys)} matching subjects.")
        if not keys:
            on_progress("No subjects matched criteria; returning 0 records.")
    return keys


def _extract_visit_ids(
    visits: Iterable[Visit],
    matching_subject_keys: set[str] | None,
    on_progress: Callable[[str], None] | None,
) -> set[int]:
    """Collect matching visit IDs (filtered by matched subject keys) and report progress."""
    visit_list = list(visits)
    if matching_subject_keys:
        visit_list = [v for v in visit_list if v.subject_key in matching_subject_keys]
    ids = {v.visit_id for v in visit_list if v.visit_id is not None}
    if on_progress:
        on_progress(f"Retrieved {len(ids)} matching visits.")
        if not ids:
            on_progress("No visits matched criteria; returning 0 records.")
    return ids


def _filter_extracted_records(
    records: Iterable[Record],
    matching_subject_keys: set[str] | None,
    matching_visit_ids: set[int] | None,
    on_progress: Callable[[str], None] | None,
) -> list[Record]:
    """Apply client-side subject and visit filtering to fetched records."""
    result_records = list(records)
    if matching_subject_keys:
        if on_progress:
            on_progress("Filtering records by matched subject keys...")
        result_records = [r for r in result_records if r.subject_key in matching_subject_keys]
    if matching_visit_ids:
        if on_progress:
            on_progress("Filtering records by matched visit IDs...")
        result_records = [r for r in result_records if r.visit_id in matching_visit_ids]
    if on_progress:
        on_progress(f"Extraction complete: found {len(result_records)} matching records.")
    return result_records


def _build_record_filters(
    record_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None,
    other_filters: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``record_filter`` and keyword ``other_filters`` into one dictionary."""
    merged = dict(record_filter) if record_filter else {}
    merged.update(other_filters)
    return merged


def _build_audit_trail_kwargs(
    start_date: str | None,
    end_date: str | None,
    user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None,
    filters: dict[str, Any],
) -> dict[str, Any]:
    """Build combined filter and UTC ``dateCreated`` bounds for ``get_record_revisions``."""
    final_filter_dict: dict[str, Any] = dict(user_filter) if user_filter else {}
    final_filter_dict.update(filters)

    if start_date is not None or end_date is not None:
        date_bounds: list[tuple[str, Any]] = []
        for key_variant in ("date_created", "dateCreated"):
            if key_variant in final_filter_dict:
                existing = final_filter_dict.pop(key_variant)
                if isinstance(existing, list):
                    for item in existing:
                        date_bounds.append(item if isinstance(item, tuple) else ("==", item))
                elif isinstance(existing, tuple):
                    date_bounds.append(existing)
                else:
                    date_bounds.append(("==", existing))

        if start_date is not None:
            start_utc = normalize_utc_timestamp_bound(start_date, is_end=False)
            date_bounds.append((">=", start_utc))
        if end_date is not None:
            end_utc = normalize_utc_timestamp_bound(end_date, is_end=True)
            date_bounds.append(("<=", end_utc))

        final_filter_dict["dateCreated"] = date_bounds[0] if len(date_bounds) == 1 else date_bounds

    return final_filter_dict


class DataExtractionWorkflow:
    """Provides methods for complex data extraction tasks involving multiple iMednet endpoints.

    Args:
        sdk: An instance of the ImednetSDK or AsyncImednetSDK.
    """

    def __init__(self, sdk: "ImednetFacade | AsyncImednetFacade"):
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
        """Extracts records based on criteria spanning subjects, visits, and records."""
        ensure_sync_sdk(
            self._sdk,
            _OWNER,
            "extract_records_by_criteria",
            "async_extract_records_by_criteria",
        )
        sdk = cast("ImednetFacade", self._sdk)

        matching_subject_keys: set[str] | None = None
        if subject_filter:
            if on_progress:
                on_progress("Querying subjects matching filter criteria...")
            subjects = sdk.get_subjects(study_key, **subject_filter)
            matching_subject_keys = _extract_subject_keys(subjects, on_progress)
            if not matching_subject_keys:
                return []

        matching_visit_ids: set[int] | None = None
        if visit_filter:
            if on_progress:
                on_progress("Querying visits matching filter criteria...")
            visits = sdk.get_visits(study_key, **visit_filter)
            matching_visit_ids = _extract_visit_ids(visits, matching_subject_keys, on_progress)
            if not matching_visit_ids:
                return []

        final_record_filter_dict = _build_record_filters(record_filter, other_filters)
        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")
        records = sdk.get_records(
            study_key=study_key,
            record_data_filter=None,
            **final_record_filter_dict,
        )
        return _filter_extracted_records(
            records, matching_subject_keys, matching_visit_ids, on_progress
        )

    async def async_extract_records_by_criteria(
        self,
        study_key: str,
        record_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        subject_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        visit_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        on_progress: Callable[[str], None] | None = None,
        **other_filters: Any,
    ) -> list[Record]:
        """Asynchronously extract records, fetching independent study resources concurrently."""
        ensure_async_sdk(
            self._sdk,
            _OWNER,
            "async_extract_records_by_criteria",
            "extract_records_by_criteria",
        )
        sdk = cast("AsyncImednetFacade", self._sdk)

        matching_subject_keys: set[str] | None = None
        matching_visit_ids: set[int] | None = None

        if subject_filter and visit_filter:
            if on_progress:
                on_progress("Querying subjects and visits matching filter criteria...")
            subjects, visits = await asyncio.gather(
                sdk.async_get_subjects(study_key, **subject_filter),
                sdk.async_get_visits(study_key, **visit_filter),
            )
            matching_subject_keys = _extract_subject_keys(subjects, on_progress)
            if not matching_subject_keys:
                return []
            matching_visit_ids = _extract_visit_ids(visits, matching_subject_keys, on_progress)
            if not matching_visit_ids:
                return []
        elif subject_filter:
            if on_progress:
                on_progress("Querying subjects matching filter criteria...")
            subjects = await sdk.async_get_subjects(study_key, **subject_filter)
            matching_subject_keys = _extract_subject_keys(subjects, on_progress)
            if not matching_subject_keys:
                return []
        elif visit_filter:
            if on_progress:
                on_progress("Querying visits matching filter criteria...")
            visits = await sdk.async_get_visits(study_key, **visit_filter)
            matching_visit_ids = _extract_visit_ids(visits, None, on_progress)
            if not matching_visit_ids:
                return []

        final_record_filter_dict = _build_record_filters(record_filter, other_filters)
        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")
        records = await sdk.async_get_records(
            study_key=study_key,
            record_data_filter=None,
            **final_record_filter_dict,
        )
        return _filter_extracted_records(
            records, matching_subject_keys, matching_visit_ids, on_progress
        )

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
        ensure_sync_sdk(self._sdk, _OWNER, "extract_audit_trail", "async_extract_audit_trail")
        sdk = cast("ImednetFacade", self._sdk)
        call_kwargs = _build_audit_trail_kwargs(start_date, end_date, user_filter, filters)
        return list(sdk.get_record_revisions(study_key, **call_kwargs))

    async def async_extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **filters: Any,
    ) -> list[RecordRevision]:
        """Asynchronously extract the audit trail (record revisions) based on specified filters.

        Args:
            study_key: The key identifying the study.
            start_date: Optional inclusive lower UTC date/timestamp bound for ``dateCreated``.
            end_date: Optional inclusive upper UTC date/timestamp bound for ``dateCreated``.
            user_filter: Optional dictionary of base filter conditions.
            **filters: Additional key-value pairs to be added as equality filters.

        Returns:
            A list of RecordRevision objects matching the criteria.
        """
        ensure_async_sdk(self._sdk, _OWNER, "async_extract_audit_trail", "extract_audit_trail")
        sdk = cast("AsyncImednetFacade", self._sdk)
        call_kwargs = _build_audit_trail_kwargs(start_date, end_date, user_filter, filters)
        return list(await sdk.async_get_record_revisions(study_key, **call_kwargs))


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
