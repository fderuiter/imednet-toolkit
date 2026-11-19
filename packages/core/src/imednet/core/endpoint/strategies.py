"""Parameter processing strategies for endpoints.

This module implements the ParamProcessor strategy pattern, allowing endpoints
to customize how filters are processed and special parameters are extracted.
"""

from typing import Any, Protocol, runtime_checkable

from imednet.core.protocols import ParamProcessor

COMMON_LIST_PARAMS: tuple[str, ...] = ("page", "size", "limit", "sort", "filter")


class DefaultParamProcessor(ParamProcessor):
    """Default parameter processor for list endpoints.

    Extracts standard top-level pagination, limit, sorting, and raw filter
    parameters so they are emitted as separate query parameters instead of
    being serialized into ``filter`` predicates.
    """

    def process_filters(self, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extract standard top-level controls from filters.

        Args:
            filters: The input filters dictionary.

        Returns:
            A tuple of (remaining attribute filters, extracted top-level parameters).
        """
        cleaned_filters = filters.copy()
        special_params: dict[str, Any] = {}
        for key in COMMON_LIST_PARAMS:
            if key in cleaned_filters:
                value = cleaned_filters.pop(key)
                if value is not None:
                    special_params[key] = value
        return cleaned_filters, special_params


class MappingParamProcessor(DefaultParamProcessor):
    """ParamProcessor that maps specific filter keys to API parameters.

    Extracts common top-level list parameters plus keys defined in the mapping
    and returns them as special parameters, optionally renaming them according
    to the mapping values.
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
        """Process filters using standard controls and the configured mapping.

        Args:
            filters: The input filters dictionary.

        Returns:
            A tuple of (cleaned filters, special parameters).
        """
        cleaned_filters, special_params = super().process_filters(filters)

        for filter_key, api_key in self.mapping.items():
            value = None
            if filter_key in cleaned_filters:
                value = cleaned_filters.pop(filter_key)
            elif filter_key in self.defaults and api_key not in special_params:
                value = self.defaults[filter_key]

            if value is not None:
                formatted = str(value).lower() if isinstance(value, bool) else value
                if api_key in special_params and special_params[api_key] != formatted:
                    from imednet.errors.validation import ConfigurationError

                    raise ConfigurationError(
                        f"Conflicting values provided for '{api_key}': "
                        f"{special_params[api_key]!r} vs {formatted!r}."
                    )
                special_params[api_key] = formatted

        return cleaned_filters, special_params


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
