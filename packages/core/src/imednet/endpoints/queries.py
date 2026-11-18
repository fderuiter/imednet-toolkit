"""Endpoint for managing queries (dialogue/questions) in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.queries import Query

if TYPE_CHECKING:
    import pandas as pd


class QueriesOperationDef:
    """Definition for Query operations."""

    PATH = "queries"
    MODEL = Query
    _id_param = "annotationId"


class QueriesEndpoint(QueriesOperationDef, EdcSyncListGetEndpoint[Query]):  # type: ignore[misc]
    """Synchronous endpoint for managing Queries."""

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried queries directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried queries.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, queries_to_dataframe

        check_pandas("queries_to_dataframe")
        queries = list(self.list(study_key=study_key, **filters))
        return queries_to_dataframe(queries)


class AsyncQueriesEndpoint(QueriesOperationDef, EdcAsyncListGetEndpoint[Query]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Queries."""

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried queries asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried queries.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, queries_to_dataframe

        check_pandas("queries_to_dataframe")
        queries = [query async for query in self.list(study_key=study_key, **filters)]
        return queries_to_dataframe(queries)
