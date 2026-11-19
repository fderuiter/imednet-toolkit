"""Endpoint for managing users in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.edc_mixin import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.core.endpoint.structs import ParamState
from imednet.core.protocols import ParamProcessor
from imednet.models.users import User

if TYPE_CHECKING:
    import pandas as pd


class UsersParamProcessor(ParamProcessor):
    """Parameter processor for user listing operations."""

    def process_filters(self, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extract supported parameters and leave any unsupported ones in filters."""
        filters = filters.copy()
        special_params: dict[str, Any] = {}

        # Handle includeInactive / include_inactive
        if "includeInactive" in filters:
            val = filters.pop("includeInactive")
            special_params["includeInactive"] = (
                str(val).lower() if isinstance(val, bool) else str(val)
            )
            filters.pop("include_inactive", None)
        elif "include_inactive" in filters:
            val = filters.pop("include_inactive")
            special_params["includeInactive"] = (
                str(val).lower() if isinstance(val, bool) else str(val)
            )
        else:
            special_params["includeInactive"] = "false"

        # Handle page, size, limit, sort, and raw filter
        for key in ("page", "size", "limit", "sort"):
            if key in filters:
                val = filters.pop(key)
                if val is not None:
                    special_params[key] = val
        if "filter" in filters:
            special_params["filter"] = filters.pop("filter")

        return filters, special_params


class UsersOperationDef:
    """Definition for User operations."""

    PATH = "users"
    MODEL = User
    PARAM_PROCESSOR = UsersParamProcessor()
    FILTER_ATTRIBUTES: frozenset[str] = frozenset()

    def _resolve_params(
        self,
        study_key: str | None,
        extra_params: dict[str, Any] | None,
        filters: dict[str, Any],
    ) -> ParamState:
        """Resolve parameters for the users endpoint with strict validation.

        Args:
            study_key: The study key.
            extra_params: Additional query parameters.
            filters: Key-value filters to apply.

        Returns:
            A ParamState containing the resolved study and parameters.

        Raises:
            ConfigurationError: If any unsupported query filter parameters are passed.
        """
        explicit_study_key_filter = study_key is not None and "studyKey" in filters
        filters = self._auto_filter(filters.copy())  # type: ignore[attr-defined]

        processed_filters, special_params = self.param_processor.process_filters(filters)  # type: ignore[attr-defined]
        if special_params:
            extra_params = {} if extra_params is None else extra_params.copy()
            extra_params.update(special_params)

        if study_key:
            processed_filters["studyKey"] = study_key

        study, processed_filters = self.study_key_strategy.process(processed_filters)  # type: ignore[attr-defined]
        self._validate_study_key(study)  # type: ignore[attr-defined]

        # Check for unsupported query parameters
        unsupported = [k for k in processed_filters if k != "studyKey"]
        if explicit_study_key_filter:
            unsupported.append("studyKey")
        if extra_params:
            unsupported.extend(
                [
                    k
                    for k in extra_params
                    if k not in ("includeInactive", "page", "size", "limit", "sort")
                ]
            )
        if unsupported:
            from imednet.errors.validation import ConfigurationError

            raise ConfigurationError(
                f"Endpoint '{self.__class__.__name__}' does not support query filtering. "
                f"Unsupported parameter(s): {', '.join(sorted(set(unsupported)))}."
            )

        params: dict[str, Any] = {}
        if extra_params:
            params.update(extra_params)

        if "size" in params and params["size"] is not None:
            from imednet.constants import LARGE_PAGE_SIZE
            from imednet.core.paginator import _parse_int_param

            raw_size = _parse_int_param("size", params["size"])
            if raw_size <= 0:
                raise ValueError(f"size must be a positive integer, got {raw_size}")
            params["size"] = min(raw_size, LARGE_PAGE_SIZE)

        # Ensure 'filter' is never generated under any circumstances
        params.pop("filter", None)

        return ParamState(study=study, params=params, other_filters={})


class UsersEndpoint(UsersOperationDef, EdcSyncListEndpoint[User]):  # type: ignore[misc]
    """Synchronous endpoint for managing Users."""

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried users directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried users.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, users_to_dataframe

        check_pandas("users_to_dataframe")
        users = list(self.list(study_key=study_key, **filters))
        return users_to_dataframe(users)


class AsyncUsersEndpoint(UsersOperationDef, EdcAsyncListEndpoint[User]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Users."""

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried users asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried users.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, users_to_dataframe

        check_pandas("users_to_dataframe")
        users = [user async for user in self.list(study_key=study_key, **filters)]
        return users_to_dataframe(users)
