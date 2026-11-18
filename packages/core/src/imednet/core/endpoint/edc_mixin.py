"""EDC-specific endpoint base classes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from imednet.core.context import get_study_context
from imednet.core.endpoint.base import (
    AsyncGetEndpoint,
    AsyncListEndpoint,
    AsyncListGetEndpoint,
    SyncGetEndpoint,
    SyncListEndpoint,
    SyncListGetEndpoint,
)
from imednet.models.base import ImednetBaseModel

if TYPE_CHECKING:
    import pandas as pd

T = TypeVar("T", bound=ImednetBaseModel)

_EDC_BASE_PATH = "/api/v1/edc/studies"


def _inject_study_key(filters: dict[str, Any]) -> dict[str, Any]:
    """Inject the default study key from context into filters if absent."""
    study_key = get_study_context()
    if "studyKey" not in filters and study_key:
        filters["studyKey"] = study_key
    return filters


def _dataframe_feature_name(path: str) -> str:
    """Return the pandas feature identifier for an EDC endpoint path."""
    cleaned = path.strip("/")
    return f"{cleaned}_to_dataframe" if cleaned else "to_dataframe"


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


class EdcSyncListEndpoint(_EdcEndpointBase, SyncListEndpoint[T]):
    """Sync EDC list-only endpoint base."""

    BASE_PATH = _EDC_BASE_PATH

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried resources directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried resources.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import _models_to_dataframe, check_pandas

        feature_name = _dataframe_feature_name(self.PATH)
        check_pandas(feature_name)
        items = list(self.list(study_key=study_key, **filters))
        return _models_to_dataframe(items, feature_name)


class EdcAsyncListEndpoint(_EdcEndpointBase, AsyncListEndpoint[T]):
    """Async EDC list-only endpoint base."""

    BASE_PATH = _EDC_BASE_PATH

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried resources asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried resources.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import _models_to_dataframe, check_pandas

        feature_name = _dataframe_feature_name(self.PATH)
        check_pandas(feature_name)
        items = [item async for item in self.list(study_key=study_key, **filters)]
        return _models_to_dataframe(items, feature_name)


class EdcSyncGetEndpoint(_EdcEndpointBase, SyncGetEndpoint[T]):
    """Sync EDC get-only endpoint base."""

    BASE_PATH = _EDC_BASE_PATH


class EdcAsyncGetEndpoint(_EdcEndpointBase, AsyncGetEndpoint[T]):
    """Async EDC get-only endpoint base."""

    BASE_PATH = _EDC_BASE_PATH


class EdcSyncListGetEndpoint(EdcSyncListEndpoint[T], SyncListGetEndpoint[T]):
    """Sync EDC list/get endpoint base."""

    BASE_PATH = _EDC_BASE_PATH


class EdcAsyncListGetEndpoint(EdcAsyncListEndpoint[T], AsyncListGetEndpoint[T]):
    """Async EDC list/get endpoint base."""

    BASE_PATH = _EDC_BASE_PATH


# Backward-compatible aliases. New code should use EdcSync* / EdcAsync* classes.
EdcGenericListGetEndpoint = EdcSyncListGetEndpoint
EdcGenericGetEndpoint = EdcSyncGetEndpoint
EdcGenericListEndpoint = EdcSyncListEndpoint
