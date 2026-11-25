"""Base models for the iMedNet SDK."""

from __future__ import annotations

import logging
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
_logger: logging.Logger = logging.getLogger(__name__)
_drift_logger: logging.Logger = logging.getLogger("imednet.drift")


def _identity(v: Any) -> Any:
    """Return the value as-is."""
    return v


def _optional_str(v: Any) -> Any:
    """Convert value to string, returning None if missing."""
    if type(v) is str:
        return None if not v or v.isspace() else v
    if v is None:
        return None
    if isinstance(v, str):
        return None if not v or v.isspace() else str(v)
    if isinstance(v, (list, dict)):
        _guard_str_container(v)
        return None
    return parse_str_or_default(v)


def _required_int(v: Any) -> int:
    """Convert value to int, converting OverflowError to ValueError for Pydantic validation."""
    if type(v) is int:
        return v
    try:
        return parse_int_or_default(v)
    except OverflowError as exc:
        raise ValueError(str(exc)) from exc


def _optional_int(v: Any) -> Any:
    """Convert value to int, returning None if missing."""
    if type(v) is int or v is None:
        return v
    strict = is_strict_mode()
    if not strict and is_missing_value(v):
        return None
    try:
        return parse_int_or_default(v, strict=strict)
    except OverflowError as exc:
        raise ValueError(str(exc)) from exc


def _optional_bool(v: Any) -> Any:
    """Convert value to bool, returning None if missing."""
    if type(v) is bool or v is None:
        return v
    strict = is_strict_mode()
    if not strict and is_missing_value(v):
        return None
    return parse_bool(v, strict=strict)


def _optional_datetime(v: Any) -> Any:
    """Convert value to datetime, returning None if missing."""
    if type(v) is datetime or v is None or isinstance(v, datetime):
        return v
    if type(v) is str and v and not v.isspace():
        return parse_datetime(v)
    strict = is_strict_mode()
    if not strict and is_missing_value(v):
        return None
    return parse_datetime(v, strict=strict)


def _optional_list(v: Any) -> Any:
    """Convert value to list, returning None if missing."""
    if type(v) is list or v is None or isinstance(v, list):
        return v
    strict = is_strict_mode()
    if not strict and is_missing_value(v):
        return None
    return parse_list_or_default(v, strict=strict)


def _optional_dict(v: Any) -> Any:
    """Convert value to dict, returning None if missing."""
    if type(v) is dict or v is None or isinstance(v, dict):
        return v
    strict = is_strict_mode()
    if not strict and is_missing_value(v):
        return None
    return parse_dict_or_default(v, strict=strict)


def _extract_single_item(v: Any) -> Any:
    """Extract the first item if a list is provided where an object was expected."""
    if type(v) is not list and not isinstance(v, list):
        return v
    if is_strict_mode():
        raise ValueError("Expected object, got list")
    if len(v) > 0 and isinstance(v[0], dict):
        _logger.warning(
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
        normalizer = _optional_list if optional else parse_list_or_default
    elif origin is dict:
        normalizer = _optional_dict if optional else parse_dict_or_default
    elif annotation is str:
        normalizer = _optional_str if optional else parse_str_or_default
    elif annotation is int:
        normalizer = _optional_int if optional else _required_int
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
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Precompile per-field normalizers when model annotations are resolved."""
        super().__pydantic_init_subclass__(**kwargs)
        _NORMALIZERS.pop(cls, None)
        if getattr(cls, "__pydantic_complete__", False):
            _NORMALIZERS[cls] = {
                field_name: _get_normalizer(cls, field_name) for field_name in cls.model_fields
            }

    @classmethod
    def from_json(cls, data: Any) -> Self:
        """Validate data coming from JSON APIs."""
        if isinstance(data, list) and not is_strict_mode():
            if len(data) > 0 and isinstance(data[0], dict):
                _logger.warning(
                    f"Structural shift detected: API returned a list where an object ({cls.__name__}) was expected. Coercing by extracting the first item."
                )
                data = data[0]
            elif len(data) == 0:
                _logger.warning(
                    f"Structural shift detected: API returned an empty list where an object ({cls.__name__}) was expected. Coercing to empty dict."
                )
                data = {}

        try:
            return cls.model_validate(data)
        except Exception as e:
            msg = f"Drift detected (destructive): {cls.__name__} validation failed: {e}"
            if msg not in _drift_reported:
                _drift_reported.add(msg)
                _drift_logger.warning(msg)
            raise

    @model_validator(mode="before")
    @classmethod
    def _detect_drift(cls, data: Any) -> Any:
        """Compare incoming JSON data against the model definition to detect API drift."""
        if type(data) is not dict and not isinstance(data, dict):
            return data

        try:
            defined_fields, required_fields = _FIELD_METADATA_CACHE[cls]
        except KeyError:
            defined_fields, required_fields = _get_field_metadata(cls)

        extra_mode = cls.model_config.get("extra")
        if extra_mode != "allow":
            is_strict = is_strict_mode()
            unexpected_fields = data.keys() - defined_fields
            if unexpected_fields:
                msg = f"Drift detected (additive): {cls.__name__} received unexpected fields: {', '.join(sorted(unexpected_fields))}"
                if is_strict:
                    raise ValueError(msg)
                if extra_mode == "ignore" and msg not in _drift_reported:
                    _drift_reported.add(msg)
                    _drift_logger.warning(msg)

        if required_fields:
            missing_fields: list[str] | None = None
            for name, alias in required_fields:
                if name not in data and (not alias or alias not in data):
                    if missing_fields is None:
                        missing_fields = [name]
                    else:
                        missing_fields.append(name)
            if missing_fields:
                msg = f"Drift detected (destructive): {cls.__name__} missing required fields: {', '.join(sorted(missing_fields))}"
                if msg not in _drift_reported:
                    _drift_reported.add(msg)
                    _drift_logger.warning(msg)

        return data

    @field_validator("*", check_fields=False, mode="before")
    def _normalise(cls, v: Any, info: Any) -> Any:
        """Normalize common primitive types before validation."""
        try:
            return _NORMALIZERS[cls][info.field_name](v)  # type: ignore[index]
        except KeyError:
            if not info.field_name:
                return v
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
