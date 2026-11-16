"""Endpoint for managing variables (data points on forms) in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.strategies import PopStudyKeyStrategy
from imednet.models.variables import Variable

if TYPE_CHECKING:
    import pandas as pd


class VariablesOperationDef:
    """Definition for Variable operations."""

    PATH = "variables"
    MODEL = Variable
    _id_param = "variableId"
    STUDY_KEY_STRATEGY = PopStudyKeyStrategy()
    PAGE_SIZE = 500


class VariablesEndpoint(VariablesOperationDef, EdcSyncListGetEndpoint[Variable]):  # type: ignore[misc]
    """Synchronous endpoint for managing Variables."""

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried variables directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried variables.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import variables_to_dataframe

        variables = list(self.list(study_key=study_key, **filters))
        return variables_to_dataframe(variables)


class AsyncVariablesEndpoint(VariablesOperationDef, EdcAsyncListGetEndpoint[Variable]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Variables."""

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried variables asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried variables.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import variables_to_dataframe

        variables = [var async for var in self.list(study_key=study_key, **filters)]
        return variables_to_dataframe(variables)
