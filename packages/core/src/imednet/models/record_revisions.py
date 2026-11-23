# pylint: disable=duplicate-code
"""Record revision history models for iMedNet."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine
from imednet.models.records import (
    RecordStatus,
    normalize_record_status,
    validate_record_transition,
)
from imednet.utils.dates import parse_iso_datetime

_SIGNED_ASCII_INT_RE = re.compile(r"^[+-]?[0-9]+$")


def _coerce_optional_int_for_bounds(value: Any, field_name: str) -> int | None:
    """Coerce optional integer input while rejecting booleans, floats, and non-integer strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not bool.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if _SIGNED_ASCII_INT_RE.fullmatch(stripped):
            return int(stripped)
        raise ValueError(f"{field_name} must be an integer, got {value!r}.")
    raise ValueError(f"{field_name} must be an integer, not {type(value).__name__}.")


def _validate_revision_bounds(
    record_revision: int | None,
    data_revision: int | None,
) -> None:
    """Validate 21 CFR Part 11 numeric bounds on a record revision entry."""
    if isinstance(record_revision, bool):
        raise ValueError("record_revision must be an integer >= 1, not bool.")
    if isinstance(data_revision, bool):
        raise ValueError("data_revision must be an integer >= 0, not bool.")
    if record_revision is not None and record_revision < 1:
        raise ValueError(f"record_revision must be >= 1, got {record_revision}.")
    if data_revision is not None and data_revision < 0:
        raise ValueError(f"data_revision must be >= 0, got {data_revision}.")
    if (
        record_revision is not None
        and data_revision is not None
        and data_revision > record_revision
    ):
        raise ValueError(
            f"data_revision ({data_revision}) cannot exceed record_revision ({record_revision})."
        )


def _is_revision_deleted(revision: RecordRevision) -> bool:
    """Return True when a revision represents a soft-deleted terminal Record state."""
    return (
        getattr(revision, "deleted", None) is True
        or normalize_record_status(getattr(revision, "record_status", None)) == RecordStatus.DELETED
    )


def _validate_revision_attribution_and_timestamp(revision: RecordRevision) -> datetime:
    """Verify 21 CFR 11.10(e) operator attribution and parse UTC date_created timestamp."""
    user = getattr(revision, "user", None)
    if user is None or not str(user).strip():
        raise ValueError("RecordRevision chain validation requires non-empty 'user' attribution.")

    reason = getattr(revision, "reason_for_change", None)
    if reason is None or not str(reason).strip():
        raise ValueError(
            "RecordRevision chain validation requires non-empty 'reason_for_change' attribution."
        )

    raw_ts = getattr(revision, "date_created", None)
    if raw_ts is None or not str(raw_ts).strip():
        raise ValueError(
            "RecordRevision chain validation requires non-empty 'date_created' timestamp."
        )

    dt = raw_ts if isinstance(raw_ts, datetime) else parse_iso_datetime(str(raw_ts).strip())
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class RecordRevision(ImednetBaseModel):
    """Historical version of a record including change reason and user."""

    record_revision: int | None = Field(default=None, alias="recordRevision")
    data_revision: int | None = Field(default=None, alias="dataRevision")
    record_status: RecordStatus | str | None = Field(default=None, alias="recordStatus")

    @model_validator(mode="before")
    @classmethod
    def _validate_bounds(cls, data: Any) -> Any:
        """Enforce ``record_revision >= 1`` and ``0 <= data_revision <= record_revision``."""
        if isinstance(data, dict):
            raw_rec = (
                data["record_revision"] if "record_revision" in data else data.get("recordRevision")
            )
            raw_data = (
                data["data_revision"] if "data_revision" in data else data.get("dataRevision")
            )
            rec_rev = _coerce_optional_int_for_bounds(raw_rec, "record_revision")
            data_rev = _coerce_optional_int_for_bounds(raw_data, "data_revision")
            _validate_revision_bounds(rec_rev, data_rev)
        return data

    @field_validator("record_status", mode="before")
    @classmethod
    def _validate_record_status(cls, value: Any) -> RecordStatus | str | None:
        """Normalize known record statuses and preserve custom study strings."""
        if value is None or isinstance(value, (RecordStatus, str)):
            return normalize_record_status(value)
        return normalize_record_status(str(value))

    def __setattr__(self, name: str, value: Any) -> None:
        """Enforce revision bounds and status normalization on direct attribute mutation."""
        if name in {"record_revision", "data_revision"} and (
            "record_revision" in self.__dict__ or "data_revision" in self.__dict__
        ):
            coerced = _coerce_optional_int_for_bounds(value, name)
            next_rec_rev = coerced if name == "record_revision" else self.record_revision
            next_data_rev = coerced if name == "data_revision" else self.data_revision
            _validate_revision_bounds(next_rec_rev, next_data_rev)
            value = coerced
        elif name == "record_status" and "record_status" in self.__dict__:
            value = normalize_record_status(value)
        super().__setattr__(name, value)

    def validate_follows(self, previous: RecordRevision) -> bool:
        """Validate that this revision contiguously and chronologically follows *previous*."""
        if previous.record_revision is None or self.record_revision is None:
            raise ValueError("RecordRevision chain validation requires non-null record_revision.")
        if self.record_revision <= previous.record_revision:
            raise ValueError(
                f"Non-monotonic record_revision: {self.record_revision} must be greater than "
                f"previous {previous.record_revision}."
            )
        if self.record_revision != previous.record_revision + 1:
            raise ValueError(
                f"Missing record_revision in chain: expected {previous.record_revision + 1}, "
                f"got {self.record_revision}."
            )
        if (
            previous.data_revision is not None
            and self.data_revision is not None
            and self.data_revision < previous.data_revision
        ):
            raise ValueError(
                f"Decreasing data_revision: {self.data_revision} cannot be less than "
                f"previous {previous.data_revision}."
            )
        if _is_revision_deleted(previous):
            target_status = (
                self.record_status
                if self.record_status is not None
                and (not isinstance(self.record_status, str) or self.record_status.strip())
                else RecordStatus.RECORD_INCOMPLETE
            )
            validate_record_transition(
                previous.record_status,
                target_status,
                deleted=True,
            )

        prev_dt = _validate_revision_attribution_and_timestamp(previous)
        curr_dt = _validate_revision_attribution_and_timestamp(self)
        if curr_dt < prev_dt:
            raise ValueError(
                f"Backdated date_created (Regressed UTC date_created): "
                f"{curr_dt.isoformat()} precedes previous {prev_dt.isoformat()}."
            )
        return True

    def can_follow(self, previous: RecordRevision) -> bool:
        """Return True if this revision monotonically follows *previous*."""
        try:
            return self.validate_follows(previous)
        except ValueError:
            return False

    @classmethod
    def validate_chain(
        cls,
        revisions: Sequence[RevisionInput],
        *,
        allow_partial: bool = False,
    ) -> list[RecordRevision]:
        """Validate a chronological sequence of record revisions for 21 CFR 11.10(e) integrity."""
        return validate_record_revision_chain(revisions, allow_partial=allow_partial)


