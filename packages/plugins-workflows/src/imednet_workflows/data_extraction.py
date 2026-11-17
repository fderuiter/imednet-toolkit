"""Provides workflows for extracting specific datasets from iMednet studies."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from imednet.spi.models import Record, RecordRevision

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


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
            start_date: Optional start date filter (YYYY-MM-DD format expected by API).
            end_date: Optional end date filter (YYYY-MM-DD format expected by API).
            user_filter: Optional dictionary of base filter conditions.
            **filters: Additional key-value pairs to be added as equality filters.

        Returns:
            A list of RecordRevision objects matching the criteria.
        """
        # Start with the user_filter dict if provided, otherwise an empty dict
        final_filter_dict = dict(user_filter) if user_filter else {}

        # Add additional filters from kwargs
        final_filter_dict.update(filters)

        # Prepare keyword arguments for date filters if they exist
        date_kwargs = {}
        if start_date:
            date_kwargs["start_date"] = start_date
        if end_date:
            date_kwargs["end_date"] = end_date

        # Fetch record revisions
        revisions = self._sdk.get_record_revisions(
            study_key,
            **final_filter_dict,
            **date_kwargs,
        )
        return list(revisions)


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
