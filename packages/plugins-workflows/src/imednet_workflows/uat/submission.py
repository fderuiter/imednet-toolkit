"""Two-phase bulk record submission workflow for UAT."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from imednet.spi.facade import ImednetFacade
from imednet.spi.models import ImednetBaseModel, Job
from imednet.spi.utils import JobFailedError, JobPoller, JobTimeoutError

from .generator import GeneratedRecordSet
from .models import RecordTestType

logger = logging.getLogger(__name__)


class BatchSubmission(ImednetBaseModel):
    """Tracks a single API call to records.create()."""

    phase: Literal["registration", "data"]
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


class BulkRecordSubmissionWorkflow:
    """Two-phase bulk record submission for UAT scenarios.

    Phase 1: Submit all RegisterSubjectRequest payloads and await job completion.
    Phase 2: Submit all UpdateScheduledRecordRequest and CreateNewRecordRequest payloads.

    Parameters
    ----------
    sdk : ImednetFacade
        Initialized synchronous SDK instance.
    batch_size : int
        Maximum number of records per API call. Default: 100.
    await_registration : bool
        If True, block until all Phase 1 jobs reach terminal state before
        submitting Phase 2 records. Default: True.
    registration_timeout : float
        Seconds to wait for Phase 1 jobs. Default: 600.
    skip_existing_subjects : bool
        If True, query the API for existing UAT-tagged subjects and skip
        registration for any that already exist. Default: True.
    """

    def __init__(
        self,
        sdk: ImednetFacade,
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

    def submit(
        self,
        study_key: str,
        record_sets: list[GeneratedRecordSet],
        *,
        email_notify: bool | str | None = None,
        keyword_tag: str = "UAT",
    ) -> SubmissionResult:
        """Execute the two-phase submission.

        Returns a SubmissionResult with all jobs and metadata.
        Raises BulkSubmissionError if Phase 1 jobs fail and Phase 2 depends on them.
        """
        result = SubmissionResult(
            study_key=study_key,
            started_at=datetime.now(UTC),
        )

        registration_sets = [
            rs for rs in record_sets if rs.test_type == RecordTestType.REGISTER_SUBJECT
        ]
        data_sets = [rs for rs in record_sets if rs.test_type != RecordTestType.REGISTER_SUBJECT]

        existing_subjects: set[str] = set()
        if self.skip_existing_subjects:
            existing_subjects = self._find_existing_uat_subjects(study_key, keyword_tag)

        # Phase 1: Registration
        for rs in registration_sets:
            # Let's assume GeneratedRecordSet.payloads and GeneratedRecordSet.subject_keys are 1:1
            filtered_payloads = []
            for i, payload in enumerate(rs.payloads):
                subj_key = rs.subject_keys[i] if i < len(rs.subject_keys) else None
                if subj_key and subj_key in existing_subjects:
                    logger.info("Skipping registration for existing subject: %s", subj_key)
                    continue
                filtered_payloads.append(payload)

            if not filtered_payloads:
                result.skipped_forms.append(rs.form_key)
                continue

            for chunk in self._chunk_payloads(filtered_payloads):
                batch = self._submit_batch(
                    study_key,
                    chunk,
                    phase="registration",
                    form_key=rs.form_key,
                    form_name=rs.form_name,
                    email_notify=email_notify,
                )
                result.registration_batches.append(batch)

        # Await registration completion if required
        if self.await_registration and result.registration_batches:
            self._await_registration_jobs(study_key, result.registration_batches)

        # Phase 2: Data Submission
        for rs in data_sets:
            for chunk in self._chunk_payloads(rs.payloads):
                batch = self._submit_batch(
                    study_key,
                    chunk,
                    phase="data",
                    form_key=rs.form_key,
                    form_name=rs.form_name,
                    email_notify=email_notify,
                )
                result.data_batches.append(batch)

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
        phase: Literal["registration", "data"],
        form_key: str | None,
        form_name: str | None,
        email_notify: bool | str | None,
    ) -> BatchSubmission:
        """Submit a single batch and return a BatchSubmission."""
        job = self.sdk.create_record(study_key, payloads, email_notify=email_notify)
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

        poller = JobPoller(get_job=self.sdk.get_job)
        batch_ids = [b.job.batch_id for b in batches_to_poll if b.job.batch_id is not None]
        summary = poller.poll_many(
            study_key,
            batch_ids,
            timeout=float(self.registration_timeout),
            fail_fast=False,
        )

        failed_batches: list[BatchSubmission] = []
        for batch in batches_to_poll:
            batch_id = batch.job.batch_id
            if batch_id in summary.failures:
                exc = summary.failures[batch_id]
                if isinstance(exc, JobTimeoutError):
                    batch.job.state = "TIMEOUT"
                elif isinstance(exc, JobFailedError):
                    status = getattr(exc, "status", None)
                    batch.job.state = getattr(status, "state", "FAILED") or "FAILED"
                else:
                    batch.job.state = "FAILED"
                failed_batches.append(batch)
            elif batch_id in summary.results:
                status = summary.results[batch_id]
                batch.job.state = status.state
                if not status.state or status.state.upper() not in ("COMPLETED", "SUCCESS"):
                    failed_batches.append(batch)
            else:
                batch.job.state = "UNKNOWN"
                failed_batches.append(batch)

        if failed_batches:
            raise BulkSubmissionError(
                f"{len(failed_batches)} registration batches failed.",
                failed_batches=failed_batches,
            )

    def _find_existing_uat_subjects(self, study_key: str, keyword_tag: str) -> set[str]:
        """Query subjects filtered by keyword to find pre-existing UAT subjects."""
        subjects = self.sdk.get_subjects(study_key, keyword=keyword_tag)
        existing_keys = set()
        for subj in subjects:
            key = getattr(subj, "subject_key", None) or getattr(subj, "subjectKey", None)
            if key:
                existing_keys.add(key)
        return existing_keys
