"""Provides workflows for extracting specific datasets from iMednet studies."""

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from imednet.spi.models import Record, RecordRevision
from imednet.spi.sink_base import extract_record_id, is_record_deleted
from imednet.spi.utils import (
    REVISION_DATE_ATTRIBUTES,
    expand_filter_branches,
    iter_branched_results,
    matches_revision_date_bound,
    matches_revision_date_bounds,
    parse_revision_timestamp_utc,
    split_revision_date_bounds,
)

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

_parse_date_created_utc = parse_revision_timestamp_utc
_matches_local_date_bound = matches_revision_date_bound
_REVISION_DATE_FILTER_KEYS: frozenset[str] = frozenset(
    {
        *REVISION_DATE_ATTRIBUTES,
        "date_created",
        "last_updated",
    }
)


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
        record_filter: dict[str, Any | tuple[str, Any] | list[Any]] | str | None = None,
        subject_filter: dict[str, Any | tuple[str, Any] | list[Any]] | str | None = None,
        visit_filter: dict[str, Any | tuple[str, Any] | list[Any]] | str | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        include_deleted: bool = False,
        **other_filters: Any,
    ) -> list[Record]:
        """Extracts records based on criteria spanning subjects, visits, and records.

        Args:
            study_key: The key identifying the study.
            record_filter: Dictionary or expression of conditions for the records endpoint.
            subject_filter: Dictionary or expression of conditions for the subjects endpoint.
            visit_filter: Dictionary or expression of conditions for the visits endpoint.
            on_progress: Optional callback invoked with progress status strings.
            include_deleted: When True, include voided (deleted=True) records.
            **other_filters: Additional keyword arguments passed as filters to the
                             records endpoint `list` method.

        Returns:
            A list of deduplicated Record objects matching all specified criteria.
        """
        matching_subject_keys: set[str] | None = None
        if subject_filter is not None and subject_filter != {}:
            subject_branches = expand_filter_branches(subject_filter)
            if not subject_branches:
                return []
            if on_progress:
                on_progress("Querying subjects matching filter criteria...")
            subjects = list(
                iter_branched_results(
                    lambda **branch: self._sdk.get_subjects(study_key, **branch),
                    subject_branches,
                    dedup_key=lambda s: getattr(s, "subject_key", None),
                )
            )
            matching_subject_keys = {
                sk for s in subjects if (sk := getattr(s, "subject_key", None)) is not None
            }
            if on_progress:
                on_progress(f"Retrieved {len(matching_subject_keys)} matching subjects.")
            if not matching_subject_keys:
                if on_progress:
                    on_progress("No subjects matched criteria; returning 0 records.")
                return []

        matching_visit_ids: set[int] | None = None
        if visit_filter is not None and visit_filter != {}:
            visit_branches = expand_filter_branches(visit_filter)
            if not visit_branches:
                return []
            if on_progress:
                on_progress("Querying visits matching filter criteria...")
            visits = list(
                iter_branched_results(
                    lambda **branch: self._sdk.get_visits(study_key, **branch),
                    visit_branches,
                    dedup_key=lambda v: getattr(v, "visit_id", None),
                )
            )

            if matching_subject_keys is not None:
                visits = [v for v in visits if v.subject_key in matching_subject_keys]

            matching_visit_ids = {
                vid for v in visits if (vid := getattr(v, "visit_id", None)) is not None
            }
            if on_progress:
                on_progress(f"Retrieved {len(matching_visit_ids)} matching visits.")
            if not matching_visit_ids:
                if on_progress:
                    on_progress("No visits matched criteria; returning 0 records.")
                return []

        if isinstance(record_filter, str):
            final_record_filter_dict: dict[str, Any] = {"filter": record_filter}
        else:
            final_record_filter_dict = dict(record_filter) if record_filter else {}
        final_record_filter_dict.update(other_filters)

        record_data_filter = final_record_filter_dict.pop("record_data_filter", None)
        if record_data_filter is None and "recordDataFilter" in final_record_filter_dict:
            record_data_filter = final_record_filter_dict.pop("recordDataFilter")

        record_branches = expand_filter_branches(final_record_filter_dict)
        if not record_branches:
            return []

        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")
        records: list[Record] = list(
            iter_branched_results(
                lambda **branch: self._sdk.get_records(
                    study_key=study_key,
                    record_data_filter=record_data_filter,
                    **branch,
                ),
                record_branches,
                dedup_key=extract_record_id,
            )
        )

        if not include_deleted:
            records = [r for r in records if not is_record_deleted(r)]

        if matching_subject_keys is not None:
            if on_progress:
                on_progress("Filtering records by matched subject keys...")
            records = [r for r in records if r.subject_key in matching_subject_keys]

        if matching_visit_ids is not None:
            if on_progress:
                on_progress("Filtering records by matched visit IDs...")
            records = [r for r in records if r.visit_id in matching_visit_ids]

        if on_progress:
            on_progress(f"Extraction complete: found {len(records)} matching records.")

        return records

    def extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | str | None = None,
        **filters: Any,
    ) -> list[RecordRevision]:
        """Extracts the audit trail (record revisions) based on specified filters.

        Args:
            study_key: The key identifying the study.
            start_date: Optional inclusive lower UTC date/timestamp bound for ``dateCreated``.
            end_date: Optional inclusive upper UTC date/timestamp bound for ``dateCreated``.
            user_filter: Optional dictionary or filter string of base filter conditions.
            **filters: Additional key-value pairs to be added as equality filters.

        Returns:
            A list of deduplicated RecordRevision objects matching the criteria.
        """
        if isinstance(user_filter, str):
            final_filter_dict: dict[str, Any] = {"filter": user_filter}
        else:
            final_filter_dict = dict(user_filter) if user_filter else {}
        final_filter_dict.update(filters)

        date_entries: dict[str, Any] = {}
        non_date_entries: dict[str, Any] = {}
        for key, val in final_filter_dict.items():
            if key in _REVISION_DATE_FILTER_KEYS:
                if isinstance(val, (list, set, frozenset)) and len(val) == 0:
                    return []
                date_entries[key] = val
            else:
                non_date_entries[key] = val

        base_branches = expand_filter_branches(
            non_date_entries,
            multi_bound_attributes=REVISION_DATE_ATTRIBUTES,
        )
        if not base_branches:
            return []

        prepared_branches: list[dict[str, Any]] = []
        for branch in base_branches:
            combined_branch = dict(branch)
            for d_key, d_val in date_entries.items():
                if d_key in combined_branch:
                    prev_val = combined_branch[d_key]
                    prev_items = list(prev_val) if isinstance(prev_val, list) else [prev_val]
                    next_items = list(d_val) if isinstance(d_val, list) else [d_val]
                    combined_branch[d_key] = prev_items + next_items
                else:
                    combined_branch[d_key] = d_val
            upstream_filters, local_date_bounds = split_revision_date_bounds(
                combined_branch,
                start_date=start_date,
                end_date=end_date,
            )
            prepared_branches.append(
                {
                    "_upstream": upstream_filters,
                    "_local_bounds": local_date_bounds,
                }
            )

        def _fetch_revision_branch(
            _upstream: dict[str, Any],
            _local_bounds: Any,
        ) -> Iterable[RecordRevision]:
            revs = self._sdk.get_record_revisions(study_key, **_upstream)
            if _local_bounds:
                return (rev for rev in revs if matches_revision_date_bounds(rev, _local_bounds))
            return revs

        return list(
            iter_branched_results(
                _fetch_revision_branch,
                prepared_branches,
                dedup_key=lambda rev: getattr(rev, "record_revision_id", None),
            )
        )


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
