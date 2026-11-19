"""Facade protocols for the iMednet SDK.

These protocols define the expected interface for the iMednet SDK clients,
allowing for easier mocking and decoupling in dependent packages.
"""

from typing import Any, Protocol

from imednet.models.codings import Coding
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.jobs import Job, JobStatus
from imednet.models.queries import Query
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.utils.job_poller import JobProgressHandler
from imednet.utils.typing import FilterValue, JsonDict


class ImednetFacade(Protocol):
    """Protocol for the synchronous iMednet SDK facade."""

    def get_codings(self, study_key: str, **filters: FilterValue) -> list[Coding]:
        """Fetch code list definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Coding models.
        """
        ...

    def get_forms(self, study_key: str, **filters: FilterValue) -> list[Form]:
        """Fetch form definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Form models.
        """
        ...

    def get_intervals(self, study_key: str, **filters: FilterValue) -> list[Interval]:
        """Fetch visit interval definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Interval models.
        """
        ...

    def get_job(self, study_key: str, batch_id: str) -> JobStatus:
        """Fetch the status of a specific background job.

        Args:
            study_key: The unique identifier for the study.
            batch_id: The unique identifier for the job batch.

        Returns:
            A JobStatus model.
        """
        ...

    def get_queries(self, study_key: str, **filters: FilterValue) -> list[Query]:
        """Fetch queries for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Query models.
        """
        ...

    def get_record_revisions(self, study_key: str, **filters: FilterValue) -> list[RecordRevision]:
        """Fetch record revision history for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of RecordRevision models.
        """
        ...

    def get_records(self, study_key: str, **filters: FilterValue) -> list[Record]:
        """Fetch data records for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Record models.
        """
        ...

    def get_sites(self, study_key: str, **filters: FilterValue) -> list[Site]:
        """Fetch site definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Site models.
        """
        ...

    def get_studies(self, **filters: FilterValue) -> list[Study]:
        """Fetch all studies accessible to the current user.

        Args:
            **filters: Optional API filters to apply.

        Returns:
            A list of Study models.
        """
        ...

    def get_subjects(self, study_key: str, **filters: FilterValue) -> list[Subject]:
        """Fetch subject records for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Subject models.
        """
        ...

    def get_users(self, study_key: str, **filters: FilterValue) -> list[User]:
        """Fetch user definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of User models.
        """
        ...

    def get_variables(self, study_key: str, **filters: FilterValue) -> list[Variable]:
        """Fetch variable definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Variable models.
        """
        ...

    def get_visits(self, study_key: str, **filters: FilterValue) -> list[Visit]:
        """Fetch visit definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Visit models.
        """
        ...

    def create_record(
        self,
        study_key: str,
        records_data: list[JsonDict],
        email_notify: bool | str | None = None,
        *,
        schema: Any = None,
    ) -> Job:
        """Submit a request to create one or more data records.

        Args:
            study_key: The unique identifier for the study.
            records_data: List of dictionaries containing record field data.
            email_notify: Optional email notification settings.
            schema: Optional validation schema.

        Returns:
            A Job model representing the background operation.
        """
        ...

    def poll_job(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: float = 5,
        timeout: float = 300,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Wait for a background job to reach a terminal state.

        Args:
            study_key: The unique identifier for the study.
            batch_id: The unique identifier for the job batch.
            interval: Time between poll attempts in seconds.
            timeout: Maximum time to wait in seconds.
            on_progress: Optional callback invoked on each poll cycle.

        Returns:
            The final JobStatus of the operation.
        """
        ...

    @property
    def auth(self) -> Any:
        """Access the authentication provider for the facade."""
        ...


class AsyncImednetFacade(Protocol):
    """Protocol for the asynchronous iMednet SDK facade."""

    async def async_get_codings(self, study_key: str, **filters: FilterValue) -> list[Coding]:
        """Asynchronously fetch code list definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Coding models.
        """
        ...

    async def async_get_forms(self, study_key: str, **filters: FilterValue) -> list[Form]:
        """Asynchronously fetch form definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Form models.
        """
        ...

    async def async_get_intervals(self, study_key: str, **filters: FilterValue) -> list[Interval]:
        """Asynchronously fetch visit interval definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Interval models.
        """
        ...

    async def async_get_job(self, study_key: str, batch_id: str) -> JobStatus:
        """Asynchronously fetch the status of a specific background job.

        Args:
            study_key: The unique identifier for the study.
            batch_id: The unique identifier for the job batch.

        Returns:
            A JobStatus model.
        """
        ...

    async def async_get_queries(self, study_key: str, **filters: FilterValue) -> list[Query]:
        """Asynchronously fetch queries for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Query models.
        """
        ...

    async def async_get_record_revisions(
        self, study_key: str, **filters: FilterValue
    ) -> list[RecordRevision]:
        """Asynchronously fetch record revision history for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of RecordRevision models.
        """
        ...

    async def async_get_records(self, study_key: str, **filters: FilterValue) -> list[Record]:
        """Asynchronously fetch data records for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Record models.
        """
        ...

    async def async_get_sites(self, study_key: str, **filters: FilterValue) -> list[Site]:
        """Asynchronously fetch site definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Site models.
        """
        ...

    async def async_get_studies(self, **filters: FilterValue) -> list[Study]:
        """Asynchronously fetch all studies accessible to the current user.

        Args:
            **filters: Optional API filters to apply.

        Returns:
            A list of Study models.
        """
        ...

    async def async_get_subjects(self, study_key: str, **filters: FilterValue) -> list[Subject]:
        """Asynchronously fetch subject records for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Subject models.
        """
        ...

    async def async_get_users(self, study_key: str, **filters: FilterValue) -> list[User]:
        """Asynchronously fetch user definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of User models.
        """
        ...

    async def async_get_variables(self, study_key: str, **filters: FilterValue) -> list[Variable]:
        """Asynchronously fetch variable definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Variable models.
        """
        ...

    async def async_get_visits(self, study_key: str, **filters: FilterValue) -> list[Visit]:
        """Asynchronously fetch visit definitions for a specific study.

        Args:
            study_key: The unique identifier for the study.
            **filters: Optional API filters to apply.

        Returns:
            A list of Visit models.
        """
        ...

    async def async_create_record(
        self,
        study_key: str,
        records_data: list[JsonDict],
        email_notify: bool | str | None = None,
        *,
        schema: Any = None,
    ) -> Job:
        """Asynchronously submit a request to create one or more data records.

        Args:
            study_key: The unique identifier for the study.
            records_data: List of dictionaries containing record field data.
            email_notify: Optional email notification settings.
            schema: Optional validation schema.

        Returns:
            A Job model representing the background operation.
        """
        ...

    async def async_poll_job(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: float = 5,
        timeout: float = 300,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Asynchronously wait for a background job to reach a terminal state.

        Args:
            study_key: The unique identifier for the study.
            batch_id: The unique identifier for the job batch.
            interval: Time between poll attempts in seconds.
            timeout: Maximum time to wait in seconds.
            on_progress: Optional callback invoked on each poll cycle.

        Returns:
            The final JobStatus of the operation.
        """
        ...
