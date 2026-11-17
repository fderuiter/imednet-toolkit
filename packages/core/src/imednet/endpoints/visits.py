"""Endpoint for managing visits (interval instances) in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.visits import Visit

if TYPE_CHECKING:
    import pandas as pd


class VisitsOperationDef:
    """Definition for Visit operations."""

    PATH = "visits"
    MODEL = Visit
    _id_param = "visitId"


class VisitsEndpoint(VisitsOperationDef, EdcSyncListGetEndpoint[Visit]):  # type: ignore[misc]
    """Synchronous endpoint for managing Visits."""

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried visits directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried visits.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import visits_to_dataframe

        visits = list(self.list(study_key=study_key, **filters))
        return visits_to_dataframe(visits)


class AsyncVisitsEndpoint(VisitsOperationDef, EdcAsyncListGetEndpoint[Visit]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Visits."""

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried visits asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried visits.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import visits_to_dataframe

        visits = [visit async for visit in self.list(study_key=study_key, **filters)]
        return visits_to_dataframe(visits)
