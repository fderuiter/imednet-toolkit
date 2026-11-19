"""Workflow for registering subjects in iMednet via the Records API.

This workflow is self-contained and does not borrow from record_update.py.
It provides a simple, robust interface for registering one or more subjects.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from imednet.spi.models import Job, RegisterSubjectRequest, Site

from ._guards import ensure_async_sdk, ensure_sync_sdk

# Use TYPE_CHECKING to avoid circular import at runtime
if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

_OWNER = "RegisterSubjectsWorkflow"


def _prepare_subject_payloads(
    subjects: list[RegisterSubjectRequest],
    available_sites: Iterable[Site],
) -> list[dict[str, Any]]:
    """Validate target site names and serialize ``RegisterSubjectRequest`` payloads."""
    sites = {s.site_name for s in available_sites}
    errors: list[str] = []
    for idx, subj in enumerate(subjects):
        if not subj.site_name:
            errors.append(f"Index {idx}: siteName is required")
        elif subj.site_name not in sites:
            errors.append(f"Index {idx}: site '{subj.site_name}' not found")

    if errors:
        raise ValueError("; ".join(errors))

    return [s.model_dump(by_alias=True) for s in subjects]


class RegisterSubjectsWorkflow:
    """Manages the registration of subjects using the iMedNet SDK.

    Attributes:
        _sdk: An instance of the ImednetSDK or AsyncImednetSDK facade.
    """

    def __init__(self, sdk: "ImednetFacade | AsyncImednetFacade"):
        """Initialize the RegisterSubjectsWorkflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
        """
        self._sdk = sdk

    def register_subjects(
        self,
        study_key: str,
        subjects: list[RegisterSubjectRequest],
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Job:
        """Registers multiple subjects in the specified study."""
        ensure_sync_sdk(self._sdk, _OWNER, "register_subjects", "async_register_subjects")
        sdk = cast("ImednetFacade", self._sdk)
        subjects_data = _prepare_subject_payloads(subjects, sdk.get_sites(study_key=study_key))

        job = sdk.create_record(
            study_key=study_key, records_data=subjects_data, email_notify=email_notify
        )

        if not wait_for_completion or not job.batch_id:
            return job

        return cast(
            Job,
            sdk.poll_job(study_key, job.batch_id, timeout=timeout, interval=poll_interval),
        )

    async def async_register_subjects(
        self,
        study_key: str,
        subjects: list[RegisterSubjectRequest],
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Job:
        """Asynchronously register multiple subjects in the specified study."""
        ensure_async_sdk(self._sdk, _OWNER, "async_register_subjects", "register_subjects")
        sdk = cast("AsyncImednetFacade", self._sdk)
        sites = await sdk.async_get_sites(study_key=study_key)
        subjects_data = _prepare_subject_payloads(subjects, sites)

        job = await sdk.async_create_record(
            study_key=study_key, records_data=subjects_data, email_notify=email_notify
        )

        if not wait_for_completion or not job.batch_id:
            return job

        return cast(
            Job,
            await sdk.async_poll_job(
                study_key, job.batch_id, timeout=timeout, interval=poll_interval
            ),
        )
