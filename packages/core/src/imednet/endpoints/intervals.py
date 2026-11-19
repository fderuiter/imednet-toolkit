"""Endpoint for managing intervals (visit definitions) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.intervals import Interval


class IntervalsOperationDef:
    """Definition for Interval operations."""

    PATH = "intervals"
    MODEL = Interval
    _id_param = "intervalId"
    PAGE_SIZE = 500
    FILTER_ATTRIBUTES = frozenset(
        {
            "intervalId",
            "intervalName",
            "intervalDescription",
            "intervalSequence",
            "intervalGroupId",
            "intervalGroupName",
            "timeline",
            "definedUsingInterval",
            "windowCalculationForm",
            "windowCalculationDate",
            "actualDateForm",
            "actualDate",
            "dueDateWillBeIn",
            "negativeSlack",
            "positiveSlack",
            "eproGracePeriod",
            "disabled",
            "dateCreated",
            "dateModified",
            "lastUpdated",
        }
    )


class IntervalsEndpoint(IntervalsOperationDef, EdcSyncListGetEndpoint[Interval]):  # type: ignore[misc]
    """Synchronous endpoint for managing Intervals."""


class AsyncIntervalsEndpoint(IntervalsOperationDef, EdcAsyncListGetEndpoint[Interval]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Intervals."""
