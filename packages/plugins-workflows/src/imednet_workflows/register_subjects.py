"""Workflow for registering subjects in iMednet via the Records API.

This workflow is self-contained and does not borrow from record_update.py.
It provides a simple, robust interface for registering one or more subjects.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from imednet.spi.models import Job, RegisterSubjectRequest
from imednet.spi.utils import (
    DEFAULT_BATCH_CHUNK_SIZE,
    BatchCoordinator,
    BatchSubmissionSummary,
    JobPollSummary,
)
from imednet.spi.validation import SchemaCache

# Use TYPE_CHECKING to avoid circular import at runtime
if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


def _extract_subject_identifier(
    subj: RegisterSubjectRequest | dict[str, Any],
    payload: dict[str, Any],
    subject_key_field: str | None,
) -> str | None:
    """Extract an optional subject key or OID candidate for enrollment deduplication."""
    for attr in ("subject_key", "subjectKey", "subject_oid", "subjectOid"):
        direct = (
            subj.get(attr) if isinstance(subj, dict) else getattr(subj, attr, None)
        ) or payload.get(attr)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

    data = payload.get("data")
    if isinstance(data, dict):
        if subject_key_field and subject_key_field in data and data[subject_key_field] is not None:
            cand = str(data[subject_key_field]).strip()
            return cand or None
        for key in ("subjectKey", "subject_key", "subjectOid", "subject_oid", "SUBJID"):
            if key in data and data[key] is not None:
                cand = str(data[key]).strip()
                if cand:
                    return cand
    return None


class RegisterSubjectsWorkflow:
    """Manages the registration of subjects using the iMedNet SDK.

    Attributes:
        _sdk (ImednetSDK): An instance of the ImednetSDK.
    """

    def __init__(
        self,
        sdk: "ImednetFacade",
        *,
        batch_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        filter_enrolled: bool = False,
    ):  # Use string literal for type hint
        """Initialize the RegisterSubjectsWorkflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
            batch_size: Maximum number of records per chunked POST request (default: 100).
            filter_enrolled: If True, filter out subjects already enrolled in the study.
        """
        self._sdk = sdk
        self._batch_size = batch_size
        self._filter_enrolled = filter_enrolled

    def _poll_many(
        self, study_key: str, batch_ids: list[str], *, timeout: int, poll_interval: int
    ) -> JobPollSummary:
        """Poll multiple registration chunk jobs via poll_jobs with fallback to poll_job."""
        poll_jobs_fn = getattr(self._sdk, "poll_jobs", None)
        if callable(poll_jobs_fn):
            candidate = poll_jobs_fn(
                study_key,
                batch_ids,
                timeout=float(timeout),
                interval=float(poll_interval),
            )
            if isinstance(candidate, JobPollSummary):
                return candidate

        results = {}
        failures = {}
        for bid in batch_ids:
            try:
                results[bid] = self._sdk.poll_job(
                    study_key, bid, timeout=timeout, interval=poll_interval
                )
            except Exception as exc:
                failures[bid] = exc
                raise
        return JobPollSummary(
            study_key=study_key,
            results=results,
            failures=failures,
            elapsed_total=0.0,
        )

    def register_subjects_batch(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]],
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: SchemaCache | None = None,
    ) -> BatchSubmissionSummary:
        """Validate sites once, optionally filter enrolled subjects, and submit in bounded chunks."""
        sites = {s.site_name for s in self._sdk.get_sites(study_key=study_key)}
        normalized_pairs: list[
            tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]
        ] = []
        errors: list[str] = []
        for idx, raw_subj in enumerate(subjects):
            subj = (
                RegisterSubjectRequest.model_validate(raw_subj)
                if isinstance(raw_subj, dict)
                else raw_subj
            )
            normalized_pairs.append((raw_subj, subj))
            if not subj.site_name:
                errors.append(f"Index {idx}: siteName is required")
            elif subj.site_name not in sites:
                errors.append(f"Index {idx}: site '{subj.site_name}' not found")

        if errors:
            raise ValueError("; ".join(errors))

        should_filter = (
            self._filter_enrolled if filter_enrolled is None else filter_enrolled
        ) or skip_existing

        enrolled_keys: set[str] = set()
        if should_filter:
            for existing in self._sdk.get_subjects(study_key=study_key):
                if getattr(existing, "deleted", False):
                    continue
                for attr in ("subject_key", "subjectKey", "subject_oid", "subjectOid"):
                    val = getattr(existing, attr, None)
                    if isinstance(val, str) and val.strip():
                        enrolled_keys.add(val.strip())

        subjects_data: list[dict[str, Any]] = []
        skipped_count = 0
        seen_in_batch: set[str] = set()
        for raw_subj, subj in normalized_pairs:
            dumped = subj.model_dump(by_alias=True)
            if should_filter:
                cand_id = _extract_subject_identifier(raw_subj, dumped, subject_key_field)
                if cand_id and (cand_id in enrolled_keys or cand_id in seen_in_batch):
                    skipped_count += 1
                    continue
                if cand_id:
                    seen_in_batch.add(cand_id)
            subjects_data.append(dumped)

        def _submit_chunk(chunk: list[dict[str, Any]]) -> Job:
            if schema is not None:
                return self._sdk.create_record(
                    study_key=study_key,
                    records_data=chunk,
                    email_notify=email_notify,
                    schema=schema,
                )
            return self._sdk.create_record(
                study_key=study_key,
                records_data=chunk,
                email_notify=email_notify,
            )

        coordinator = BatchCoordinator(chunk_size=chunk_size or self._batch_size)
        return coordinator.submit(
            study_key,
            subjects_data,
            _submit_chunk,
            schema=schema,
            skipped_count=skipped_count,
            wait_for_completion=wait_for_completion,
            poll_single=lambda bid: self._sdk.poll_job(
                study_key, bid, timeout=timeout, interval=poll_interval
            ),
            poll_many=lambda bids: self._poll_many(
                study_key, bids, timeout=timeout, poll_interval=poll_interval
            ),
            require_batch_id=False,
        )

    def register_subjects(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]],
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: SchemaCache | None = None,
    ) -> Job:
        """Registers multiple subjects in the specified study.

        Sites and subject identifiers are validated before submission.

        Args:
            study_key: The key identifying the study.
            subjects: A list of RegisterSubjectRequest objects, each defining a subject
                      to be registered.
            email_notify: Optional email address to notify upon completion.
            wait_for_completion: If True, wait for the job to complete.
            timeout: Timeout in seconds for waiting.
            poll_interval: Polling interval in seconds.
            filter_enrolled: Optional override to skip already enrolled subjects.
            skip_existing: Alias to skip already enrolled subjects.
            subject_key_field: Optional variable name in ``data`` holding a subject key.
            schema: Optional schema cache for single-pass record validation.

        Returns:
            A :class:`Job` object representing the background job
            created for the registration request.

        Raises:
            ApiError: If the API call fails.
        """
        summary = self.register_subjects_batch(
            study_key=study_key,
            subjects=subjects,
            email_notify=email_notify,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
        )
        if summary.last_job is not None:
            return cast(Job, summary.last_job)
        return Job(state="COMPLETED")
