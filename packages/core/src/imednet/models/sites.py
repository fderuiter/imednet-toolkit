# pylint: disable=duplicate-code
"""Site (study location) models for iMedNet."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import Field, field_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


def _normalize_status_token(value: str) -> str:
    """Collapse whitespace, underscores, and hyphens into a lowercase token."""
    return re.sub(r"[\s_-]+", " ", value.strip()).lower()


class SiteEnrollmentStatus(str, Enum):
    """Normalized enrollment lifecycle statuses for study sites."""

    ENROLLMENT_OPEN = "Enrollment Open"
    ENROLLMENT_CLOSED = "Enrollment Closed"
    ACTIVE = "Active"
    CLOSED = "Closed"
    INACTIVE = "Inactive"
    READ_ONLY = "Read Only"

    def __str__(self) -> str:
        """Return the canonical status string value."""
        return str(self.value)

    @classmethod
    def _missing_(cls, value: object) -> SiteEnrollmentStatus | None:
        """Resolve site enrollment status across case and separator conventions."""
        if isinstance(value, str):
            token = _normalize_status_token(value)
            if not token:
                return None
            for member in cls:
                if (
                    _normalize_status_token(member.value) == token
                    or _normalize_status_token(member.name) == token
                ):
                    return member
        return None

    @property
    def is_eligible(self) -> bool:
        """Return True when the site status permits subject enrollment."""
        return self in ELIGIBLE_SITE_ENROLLMENT_STATUSES


ELIGIBLE_SITE_ENROLLMENT_STATUSES: frozenset[SiteEnrollmentStatus] = frozenset(
    {SiteEnrollmentStatus.ENROLLMENT_OPEN, SiteEnrollmentStatus.ACTIVE}
)

SiteStatusInput = SiteEnrollmentStatus | str | None


def normalize_site_enrollment_status(value: SiteStatusInput) -> SiteStatusInput:
    """Normalize known site enrollment statuses while preserving custom strings."""
    if value is None or isinstance(value, SiteEnrollmentStatus):
        return value
    if isinstance(value, str):
        try:
            return SiteEnrollmentStatus(value)
        except ValueError:
            return value
    return str(value)


def is_site_enrollment_eligible(value: SiteEnrollmentStatus | str | None) -> bool:
    """Return True if *value* represents an open/active enrollment site status."""
    if not isinstance(value, (SiteEnrollmentStatus, str)):
        return False
    normalized = normalize_site_enrollment_status(value)
    return (
        isinstance(normalized, SiteEnrollmentStatus)
        and normalized in ELIGIBLE_SITE_ENROLLMENT_STATUSES
    )


class Site(ImednetBaseModel):
    """A site participating in a study."""

    site_enrollment_status: SiteEnrollmentStatus | str | None = Field(
        default=None,
        alias="siteEnrollmentStatus",
    )

    @field_validator("site_enrollment_status", mode="before")
    @classmethod
    def _validate_site_enrollment_status(
        cls,
        value: Any,
    ) -> SiteEnrollmentStatus | str | None:
        """Normalize known site enrollment statuses and preserve unknown strings."""
        if value is None or isinstance(value, (SiteEnrollmentStatus, str)):
            return normalize_site_enrollment_status(value)
        return normalize_site_enrollment_status(str(value))

    @property
    def is_enrollment_open(self) -> bool:
        """Return True when this site is open/active for subject enrollment."""
        return is_site_enrollment_eligible(self.site_enrollment_status)

    @property
    def is_eligible(self) -> bool:
        """Alias for :attr:`is_enrollment_open`."""
        return is_site_enrollment_eligible(self.site_enrollment_status)


Site = ModelEngine.get_model('Site', Site)
