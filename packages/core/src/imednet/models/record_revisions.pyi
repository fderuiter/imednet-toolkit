"""Record revision history models for iMedNet."""

from __future__ import annotations

from imednet.models.base import ImednetBaseModel

class RecordRevision(ImednetBaseModel):
    """Historical version of a record including change reason and user."""

    study_key: str | None
    record_revision_id: int | None
    record_id: int | None
    record_revision: int | None
    data_revision: int | None
    record_status: str | None
    subject_id: int | None
    subject_key: str | None
    site_id: int | None
    form_key: str | None
    interval_id: int | None
    deleted: bool | None
    date_created: str | None
    reason_for_change: str | None
    record_oid: str | None
    role: str | None
    subject_oid: str | None
    user: str | None
    last_updated: str | None
