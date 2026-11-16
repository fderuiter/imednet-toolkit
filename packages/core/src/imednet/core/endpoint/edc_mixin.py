"""EDC-specific endpoint base class and mixin."""

from __future__ import annotations

from typing import Any, TypeVar

from imednet.core.context import get_study_context
from imednet.core.endpoint.base import (
    AsyncGetEndpoint,
    AsyncListGetEndpoint,
    SyncGetEndpoint,
    SyncListGetEndpoint,
)
from imednet.models.base import ImednetBaseModel

T = TypeVar("T", bound=ImednetBaseModel)

_EDC_BASE_PATH = "/api/v1/edc/studies"


def _inject_study_key(filters: dict[str, Any]) -> dict[str, Any]:
    """Inject the default study key from context into filters if absent."""
    study_key = get_study_context()
    if "studyKey" not in filters and study_key:
        filters["studyKey"] = study_key
    return filters


class EdcEndpointMixin:
    """Mixin providing EDC-specific logic for endpoints.

    This includes the base path for EDC resources and automatic injection
    of the default study key into filters.
    """

    BASE_PATH = _EDC_BASE_PATH

    def _auto_filter(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Inject default studyKey if missing.

        Args:
            filters: The current dictionary of filters.

        Returns:
            The filters dictionary with studyKey injected if applicable.
        """
        return _inject_study_key(filters)


class _EdcEndpointBase:
    """Single base class for EDC API endpoints with list and get operations.

    Combines the EDC-specific path prefix and automatic study-key injection
    with the full list/get composition provided by
    :class:`~imednet.core.endpoint.base.GenericListGetEndpoint`.

    Concrete endpoints should inherit **only** from this class (plus the
    generic type parameter) and declare ``PATH``, ``MODEL``, and any
    overrides as class attributes:

    .. code-block:: python

        class RecordsEndpoint(EdcGenericListGetEndpoint[Record]):
            PATH = "records"
            MODEL = Record
            _id_param = "recordId"
    """

    def _auto_filter(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Inject the default study key from context if not already present.

        Args:
            filters: The current dictionary of filters.

        Returns:
            The filters dictionary with studyKey injected if applicable.
        """
        return _inject_study_key(filters)


class EdcSyncGetEndpoint(_EdcEndpointBase, SyncGetEndpoint[T]):
    """Sync EDC get-only endpoint base."""

    BASE_PATH = _EDC_BASE_PATH


class EdcAsyncGetEndpoint(_EdcEndpointBase, AsyncGetEndpoint[T]):
    """Async EDC get-only endpoint base."""

    BASE_PATH = _EDC_BASE_PATH


class EdcSyncListGetEndpoint(_EdcEndpointBase, SyncListGetEndpoint[T]):
    """Sync EDC list/get endpoint base."""

    BASE_PATH = _EDC_BASE_PATH


class EdcAsyncListGetEndpoint(_EdcEndpointBase, AsyncListGetEndpoint[T]):
    """Async EDC list/get endpoint base."""

    BASE_PATH = _EDC_BASE_PATH


# Backward-compatible aliases. New code should use EdcSync* / EdcAsync* classes.
EdcGenericListGetEndpoint = EdcSyncListGetEndpoint
EdcGenericGetEndpoint = EdcSyncGetEndpoint
