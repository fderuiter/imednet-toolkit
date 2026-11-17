"""Endpoint for managing intervals (visit definitions) in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.strategies import PopStudyKeyStrategy
from imednet.models.intervals import Interval

if TYPE_CHECKING:
    import pandas as pd


class IntervalsOperationDef:
    """Definition for Interval operations."""

    PATH = "intervals"
    MODEL = Interval
    _id_param = "intervalId"
    STUDY_KEY_STRATEGY = PopStudyKeyStrategy()
    PAGE_SIZE = 500


class IntervalsEndpoint(IntervalsOperationDef, EdcSyncListGetEndpoint[Interval]):  # type: ignore[misc]
    """Synchronous endpoint for managing Intervals."""

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried intervals directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried intervals.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import intervals_to_dataframe

        intervals = list(self.list(study_key=study_key, **filters))
        return intervals_to_dataframe(intervals)


class AsyncIntervalsEndpoint(IntervalsOperationDef, EdcAsyncListGetEndpoint[Interval]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Intervals."""

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried intervals asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried intervals.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import intervals_to_dataframe

        intervals = [interval async for interval in self.list(study_key=study_key, **filters)]
        return intervals_to_dataframe(intervals)
