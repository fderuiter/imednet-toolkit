"""Provides workflows for managing queries within iMednet studies."""

from typing import TYPE_CHECKING, Any

from imednet.spi.models import Query, QueryStatus, calculate_legacy_status
from imednet.spi.utils import build_filter_string, validate_filter_syntax

from .chunked_pipeline import (
    DEFAULT_OR_FILTER_BATCH_SIZE,
    MAX_OR_FILTER_PUSHDOWN_ITEMS,
    iter_chunks,
)

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

_TOP_LEVEL_QUERY_CONTROLS = frozenset({"page", "size", "limit", "sort"})


def _can_push_single_subject_with_filters(
    subject_key: str,
    filter_kwargs: dict[str, Any],
) -> bool:
    """Return True if ``subject_key`` can be combined with ``filter_kwargs`` using pure AND."""
    if "subject_key" in filter_kwargs or "subjectKey" in filter_kwargs:
        return False
    attr_filters = {k: v for k, v in filter_kwargs.items() if k != "filter"}
    attr_filters["subject_key"] = subject_key
    try:
        built_attr = build_filter_string(attr_filters)
        raw_filter = filter_kwargs.get("filter")
        if raw_filter is not None:
            validate_filter_syntax(raw_filter)
            raw_clean = raw_filter.strip()
            combined = (
                f"{raw_clean};{built_attr}"
                if raw_clean and built_attr
                else (raw_clean or built_attr)
            )
        else:
            combined = built_attr
        validate_filter_syntax(combined)
    except ValueError:
        return False
    return True


class QueryStateCounts(dict[str, int]):
    """State counts dictionary supporting case-insensitive and enum key access."""

    def __getitem__(self, key: Any) -> int:
        """Retrieve state count supporting case-insensitive and enum keys."""
        if isinstance(key, str) and key not in self:
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
        if hasattr(key, "value") and key.value in self:
            return self[key.value]
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = 0) -> Any:  # type: ignore[override]
        """Get state count with fallback default on missing keys."""
        try:
            return self[key]
        except KeyError:
            return default


