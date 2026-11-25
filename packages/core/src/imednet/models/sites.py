# pylint: disable=duplicate-code
"""Site (study location) models for iMedNet."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, field_validator

from imednet.models._lifecycle import (
    coerce_field_status,
    coerce_status,
    resolve_status_member,
)
from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


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
        return resolve_status_member(cls, value)

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
    return coerce_status(SiteEnrollmentStatus, value)


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
        return coerce_field_status(SiteEnrollmentStatus, value)

    @property
    def is_enrollment_open(self) -> bool:
        """Return True when this site is open/active for subject enrollment."""
        return is_site_enrollment_eligible(self.site_enrollment_status)

    @property
    def is_eligible(self) -> bool:
        """Alias for :attr:`is_enrollment_open`."""
        return is_site_enrollment_eligible(self.site_enrollment_status)


Site = ModelEngine.get_model('Site', Site)

__all__ = [
    "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
    "Site",
    "SiteEnrollmentStatus",
    "SiteStatusInput",
    "is_site_enrollment_eligible",
    "normalize_site_enrollment_status",
]
