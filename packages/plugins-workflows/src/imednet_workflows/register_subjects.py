"""Workflow for registering subjects in iMednet via the Records API.

This workflow is self-contained and does not borrow from record_update.py.
It provides a simple, robust interface for registering one or more subjects.
"""

from typing import TYPE_CHECKING, cast

from imednet.spi.models import Job, RegisterSubjectRequest

# Use TYPE_CHECKING to avoid circular import at runtime
if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


class RegisterSubjectsWorkflow:
    """Manages the registration of subjects using the iMedNet SDK.

    Attributes:
        _sdk (ImednetSDK): An instance of the ImednetSDK.
    """

    def __init__(self, sdk: "ImednetFacade"):  # Use string literal for type hint
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

        Returns:
            A :class:`Job` object representing the background job
            created for the registration request.

        Raises:
            ApiError: If the API call fails.
        """
        # Validate that each site exists before posting
        sites = {s.site_name for s in self._sdk.get_sites(study_key=study_key)}
        errors: list[str] = []
        for idx, subj in enumerate(subjects):
            if not subj.site_name:
                errors.append(f"Index {idx}: siteName is required")
            elif subj.site_name not in sites:
                errors.append(f"Index {idx}: site '{subj.site_name}' not found")

        if errors:
            raise ValueError("; ".join(errors))

        subjects_data = [s.model_dump(by_alias=True) for s in subjects]

        job = self._sdk.create_record(
            study_key=study_key, records_data=subjects_data, email_notify=email_notify
        )

        if not wait_for_completion:
            return job

        if not job.batch_id:
            return job

        from imednet.spi.facade import ImednetFacade

        return cast(
            Job,
            (cast(ImednetFacade, self._sdk)).poll_job(  # type: ignore[redundant-cast]
                study_key, job.batch_id, timeout=timeout, interval=poll_interval
            ),
        )
