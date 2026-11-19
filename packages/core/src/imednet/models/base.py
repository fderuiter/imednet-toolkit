"""Base models for the iMedNet SDK."""

from __future__ import annotations

import types
from collections.abc import Callable
from datetime import datetime
from typing import (
    Any,
    Generic,
    Self,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from imednet.core.context import is_strict_mode
from imednet.utils.validators import (
    _guard_str_container,
    is_missing_value,
    parse_bool,
    parse_datetime,
    parse_dict_or_default,
    parse_int_or_default,
    parse_list_or_default,
    parse_str_or_default,
)

_NORMALIZERS: dict[type, dict[str, Callable[[Any], Any]]] = {}


def _identity(v: Any) -> Any:
    """Return the value as-is."""
    return v


def _optional_str(v: Any) -> Any:
    """Convert value to string, returning None if missing."""
    if is_missing_value(v):
        return None
    if isinstance(v, (list, dict)):
        _guard_str_container(v)
        return None
    return parse_str_or_default(v)


def _optional_int(v: Any) -> Any:
    """Convert value to int, returning None if missing."""
    return None if is_missing_value(v) else parse_int_or_default(v)


def _optional_bool(v: Any) -> Any:
    """Convert value to bool, returning None if missing."""
    return None if is_missing_value(v) else parse_bool(v)


def _optional_datetime(v: Any) -> Any:
    """Convert value to datetime, returning None if missing."""
    return None if is_missing_value(v) else parse_datetime(v)


def _extract_single_item(v: Any) -> Any:
    """Extract the first item if a list is provided where an object was expected."""
    if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
        import logging

        logging.getLogger(__name__).warning(
            "Structural shift detected: API returned a list where an object was expected. Coercing by extracting the first item."
        )
        return v[0]
    return v


def _get_normalizer(cls: type[BaseModel], field_name: str) -> Callable[[Any], Any]:
    """Determine the appropriate normalization function for a model field.

    Analyzes type hints to select a parser for strings, integers, booleans,
    datetimes, or nested models.
    """
    if cls in _NORMALIZERS and field_name in _NORMALIZERS[cls]:
        return _NORMALIZERS[cls][field_name]

    field = cls.model_fields[field_name]
    annotation = field.annotation
    origin = get_origin(annotation)
    optional = False

    if origin is Union or origin is getattr(types, "UnionType", type(None)):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            annotation = args[0]
            origin = get_origin(annotation)
            optional = True

    normalizer = _identity

    if origin is list:
        normalizer = parse_list_or_default
    elif origin is dict:
        normalizer = parse_dict_or_default
    elif annotation is str:
        normalizer = _optional_str if optional else parse_str_or_default
    elif annotation is int:
        normalizer = _optional_int if optional else parse_int_or_default
    elif annotation is bool:
        normalizer = _optional_bool if optional else parse_bool
    elif annotation is datetime:
        normalizer = _optional_datetime if optional else parse_datetime
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        normalizer = _extract_single_item

    if cls not in _NORMALIZERS:
        _NORMALIZERS[cls] = {}
    _NORMALIZERS[cls][field_name] = normalizer
    return normalizer


_drift_reported: set[str] = set()
_FIELD_METADATA_CACHE: dict[type, tuple[set[str], list[tuple[str, str | None]]]] = {}


def _get_field_metadata(cls: type[BaseModel]) -> tuple[set[str], list[tuple[str, str | None]]]:
    """Retrieve or compute cached field metadata (defined fields and required fields) for a model class."""
    if cls in _FIELD_METADATA_CACHE:
        return _FIELD_METADATA_CACHE[cls]

    defined_fields = set(cls.model_fields.keys())
    required_fields: list[tuple[str, str | None]] = []

    for name, field in cls.model_fields.items():
        if field.alias:
            defined_fields.add(field.alias)
        if field.is_required():
            required_fields.append((name, field.alias))

    if hasattr(cls, "model_computed_fields"):
        defined_fields.update(cls.model_computed_fields.keys())

    meta = (defined_fields, required_fields)
    _FIELD_METADATA_CACHE[cls] = meta
    return meta


class ImednetBaseModel(BaseModel):
    """Core base model for all iMedNet API responses.

    Design philosophy:
    extra='ignore' silently drops new undocumented fields the API introduces.
    populate_by_name allows models to be instantiated using either pythonic
    snake_case names or original API camelCase names via Field aliases.
    str_strip_whitespace trims leading and trailing whitespace from string values.
    """

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    @classmethod
    def from_json(cls, data: Any) -> Self:
        """Validate data coming from JSON APIs."""
        if isinstance(data, list):
            if len(data) > 0 and isinstance(data[0], dict):
                import logging

                logging.getLogger(__name__).warning(
                    f"Structural shift detected: API returned a list where an object ({cls.__name__}) was expected. Coercing by extracting the first item."
                )
                data = data[0]
            elif len(data) == 0:
                import logging

                logging.getLogger(__name__).warning(
                    f"Structural shift detected: API returned an empty list where an object ({cls.__name__}) was expected. Coercing to empty dict."
                )
                data = {}

        try:
            return cls.model_validate(data)
        except Exception as e:
            import logging

            msg = f"Drift detected (destructive): {cls.__name__} validation failed: {e}"
            if msg not in _drift_reported:
                _drift_reported.add(msg)
                logging.getLogger("imednet.drift").warning(msg)
            raise

    @model_validator(mode="before")
    @classmethod
    def _detect_drift(cls, data: Any) -> Any:
        """Compare incoming JSON data against the model definition to detect API drift."""
        if not isinstance(data, dict):
            return data

        import logging

        logger = logging.getLogger("imednet.drift")

        try:
            defined_fields, required_fields = _FIELD_METADATA_CACHE[cls]
        except KeyError:
            defined_fields, required_fields = _get_field_metadata(cls)

        incoming_keys = set(data.keys())

        unexpected_fields = incoming_keys - defined_fields
        if unexpected_fields:
            is_strict = is_strict_mode()
            msg = f"Drift detected (additive): {cls.__name__} received unexpected fields: {', '.join(sorted(unexpected_fields))}"
            if is_strict:
                raise ValueError(msg)
            if cls.model_config.get("extra") != "ignore":  # noqa: SIM102
                if msg not in _drift_reported:
                    _drift_reported.add(msg)
                    logger.warning(msg)

        missing_fields = []
        for name, alias in required_fields:
            if name not in incoming_keys and (not alias or alias not in incoming_keys):
                missing_fields.append(name)
        if missing_fields:
            msg = f"Drift detected (destructive): {cls.__name__} missing required fields: {', '.join(sorted(missing_fields))}"
            if msg not in _drift_reported:
                _drift_reported.add(msg)
                logger.warning(msg)

        return data

    @field_validator("*", check_fields=False, mode="before")
    def _normalise(cls, v: Any, info: Any) -> Any:
        """Normalize common primitive types before validation."""
        if not info.field_name:
            return v

        # Bolt Optimization: Avoid function call overhead in hot path
        try:
            return _NORMALIZERS[cls][info.field_name](v)  # type: ignore[index]
        except KeyError:
            return _get_normalizer(cls, info.field_name)(v)  # type: ignore[arg-type]


class SortField(ImednetBaseModel):
    """Sorting information for a field in a paginated response."""

    property: str = Field(..., description="Property to sort by")
    direction: str = Field(..., description="Sort direction (ASC or DESC)")


class Pagination(ImednetBaseModel):
    """Pagination information in an API response."""

    current_page: int = Field(0, alias="currentPage")
    size: int = Field(25, alias="size")
    total_pages: int = Field(0, alias="totalPages")
    total_elements: int = Field(0, alias="totalElements")
    sort: list[SortField] = Field(default_factory=list)


class ErrorField(ImednetBaseModel):
    """Offending request field details in an API error response."""

    attribute: str = Field(
        default="", description="Origination request attribute which caused the error"
    )
    value: str = Field(
        default="", description="The value of request attribute passed in the request"
    )


class Error(ImednetBaseModel):
    """Error information in an API response."""

    code: str = Field(default="", description="Error code")
    description: str = Field(default="", description="Error description message")
    message: str = Field(default="", description="Error message")
    field: ErrorField | None = Field(default=None, description="Offending request field details")
    details: dict[str, Any] = Field(default_factory=dict)


class Metadata(ImednetBaseModel):
    """Metadata information in an API response."""

    status: str = Field(default="", description="Response status")
    method: str = Field(default="", description="HTTP method")
    path: str = Field(default="", description="Request path")
    timestamp: datetime
    error: Error = Field(default_factory=lambda: Error())


T = TypeVar("T")


class ApiResponse(ImednetBaseModel, Generic[T]):
    """Generic API response model."""

    metadata: Metadata
    pagination: Pagination | None = None
    data: T | None = None
