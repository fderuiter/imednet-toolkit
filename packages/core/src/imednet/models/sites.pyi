# pylint: disable=duplicate-code
"""Site (study location) models for iMedNet."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, field_validator

from imednet.models.base import ImednetBaseModel

def _normalize_status_token(value: str) -> str: ...

class SiteEnrollmentStatus(str, Enum):
    """Normalized enrollment lifecycle statuses for study sites."""

    ENROLLMENT_OPEN = "Enrollment Open"
    ENROLLMENT_CLOSED = "Enrollment Closed"
    ACTIVE = "Active"
    CLOSED = "Closed"
    INACTIVE = "Inactive"
    READ_ONLY = "Read Only"

    def __str__(self) -> str: ...
    @classmethod
    def _missing_(cls, value: object) -> SiteEnrollmentStatus | None: ...
    @property
    def is_eligible(self) -> bool: ...

ELIGIBLE_SITE_ENROLLMENT_STATUSES: frozenset[SiteEnrollmentStatus]

SiteStatusInput = SiteEnrollmentStatus | str | None

def normalize_site_enrollment_status(value: SiteStatusInput) -> SiteStatusInput: ...
def is_site_enrollment_eligible(value: SiteEnrollmentStatus | str | None) -> bool: ...

class Site(ImednetBaseModel):
    """A site participating in a study."""

    site_enrollment_status: SiteEnrollmentStatus | str | None = Field(
        default=None, alias="siteEnrollmentStatus"
    )
    study_key: str | None = Field(default=None, alias="studyKey")
    site_id: int | None = Field(default=None, alias="siteId")
    site_name: str | None = Field(default=None, alias="siteName")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def __init__(
        self,
        *,
        site_enrollment_status: SiteEnrollmentStatus | str | None = None,
        siteEnrollmentStatus: SiteEnrollmentStatus | str | None = None,
        study_key: str | None = None,
        studyKey: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        site_name: str | None = None,
        siteName: str | None = None,
        date_created: str | None = None,
        dateCreated: str | None = None,
        date_modified: str | None = None,
        dateModified: str | None = None,
        last_updated: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...
    @field_validator("site_enrollment_status", mode="before")
    @classmethod
    def _validate_site_enrollment_status(cls, value: Any) -> SiteEnrollmentStatus | str | None: ...
    @property
    def is_enrollment_open(self) -> bool: ...
    @property
    def is_eligible(self) -> bool: ...

__all__ = [
    "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
    "Site",
    "SiteEnrollmentStatus",
    "SiteStatusInput",
    "is_site_enrollment_eligible",
    "normalize_site_enrollment_status",
]
