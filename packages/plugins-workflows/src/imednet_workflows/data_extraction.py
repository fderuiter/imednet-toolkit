"""Provides workflows for extracting specific datasets from iMednet studies."""

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any

from imednet.spi.cdisc import OdmAuditRecord, map_revisions_to_audit_records
from imednet.spi.models import Record, RecordRevision
from imednet.spi.sink_base import is_record_deleted
from imednet.spi.utils import (
    matches_revision_date_bound,
    matches_revision_date_bounds,
    parse_revision_timestamp_utc,
    resolve_enrichment_pipeline,
    split_revision_date_bounds,
)

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

_parse_date_created_utc = parse_revision_timestamp_utc
_matches_local_date_bound = matches_revision_date_bound


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

        upstream_filters, local_date_bounds = split_revision_date_bounds(
            final_filter_dict,
            start_date=start_date,
            end_date=end_date,
        )

        revisions = list(
            self._sdk.get_record_revisions(
                study_key,
                **upstream_filters,
            )
        )
        if local_date_bounds:
            revisions = [
                rev for rev in revisions if matches_revision_date_bounds(rev, local_date_bounds)
            ]
        return revisions

    def extract_odm_audit_records(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        default_location_oid: str | None = None,
        mask_phi: bool = True,
        phi_keys: Collection[str] | None = None,
        **filters: Any,
    ) -> list[OdmAuditRecord]:
        """Extract study RecordRevision audit history mapped to ODM 1.3.2 ``<AuditRecord>`` models."""
        revisions = self.extract_audit_trail(
            study_key=study_key,
            start_date=start_date,
            end_date=end_date,
            user_filter=user_filter,
            **filters,
        )
        resolved_phi: set[str] = set(phi_keys) if phi_keys is not None else set()
        if mask_phi:
            pipeline = resolve_enrichment_pipeline(study_key)
            if pipeline is not None and getattr(pipeline, "phi_keys", None):
                resolved_phi.update(pipeline.phi_keys)
        _by_rec, all_ordered = map_revisions_to_audit_records(
            revisions,
            default_location_oid=default_location_oid,
            mask_phi=mask_phi,
            custom_phi_keys=resolved_phi or None,
        )
        return all_ordered


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
