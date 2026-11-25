# pylint: disable=duplicate-code
"""Record revision history models for iMedNet."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine
from imednet.models.records import RecordStatus, normalize_record_status


def _coerce_optional_int_for_bounds(value: Any, field_name: str) -> int | None:
    """Coerce optional integer input while rejecting booleans for bounds checking."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not bool.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _parse_revision_timestamp(value: Any) -> datetime | None:
    """Parse an optional revision timestamp into a UTC-normalized datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


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
            next_rec_rev = value if name == "record_revision" else self.record_revision
            next_data_rev = value if name == "data_revision" else self.data_revision
            _validate_revision_bounds(next_rec_rev, next_data_rev)
        elif name == "record_status" and "record_status" in self.__dict__:
            value = normalize_record_status(value)
        super().__setattr__(name, value)

    def validate_follows(
        self,
        previous: RecordRevision,
        *,
        allow_partial: bool = False,
    ) -> bool:
        """Validate that this revision chronologically follows *previous*.

        Args:
            previous: Immediately preceding revision in the sequence.
            allow_partial: When True, allow gaps > +1 in ``record_revision`` for
                actor-filtered or date-bounded audit slices while still enforcing
                strictly increasing ``record_revision``, non-decreasing ``data_revision``,
                ordered ``date_created`` timestamps, and irreversible deletions.
        """
        if previous.record_revision is None or self.record_revision is None:
            raise ValueError("RecordRevision chain validation requires non-null record_revision.")
        if self.record_revision <= previous.record_revision:
            raise ValueError(
                f"Non-monotonic record_revision: {self.record_revision} must be greater than "
                f"previous {previous.record_revision}."
            )
        if not allow_partial and self.record_revision != previous.record_revision + 1:
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
        prev_dt = _parse_revision_timestamp(getattr(previous, "date_created", None))
        curr_dt = _parse_revision_timestamp(getattr(self, "date_created", None))
        if prev_dt is not None and curr_dt is not None and curr_dt < prev_dt:
            raise ValueError(
                f"Backdated date_created: {getattr(self, 'date_created', None)!r} precedes "
                f"previous {getattr(previous, 'date_created', None)!r}."
            )
        if bool(getattr(previous, "deleted", False)) and not bool(getattr(self, "deleted", False)):
            raise ValueError(
                "Irreversible deletion violated: deleted record cannot revert to active state."
            )
        return True

    def can_follow(
        self,
        previous: RecordRevision,
        *,
        allow_partial: bool = False,
    ) -> bool:
        """Return True if this revision monotonically follows *previous*."""
        try:
            return self.validate_follows(previous, allow_partial=allow_partial)
        except ValueError:
            return False

    @classmethod
    def validate_chain(
        cls,
        revisions: Sequence[RevisionInput],
        *,
        allow_partial: bool = False,
    ) -> list[RecordRevision]:
        """Validate a chronological sequence of record revisions for monotonicity."""
        return validate_record_revision_chain(revisions, allow_partial=allow_partial)


RecordRevision = ModelEngine.get_model('RecordRevision', RecordRevision)

RevisionInput = RecordRevision | dict[str, Any]


def validate_record_revision_chain(
    revisions: Sequence[RevisionInput],
    *,
    allow_partial: bool = False,
) -> list[RecordRevision]:
    """Validate strict or partial-slice 21 CFR Part 11 invariants across *revisions*."""
    parsed: list[RecordRevision] = [
        item if isinstance(item, RecordRevision) else RecordRevision.model_validate(item)
        for item in revisions
    ]
    chains: dict[tuple[str, str, Any], list[RecordRevision]] = {}
    for rev in parsed:
        study_scope = str(getattr(rev, "study_key", None) or "")
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
                f"Missing initial record_revision in chain: expected 1, got {group[0].record_revision}."
            )
        for prev, curr in pairwise(group):
            curr.validate_follows(prev, allow_partial=allow_partial)

    return parsed


def is_valid_record_revision_chain(
    revisions: Sequence[RevisionInput],
    *,
    allow_partial: bool = False,
) -> bool:
    """Return True if *revisions* satisfy bounds and monotonic chain invariants."""
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
