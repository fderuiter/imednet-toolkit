"""Endpoint for managing sites (study locations) in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.strategies import PopStudyKeyStrategy
from imednet.models.sites import Site

if TYPE_CHECKING:
    import pandas as pd


class SitesOperationDef:
    """Definition for Site operations."""

    PATH = "sites"
    MODEL = Site
    _id_param = "siteId"
    STUDY_KEY_STRATEGY = PopStudyKeyStrategy()


class SitesEndpoint(SitesOperationDef, EdcSyncListGetEndpoint[Site]):  # type: ignore[misc]
    """Synchronous endpoint for managing Sites."""

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried sites directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried sites.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, sites_to_dataframe

        check_pandas("sites_to_dataframe")
        sites = list(self.list(study_key=study_key, **filters))
        return sites_to_dataframe(sites)


class AsyncSitesEndpoint(SitesOperationDef, EdcAsyncListGetEndpoint[Site]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Sites."""

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried sites asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried sites.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, sites_to_dataframe

        check_pandas("sites_to_dataframe")
        sites = [site async for site in self.list(study_key=study_key, **filters)]
        return sites_to_dataframe(sites)
