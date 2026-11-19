"""Parameter processing strategies for endpoints.

This module implements the ParamProcessor strategy pattern, allowing endpoints
to customize how filters are processed and special parameters are extracted.
"""

from typing import Any, Protocol, runtime_checkable

from imednet.core.protocols import ParamProcessor
from imednet.errors.validation import ConfigurationError

COMMON_LIST_PARAM_MAPPING: dict[str, str] = {
    "page": "page",
    "size": "size",
    "limit": "limit",
    "sort": "sort",
    "filter": "filter",
}

_RAW_PASSTHROUGH_API_KEYS: frozenset[str] = frozenset(
    {*COMMON_LIST_PARAM_MAPPING.values(), "recordDataFilter"}
)


class DefaultParamProcessor(ParamProcessor):
    """Default parameter processor for collection list endpoints.

    Extracts standard top-level query parameters (``page``, ``size``, ``limit``,
    ``sort``, and raw ``filter``) so they are routed as URL query arguments
    rather than serialized inside ``filter``.
    """

    def process_filters(self, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extract standard top-level query parameters from filters.

        Args:
            filters: The input filters dictionary.

        Returns:
            A tuple of (remaining resource filters, extracted top-level query parameters).
        """
        cleaned = filters.copy()
        special_params: dict[str, Any] = {}
        for key, api_key in COMMON_LIST_PARAM_MAPPING.items():
            if key in cleaned:
                value = cleaned.pop(key)
                if value is not None:
                    special_params[api_key] = value
        return cleaned, special_params


class MappingParamProcessor(ParamProcessor):
    """ParamProcessor that maps specific filter keys to API parameters.

    Extracts standard collection query keys (``page``, ``size``, ``limit``,
    ``sort``, ``filter``) plus any endpoint-specific keys defined in ``mapping``
    and returns them as special parameters.
    """

    def __init__(
        self,
        mapping: dict[str, str],
        defaults: dict[str, Any] | None = None,
        *,
        include_common_params: bool = True,
    ) -> None:
        """Initialize the processor with a mapping.

        Args:
            mapping: A dictionary where keys are the filter keys to look for,
                     and values are the API parameter names to map them to.
            defaults: A dictionary of default values for keys not found in filters.
            include_common_params: Whether to include standard pagination, sort, and filter keys.
        """
        base_mapping = dict(COMMON_LIST_PARAM_MAPPING) if include_common_params else {}
        base_mapping.update(mapping)
        self.mapping = base_mapping
        self.defaults = defaults or {}

    def process_filters(self, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Process filters using the configured mapping.

        Args:
            filters: The input filters dictionary.

        Returns:
            A tuple of (cleaned filters, special parameters).
        """
        filters = filters.copy()
        special_params: dict[str, Any] = {}

        for filter_key, api_key in self.mapping.items():
            value = None
            if filter_key in filters:
                value = filters.pop(filter_key)
            elif filter_key in self.defaults:
                value = self.defaults[filter_key]

            if value is not None:
                formatted = (
                    str(value).lower()
                    if isinstance(value, bool) and api_key not in _RAW_PASSTHROUGH_API_KEYS
                    else value
                )
                if api_key in special_params and special_params[api_key] != formatted:
                    raise ConfigurationError(
                        f"Conflicting values provided for query parameter '{api_key}'."
                    )
                special_params[api_key] = formatted

        return filters, special_params


@runtime_checkable
class StudyKeyStrategy(Protocol):
    """Protocol for study key handling strategies."""

    def process(self, filters: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """Process the study key from filters.

        Args:
            filters: The current filters dictionary.

        Returns:
            A tuple containing the extracted study key (if any) and the modified filters.
        """
        ...


class KeepStudyKeyStrategy:
    """Strategy that requires a study key and keeps it in the filters.

    Used when the API expects 'studyKey' as a query parameter.
    """

    def process(self, filters: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """Extract study key and keep in filters.

        Args:
            filters: The filters dictionary.

        Returns:
            Tuple of (study_key, filters).
        """
        study_key = filters.get("studyKey")
        return study_key, filters


class PopStudyKeyStrategy:
    """Strategy that requires a study key but removes it from the filters.

    Used when the study key is part of the path or handled separately,
    not sent as a query parameter.
    """

    def process(self, filters: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """Extract study key and remove from filters.

        Args:
            filters: The filters dictionary.

        Returns:
            Tuple of (study_key, modified_filters).
        """
        filters_copy = filters.copy()
        study_key = filters_copy.pop("studyKey", None)
        return study_key, filters_copy


class OptionalStudyKeyStrategy:
    """Strategy that allows the study key to be optional.

    If present, it is kept in the filters.
    """

    def process(self, filters: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """Extract study key if present.

        Args:
            filters: The filters dictionary.

        Returns:
            Tuple of (study_key_or_none, filters).
        """
        study_key = filters.get("studyKey")
        return study_key, filters
