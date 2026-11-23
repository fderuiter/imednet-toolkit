"""Provides a workflow to retrieve comprehensive data for a specific subject."""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from imednet.spi.cli import PanelSection
from imednet.spi.models import Query, Record, Subject, Visit
from imednet.spi.sink_base import is_record_deleted

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

_SUBJECT_PRIORITY_KEYS: tuple[str, ...] = (
    "subject_key",
    "subject_status",
    "site_name",
    "site_id",
    "enrollment_start_date",
    "subject_id",
    "subject_oid",
    "study_key",
    "date_created",
    "date_modified",
    "keywords",
)
_VISIT_PRIORITY_KEYS: tuple[str, ...] = (
    "visit_id",
    "interval_name",
    "visit_date",
    "start_date",
    "end_date",
    "visit_date_form",
    "visit_date_question",
    "date_created",
    "date_modified",
)
_RECORD_PRIORITY_KEYS: tuple[str, ...] = (
    "record_id",
    "form_key",
    "record_status",
    "visit_id",
    "interval_id",
    "date_created",
    "date_modified",
    "keywords",
    "record_data",
)
_QUERY_PRIORITY_KEYS: tuple[str, ...] = (
    "annotation_id",
    "variable",
    "annotation_type",
    "description",
    "record_id",
    "date_created",
    "date_modified",
    "query_comments",
)


def _model_or_mapping_to_ordered_dict(
    item: Any,
    priority_keys: Sequence[str],
    *,
    omit_keys: frozenset[str] = frozenset(),
) -> Any:
    """Convert a model or mapping into a key-ordered dictionary while preserving nested values."""
    if item is None:
        return None
    if isinstance(item, BaseModel):
        raw: dict[str, Any] = item.model_dump(by_alias=False)
    elif isinstance(item, Mapping):
        raw = dict(item)
    else:
        return item

    ordered: dict[str, Any] = {}
    for key in priority_keys:
        if key in raw and key not in omit_keys:
            ordered[key] = raw[key]
    for key, val in raw.items():
        if key not in ordered and key not in omit_keys:
            ordered[key] = val
    return ordered


def build_subject_panel_sections(
    subject_details: Any,
    visits: Sequence[Any],
    records: Sequence[Any],
    queries: Sequence[Any],
) -> list[PanelSection]:
    """Build ordered side-panel sections with Subject details first, then Visits, Records, Queries."""
    redundant_child_keys = frozenset({"study_key", "subject_id", "subject_key", "subject_oid"})
    projected_subject = _model_or_mapping_to_ordered_dict(subject_details, _SUBJECT_PRIORITY_KEYS)
    projected_visits = [
        _model_or_mapping_to_ordered_dict(v, _VISIT_PRIORITY_KEYS, omit_keys=redundant_child_keys)
        for v in visits
    ]
    projected_records = [
        _model_or_mapping_to_ordered_dict(r, _RECORD_PRIORITY_KEYS, omit_keys=redundant_child_keys)
        for r in records
    ]
    projected_queries = [
        _model_or_mapping_to_ordered_dict(q, _QUERY_PRIORITY_KEYS, omit_keys=redundant_child_keys)
        for q in queries
    ]
    return [
        PanelSection("Subject Details", projected_subject, empty_placeholder="None"),
        PanelSection("Visits", projected_visits, empty_placeholder="No visits recorded"),
        PanelSection("Records", projected_records, empty_placeholder="No records recorded"),
        PanelSection("Queries", projected_queries, empty_placeholder="No queries recorded"),
    ]


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

    def to_panel_sections(self) -> list[PanelSection]:
        """Project ordered side-panel sections via the CLI SPI protocol."""
        return build_subject_panel_sections(
            self.subject_details,
            self.visits,
            self.records,
            self.queries,
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
            include_deleted: Whether voided (``deleted=True``) records should be retained.

        Returns:
            A SubjectComprehensiveData object containing the aggregated data.
        """
        results = SubjectComprehensiveData(subject_details=None)
        subject_filter_dict: dict[str, Any] = {"subject_key": subject_key}

        # Fetch Subject Details
        subject_list = list(self._sdk.get_subjects(study_key, **subject_filter_dict))
        if subject_list:
            results.subject_details = subject_list[0]

        # Fetch Visits
        results.visits = list(self._sdk.get_visits(study_key, **subject_filter_dict))

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
