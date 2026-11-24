"""Base models for the iMedNet SDK."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Generic, Self, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

_NORMALIZERS: dict[type, dict[str, Callable[[Any], Any]]]

def _identity(v: Any) -> Any: ...
def _optional_str(v: Any) -> Any: ...
def _required_int(v: Any) -> int: ...
def _optional_int(v: Any) -> Any: ...
def _optional_bool(v: Any) -> Any: ...
def _optional_datetime(v: Any) -> Any: ...
def _optional_list(v: Any) -> Any: ...
def _optional_dict(v: Any) -> Any: ...
def _extract_single_item(v: Any) -> Any: ...
def _get_normalizer(cls: type[BaseModel], field_name: str) -> Callable[[Any], Any]: ...

_drift_reported: set[str]
_FIELD_METADATA_CACHE: dict[type, tuple[set[str], list[tuple[str, str | None]]]]

def _get_field_metadata(cls: type[BaseModel]) -> tuple[set[str], list[tuple[str, str | None]]]: ...

class ImednetBaseModel(BaseModel):
    """Core base model for all iMedNet API responses.

    Design philosophy:
    extra='ignore' silently drops new undocumented fields the API introduces.
    populate_by_name allows models to be instantiated using either pythonic
    snake_case names or original API camelCase names via Field aliases.
    str_strip_whitespace trims leading and trailing whitespace from string values.
    """

    @classmethod
    def from_json(cls, data: Any) -> Self: ...
    @model_validator(mode="before")
    @classmethod
    def _detect_drift(cls, data: Any) -> Any: ...
    @field_validator("*", check_fields=False, mode="before")
    def _normalise(cls, v: Any, info: Any) -> Any: ...

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

    def __init__(
        self,
        *,
        current_page: int = 0,
        currentPage: int = 0,
        size: int = 25,
        total_pages: int = 0,
        totalPages: int = 0,
        total_elements: int = 0,
        totalElements: int = 0,
        sort: list[SortField] = ...,
    ) -> None: ...

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
