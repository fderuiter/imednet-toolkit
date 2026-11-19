"""Provides a workflow to retrieve comprehensive data for a specific subject."""

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

from imednet.spi.models import Query, Record, Subject, Visit

from ._guards import ensure_async_sdk, ensure_sync_sdk

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


def _assemble_subject_data(
    subjects: Iterable[Subject],
    visits: Iterable[Visit],
    records: Iterable[Record],
    queries: Iterable[Query],
) -> SubjectComprehensiveData:
    """Assemble ``SubjectComprehensiveData`` from fetched endpoint collections."""
    subject_list = list(subjects)
    return SubjectComprehensiveData(
        subject_details=subject_list[0] if subject_list else None,
        visits=list(visits),
        records=list(records),
        queries=list(queries),
    )


class SubjectDataWorkflow:
    """Provides methods to retrieve comprehensive data related to a specific subject.

    Args:
        sdk: An instance of the ImednetFacade or AsyncImednetFacade.
    """

    def __init__(self, sdk: "ImednetFacade | AsyncImednetFacade"):
        """Initialize the subject data workflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
        """
        self._sdk = sdk

    def get_all_subject_data(self, study_key: str, subject_key: str) -> SubjectComprehensiveData:
        """Retrieves subject details, visits, records, and queries for a specific subject.

        Args:
            study_key: The key identifying the study.
            subject_key: The key identifying the subject.

        Returns:
            A SubjectComprehensiveData object containing the aggregated data.
        """
        ensure_sync_sdk(
            self._sdk,
            "SubjectDataWorkflow",
            "get_all_subject_data",
            "async_get_all_subject_data",
        )
        sdk = cast("ImednetFacade", self._sdk)
        subject_filter_dict: dict[str, Any] = {"subject_key": subject_key}
        return _assemble_subject_data(
            sdk.get_subjects(study_key, **subject_filter_dict),
            sdk.get_visits(study_key, **subject_filter_dict),
            sdk.get_records(study_key, **subject_filter_dict),
            sdk.get_queries(study_key, **subject_filter_dict),
        )

    async def async_get_all_subject_data(
        self, study_key: str, subject_key: str
    ) -> SubjectComprehensiveData:
        """Asynchronously retrieve subject details, visits, records, and queries concurrently.

        Args:
            study_key: The key identifying the study.
            subject_key: The key identifying the subject.

        Returns:
            A SubjectComprehensiveData object containing the aggregated data.
        """
        ensure_async_sdk(
            self._sdk,
            "SubjectDataWorkflow",
            "async_get_all_subject_data",
            "get_all_subject_data",
        )
        sdk = cast("AsyncImednetFacade", self._sdk)
        subject_filter_dict: dict[str, Any] = {"subject_key": subject_key}
        subjects, visits, records, queries = await asyncio.gather(
            sdk.async_get_subjects(study_key, **subject_filter_dict),
            sdk.async_get_visits(study_key, **subject_filter_dict),
            sdk.async_get_records(study_key, **subject_filter_dict),
            sdk.async_get_queries(study_key, **subject_filter_dict),
        )
        return _assemble_subject_data(subjects, visits, records, queries)


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.subject_data.get_all_subject_data(...)`).
# - Simplifies common tasks by abstracting away the need to call multiple individual endpoints.
