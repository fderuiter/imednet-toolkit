"""Provides workflows for managing queries within iMednet studies."""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from imednet.spi.models import Query, QueryStatus, Subject, calculate_legacy_status

from ._guards import ensure_async_sdk, ensure_sync_sdk

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

_OWNER = "QueryManagementWorkflow"


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


def _build_subject_query_filters(
    subject_key: str | list[str],
    additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None,
) -> dict[str, Any]:
    """Build combined query filters scoped to one or more subject keys."""
    final_filter_dict: dict[str, Any] = {"subject_key": subject_key}
    if additional_filter:
        final_filter_dict.update(additional_filter)
    return final_filter_dict


def _extract_site_subject_keys(subjects: Iterable[Subject]) -> list[str]:
    """Return subject keys from a site-filtered subject collection."""
    return [s.subject_key for s in subjects if s.subject_key is not None]


def _aggregate_query_state_counts(queries: Iterable[Query]) -> QueryStateCounts:
    """Count queries grouped by normalized ``QueryStatus`` values."""
    state_counts = QueryStateCounts({s.value: 0 for s in QueryStatus})
    for query in queries:
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


class QueryManagementWorkflow:
    """Provides methods for common query management tasks.

    Args:
        sdk: An instance of the ImednetSDK or AsyncImednetSDK.
    """

    def __init__(self, sdk: "ImednetFacade | AsyncImednetFacade"):
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

    def _filter_open(self, queries: Iterable[Query]) -> list[Query]:
        """Filter an iterable of queries down to those in an open state."""
        return [query for query in queries if self._is_query_open(query)]

    def get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all open queries for a given study, potentially filtered further."""
        ensure_sync_sdk(self._sdk, _OWNER, "get_open_queries", "async_get_open_queries")
        sdk = cast("ImednetFacade", self._sdk)
        filters = dict(additional_filter) if additional_filter else {}
        all_matching_queries = sdk.get_queries(study_key, **filters, **kwargs)
        return self._filter_open(all_matching_queries)

    async def async_get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronous variant of :meth:`get_open_queries`."""
        ensure_async_sdk(self._sdk, _OWNER, "async_get_open_queries", "get_open_queries")
        sdk = cast("AsyncImednetFacade", self._sdk)
        filters = dict(additional_filter) if additional_filter else {}
        all_matching_queries = await sdk.async_get_queries(study_key, **filters, **kwargs)
        return self._filter_open(all_matching_queries)

    def get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all queries for a specific subject within a study."""
        ensure_sync_sdk(
            self._sdk,
            _OWNER,
            "get_queries_for_subject",
            "async_get_queries_for_subject",
        )
        sdk = cast("ImednetFacade", self._sdk)
        final_filter_dict = _build_subject_query_filters(subject_key, additional_filter)
        return list(sdk.get_queries(study_key, **final_filter_dict, **kwargs))

    async def async_get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronous variant of :meth:`get_queries_for_subject`."""
        ensure_async_sdk(
            self._sdk,
            _OWNER,
            "async_get_queries_for_subject",
            "get_queries_for_subject",
        )
        sdk = cast("AsyncImednetFacade", self._sdk)
        final_filter_dict = _build_subject_query_filters(subject_key, additional_filter)
        return list(await sdk.async_get_queries(study_key, **final_filter_dict, **kwargs))

    def get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all queries for a specific site within a study."""
        ensure_sync_sdk(self._sdk, _OWNER, "get_queries_by_site", "async_get_queries_by_site")
        sdk = cast("ImednetFacade", self._sdk)
        subject_keys = _extract_site_subject_keys(sdk.get_subjects(study_key, site_name=site_key))
        if not subject_keys:
            return []
        final_filter_dict = _build_subject_query_filters(subject_keys, additional_filter)
        return list(sdk.get_queries(study_key, **final_filter_dict, **kwargs))

    async def async_get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronous variant of :meth:`get_queries_by_site`."""
        ensure_async_sdk(self._sdk, _OWNER, "async_get_queries_by_site", "get_queries_by_site")
        sdk = cast("AsyncImednetFacade", self._sdk)
        subjects = await sdk.async_get_subjects(study_key, site_name=site_key)
        subject_keys = _extract_site_subject_keys(subjects)
        if not subject_keys:
            return []
        final_filter_dict = _build_subject_query_filters(subject_keys, additional_filter)
        return list(await sdk.async_get_queries(study_key, **final_filter_dict, **kwargs))

    def get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Counts queries grouped by their current QueryStatus values."""
        ensure_sync_sdk(
            self._sdk,
            _OWNER,
            "get_query_state_counts",
            "async_get_query_state_counts",
        )
        sdk = cast("ImednetFacade", self._sdk)
        return _aggregate_query_state_counts(sdk.get_queries(study_key, **kwargs))

    async def async_get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Asynchronous variant of :meth:`get_query_state_counts`."""
        ensure_async_sdk(
            self._sdk,
            _OWNER,
            "async_get_query_state_counts",
            "get_query_state_counts",
        )
        sdk = cast("AsyncImednetFacade", self._sdk)
        return _aggregate_query_state_counts(await sdk.async_get_queries(study_key, **kwargs))


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.query_management.get_open_queries(...)`).
# - Provides convenient ways to access query information without manually constructing
#   complex filters.
