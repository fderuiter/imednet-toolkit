"""Endpoint for managing subjects in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.subjects import Subject

if TYPE_CHECKING:
    import pandas as pd


class ListBySiteOperation:
    """List by site operation."""

    def __init__(self, endpoint: Any, study_key: str, site_id: str | int):
        """Initialize."""
        self.endpoint = endpoint
        self.study_key = study_key
        self.site_id = site_id

    def execute_sync(self, client: Any, parse_func: Any = None) -> list[Subject]:
        """Execute sync."""
        return list(
            self.endpoint._list_sync(
                client, self.endpoint.PAGINATOR_CLS, study_key=self.study_key, site_id=self.site_id
            )
        )

    async def execute_async(self, client: Any, parse_func: Any = None) -> list[Subject]:
        """Execute async."""
        return await self.endpoint._list_async(  # type: ignore[no-any-return]
            client,
            self.endpoint.ASYNC_PAGINATOR_CLS,
            study_key=self.study_key,
            site_id=self.site_id,
        )


class SubjectsOperationDef:
    """Definition for Subject operations."""

    PATH = "subjects"
    MODEL = Subject
    _id_param = "subjectKey"

    @execute_operation  # type: ignore[arg-type]
    def list_by_site(self, study_key: str, site_id: str | int) -> ListBySiteOperation:
        """List subjects by site ID."""
        return ListBySiteOperation(self, study_key, site_id)


class SubjectsEndpoint(SubjectsOperationDef, EdcSyncListGetEndpoint[Subject]):  # type: ignore[misc]
    """Synchronous endpoint for managing Subjects."""

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried subjects directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried subjects.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import subjects_to_dataframe

        subjects = list(self.list(study_key=study_key, **filters))
        return subjects_to_dataframe(subjects)


class AsyncSubjectsEndpoint(SubjectsOperationDef, EdcAsyncListGetEndpoint[Subject]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Subjects."""

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried subjects asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried subjects.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import subjects_to_dataframe

        subjects = [subject async for subject in self.list(study_key=study_key, **filters)]
        return subjects_to_dataframe(subjects)
