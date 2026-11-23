"""Parameter processing strategies for endpoints.

This module implements the ParamProcessor strategy pattern, allowing endpoints
to customize how filters are processed and special parameters are extracted.
"""

from collections.abc import Sequence
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


def _format_sort_clause(item: Any) -> str:
    """Format a single sort clause as ``<property>,asc`` or ``<property>,desc``."""
    if (
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], str)
    ):
        prop = item[0].strip()
        direction = item[1].strip()
        dir_lower = direction.lower()
        if prop and dir_lower in {"asc", "desc"}:
            return f"{prop},{dir_lower}"
        return f"{prop},{direction}"

    text = str(item).strip()
    if not text or ";" in text or ":" in text:
        return text
    if "," in text:
        prop_part, dir_part = text.split(",", 1)
        prop_clean = prop_part.strip()
        dir_lower = dir_part.strip().lower()
        if prop_clean and dir_lower in {"asc", "desc"}:
            return f"{prop_clean},{dir_lower}"
        return text
    return f"{text},asc"


def _normalize_sort_param(value: Any) -> Any:
    """Normalize multi-key ``sort`` inputs into repeated ``<property>,<asc|desc>`` parameters."""
    if isinstance(value, (str, bytes)):
        return value
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], str)
        and "," not in value[0]
        and ";" not in value[0]
        and ":" not in value[0]
        and value[1].strip().lower() in {"asc", "desc"}
    ):
        return _format_sort_clause(value)
    if isinstance(value, Sequence):
        return [_format_sort_clause(item) for item in value]
    return value


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
        for key, param_name in COMMON_LIST_PARAM_MAPPING.items():
            if key in cleaned:
                value = cleaned.pop(key)
                if value is not None:
                    special_params[param_name] = (
                        _normalize_sort_param(value) if param_name == "sort" else value
                    )
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

        for filter_key, param_name in self.mapping.items():
            value = None
            if filter_key in filters:
                value = filters.pop(filter_key)
            elif filter_key in self.defaults:
                value = self.defaults[filter_key]

            if value is not None:
                if param_name == "sort":
                    formatted: Any = _normalize_sort_param(value)
                elif isinstance(value, bool) and param_name not in _RAW_PASSTHROUGH_API_KEYS:
                    formatted = str(value).lower()
                else:
                    formatted = value
                if param_name in special_params and special_params[param_name] != formatted:
                    raise ConfigurationError(
                        f"Conflicting values provided for query parameter '{param_name}'."
                    )
                special_params[param_name] = formatted

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
