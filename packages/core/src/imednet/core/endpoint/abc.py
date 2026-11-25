"""Abstract base class for all API endpoints."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from imednet.core.protocols import ClientProvider
from imednet.models.base import ImednetBaseModel

T = TypeVar("T", bound=ImednetBaseModel)


class EndpointABC(ABC, ClientProvider, Generic[T]):
    """Abstract base class defining the contract for all API endpoints.

    This ensures that all endpoint implementations provide necessary
    class attributes like PATH and MODEL, and implement core path building logic.
    """

    PATH: ClassVar[str]
    """The relative path for the endpoint."""

    MODEL: type[T]
    """The model class associated with this endpoint."""

    requires_study_key: bool = True
    """
    Whether this endpoint requires a study key.
    Defaults to True. Override in subclasses if needed.
    """

    _id_param: str = "id"
    """
    The query parameter name for the ID.
    Defaults to "id". Override in subclasses if needed.
    """

    @abstractmethod
    def _build_path(self, *segments: Any) -> str:
        """Build the full API path given segments.

        Must be implemented by the base endpoint logic.
        """

    @abstractmethod
    def _auto_filter(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Apply automatic filters (e.g., default study key).

        Must be implemented by the base endpoint logic.
        """

    def _validate_study_key(self, study_key: Any) -> None:
        """Validate that a study key is provided if required and safe for URL paths."""
        from imednet.errors.validation import ConfigurationError
        from imednet.utils.url import validate_path_segment

        if self.requires_study_key:
            if study_key is None:
                raise ConfigurationError(
                    "No study key provided. You must either pass 'study_key' explicitly "
                    "to the endpoint method or set it using ImednetSDK.study_context(...)."
                )
            validate_path_segment(study_key, name="study_key")
            return

        if study_key is not None:
            if isinstance(study_key, (list, tuple, set, frozenset)):
                if not study_key:
                    validate_path_segment("", name="study_key")
                for item in study_key:
                    validate_path_segment(item, name="study_key")
            else:
                validate_path_segment(study_key, name="study_key")

    @staticmethod
    def _normalize_static_path(static_path: str) -> list[str]:
        """Normalize static endpoint PATH segments, collapsing duplicate slashes."""
        from imednet.utils.url import build_safe_path

        normalized = build_safe_path(static_path)
        return [part for part in normalized.split("/") if part]

    def _get_endpoint_path(self, study_key: str | None, *extra_segments: Any) -> str:
        """Build the endpoint path separating static PATH prefixes from dynamic identifiers."""
        from imednet.utils.url import validate_path_segment

        self._validate_study_key(study_key)
        segments: list[str] = []
        if self.requires_study_key:
            segments.append(validate_path_segment(study_key, name="study_key"))
        if self.PATH:
            segments.extend(self._normalize_static_path(self.PATH))
        for segment in extra_segments:
            segments.append(validate_path_segment(segment, name="segment"))
        return self._build_path(*segments)

    def _raise_not_found(self, study_key: str | None, item_id: Any = None) -> None:
        """Raise a standardized NotFoundError."""
        from imednet.errors import NotFoundError

        msg_parts = [f"{self.MODEL.__name__}"]
        if item_id is not None:
            msg_parts.append(str(item_id))
        msg_parts.append("not found")
        if self.requires_study_key and study_key:
            msg_parts.append(f"in study {study_key}")

        raise NotFoundError(" ".join(msg_parts))
