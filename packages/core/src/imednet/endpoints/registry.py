"""Registry for all available API endpoints.

This module decouples the endpoint implementations from the main SDK class,
allowing for easier extension and maintenance.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from imednet.core.endpoint.base import GenericEndpoint


class _LazyEndpointRegistry(Mapping[str, "type[GenericEndpoint[Any]]"]):
    """Immutable mapping that imports endpoint classes on first lookup."""

    def __init__(self, specs: dict[str, tuple[str, str]]) -> None:
        """Initialize the lazy registry with ``attr -> (module_path, class_name)`` specs."""
        self._specs = dict(specs)
        self._cache: dict[str, type[GenericEndpoint[Any]]] = {}

    def __getitem__(self, key: str) -> type[GenericEndpoint[Any]]:
        """Resolve and cache the endpoint class for *key*."""
        if key in self._cache:
            return self._cache[key]
        module_path, class_name = self._specs[key]
        mod = import_module(module_path)  # nosem
        cls = cast("type[GenericEndpoint[Any]]", getattr(mod, class_name))
        self._cache[key] = cls
        return cls

    def __iter__(self) -> Iterator[str]:
        """Iterate registered endpoint attribute names without importing modules."""
        return iter(self._specs)

    def __len__(self) -> int:
        """Return total number of registered endpoints."""
        return len(self._specs)

    def __contains__(self, key: object) -> bool:
        """Return True if *key* is a registered endpoint attribute name."""
        return key in self._specs


#: Mapping of synchronous SDK attribute names to lazy endpoint classes.
ENDPOINT_REGISTRY: Mapping[str, type[GenericEndpoint[Any]]] = _LazyEndpointRegistry(
    {
        "codings": ("imednet.endpoints.codings", "CodingsEndpoint"),
        "forms": ("imednet.endpoints.forms", "FormsEndpoint"),
        "intervals": ("imednet.endpoints.intervals", "IntervalsEndpoint"),
        "jobs": ("imednet.endpoints.jobs", "JobsEndpoint"),
        "queries": ("imednet.endpoints.queries", "QueriesEndpoint"),
        "record_revisions": ("imednet.endpoints.record_revisions", "RecordRevisionsEndpoint"),
        "records": ("imednet.endpoints.records", "RecordsEndpoint"),
        "sites": ("imednet.endpoints.sites", "SitesEndpoint"),
        "studies": ("imednet.endpoints.studies", "StudiesEndpoint"),
        "subjects": ("imednet.endpoints.subjects", "SubjectsEndpoint"),
        "users": ("imednet.endpoints.users", "UsersEndpoint"),
        "variables": ("imednet.endpoints.variables", "VariablesEndpoint"),
        "visits": ("imednet.endpoints.visits", "VisitsEndpoint"),
    }
)

#: Mapping of asynchronous SDK attribute names to lazy endpoint classes.
ASYNC_ENDPOINT_REGISTRY: Mapping[str, type[GenericEndpoint[Any]]] = _LazyEndpointRegistry(
    {
        "codings": ("imednet.endpoints.codings", "AsyncCodingsEndpoint"),
        "forms": ("imednet.endpoints.forms", "AsyncFormsEndpoint"),
        "intervals": ("imednet.endpoints.intervals", "AsyncIntervalsEndpoint"),
        "jobs": ("imednet.endpoints.jobs", "AsyncJobsEndpoint"),
        "queries": ("imednet.endpoints.queries", "AsyncQueriesEndpoint"),
        "record_revisions": (
            "imednet.endpoints.record_revisions",
            "AsyncRecordRevisionsEndpoint",
        ),
        "records": ("imednet.endpoints.records", "AsyncRecordsEndpoint"),
        "sites": ("imednet.endpoints.sites", "AsyncSitesEndpoint"),
        "studies": ("imednet.endpoints.studies", "AsyncStudiesEndpoint"),
        "subjects": ("imednet.endpoints.subjects", "AsyncSubjectsEndpoint"),
        "users": ("imednet.endpoints.users", "AsyncUsersEndpoint"),
        "variables": ("imednet.endpoints.variables", "AsyncVariablesEndpoint"),
        "visits": ("imednet.endpoints.visits", "AsyncVisitsEndpoint"),
    }
)
