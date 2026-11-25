"""Endpoint for managing intervals (visit definitions) in a study."""

from typing import ClassVar

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.intervals import Interval


class IntervalsOperationDef:
    """Definition for Interval operations."""

    PATH: ClassVar[str] = "intervals"
    MODEL = Interval
    _id_param = "intervalId"
    _lookup_key_hint: ClassVar[str | None] = "intervalName"
    PAGE_SIZE: ClassVar[int] = 500
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset()


class IntervalsEndpoint(IntervalsOperationDef, EdcSyncListGetEndpoint[Interval]):
    """Synchronous endpoint for managing Intervals."""


class AsyncIntervalsEndpoint(IntervalsOperationDef, EdcAsyncListGetEndpoint[Interval]):
    """Asynchronous endpoint for managing Intervals."""
