"""Two-phase bulk record submission workflow for UAT."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import Field

from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet.spi.models import ImednetBaseModel, Job, JobState, normalize_job_state
from imednet.spi.utils import AsyncJobPoller, JobFailedError, JobPoller, JobTimeoutError

from .._guards import ensure_async_sdk, ensure_sync_sdk
from .generator import GeneratedRecordSet
from .models import RecordTestType

logger = logging.getLogger(__name__)
_OWNER = "BulkRecordSubmissionWorkflow"


class SubmissionPhase(StrEnum):
    """Phases of a two-phase bulk submission workflow."""

    PENDING = "pending"
    REGISTRATION = "registration"
    AWAITING_REGISTRATION = "awaiting_registration"
    DATA = "data"
    COMPLETED = "completed"
    FAILED = "failed"


class BatchSubmission(ImednetBaseModel):
    """Tracks a single API call to records.create()."""

    phase: Literal["registration", "data"] | SubmissionPhase
    form_key: str | None = None
    form_name: str | None = None
    record_count: int
    job: Job
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SubmissionResult(ImednetBaseModel):
    """Aggregate result of a full UAT submission run."""

    study_key: str
    started_at: datetime
    completed_at: datetime | None = None
    registration_batches: list[BatchSubmission] = Field(default_factory=list)
    data_batches: list[BatchSubmission] = Field(default_factory=list)
    skipped_forms: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def all_batches(self) -> list[BatchSubmission]:
        """Return all submission batches from both phases."""
        return self.registration_batches + self.data_batches

    @property
    def all_batch_ids(self) -> list[str]:
        """Return all batch IDs from all submitted jobs."""
        return [b.job.batch_id for b in self.all_batches if b.job.batch_id]

    @property
    def total_records_submitted(self) -> int:
        """Return the total count of records submitted across all batches."""
        return sum(b.record_count for b in self.all_batches)


class BulkSubmissionError(Exception):
    """Raised when Phase 1 registration jobs fail, blocking Phase 2 submission."""

    def __init__(self, message: str, failed_batches: list[BatchSubmission]) -> None:
        """Initialize the error.

        Args:
            message: The error message.
            failed_batches: List of batches that failed during submission.
        """
        super().__init__(message)
        self.failed_batches = failed_batches


def _subject_has_keyword(subj: Any, keyword_tag: str) -> bool:
    """Return True if ``subj`` has a keyword matching ``keyword_tag``."""
    if not keyword_tag:
        return True
    keywords = subj.get("keywords") if isinstance(subj, dict) else getattr(subj, "keywords", None)
    if not keywords:
        return False
    for kw in keywords:
        if isinstance(kw, str):
            if kw == keyword_tag:
                return True
            continue
        if isinstance(kw, dict):
            candidates = (
                kw.get("keyword_key"),
                kw.get("keywordKey"),
                kw.get("keyword_name"),
                kw.get("keywordName"),
            )
        else:
            candidates = (
                getattr(kw, "keyword_key", None),
                getattr(kw, "keywordKey", None),
                getattr(kw, "keyword_name", None),
                getattr(kw, "keywordName", None),
            )
        if keyword_tag in candidates:
            return True
    return False


def _extract_subject_keys_set(subjects: Iterable[Any], keyword_tag: str) -> set[str]:
    """Extract non-deleted subject keys matching ``keyword_tag`` from Subject items."""
    existing_keys: set[str] = set()
    for subj in subjects:
        deleted = (
            subj.get("deleted", False)
            if isinstance(subj, dict)
            else getattr(subj, "deleted", False)
        )
        if deleted:
            continue
        if not _subject_has_keyword(subj, keyword_tag):
            continue
        key = (
            (subj.get("subject_key") or subj.get("subjectKey"))
            if isinstance(subj, dict)
            else (getattr(subj, "subject_key", None) or getattr(subj, "subjectKey", None))
        )
        if key:
            existing_keys.add(key)
    return existing_keys


def _filter_registration_set_payloads(
    rs: GeneratedRecordSet,
    existing_subjects: set[str],
    result: SubmissionResult,
) -> list[dict[str, Any]]:
    """Filter payloads in a registration set against pre-existing subject keys."""
    filtered_payloads: list[dict[str, Any]] = []
    for i, payload in enumerate(rs.payloads):
        subj_key = rs.subject_keys[i] if i < len(rs.subject_keys) else None
        if subj_key and subj_key in existing_subjects:
            logger.info("Skipping registration for existing subject: %s", subj_key)
            continue
        filtered_payloads.append(payload)

    if not filtered_payloads:
        result.skipped_forms.append(rs.form_key)
    return filtered_payloads


def _reconcile_registration_poll_summary(
    batches_to_poll: list[BatchSubmission],
    summary: Any,
) -> None:
    """Update Phase 1 batch job states from ``JobPollSummary`` and raise on failures."""
    failed_batches: list[BatchSubmission] = []
    for batch in batches_to_poll:
        batch_id = batch.job.batch_id
        if batch_id in summary.failures:
            exc = summary.failures[batch_id]
            if isinstance(exc, JobTimeoutError):
                target_state = JobState.TIMEOUT
            elif isinstance(exc, JobFailedError):
                status = getattr(exc, "status", None)
                raw_state = getattr(status, "state", None)
                target_state = normalize_job_state(raw_state) if raw_state else JobState.FAILED
            else:
                target_state = JobState.FAILED
            batch.job.transition_to(target_state)
            if not isinstance(batch.job, Job):
                batch.job.state = target_state
            failed_batches.append(batch)
        elif batch_id in summary.results:
            status = summary.results[batch_id]
            raw_state = getattr(status, "state", None)
            target_state = normalize_job_state(raw_state) if raw_state else JobState.FAILED
            batch.job.transition_to(target_state)
            if not isinstance(batch.job, Job):
                batch.job.state = target_state
            is_success = (
                batch.job.is_successful
                if isinstance(batch.job.is_successful, bool)
                else (
                    getattr(batch.job, "state", None)
                    in (
                        JobState.COMPLETED,
                        JobState.COMPLETE,
                        JobState.SUCCESS,
                        "COMPLETED",
                        "COMPLETE",
                        "SUCCESS",
                    )
                )
            )
            if not is_success:
                failed_batches.append(batch)
        else:
            batch.job.transition_to(JobState.FAILED)
            if not isinstance(batch.job, Job):
                batch.job.state = JobState.FAILED
            failed_batches.append(batch)

    if failed_batches:
        raise BulkSubmissionError(
            f"{len(failed_batches)} registration batches failed.",
            failed_batches=failed_batches,
        )


class BulkRecordSubmissionWorkflow:
    """Two-phase bulk record submission for UAT scenarios.

    Phase 1: Submit all RegisterSubjectRequest payloads and await job completion.
    Phase 2: Submit all UpdateScheduledRecordRequest and CreateNewRecordRequest payloads.
    """

    def __init__(
        self,
        sdk: ImednetFacade | AsyncImednetFacade,
        batch_size: int = 100,
        await_registration: bool = True,
        registration_timeout: float = 600.0,
        skip_existing_subjects: bool = True,
    ) -> None:
        """Initialize the workflow."""
        self.sdk = sdk
        self.batch_size = batch_size
        self.await_registration = await_registration
        self.registration_timeout = registration_timeout
        self.skip_existing_subjects = skip_existing_subjects
        self.phase: SubmissionPhase = SubmissionPhase.PENDING

    @staticmethod
    def _split_record_sets(
        record_sets: list[GeneratedRecordSet],
    ) -> tuple[list[GeneratedRecordSet], list[GeneratedRecordSet]]:
        """Partition generated sets into Phase 1 registration and Phase 2 data sets."""
        registration_sets = [
            rs for rs in record_sets if rs.test_type == RecordTestType.REGISTER_SUBJECT
        ]
        data_sets = [rs for rs in record_sets if rs.test_type != RecordTestType.REGISTER_SUBJECT]
        return registration_sets, data_sets

    def _advance_post_registration_phase(self, result: SubmissionResult) -> None:
        """Set ``self.phase`` after Phase 1 registration batches have been submitted."""
        if self.await_registration and result.registration_batches:
            self.phase = SubmissionPhase.AWAITING_REGISTRATION
        else:
            self.phase = SubmissionPhase.DATA

    def submit(
        self,
        study_key: str,
        record_sets: list[GeneratedRecordSet],
        *,
        email_notify: bool | str | None = None,
        keyword_tag: str = "UAT",
    ) -> SubmissionResult:
        """Execute the synchronous two-phase submission."""
        ensure_sync_sdk(self.sdk, _OWNER, "submit", "async_submit")
        result = SubmissionResult(study_key=study_key, started_at=datetime.now(UTC))
        registration_sets, data_sets = self._split_record_sets(record_sets)

        existing_subjects: set[str] = set()
        if self.skip_existing_subjects:
            existing_subjects = self._find_existing_uat_subjects(study_key, keyword_tag)

        self.phase = SubmissionPhase.REGISTRATION
        for rs in registration_sets:
            filtered_payloads = _filter_registration_set_payloads(rs, existing_subjects, result)
            for chunk in self._chunk_payloads(filtered_payloads):
                result.registration_batches.append(
                    self._submit_batch(
                        study_key,
                        chunk,
                        phase="registration",
                        form_key=rs.form_key,
                        form_name=rs.form_name,
                        email_notify=email_notify,
                    )
                )

        self._advance_post_registration_phase(result)
        if self.phase == SubmissionPhase.AWAITING_REGISTRATION:
            try:
                self._await_registration_jobs(study_key, result.registration_batches)
            except Exception:
                self.phase = SubmissionPhase.FAILED
                raise
            self.phase = SubmissionPhase.DATA

        if self.phase == SubmissionPhase.DATA:
            for rs in data_sets:
                for chunk in self._chunk_payloads(rs.payloads):
                    result.data_batches.append(
                        self._submit_batch(
                            study_key,
                            chunk,
                            phase="data",
                            form_key=rs.form_key,
                            form_name=rs.form_name,
                            email_notify=email_notify,
                        )
                    )
            self.phase = SubmissionPhase.COMPLETED

        result.completed_at = datetime.now(UTC)
        return result

    async def async_submit(
        self,
        study_key: str,
        record_sets: list[GeneratedRecordSet],
        *,
        email_notify: bool | str | None = None,
        keyword_tag: str = "UAT",
    ) -> SubmissionResult:
        """Asynchronously execute the two-phase submission."""
        ensure_async_sdk(self.sdk, _OWNER, "async_submit", "submit")
        result = SubmissionResult(study_key=study_key, started_at=datetime.now(UTC))
        registration_sets, data_sets = self._split_record_sets(record_sets)

        existing_subjects: set[str] = set()
        if self.skip_existing_subjects:
            existing_subjects = await self._async_find_existing_uat_subjects(study_key, keyword_tag)

        self.phase = SubmissionPhase.REGISTRATION
        for rs in registration_sets:
            filtered_payloads = _filter_registration_set_payloads(rs, existing_subjects, result)
            for chunk in self._chunk_payloads(filtered_payloads):
                result.registration_batches.append(
                    await self._async_submit_batch(
                        study_key,
                        chunk,
                        phase="registration",
                        form_key=rs.form_key,
                        form_name=rs.form_name,
                        email_notify=email_notify,
                    )
                )

        self._advance_post_registration_phase(result)
        if self.phase == SubmissionPhase.AWAITING_REGISTRATION:
            try:
                await self._async_await_registration_jobs(study_key, result.registration_batches)
            except Exception:
                self.phase = SubmissionPhase.FAILED
                raise
            self.phase = SubmissionPhase.DATA

        if self.phase == SubmissionPhase.DATA:
            for rs in data_sets:
                for chunk in self._chunk_payloads(rs.payloads):
                    result.data_batches.append(
                        await self._async_submit_batch(
                            study_key,
                            chunk,
                            phase="data",
                            form_key=rs.form_key,
                            form_name=rs.form_name,
                            email_notify=email_notify,
                        )
                    )
            self.phase = SubmissionPhase.COMPLETED

        result.completed_at = datetime.now(UTC)
        return result

    def _chunk_payloads(self, payloads: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
        """Yield successive batch_size chunks of payloads."""
        for i in range(0, len(payloads), self.batch_size):
            yield payloads[i : i + self.batch_size]

    def _submit_batch(
        self,
        study_key: str,
        payloads: list[dict[str, Any]],
        phase: Literal["registration", "data"] | SubmissionPhase,
        form_key: str | None,
        form_name: str | None,
        email_notify: bool | str | None,
    ) -> BatchSubmission:
        """Submit a single batch synchronously and return a BatchSubmission."""
        sdk = cast("ImednetFacade", self.sdk)
        job = sdk.create_record(study_key, payloads, email_notify=email_notify)
        return BatchSubmission(
            phase=phase,
            form_key=form_key,
            form_name=form_name,
            record_count=len(payloads),
            job=job,
        )

    async def _async_submit_batch(
        self,
        study_key: str,
        payloads: list[dict[str, Any]],
        phase: Literal["registration", "data"] | SubmissionPhase,
        form_key: str | None,
        form_name: str | None,
        email_notify: bool | str | None,
    ) -> BatchSubmission:
        """Submit a single batch asynchronously and return a BatchSubmission."""
        sdk = cast("AsyncImednetFacade", self.sdk)
        job = await sdk.async_create_record(study_key, payloads, email_notify=email_notify)
        return BatchSubmission(
            phase=phase,
            form_key=form_key,
            form_name=form_name,
            record_count=len(payloads),
            job=job,
        )

    def _await_registration_jobs(self, study_key: str, batches: list[BatchSubmission]) -> None:
        """Block until all Phase 1 jobs reach terminal state concurrently."""
        batches_to_poll = [b for b in batches if b.job.batch_id]
        if not batches_to_poll:
            return

        sdk = cast("ImednetFacade", self.sdk)
        poller = JobPoller(get_job=sdk.get_job)
        batch_ids = [b.job.batch_id for b in batches_to_poll if b.job.batch_id is not None]
        summary = poller.poll_many(
            study_key,
            batch_ids,
            timeout=float(self.registration_timeout),
            fail_fast=False,
        )
        _reconcile_registration_poll_summary(batches_to_poll, summary)

    async def _async_await_registration_jobs(
        self, study_key: str, batches: list[BatchSubmission]
    ) -> None:
        """Await all Phase 1 registration jobs concurrently via ``AsyncJobPoller``."""
        batches_to_poll = [b for b in batches if b.job.batch_id]
        if not batches_to_poll:
            return

        sdk = cast("AsyncImednetFacade", self.sdk)
        poller = AsyncJobPoller(get_job=sdk.async_get_job)
        batch_ids = [b.job.batch_id for b in batches_to_poll if b.job.batch_id is not None]
        summary = await poller.async_poll_many(
            study_key,
            batch_ids,
            timeout=float(self.registration_timeout),
            fail_fast=False,
        )
        _reconcile_registration_poll_summary(batches_to_poll, summary)

    @staticmethod
    def _subject_has_keyword(subj: Any, keyword_tag: str) -> bool:
        """Return True if the subject has a keyword matching keyword_tag."""
        return _subject_has_keyword(subj, keyword_tag)

    def _find_existing_uat_subjects(self, study_key: str, keyword_tag: str) -> set[str]:
        """Query non-deleted subjects and inspect Subject keywords locally for UAT tags."""
        sdk = cast("ImednetFacade", self.sdk)
        return _extract_subject_keys_set(sdk.get_subjects(study_key, deleted=False), keyword_tag)

    async def _async_find_existing_uat_subjects(self, study_key: str, keyword_tag: str) -> set[str]:
        """Asynchronously query non-deleted subjects and filter locally for UAT tags."""
        sdk = cast("AsyncImednetFacade", self.sdk)
        subjects = await sdk.async_get_subjects(study_key, deleted=False)
        return _extract_subject_keys_set(subjects, keyword_tag)


__all__ = [
    "BatchSubmission",
    "BulkRecordSubmissionWorkflow",
    "BulkSubmissionError",
    "SubmissionPhase",
    "SubmissionResult",
]
