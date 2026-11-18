"""Endpoint for managing variables (data points on forms) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.strategies import PopStudyKeyStrategy
from imednet.models.variables import Variable


class VariablesOperationDef:
    """Definition for Variable operations."""

    PATH = "variables"
    MODEL = Variable
    _id_param = "variableId"
    STUDY_KEY_STRATEGY = PopStudyKeyStrategy()
    PAGE_SIZE = 500


class VariablesEndpoint(VariablesOperationDef, EdcSyncListGetEndpoint[Variable]):  # type: ignore[misc]
    """Synchronous endpoint for managing Variables."""


class AsyncVariablesEndpoint(VariablesOperationDef, EdcAsyncListGetEndpoint[Variable]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Variables."""
