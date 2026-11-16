"""Core endpoint abstractions and protocols."""

from .base import (
    AsyncGetEndpoint,
    AsyncListGetEndpoint,
    GenericEndpoint,
    GenericGetEndpoint,
    GenericListGetEndpoint,
    SyncGetEndpoint,
    SyncListGetEndpoint,
)
from .edc_mixin import (
    EdcAsyncGetEndpoint,
    EdcAsyncListGetEndpoint,
    EdcGenericGetEndpoint,
    EdcGenericListGetEndpoint,
    EdcSyncGetEndpoint,
    EdcSyncListGetEndpoint,
)
from .protocols import SupportsCreate, SupportsGet, SupportsList

__all__ = [
    "AsyncGetEndpoint",
    "AsyncListGetEndpoint",
    "EdcAsyncGetEndpoint",
    "EdcAsyncListGetEndpoint",
    "EdcGenericGetEndpoint",
    "EdcGenericListGetEndpoint",
    "EdcSyncGetEndpoint",
    "EdcSyncListGetEndpoint",
    "GenericEndpoint",
    "GenericGetEndpoint",
    "GenericListGetEndpoint",
    "SupportsCreate",
    "SupportsGet",
    "SupportsList",
    "SyncGetEndpoint",
    "SyncListGetEndpoint",
]
