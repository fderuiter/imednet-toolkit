"""Endpoint for managing variables (data points on forms) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.variables import Variable


class VariablesOperationDef:
    """Definition for Variable operations."""

    PATH = "variables"
    MODEL = Variable
    _id_param = "variableId"
    PAGE_SIZE = 500
    FILTER_ATTRIBUTES = frozenset(
        {
            "variableId",
            "variableType",
            "variableName",
            "sequence",
            "revision",
            "disabled",
            "dateCreated",
            "dateModified",
            "lastUpdated",
            "formId",
            "variableOid",
            "deleted",
            "formKey",
            "formName",
            "label",
            "blinded",
        }
    )


class VariablesEndpoint(VariablesOperationDef, EdcSyncListGetEndpoint[Variable]):  # type: ignore[misc]
    """Synchronous endpoint for managing Variables."""


class AsyncVariablesEndpoint(VariablesOperationDef, EdcAsyncListGetEndpoint[Variable]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Variables."""
