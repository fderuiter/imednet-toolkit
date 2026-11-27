"""Provides workflows for extracting specific datasets from iMednet studies."""

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any

from imednet.spi.cdisc import OdmAuditRecord, map_revisions_to_audit_records
from imednet.spi.models import Record, RecordRevision
from imednet.spi.sink_base import is_record_deleted
from imednet.spi.utils import (
    SyncToAsyncFacadeAdapter,
    call_async_facade_method,
    is_async_facade,
    matches_revision_date_bound,
    matches_revision_date_bounds,
    parse_revision_timestamp_utc,
    resolve_enrichment_pipeline,
    run_sync_over_async,
    sdk_governance_context,
    split_revision_date_bounds,
)

from ._workflow_helpers import ensure_sync_workflow_mode

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

_parse_date_created_utc = parse_revision_timestamp_utc
_matches_local_date_bound = matches_revision_date_bound


class AsyncDataExtractionWorkflow:
    """Canonical asynchronous workflow for multi-endpoint clinical data extraction."""

    supports_async = True

    def __init__(self, sdk: "AsyncImednetFacade") -> None:
        """Initialize the asynchronous data extraction workflow."""
        self._sdk = sdk

    async def extract_records_by_criteria(
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
        """Asynchronously extract records matching combined subject, visit, and record filters."""
        matching_subject_keys: set[str] | None = None
        if subject_filter:
            if on_progress:
                on_progress("Querying subjects matching filter criteria...")
            subjects = await call_async_facade_method(
                self._sdk, "get_subjects", study_key, **subject_filter
            )
            matching_subject_keys = {s.subject_key for s in subjects if s.subject_key is not None}
            if on_progress:
                on_progress(f"Retrieved {len(matching_subject_keys)} matching subjects.")
            if not matching_subject_keys:
                if on_progress:
                    on_progress("No subjects matched criteria; returning 0 records.")
                return []

        matching_visit_ids: set[int] | None = None
        if visit_filter:
            if on_progress:
                on_progress("Querying visits matching filter criteria...")
            visits = list(
                await call_async_facade_method(self._sdk, "get_visits", study_key, **visit_filter)
            )
            if matching_subject_keys:
                visits = [v for v in visits if v.subject_key in matching_subject_keys]

            matching_visit_ids = {v.visit_id for v in visits if v.visit_id is not None}
            if on_progress:
                on_progress(f"Retrieved {len(matching_visit_ids)} matching visits.")
            if not matching_visit_ids:
                if on_progress:
                    on_progress("No visits matched criteria; returning 0 records.")
                return []

        final_record_filter_dict = dict(record_filter) if record_filter else {}
        final_record_filter_dict.update(other_filters)

        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")
        records: list[Record] = list(
            await call_async_facade_method(
                self._sdk,
                "get_records",
                study_key=study_key,
                record_data_filter=None,
                **final_record_filter_dict,
            )
        )

        if not include_deleted:
            records = [r for r in records if not is_record_deleted(r)]

        if matching_subject_keys:
            if on_progress:
                on_progress("Filtering records by matched subject keys...")
            records = [r for r in records if r.subject_key in matching_subject_keys]
        if matching_visit_ids:
            if on_progress:
                on_progress("Filtering records by matched visit IDs...")
            records = [r for r in records if r.visit_id in matching_visit_ids]

        result_records = list(records)
        if on_progress:
            on_progress(f"Extraction complete: found {len(result_records)} matching records.")

        return result_records

    async def extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **filters: Any,
    ) -> list[RecordRevision]:
        """Asynchronously extract the audit trail (record revisions) based on filters."""
        final_filter_dict: dict[str, Any] = dict(user_filter) if user_filter else {}
        final_filter_dict.update(filters)

        upstream_filters, local_date_bounds = split_revision_date_bounds(
            final_filter_dict,
            start_date=start_date,
            end_date=end_date,
        )

        revisions: list[RecordRevision] = list(
            await call_async_facade_method(
                self._sdk,
                "get_record_revisions",
                study_key,
                **upstream_filters,
            )
        )
        if local_date_bounds:
            revisions = [
                rev for rev in revisions if matches_revision_date_bounds(rev, local_date_bounds)
            ]
        return revisions

    async def extract_odm_audit_records(
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
        """Asynchronously extract study RecordRevision audit history mapped to ODM 1.3.2 ``<AuditRecord>`` models."""
        revisions = await self.extract_audit_trail(
            study_key=study_key,
            start_date=start_date,
            end_date=end_date,
            user_filter=user_filter,
            **filters,
        )
        with sdk_governance_context(self._sdk):
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


class DataExtractionWorkflow:
    """Provides methods for complex data extraction tasks involving multiple iMednet endpoints.

    Args:
        sdk: An instance of the synchronous ImednetSDK.
    """

    def __init__(self, sdk: "ImednetFacade") -> None:
        """Initialize the data extraction workflow.

        Args:
            sdk: An instance of the synchronous iMednet SDK facade.
        """
        self._sdk = sdk
        self._async_core = AsyncDataExtractionWorkflow(SyncToAsyncFacadeAdapter(sdk))

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
        """Extracts records based on criteria spanning subjects, visits, and records."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "DataExtractionWorkflow",
            "extract_records_by_criteria",
            "extract_records_by_criteria",
        )
        return run_sync_over_async(
            lambda: self._async_core.extract_records_by_criteria(
                study_key,
                record_filter=record_filter,
                subject_filter=subject_filter,
                visit_filter=visit_filter,
                on_progress=on_progress,
                include_deleted=include_deleted,
                **other_filters,
            )
        )

    def extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **filters: Any,
    ) -> list[RecordRevision]:
        """Extracts the audit trail (record revisions) based on specified filters."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "DataExtractionWorkflow",
            "extract_audit_trail",
            "extract_audit_trail",
        )
        return run_sync_over_async(
            lambda: self._async_core.extract_audit_trail(
                study_key,
                start_date=start_date,
                end_date=end_date,
                user_filter=user_filter,
                **filters,
            )
        )

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
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "DataExtractionWorkflow",
            "extract_odm_audit_records",
            "extract_odm_audit_records",
        )
        if "extract_audit_trail" in vars(self):
            revisions = self.extract_audit_trail(
                study_key=study_key,
                start_date=start_date,
                end_date=end_date,
                user_filter=user_filter,
                **filters,
            )
            with sdk_governance_context(self._sdk):
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
        with sdk_governance_context(self._sdk):
            return run_sync_over_async(
                lambda: self._async_core.extract_odm_audit_records(
                    study_key,
                    start_date=start_date,
                    end_date=end_date,
                    user_filter=user_filter,
                    default_location_oid=default_location_oid,
                    mask_phi=mask_phi,
                    phi_keys=phi_keys,
                    **filters,
                )
            )


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
