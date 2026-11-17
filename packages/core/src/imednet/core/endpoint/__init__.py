"""Core endpoint abstractions and protocols."""

from .base import (
    AsyncGetEndpoint,
    AsyncListEndpoint,
    AsyncListGetEndpoint,
    GenericEndpoint,
    GenericGetEndpoint,
    GenericListEndpoint,
    GenericListGetEndpoint,
    SyncGetEndpoint,
    SyncListEndpoint,
    SyncListGetEndpoint,
)
from .edc_mixin import (
    EdcAsyncGetEndpoint,
    EdcAsyncListEndpoint,
    EdcAsyncListGetEndpoint,
    EdcGenericGetEndpoint,
    EdcGenericListEndpoint,
    EdcGenericListGetEndpoint,
    EdcSyncGetEndpoint,
    EdcSyncListEndpoint,
    EdcSyncListGetEndpoint,
)
from .protocols import SupportsCreate, SupportsGet, SupportsList

__all__ = [
    "AsyncGetEndpoint",
    "AsyncListEndpoint",
    "AsyncListGetEndpoint",
    "EdcAsyncGetEndpoint",
    "EdcAsyncListEndpoint",
    "EdcAsyncListGetEndpoint",
    "EdcGenericGetEndpoint",
    "EdcGenericListEndpoint",
    "EdcGenericListGetEndpoint",
    "EdcSyncGetEndpoint",
    "EdcSyncListEndpoint",
    "EdcSyncListGetEndpoint",
    "GenericEndpoint",
    "GenericGetEndpoint",
    "GenericListEndpoint",
    "GenericListGetEndpoint",
    "SupportsCreate",
    "SupportsGet",
    "SupportsList",
    "SyncGetEndpoint",
    "SyncListEndpoint",
    "SyncListGetEndpoint",
]