RecordRevision = ModelEngine.get_model('RecordRevision', RecordRevision)

RevisionInput = RecordRevision | dict[str, Any]


def validate_record_revision_chain(
    revisions: Sequence[RevisionInput],
    *,
    allow_partial: bool = False,
) -> list[RecordRevision]:
    """Validate 21 CFR 11.10(e) contiguity, UTC timestamps, attribution, and terminal deletion."""
    parsed: list[RecordRevision] = [
        item if isinstance(item, RecordRevision) else RecordRevision.model_validate(item)
        for item in revisions
    ]
    chains: dict[tuple[str, str, Any], list[RecordRevision]] = {}
    for rev in parsed:
        study_scope = str(getattr(rev, "study_key", None) or "").strip()
        rec_id = getattr(rev, "record_id", None)
        rec_oid = getattr(rev, "record_oid", None)
        if rec_id is not None:
            key: tuple[str, str, Any] = (study_scope, "id", rec_id)
        elif rec_oid:
            key = (study_scope, "oid", rec_oid)
        else:
            key = (study_scope, "unkeyed", None)
        chains.setdefault(key, []).append(rev)

    for group in chains.values():
        if not group:
            continue
        if group[0].record_revision is None:
            raise ValueError("RecordRevision chain validation requires non-null record_revision.")
        if not allow_partial and group[0].record_revision != 1:
            raise ValueError(
                f"RecordRevision chain must start at record_revision 1, got "
                f"{group[0].record_revision} (pass allow_partial=True for bounded slices)."
            )
        _validate_revision_attribution_and_timestamp(group[0])
        for prev, curr in pairwise(group):
            curr.validate_follows(prev)

    return parsed


def is_valid_record_revision_chain(
    revisions: Sequence[RevisionInput],
    *,
    allow_partial: bool = False,
) -> bool:
    """Return True if *revisions* satisfy bounds and 21 CFR 11.10(e) chain invariants."""
    try:
        validate_record_revision_chain(revisions, allow_partial=allow_partial)
        return True
    except (ValueError, ValidationError):
        return False


__all__ = [
    "RecordRevision",
    "RevisionInput",
    "is_valid_record_revision_chain",
    "validate_record_revision_chain",
]
