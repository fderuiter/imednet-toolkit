"""Provides workflows for extracting specific datasets from iMednet studies."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from imednet.spi.models import Record, RecordRevision
from imednet.spi.utils import normalize_utc_timestamp_bound

from .chunked_pipeline import (
    DEFAULT_OR_FILTER_BATCH_SIZE,
    MAX_OR_FILTER_PUSHDOWN_ITEMS,
    apply_global_sort_and_page,
    iter_chunks,
    prepare_deferred_controls,
)

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

_PAGINATION_AND_SORT_CONTROLS = frozenset({"page", "size", "limit", "sort"})
_RECORD_TOP_LEVEL_CONTROLS = _PAGINATION_AND_SORT_CONTROLS | {
    "record_data_filter",
    "recordDataFilter",
}


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

    def _fetch_records_in_or_batches(
        self,
        study_key: str,
        field_name: str,
        values: list[Any],
        record_kwargs: dict[str, Any],
    ) -> tuple[list[Record], bool]:
        """Fetch records across bounded OR-filter chunks and deduplicate by record_id."""
        batches = list(iter_chunks(values, chunk_size=DEFAULT_OR_FILTER_BATCH_SIZE))
        if len(batches) == 1:
            batch_kwargs = {**record_kwargs, field_name: batches[0]}
            return list(self._sdk.get_records(study_key=study_key, **batch_kwargs)), False

        non_controls = {
            k: v for k, v in record_kwargs.items() if k not in _PAGINATION_AND_SORT_CONTROLS
        }
        controls = {k: v for k, v in record_kwargs.items() if k in _PAGINATION_AND_SORT_CONTROLS}
        deferred_record_kwargs = {**non_controls, **prepare_deferred_controls(controls)}

        results: list[Record] = []
        seen_record_ids: set[int] = set()
        for batch in batches:
            batch_kwargs = {**deferred_record_kwargs, field_name: batch}
            for record in self._sdk.get_records(study_key=study_key, **batch_kwargs):
                rec_id = getattr(record, "record_id", None)
                if rec_id is not None:
                    if rec_id in seen_record_ids:
                        continue
                    seen_record_ids.add(rec_id)
                results.append(record)
        return results, True

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

        When merging multiple OR-filter batches or applying cohort membership
        filtering client-side, ``sort`` and zero-based ``page`` controls are
        applied globally to the complete matched record set.

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
        ordered_subject_keys: list[str] | None = None
        matching_subject_keys: set[str] | None = None
        if subject_filter:
            if on_progress:
                on_progress("Querying subjects matching filter criteria...")
            subjects = self._sdk.get_subjects(study_key, **subject_filter)
            ordered_subject_keys = list(
                dict.fromkeys(s.subject_key for s in subjects if s.subject_key is not None)
            )
            matching_subject_keys = set(ordered_subject_keys)
            if on_progress:
                on_progress(f"Retrieved {len(matching_subject_keys)} matching subjects.")
            if not matching_subject_keys:
                if on_progress:
                    on_progress("No subjects matched criteria; returning 0 records.")
                return []

        ordered_visit_ids: list[int] | None = None
        matching_visit_ids: set[int] | None = None
        if visit_filter:
            if on_progress:
                on_progress("Querying visits matching filter criteria...")
            visits = self._sdk.get_visits(study_key, **visit_filter)

            if matching_subject_keys is not None:
                visits = [v for v in visits if v.subject_key in matching_subject_keys]

            ordered_visit_ids = list(
                dict.fromkeys(v.visit_id for v in visits if v.visit_id is not None)
            )
            matching_visit_ids = set(ordered_visit_ids)
            if on_progress:
                on_progress(f"Retrieved {len(matching_visit_ids)} matching visits.")
            if not matching_visit_ids:
                if on_progress:
                    on_progress("No visits matched criteria; returning 0 records.")
                return []

        final_record_filter_dict: dict[str, Any] = dict(record_filter) if record_filter else {}
        final_record_filter_dict.update(other_filters)
        if (
            "record_data_filter" not in final_record_filter_dict
            and "recordDataFilter" not in final_record_filter_dict
        ):
            final_record_filter_dict["record_data_filter"] = None

        has_record_attr_filters = any(
            key not in _RECORD_TOP_LEVEL_CONTROLS for key in final_record_filter_dict
        )
        control_kwargs = {
            k: v for k, v in final_record_filter_dict.items() if k in _PAGINATION_AND_SORT_CONTROLS
        }
        has_cohort_filter = matching_subject_keys is not None or matching_visit_ids is not None

        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")

        records: list[Record]
        used_multi_batch = False
        used_deferred_controls = False
        if not has_record_attr_filters and (
            ordered_visit_ids is not None and len(ordered_visit_ids) <= MAX_OR_FILTER_PUSHDOWN_ITEMS
        ):
            records, used_multi_batch = self._fetch_records_in_or_batches(
                study_key=study_key,
                field_name="visit_id",
                values=ordered_visit_ids,
                record_kwargs=final_record_filter_dict,
            )
            used_deferred_controls = used_multi_batch
        elif not has_record_attr_filters and (
            ordered_subject_keys is not None
            and len(ordered_subject_keys) <= MAX_OR_FILTER_PUSHDOWN_ITEMS
        ):
            records, used_multi_batch = self._fetch_records_in_or_batches(
                study_key=study_key,
                field_name="subject_key",
                values=ordered_subject_keys,
                record_kwargs=final_record_filter_dict,
            )
            used_deferred_controls = used_multi_batch
        else:
            if has_cohort_filter:
                non_controls = {
                    k: v
                    for k, v in final_record_filter_dict.items()
                    if k not in _PAGINATION_AND_SORT_CONTROLS
                }
                backend_kwargs = {**non_controls, **prepare_deferred_controls(control_kwargs)}
                used_deferred_controls = True
            else:
                backend_kwargs = final_record_filter_dict
            records = list(
                self._sdk.get_records(
                    study_key=study_key,
                    **backend_kwargs,
                )
            )

        if matching_subject_keys is not None:
            if on_progress:
                on_progress("Filtering records by matched subject keys...")
            records = [r for r in records if r.subject_key in matching_subject_keys]

        if matching_visit_ids is not None:
            if on_progress:
                on_progress("Filtering records by matched visit IDs...")
            records = [r for r in records if r.visit_id in matching_visit_ids]

        result_records = (
            apply_global_sort_and_page(
                records,
                control_kwargs,
                default_sort_field="record_id",
                enforce_default_sort=used_multi_batch,
            )
            if used_deferred_controls
            else list(records)
        )
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

            final_filter_dict["dateCreated"] = (
                date_bounds[0] if len(date_bounds) == 1 else date_bounds
            )

        revisions = self._sdk.get_record_revisions(
            study_key,
            **final_filter_dict,
        )
        return list(revisions)


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
