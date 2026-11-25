"""Provides a workflow to retrieve comprehensive data for a specific subject."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from imednet.spi.models import Query, Record, Subject, Visit
from imednet.spi.sink_base import is_record_deleted

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


def _is_subject_deleted(subject: Any) -> bool:
    """Return True when a Subject instance or mapping is soft-deleted."""
    if isinstance(subject, Mapping):
        if bool(subject.get("is_deleted") or subject.get("deleted")):
            return True
        raw_status = subject.get("subject_status") or subject.get("subjectStatus")
    else:
        if bool(getattr(subject, "is_deleted", False) or getattr(subject, "deleted", False)):
            return True
        raw_status = getattr(subject, "subject_status", None)
    return isinstance(raw_status, str) and raw_status.strip().lower() == "deleted"


def _is_visit_deleted(visit: Any) -> bool:
    """Return True when a Visit instance or mapping is soft-deleted."""
    if isinstance(visit, Mapping):
        return bool(visit.get("is_deleted") or visit.get("deleted"))
    return bool(getattr(visit, "is_deleted", False) or getattr(visit, "deleted", False))


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


class SubjectDataWorkflow:
    """Provides methods to retrieve comprehensive data related to a specific subject.

    Args:
        sdk: An instance of the ImednetFacade.
    """

    def __init__(self, sdk: "ImednetFacade"):
        """Initialize the subject data workflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
        """
        self._sdk = sdk

    def get_all_subject_data(
        self,
        study_key: str,
        subject_key: str,
        *,
        include_deleted: bool = False,
    ) -> SubjectComprehensiveData:
        """Retrieves subject details, visits, records, and queries for a specific subject.

        Args:
            study_key: The key identifying the study.
            subject_key: The key identifying the subject.
            include_deleted: Whether soft-deleted Subject, Visit, and Record items should be retained.

        Returns:
            A SubjectComprehensiveData object containing the aggregated data.
        """
        results = SubjectComprehensiveData(subject_details=None)
        subject_filter_dict: dict[str, Any] = {"subject_key": subject_key}

        # Fetch Subject Details
        subject_list = list(self._sdk.get_subjects(study_key, **subject_filter_dict))
        if not include_deleted:
            subject_list = [s for s in subject_list if not _is_subject_deleted(s)]
        if subject_list:
            results.subject_details = subject_list[0]

        # Fetch Visits
        fetched_visits = list(self._sdk.get_visits(study_key, **subject_filter_dict))
        results.visits = (
            fetched_visits
            if include_deleted
            else [v for v in fetched_visits if not _is_visit_deleted(v)]
        )

        # Fetch Records
        fetched_records = list(self._sdk.get_records(study_key, **subject_filter_dict))
        results.records = (
            fetched_records
            if include_deleted
            else [r for r in fetched_records if not is_record_deleted(r)]
        )

        # Fetch Queries
        results.queries = list(self._sdk.get_queries(study_key, **subject_filter_dict))

        return results


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.subject_data.get_all_subject_data(...)`).
# - Simplifies common tasks by abstracting away the need to call multiple individual endpoints.
