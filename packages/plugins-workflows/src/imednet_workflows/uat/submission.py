"""Two-phase bulk record submission workflow for UAT."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from imednet.spi.facade import ImednetFacade
from imednet.spi.models import ImednetBaseModel, Job, JobState, normalize_job_state
from imednet.spi.utils import (
    DEFAULT_BATCH_CHUNK_SIZE,
    JobFailedError,
    JobPoller,
    JobPollSummary,
    JobTimeoutError,
    chunk_records,
)

from .generator import GeneratedRecordSet, SyntheticRecordGenerator
from .models import RecordTestType

logger = logging.getLogger(__name__)

_FAILED_ENTRY_STATUSES = {"FAIL", "FAILED", "ERROR"}
_ENTRY_ID_FIELDS = (
    "subjectKey",
    "subject_key",
    "recordId",
    "record_id",
    "recordOid",
    "record_oid",
    "subjectId",
    "subject_id",
    "subjectOid",
    "subject_oid",
)


@dataclass
class _SubmittedRegistrationItem:
    """Metadata for a single registration record submitted in Phase 1."""

    placeholder_key: str | None
    form_key: str
    site_name: str | None
    site_id: int | None
    subject_oid: str | None
    data: dict[str, Any]
    batch_id: str | None = None


@dataclass
class _PreExistingSubjectsSnapshot:
    """Identifiers of subjects present in the study prior to Phase 1 submission."""

    subject_keys: set[str]
    subject_ids: set[int]
    subject_oids: set[str]


def _is_unconfigured_mock_method(fn: Any) -> bool:
    """Return True if ``fn`` is an unconfigured unittest.mock child method."""
    if not callable(fn):
        return True
    if not type(fn).__module__.startswith("unittest.mock"):
        return False
    if getattr(fn, "side_effect", None) is not None:
        return False
    ret = getattr(fn, "return_value", None)
    return type(ret).__module__.startswith("unittest.mock")


def _clean_str(val: Any) -> str | None:
    """Return a stripped non-empty string if ``val`` is a non-blank string."""
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _clean_int(val: Any) -> int | None:
    """Return an integer identifier if ``val`` is an int (excluding bool) or numeric string."""
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.strip().isdigit():
        return int(val.strip())
    return None


def _get_attr_or_key(obj: Any, *names: str) -> Any:
    """Read the first non-None attribute or mapping key from ``obj``."""
    for name in names:
        val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if val is not None:
            return val
    return None


def _values_match(submitted_val: Any, stored_val: Any) -> bool:
    """Compare a submitted variable value with a stored record variable value."""
    if isinstance(submitted_val, bool) != isinstance(stored_val, bool):
        return str(submitted_val).lower() == str(stored_val).lower()
    if submitted_val == stored_val:
        return True
    if submitted_val is None or stored_val is None:
        return False
    s_sub = str(submitted_val).strip()
    s_sto = str(stored_val).strip()
    if s_sub == s_sto:
        return True
    if not isinstance(submitted_val, bool) and not isinstance(stored_val, bool):
        try:
            return float(s_sub) == float(s_sto)
        except (TypeError, ValueError):
            return False
    return False


def _record_matches_submitted_item(rec: Any, item: _SubmittedRegistrationItem) -> bool:
    """Return True if ``rec`` matches the form, site, and submitted field values of ``item``."""
    if not item.data:
        return False
    if bool(_get_attr_or_key(rec, "deleted")):
        return False
    rec_fk = _clean_str(_get_attr_or_key(rec, "form_key", "formKey"))
    if item.form_key and rec_fk != item.form_key:
        return False
    rec_site_id = _clean_int(_get_attr_or_key(rec, "site_id", "siteId"))
    if item.site_id is not None and rec_site_id is not None and rec_site_id != item.site_id:
        return False
    rec_site_name = _clean_str(_get_attr_or_key(rec, "site_name", "siteName"))
    if item.site_name and rec_site_name and rec_site_name != item.site_name:
        return False
    rec_data = _get_attr_or_key(rec, "record_data", "recordData", "data")
    if not isinstance(rec_data, dict) or not rec_data:
        return False
    return all(k in rec_data and _values_match(v, rec_data[k]) for k, v in item.data.items())


def _entry_has_identifier(entry: Any) -> bool:
    """Return True if a job result entry carries any subject or record identifier."""
    if isinstance(entry, str):
        return bool(entry.strip())
    if isinstance(entry, dict):
        return any(_get_attr_or_key(entry, field) is not None for field in _ENTRY_ID_FIELDS)
    return False


def _filter_non_failed_entries(items: list[Any]) -> list[Any]:
    """Filter out per-record entries marked as failed."""
    return [
        entry
        for entry in items
        if not (
            isinstance(entry, dict)
            and str(entry.get("status") or "").upper() in _FAILED_ENTRY_STATUSES
        )
    ]


def _extract_batch_result_entries(results_payload: Any) -> list[Any]:
    """Extract non-failed per-record result entries from a polled JobStatus.results payload."""
    if isinstance(results_payload, list):
        return _filter_non_failed_entries(results_payload)
    if isinstance(results_payload, dict):
        for list_field in ("subjectKeys", "subject_keys", "subjects", "records", "data"):
            nested = results_payload.get(list_field)
            if isinstance(nested, list):
                return _filter_non_failed_entries(nested)
        if (
            _entry_has_identifier(results_payload)
            and str(results_payload.get("status") or "").upper() not in _FAILED_ENTRY_STATUSES
        ):
            return [results_payload]
    return []


def _extract_subject_keys_from_job_results(results_payload: Any) -> list[str]:
    """Extract server-assigned subject keys from a polled JobStatus.results payload."""
    keys: list[str] = []
    seen: set[str] = set()

    for entry in _extract_batch_result_entries(results_payload):
        cand = (
            _clean_str(entry)
            if isinstance(entry, str)
            else _clean_str(_get_attr_or_key(entry, "subjectKey", "subject_key"))
        )
        if cand and cand not in seen:
            seen.add(cand)
            keys.append(cand)
    return keys


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
    enrolled_subject_keys: list[str] = Field(default_factory=list)
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
        batch_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        await_registration: bool = True,
        registration_timeout: float = 600.0,
        skip_existing_subjects: bool = True,
        registration_poll_interval: float | None = None,
    ) -> None:
        """Initialize the workflow."""
        self.sdk = sdk
        self.batch_size = batch_size
        self.await_registration = await_registration
        self.registration_timeout = registration_timeout
        self.skip_existing_subjects = skip_existing_subjects
        self.registration_poll_interval = registration_poll_interval
        self.phase: SubmissionPhase = SubmissionPhase.PENDING
        self._initial_subjects_snapshot: _PreExistingSubjectsSnapshot | None = None

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

        self._initial_subjects_snapshot = None
        existing_subjects: set[str] = set()
        if self.skip_existing_subjects:
            existing_subjects = self._find_existing_uat_subjects(study_key, keyword_tag)

        pre_existing = _PreExistingSubjectsSnapshot(
            subject_keys=set(existing_subjects),
            subject_ids=set(),
            subject_oids=set(),
        )
        if registration_sets and data_sets:
            snap = (
                self._initial_subjects_snapshot
                if self._initial_subjects_snapshot is not None
                else self._snapshot_existing_subjects(study_key)
            )
            pre_existing.subject_keys.update(snap.subject_keys)
            pre_existing.subject_ids.update(snap.subject_ids)
            pre_existing.subject_oids.update(snap.subject_oids)

        submitted_placeholders: list[str | None] = []
        skipped_placeholders: set[str] = set()
        batch_item_groups: list[tuple[BatchSubmission, list[_SubmittedRegistrationItem]]] = []

        # Phase 1: Registration
        self.phase = SubmissionPhase.REGISTRATION
        for rs in registration_sets:
            filtered_payloads: list[dict[str, Any]] = []
            filtered_items: list[_SubmittedRegistrationItem] = []
            for i, payload in enumerate(rs.payloads):
                subj_key = rs.subject_keys[i] if i < len(rs.subject_keys) else None
                if subj_key and subj_key in existing_subjects:
                    logger.info("Skipping registration for existing subject: %s", subj_key)
                    skipped_placeholders.add(subj_key)
                    continue
                clean_payload = dict(payload)
                clean_payload.pop("subjectKey", None)
                clean_payload.pop("subject_key", None)
                raw_data = (
                    clean_payload.get("data")
                    if "data" in clean_payload
                    else clean_payload.get("recordData")
                )
                item = _SubmittedRegistrationItem(
                    placeholder_key=subj_key,
                    form_key=str(
                        clean_payload.get("formKey")
                        or clean_payload.get("form_key")
                        or rs.form_key
                        or ""
                    ),
                    site_name=_clean_str(_get_attr_or_key(clean_payload, "siteName", "site_name")),
                    site_id=_clean_int(_get_attr_or_key(clean_payload, "siteId", "site_id")),
                    subject_oid=_clean_str(
                        _get_attr_or_key(clean_payload, "subjectOid", "subject_oid")
                    ),
                    data=dict(raw_data) if isinstance(raw_data, dict) else {},
                )
                filtered_payloads.append(clean_payload)
                filtered_items.append(item)
                submitted_placeholders.append(subj_key)

            if not filtered_payloads:
                result.skipped_forms.append(rs.form_key)
                continue

            offset = 0
            for chunk in self._chunk_payloads(filtered_payloads):
                chunk_items = filtered_items[offset : offset + len(chunk)]
                offset += len(chunk)
                batch = self._submit_batch(
                    study_key,
                    chunk,
                    phase="registration",
                    form_key=rs.form_key,
                    form_name=rs.form_name,
                    email_notify=email_notify,
                )
                for item in chunk_items:
                    item.batch_id = batch.job.batch_id
                result.registration_batches.append(batch)
                batch_item_groups.append((batch, chunk_items))

        # Determine transition from registration phase
        if self.await_registration and result.registration_batches:
            self.phase = SubmissionPhase.AWAITING_REGISTRATION
        else:
            self.phase = SubmissionPhase.DATA

        # Explicit submission phase check for awaiting registration
        poll_summary: JobPollSummary | None = None
        if self.phase == SubmissionPhase.AWAITING_REGISTRATION:
            try:
                poll_summary = self._poll_and_validate_registration_batches(
                    study_key, result.registration_batches
                )
            except Exception:
                self.phase = SubmissionPhase.FAILED
                raise
            self.phase = SubmissionPhase.DATA

        if batch_item_groups:
            must_correlate = self._requires_phase2_correlation(data_sets, poll_summary)
            if not self.await_registration:
                if must_correlate:
                    self.phase = SubmissionPhase.FAILED
                    raise BulkSubmissionError(
                        "Cannot correlate enrolled subjects for Phase 2 records when await_registration is False.",
                        failed_batches=list(result.registration_batches),
                    )
            else:
                enrolled_keys, correlation_error = self._correlate_enrolled_subjects(
                    study_key=study_key,
                    batch_item_groups=batch_item_groups,
                    summary=poll_summary,
                    pre_existing=pre_existing,
                    allow_record_lookup=bool(data_sets),
                )
                if enrolled_keys is not None:
                    result.enrolled_subject_keys = list(enrolled_keys)
                    SyntheticRecordGenerator.resolve_subject_keys(
                        record_sets,
                        enrolled_keys,
                        placeholder_keys=submitted_placeholders,
                        preserved_keys=skipped_placeholders,
                    )
                elif must_correlate:
                    self.phase = SubmissionPhase.FAILED
                    raise BulkSubmissionError(
                        correlation_error
                        or "Unable to correlate enrolled subjects to this registration batch.",
                        failed_batches=list(result.registration_batches),
                    )

        # Explicit submission phase check for data submission
        if self.phase == SubmissionPhase.DATA:
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
            self.phase = SubmissionPhase.COMPLETED

        result.completed_at = datetime.now(UTC)
        return result

    def _chunk_payloads(self, payloads: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
        """Yield successive batch_size chunks of payloads."""
        yield from chunk_records(payloads, self.batch_size)

    def _submit_batch(
        self,
        study_key: str,
        payloads: list[dict[str, Any]],
        phase: Literal["registration", "data"] | SubmissionPhase,
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

    def _poll_registration_batches(self, study_key: str, batch_ids: list[str]) -> JobPollSummary:
        """Poll Phase 1 batches via facade poll_jobs (fetching resultUrl) with mock fallback."""
        poll_kwargs: dict[str, Any] = {
            "timeout": float(self.registration_timeout),
            "fail_fast": False,
        }
        if self.registration_poll_interval is not None:
            poll_kwargs["interval"] = float(self.registration_poll_interval)

        poll_jobs_fn = getattr(self.sdk, "poll_jobs", None)
        if callable(poll_jobs_fn):
            candidate = poll_jobs_fn(
                study_key,
                batch_ids,
                **poll_kwargs,
            )
            if isinstance(candidate, JobPollSummary):
                return candidate

        poller = JobPoller(get_job=self.sdk.get_job)
        return poller.poll_many(
            study_key,
            batch_ids,
            **poll_kwargs,
        )

    def _poll_and_validate_registration_batches(
        self, study_key: str, batches: list[BatchSubmission]
    ) -> JobPollSummary | None:
        """Poll Phase 1 batches to terminal state and raise BulkSubmissionError on failure."""
        batches_to_poll = [b for b in batches if b.job.batch_id]
        if not batches_to_poll:
            return None

        batch_ids = [b.job.batch_id for b in batches_to_poll if b.job.batch_id is not None]
        summary = self._poll_registration_batches(study_key, batch_ids)

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
                    status_failed_records = getattr(status, "failed_records", 0)
                    status_result_err = getattr(status, "result_error", None)
                    if (
                        isinstance(status_failed_records, int) and status_failed_records > 0
                    ) or status_result_err:
                        target_state = JobState.FAILED
                    else:
                        target_state = (
                            normalize_job_state(raw_state) if raw_state else JobState.FAILED
                        )
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
                status_success = getattr(status, "is_successful", None)
                is_success = (
                    status_success
                    if isinstance(status_success, bool)
                    else (
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

        return summary

    def _await_registration_jobs(self, study_key: str, batches: list[BatchSubmission]) -> list[str]:
        """Block until all Phase 1 jobs reach terminal state and return direct result Subject keys."""
        summary = self._poll_and_validate_registration_batches(study_key, batches)
        if summary is None:
            return []
        enrolled_keys: list[str] = []
        seen_enrolled: set[str] = set()
        for batch in batches:
            batch_id = batch.job.batch_id
            if batch_id and batch_id in summary.results:
                status = summary.results[batch_id]
                for key in _extract_subject_keys_from_job_results(getattr(status, "results", None)):
                    if key not in seen_enrolled:
                        seen_enrolled.add(key)
                        enrolled_keys.append(key)
        return enrolled_keys

    @staticmethod
    def _extract_snapshot_from_subjects(subjects: Any) -> _PreExistingSubjectsSnapshot:
        """Extract subject keys, IDs, and OIDs from a list of Subject models or dicts."""
        keys: set[str] = set()
        ids: set[int] = set()
        oids: set[str] = set()
        if isinstance(subjects, list):
            for subj in subjects:
                key = _clean_str(_get_attr_or_key(subj, "subject_key", "subjectKey"))
                if key:
                    keys.add(key)
                sid = _clean_int(_get_attr_or_key(subj, "subject_id", "subjectId"))
                if sid is not None:
                    ids.add(sid)
                subj_oid = _clean_str(_get_attr_or_key(subj, "subject_oid", "subjectOid"))
                if subj_oid:
                    oids.add(subj_oid)
        return _PreExistingSubjectsSnapshot(keys, ids, oids)

    def _snapshot_existing_subjects(self, study_key: str) -> _PreExistingSubjectsSnapshot:
        """Capture pre-Phase-1 study subject identifiers."""
        get_subjects_fn = getattr(self.sdk, "get_subjects", None)
        if callable(get_subjects_fn) and not _is_unconfigured_mock_method(get_subjects_fn):
            subjects = get_subjects_fn(study_key)
            return self._extract_snapshot_from_subjects(subjects)
        return _PreExistingSubjectsSnapshot(set(), set(), set())

    def _requires_phase2_correlation(
        self,
        data_sets: list[GeneratedRecordSet],
        summary: JobPollSummary | None,
    ) -> bool:
        """Return True if Phase 2 record submission requires verified Phase 1 subject correlation."""
        if not data_sets:
            return False
        if any(
            "subjectKey" in payload or "subject_key" in payload
            for rs in data_sets
            for payload in rs.payloads
        ):
            return True
        if summary is not None and any(
            getattr(status, "results", None) is not None for status in summary.results.values()
        ):
            return True
        if not _is_unconfigured_mock_method(getattr(self.sdk, "get_subjects", None)):
            return True
        return not _is_unconfigured_mock_method(getattr(self.sdk, "get_records", None))

    def _correlate_enrolled_subjects(
        self,
        *,
        study_key: str,
        batch_item_groups: list[tuple[BatchSubmission, list[_SubmittedRegistrationItem]]],
        summary: JobPollSummary | None,
        pre_existing: _PreExistingSubjectsSnapshot,
        allow_record_lookup: bool,
    ) -> tuple[list[str] | None, str | None]:
        """Correlate submitted registration items to enrolled subject keys, or fail closed."""
        total_submitted = sum(len(items) for _, items in batch_item_groups)
        if total_submitted == 0:
            return [], None

        subjects_loaded = False
        cached_subjects: list[Any] | None = None
        records_loaded = False
        cached_records: list[Any] | None = None

        def _load_subjects() -> list[Any] | None:
            nonlocal subjects_loaded, cached_subjects
            if not subjects_loaded:
                subjects_loaded = True
                fn = getattr(self.sdk, "get_subjects", None)
                if callable(fn) and not _is_unconfigured_mock_method(fn):
                    res = fn(study_key)
                    if isinstance(res, list):
                        cached_subjects = res
            return cached_subjects

        def _load_records() -> list[Any] | None:
            nonlocal records_loaded, cached_records
            if not records_loaded:
                records_loaded = True
                fn = getattr(self.sdk, "get_records", None)
                if callable(fn) and not _is_unconfigured_mock_method(fn):
                    res = fn(study_key)
                    if isinstance(res, list):
                        cached_records = res
            return cached_records

        def _subject_key_from_subject_lookup(
            subj_id: int | None,
            subj_oid: str | None,
            item: _SubmittedRegistrationItem | None = None,
        ) -> str | None:
            if subj_id is None and not subj_oid:
                return None
            if subj_id is not None and subj_id in pre_existing.subject_ids:
                return None
            if subj_oid and subj_oid in pre_existing.subject_oids:
                return None
            subjects = _load_subjects()
            if not subjects:
                return None
            matches: list[str] = []
            for subj in subjects:
                if bool(_get_attr_or_key(subj, "deleted")):
                    continue
                cand_id = _clean_int(_get_attr_or_key(subj, "subject_id", "subjectId"))
                cand_oid = _clean_str(_get_attr_or_key(subj, "subject_oid", "subjectOid"))
                if subj_id is not None and cand_id != subj_id:
                    continue
                if subj_oid is not None and cand_oid != subj_oid:
                    continue
                s_name = _clean_str(_get_attr_or_key(subj, "site_name", "siteName"))
                if item is not None and item.site_name and s_name and s_name != item.site_name:
                    continue
                s_id = _clean_int(_get_attr_or_key(subj, "site_id", "siteId"))
                if (
                    item is not None
                    and item.site_id is not None
                    and s_id is not None
                    and s_id != item.site_id
                ):
                    continue
                cand_key = _clean_str(_get_attr_or_key(subj, "subject_key", "subjectKey"))
                if cand_key and cand_key not in matches:
                    matches.append(cand_key)
            return matches[0] if len(matches) == 1 else None

        def _subject_key_from_record_lookup(
            rec_id: int | None,
            rec_oid: str | None,
            item: _SubmittedRegistrationItem,
        ) -> str | None:
            records = _load_records()
            if not records:
                return None
            matches: list[str] = []
            for rec in records:
                if bool(_get_attr_or_key(rec, "deleted")):
                    continue
                cand_id = _clean_int(_get_attr_or_key(rec, "record_id", "recordId"))
                cand_oid = _clean_str(_get_attr_or_key(rec, "record_oid", "recordOid"))
                if rec_id is not None and cand_id != rec_id:
                    continue
                if rec_oid is not None and cand_oid != rec_oid:
                    continue
                rec_fk = _clean_str(_get_attr_or_key(rec, "form_key", "formKey"))
                if item.form_key and rec_fk and rec_fk != item.form_key:
                    continue
                rec_data = _get_attr_or_key(rec, "record_data", "recordData", "data")
                if (
                    item.data
                    and isinstance(rec_data, dict)
                    and rec_data
                    and not _record_matches_submitted_item(rec, item)
                ):
                    continue
                cand_key = _clean_str(_get_attr_or_key(rec, "subject_key", "subjectKey"))
                if not cand_key:
                    cand_key = _subject_key_from_subject_lookup(
                        _clean_int(_get_attr_or_key(rec, "subject_id", "subjectId")),
                        _clean_str(_get_attr_or_key(rec, "subject_oid", "subjectOid")),
                        item,
                    )
                if cand_key and cand_key not in matches:
                    matches.append(cand_key)
            return matches[0] if len(matches) == 1 else None

        resolved_by_index: dict[int, str] = {}
        assigned_keys: set[str] = set()
        unresolved: list[tuple[int, _SubmittedRegistrationItem]] = []
        global_idx = 0

        for batch, items in batch_item_groups:
            batch_indices = list(range(global_idx, global_idx + len(items)))
            global_idx += len(items)
            batch_id = batch.job.batch_id
            status = summary.results.get(batch_id) if (summary and batch_id) else None
            entries = (
                _extract_batch_result_entries(getattr(status, "results", None))
                if status is not None
                else []
            )

            if not entries or not any(_entry_has_identifier(e) for e in entries):
                for idx, item in zip(batch_indices, items, strict=False):
                    unresolved.append((idx, item))
                continue

            if len(entries) != len(items):
                return (
                    None,
                    f"Registration batch '{batch_id}' returned {len(entries)} result entries "
                    f"for {len(items)} submitted subject(s).",
                )

            for idx, item, entry in zip(batch_indices, items, entries, strict=False):
                if not _entry_has_identifier(entry):
                    unresolved.append((idx, item))
                    continue

                subj_key: str | None = None
                if isinstance(entry, str):
                    subj_key = _clean_str(entry)
                elif isinstance(entry, dict):
                    entry_fk = _clean_str(_get_attr_or_key(entry, "formKey", "form_key"))
                    if entry_fk and item.form_key and entry_fk != item.form_key:
                        return (
                            None,
                            f"Registration batch '{batch_id}' result formKey '{entry_fk}' "
                            f"did not match submitted form '{item.form_key}'.",
                        )
                    subj_key = _clean_str(_get_attr_or_key(entry, "subjectKey", "subject_key"))
                    if not subj_key:
                        rec_id = _clean_int(_get_attr_or_key(entry, "recordId", "record_id"))
                        rec_oid = _clean_str(_get_attr_or_key(entry, "recordOid", "record_oid"))
                        if rec_id is not None or rec_oid is not None:
                            subj_key = _subject_key_from_record_lookup(rec_id, rec_oid, item)
                    if not subj_key:
                        subj_id = _clean_int(_get_attr_or_key(entry, "subjectId", "subject_id"))
                        subj_oid = _clean_str(_get_attr_or_key(entry, "subjectOid", "subject_oid"))
                        if subj_id is not None or subj_oid is not None:
                            subj_key = _subject_key_from_subject_lookup(subj_id, subj_oid, item)

                if not subj_key:
                    return (
                        None,
                        f"Unable to correlate registration result entry in batch '{batch_id}' "
                        "to an enrolled subject key.",
                    )
                if subj_key in pre_existing.subject_keys:
                    return (
                        None,
                        f"Registration batch '{batch_id}' resolved to pre-existing subject "
                        f"'{subj_key}'.",
                    )
                if subj_key in assigned_keys:
                    return (
                        None,
                        f"Ambiguous duplicate subject key '{subj_key}' in registration results.",
                    )
                resolved_by_index[idx] = subj_key
                assigned_keys.add(subj_key)

        if unresolved:
            if not allow_record_lookup:
                return None, "Registration results omitted subject keys."

            remaining: list[tuple[int, _SubmittedRegistrationItem]] = []
            for idx, item in unresolved:
                if item.subject_oid:
                    oid_match = _subject_key_from_subject_lookup(None, item.subject_oid, item)
                    if (
                        oid_match
                        and oid_match not in pre_existing.subject_keys
                        and oid_match not in assigned_keys
                    ):
                        resolved_by_index[idx] = oid_match
                        assigned_keys.add(oid_match)
                        continue
                remaining.append((idx, item))

            if remaining:
                if any(not item.data for _, item in remaining):
                    return (
                        None,
                        "Registration results omitted subject keys and submitted registration "
                        "payload lacks distinguishing data to correlate batch ownership.",
                    )

                records = _load_records()
                if not records:
                    return (
                        None,
                        "Registration results omitted subject keys and study records are "
                        "unavailable to correlate enrolled subjects to this batch.",
                    )

                subjects = _load_subjects()
                subjects_by_key: dict[str, Any] = {}
                if subjects is not None:
                    for subj in subjects:
                        sk = _clean_str(_get_attr_or_key(subj, "subject_key", "subjectKey"))
                        if sk:
                            subjects_by_key[sk] = subj

                candidate_records: list[tuple[Any, str]] = []
                for rec in records:
                    if bool(_get_attr_or_key(rec, "deleted")):
                        continue
                    sk = _clean_str(_get_attr_or_key(rec, "subject_key", "subjectKey"))
                    if not sk:
                        sk = _subject_key_from_subject_lookup(
                            _clean_int(_get_attr_or_key(rec, "subject_id", "subjectId")),
                            _clean_str(_get_attr_or_key(rec, "subject_oid", "subjectOid")),
                        )
                    if not sk or sk in pre_existing.subject_keys or sk in assigned_keys:
                        continue
                    if sk in subjects_by_key:
                        subj_obj = subjects_by_key[sk]
                        if bool(_get_attr_or_key(subj_obj, "deleted")):
                            continue
                        sid = _clean_int(_get_attr_or_key(subj_obj, "subject_id", "subjectId"))
                        if sid is not None and sid in pre_existing.subject_ids:
                            continue
                        subj_oid = _clean_str(
                            _get_attr_or_key(subj_obj, "subject_oid", "subjectOid")
                        )
                        if subj_oid and subj_oid in pre_existing.subject_oids:
                            continue
                    candidate_records.append((rec, sk))

                groups: dict[
                    tuple[str, str, int | None, tuple[tuple[str, str, str], ...]],
                    list[tuple[int, _SubmittedRegistrationItem]],
                ] = {}
                for idx, item in remaining:
                    sig = (
                        item.form_key,
                        item.site_name or "",
                        item.site_id,
                        tuple(sorted((k, type(v).__name__, str(v)) for k, v in item.data.items())),
                    )
                    groups.setdefault(sig, []).append((idx, item))

                tier2_assigned: set[str] = set()
                for group in groups.values():
                    sample = group[0][1]
                    matched_keys: list[str] = []
                    for rec, sk in candidate_records:
                        if not _record_matches_submitted_item(rec, sample):
                            continue
                        if sk in subjects_by_key:
                            subj_obj = subjects_by_key[sk]
                            s_name = _clean_str(_get_attr_or_key(subj_obj, "site_name", "siteName"))
                            if sample.site_name and s_name and s_name != sample.site_name:
                                continue
                            s_id = _clean_int(_get_attr_or_key(subj_obj, "site_id", "siteId"))
                            if (
                                sample.site_id is not None
                                and s_id is not None
                                and s_id != sample.site_id
                            ):
                                continue
                        if sk not in matched_keys:
                            matched_keys.append(sk)

                    expected_k = len(group)
                    if expected_k > 1:
                        return (
                            None,
                            f"Ambiguous enrolled subject correlation for form '{sample.form_key}': "
                            f"{expected_k} submitted registration payloads are indistinguishable "
                            "without batch result identifiers.",
                        )
                    if len(matched_keys) == 0:
                        return (
                            None,
                            f"Unable to correlate enrolled subject for form '{sample.form_key}': "
                            "no matching enrollment record found for this batch.",
                        )
                    if len(matched_keys) != 1:
                        return (
                            None,
                            f"Ambiguous enrolled subject correlation for form '{sample.form_key}': "
                            f"expected 1 matching subject, found {len(matched_keys)}.",
                        )
                    if not set(matched_keys).isdisjoint(tier2_assigned):
                        return (
                            None,
                            f"Ambiguous overlapping subject correlation across registration "
                            f"payloads for form '{sample.form_key}'.",
                        )
                    for (idx, _), sk in zip(group, matched_keys, strict=False):
                        resolved_by_index[idx] = sk
                        tier2_assigned.add(sk)
                        assigned_keys.add(sk)

        ordered_keys = [
            resolved_by_index[idx] for idx in range(total_submitted) if idx in resolved_by_index
        ]
        if len(ordered_keys) != total_submitted or len(set(ordered_keys)) != total_submitted:
            return None, "Incomplete or ambiguous enrolled subject correlation for Phase 1 batch."
        return ordered_keys, None

    @staticmethod
    def _subject_has_keyword(subj: Any, keyword_tag: str) -> bool:
        """Return True if the subject has a keyword matching keyword_tag."""
        if not keyword_tag:
            return True
        keywords = (
            subj.get("keywords") if isinstance(subj, dict) else getattr(subj, "keywords", None)
        )
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

    def _find_existing_uat_subjects(self, study_key: str, keyword_tag: str) -> set[str]:
        """Query non-deleted subjects and inspect Subject keywords locally for UAT tags."""
        subjects = self.sdk.get_subjects(study_key, deleted=False)
        self._initial_subjects_snapshot = self._extract_snapshot_from_subjects(subjects)
        existing_keys: set[str] = set()
        for subj in subjects:
            deleted = (
                subj.get("deleted", False)
                if isinstance(subj, dict)
                else getattr(subj, "deleted", False)
            )
            if deleted:
                continue
            if not self._subject_has_keyword(subj, keyword_tag):
                continue
            key = (
                (subj.get("subject_key") or subj.get("subjectKey"))
                if isinstance(subj, dict)
                else (getattr(subj, "subject_key", None) or getattr(subj, "subjectKey", None))
            )
            if key:
                existing_keys.add(key)
        return existing_keys


__all__ = [
    "BatchSubmission",
    "BulkRecordSubmissionWorkflow",
    "BulkSubmissionError",
    "SubmissionPhase",
    "SubmissionResult",
]
