"""Parameter processing strategies for endpoints.

This module implements the ParamProcessor strategy pattern, allowing endpoints
to customize how filters are processed and special parameters are extracted.
"""

from typing import Any, Protocol, runtime_checkable

from imednet.core.protocols import ParamProcessor


def _extract_sort_param(filters: dict[str, Any], special_params: dict[str, Any]) -> None:
    """Pop top-level ``sort`` from ``filters`` into ``special_params`` when present."""
    if "sort" in filters:
        sort_val = filters.pop("sort")
        if sort_val is not None:
            special_params["sort"] = sort_val


class DefaultParamProcessor(ParamProcessor):
    """Default parameter processor.

    Extracts top-level query parameters such as ``sort`` and passes remaining
    resource filters through without modification.
    """

    def process_filters(self, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return remaining filters and extracted top-level query parameters.

        Args:
            filters: The input filters dictionary.

        Returns:
            A tuple of (copy of filters without top-level params, special params dict).
        """
        cleaned = filters.copy()
        special_params: dict[str, Any] = {}
        _extract_sort_param(cleaned, special_params)
        return cleaned, special_params


class MappingParamProcessor(ParamProcessor):
    """ParamProcessor that maps specific filter keys to API parameters.

    Extracts keys defined in the mapping (plus top-level ``sort``) and returns
    them as special parameters, optionally renaming them according to the
    mapping values.
    """

    def __init__(
        self,
        mapping: dict[str, str],
        defaults: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the processor with a mapping.

        Args:
            mapping: A dictionary where keys are the filter keys to look for,
                     and values are the API parameter names to map them to.
            defaults: A dictionary of default values for keys not found in filters.
        """
        self.mapping = mapping
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
        _extract_sort_param(filters, special_params)

        for filter_key, api_key in self.mapping.items():
            value = None
            if filter_key in filters:
                value = filters.pop(filter_key)
            elif filter_key in self.defaults and api_key not in special_params:
                value = self.defaults[filter_key]

            if value is not None:
                # Convert boolean to lowercase string if necessary, or just pass through
                if isinstance(value, bool):
                    special_params[api_key] = str(value).lower()
                else:
                    special_params[api_key] = value

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
