"""Provides a workflow to retrieve comprehensive data for a specific subject."""

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from imednet.spi.models import Query, Record, Subject, Visit
from imednet.spi.sink_base import is_record_deleted
from imednet.spi.utils import (
    SyncToAsyncFacadeAdapter,
    call_async_facade_method,
    is_async_facade,
    run_sync_over_async,
)

from ._workflow_helpers import ensure_sync_workflow_mode

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade


class SubjectComprehensiveData(BaseModel):
    """Structure to hold aggregated data for a subject."""

    subject_details: Subject | None = Field(None, description="Core details of the subject.")
    visits: list[Visit] = Field(default_factory=list, description="List of visits for the subject.")
    records: list[Record] = Field(
        default_factory=list, description="List of records for the subject."
    )
    queries: list[Query] = Field(
        default_factory=list, description="List of queries related to the subject."
    )


class AsyncSubjectDataWorkflow:
    """Canonical asynchronous workflow to retrieve comprehensive data for a subject."""

    supports_async = True

    def __init__(self, sdk: "AsyncImednetFacade") -> None:
        """Initialize the asynchronous subject data workflow."""
        self._sdk = sdk

    async def get_all_subject_data(
        self,
        study_key: str,
        subject_key: str,
        *,
        include_deleted: bool = False,
    ) -> SubjectComprehensiveData:
        """Concurrently retrieve subject details, visits, records, and queries."""
        subject_filter_dict: dict[str, Any] = {"subject_key": subject_key}
        raw_subjects, raw_visits, raw_records, raw_queries = await asyncio.gather(
            call_async_facade_method(self._sdk, "get_subjects", study_key, **subject_filter_dict),
            call_async_facade_method(self._sdk, "get_visits", study_key, **subject_filter_dict),
            call_async_facade_method(self._sdk, "get_records", study_key, **subject_filter_dict),
            call_async_facade_method(self._sdk, "get_queries", study_key, **subject_filter_dict),
        )
        subject_list: list[Subject] = list(raw_subjects)
        fetched_records: list[Record] = list(raw_records)
        filtered_records = (
            fetched_records
            if include_deleted
            else [r for r in fetched_records if not is_record_deleted(r)]
        )
        results = SubjectComprehensiveData(subject_details=None)
        if subject_list:
            results.subject_details = subject_list[0]
        results.visits = list(raw_visits)
        results.records = filtered_records
        results.queries = list(raw_queries)
        return results


class SubjectDataWorkflow:
    """Provides methods to retrieve comprehensive data related to a specific subject.

    Args:
        sdk: An instance of the synchronous ImednetFacade.
    """

    def __init__(self, sdk: "ImednetFacade") -> None:
        """Initialize the subject data workflow."""
        self._sdk = sdk
        self._async_core = AsyncSubjectDataWorkflow(SyncToAsyncFacadeAdapter(sdk))

    def get_all_subject_data(
        self,
        study_key: str,
        subject_key: str,
        *,
        include_deleted: bool = False,
    ) -> SubjectComprehensiveData:
        """Retrieves subject details, visits, records, and queries for a specific subject."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "SubjectDataWorkflow",
            "get_all_subject_data",
            "get_all_subject_data",
        )
        return run_sync_over_async(
            lambda: self._async_core.get_all_subject_data(
                study_key,
                subject_key,
                include_deleted=include_deleted,
            )
        )


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.subject_data.get_all_subject_data(...)`).
# - Simplifies common tasks by abstracting away the need to call multiple individual endpoints.
