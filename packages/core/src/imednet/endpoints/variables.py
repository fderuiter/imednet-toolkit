"""Endpoint for managing variables (data points on forms) in a study."""

from typing import ClassVar

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.variables import Variable


class VariablesOperationDef:
    """Definition for Variable operations."""

    PATH: ClassVar[str] = "variables"
    MODEL = Variable
    _id_param = "variableId"
    _lookup_key_hint: ClassVar[str | None] = "variableName"
    PAGE_SIZE: ClassVar[int] = 500
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset(
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


class VariablesEndpoint(VariablesOperationDef, EdcSyncListGetEndpoint[Variable]):
    """Synchronous endpoint for managing Variables."""


class AsyncVariablesEndpoint(VariablesOperationDef, EdcAsyncListGetEndpoint[Variable]):
    """Asynchronous endpoint for managing Variables."""
