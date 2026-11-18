"""Endpoint for managing forms in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.strategies import PopStudyKeyStrategy
from imednet.models.forms import Form

if TYPE_CHECKING:
    import pandas as pd


class FormsOperationDef:
    """Definition for Form operations."""

    PATH = "forms"
    MODEL = Form
    _id_param = "formId"
    STUDY_KEY_STRATEGY = PopStudyKeyStrategy()
    PAGE_SIZE = 500


class FormsEndpoint(FormsOperationDef, EdcSyncListGetEndpoint[Form]):  # type: ignore[misc]
    """Synchronous endpoint for managing Forms."""

    def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried forms directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried forms.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, forms_to_dataframe

        check_pandas("forms_to_dataframe")
        forms = list(self.list(study_key=study_key, **filters))
        return forms_to_dataframe(forms)


class AsyncFormsEndpoint(FormsOperationDef, EdcAsyncListGetEndpoint[Form]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Forms."""

    async def to_dataframe(
        self,
        study_key: str | None = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried forms asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried forms.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, forms_to_dataframe

        check_pandas("forms_to_dataframe")
        forms = [form async for form in self.list(study_key=study_key, **filters)]
        return forms_to_dataframe(forms)