class QueryManagementWorkflow:
    """Provides methods for common query management tasks.

    Args:
        sdk: An instance of the ImednetSDK.
    """

    def __init__(self, sdk: "ImednetFacade"):
        """Initialize the query management workflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
        """
        self._sdk = sdk

    @staticmethod
    def _is_query_open(query: Query) -> bool:
        """Determines if a query is open based on its explicit status or comments."""
        status = getattr(query, "status", None)
        if status is not None:
            return status in (QueryStatus.OPEN, QueryStatus.OPEN.value)
        comments = getattr(query, "query_comments", None)
        return calculate_legacy_status(comments) == QueryStatus.OPEN

    def get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all open queries for a given study, potentially filtered further.

        An 'open' query is defined as one where the `status` attribute is OPEN.

        Note:
            This method fetches queries based on `additional_filter` and then
            filters for the 'open' state client-side.

        Args:
            study_key: The key identifying the study.
            additional_filter: An optional dictionary of conditions to apply via the API.
            **kwargs: Additional keyword arguments passed directly to `sdk.queries.list`.

        Returns:
            A list of open Query objects matching the criteria.
        """
        filters = dict(additional_filter) if additional_filter else {}

        # Fetch potentially relevant queries
        all_matching_queries = self._sdk.get_queries(study_key, **filters, **kwargs)

        open_queries: list[Query] = []
        for query in all_matching_queries:
            status = getattr(query, "status", None)
            if status is not None:
                if status in (QueryStatus.OPEN, QueryStatus.OPEN.value):
                    open_queries.append(query)
            elif self._is_query_open(query):
                open_queries.append(query)

        return open_queries

    def get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all queries for a specific subject within a study.

        Args:
            study_key: The key identifying the study.
            subject_key: The key identifying the subject.
            additional_filter: An optional dictionary of conditions to combine
                with the subject filter.
            **kwargs: Additional keyword arguments passed directly to `sdk.queries.list`.

        Returns:
            A list of Query objects for the specified subject.
        """
        # Build filter dictionary
        final_filter_dict: dict[str, Any] = {"subject_key": subject_key}
        if additional_filter:
            final_filter_dict.update(additional_filter)

        return list(self._sdk.get_queries(study_key, **final_filter_dict, **kwargs))

    def get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all queries for a specific site within a study.

        Args:
            study_key: The key identifying the study.
            site_key: The name of the site.
            additional_filter: Extra conditions to combine with the subject filter.
            **kwargs: Additional keyword arguments passed directly to `sdk.queries.list`.

        Returns:
            A list of Query objects for the specified site.
        """
        subjects = self._sdk.get_subjects(study_key, site_name=site_key)
        subject_keys: list[Any] = list(
            dict.fromkeys(s.subject_key for s in subjects if s.subject_key is not None)
        )
        if not subject_keys:
            return []

        subject_key_set = set(subject_keys)
        control_kwargs = {k: v for k, v in kwargs.items() if k in _TOP_LEVEL_QUERY_CONTROLS}
        filter_kwargs: dict[str, Any] = dict(additional_filter) if additional_filter else {}
        for k, v in kwargs.items():
            if k not in _TOP_LEVEL_QUERY_CONTROLS:
                filter_kwargs[k] = v

        if not filter_kwargs:
            if len(subject_keys) <= MAX_OR_FILTER_PUSHDOWN_ITEMS:
                batches = list(iter_chunks(subject_keys, chunk_size=DEFAULT_OR_FILTER_BATCH_SIZE))
                if len(batches) == 1:
                    return list(
                        self._sdk.get_queries(study_key, subject_key=batches[0], **control_kwargs)
                    )
                results: list[Query] = []
                seen_ids: set[int] = set()
                for batch in batches:
                    for query in self._sdk.get_queries(
                        study_key, subject_key=batch, **control_kwargs
                    ):
                        ann_id = getattr(query, "annotation_id", None)
                        if ann_id is not None:
                            if ann_id in seen_ids:
                                continue
                            seen_ids.add(ann_id)
                        results.append(query)
                return results

            all_queries = self._sdk.get_queries(study_key, **control_kwargs)
            return [q for q in all_queries if getattr(q, "subject_key", None) in subject_key_set]

        if len(subject_keys) == 1 and _can_push_single_subject_with_filters(
            subject_keys[0], filter_kwargs
        ):
            return list(
                self._sdk.get_queries(
                    study_key,
                    subject_key=subject_keys[0],
                    **filter_kwargs,
                    **control_kwargs,
                )
            )

        filtered_queries = self._sdk.get_queries(study_key, **filter_kwargs, **control_kwargs)
        return [q for q in filtered_queries if getattr(q, "subject_key", None) in subject_key_set]

    def get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Counts queries grouped by their current QueryStatus values.

        The discrepancy lifecycle state is evaluated in linear time directly from
        each Query's status attribute, with fallback calculation for legacy payloads.

        Args:
            study_key: The key identifying the study.
            **kwargs: Additional keyword arguments passed directly to `sdk.queries.list`
                      (e.g., for initial filtering before counting).

        Returns:
            A dictionary with QueryStatus values as keys and their respective counts.
        """
        all_queries = self._sdk.get_queries(study_key, **kwargs)
        state_counts = QueryStateCounts({s.value: 0 for s in QueryStatus})

        for query in all_queries:
            status = getattr(query, "status", None)
            if status is None:
                comments = getattr(query, "query_comments", None)
                status = calculate_legacy_status(comments)
            if isinstance(status, QueryStatus):
                status_key = status.value
            else:
                try:
                    status_key = QueryStatus(str(status).strip().upper()).value
                except ValueError:
                    status_key = QueryStatus.OPEN.value
            state_counts[status_key] += 1

        return state_counts


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.query_management.get_open_queries(...)`).
# - Provides convenient ways to access query information without manually constructing
#   complex filters.
