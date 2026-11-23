"""Plugin contracts for the iMednet SDK.

This module defines the :class:`PluginProtocol` that namespace-level plugin
factories satisfy when discovered via the ``imednet.plugins`` entry-point
group (such as ``sinks = "imednet_sinks.plugin:create_sinks"``), together with
mode-separated :class:`SyncWorkflowsNamespaceProtocol`,
:class:`AsyncWorkflowsNamespaceProtocol`, :class:`WorkflowsNamespaceProtocol`,
:class:`SinksNamespaceProtocol`, and :class:`AsyncSinksNamespaceProtocol`.

Individual workflows exposed on ``sdk.workflows`` are discovered lazily by
:class:`~imednet.sdk.WorkflowRegistry` via the ``imednet.workflows``
entry-point group, where each entry point maps a workflow attribute name to a
callable accepting the SDK instance.

Minimal plugin skeleton
-----------------------

To register an individual workflow on ``sdk.workflows``:

1. Expose a class or factory callable under ``[project.entry-points."imednet.workflows"]``.
2. The callable must accept a single argument — the :class:`~imednet.sdk.ImednetSDK`
   (or :class:`~imednet.sdk.AsyncImednetSDK`) instance — and return the workflow helper.

Example ``pyproject.toml`` snippet::

    [project.entry-points."imednet.workflows"]
    my_workflow = "myplugin.workflows:MyWorkflow"

    [project.entry-points."imednet.plugins"]
    sinks = "imednet_sinks.plugin:create_sinks"

Example workflow class::

    # myplugin/workflows.py
    from imednet.sdk import ImednetSDK

    class MyWorkflow:
        def __init__(self, sdk: ImednetSDK) -> None:
            self._sdk = sdk
"""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Never, Protocol, overload, runtime_checkable

from typing_extensions import deprecated

from . import models as _models
from . import spi as _spi
from . import validation as _validation
from .utils import typing as _typing
from .utils.typing import RecordsInput

if not TYPE_CHECKING:
    from typing import _ProtocolMeta

    class _WorkflowsRuntimeProtocolMeta(_ProtocolMeta):
        """Protocol metaclass supporting lazy descriptors and mode-aware WorkflowRegistry checks."""

        def __instancecheck__(self, instance: Any) -> bool:
            """Check WorkflowRegistry mode/entry-points or static descriptors without eager imports."""
            entry_points_map = getattr(instance, "_entry_points", None)
            is_async_fn = getattr(instance, "_is_async_sdk", None)
            if isinstance(entry_points_map, dict) and callable(is_async_fn):
                is_async = bool(is_async_fn())
                if self is SyncWorkflowsNamespaceProtocol:
                    required = {
                        "data_extraction",
                        "duckdb_centralizer",
                        "query_management",
                        "record_mapper",
                        "record_update",
                        "register_subjects",
                        "subject_data",
                        "uat",
                        "uat_inspector",
                    }
                    return (not is_async) and required.issubset(entry_points_map)
                if self is AsyncWorkflowsNamespaceProtocol:
                    required = {"record_update", "register_subjects", "uat_inspector"}
                    return is_async and required.issubset(entry_points_map)
            if sys.version_info < (3, 12) and getattr(
                self, "_is_runtime_protocol", False
            ):  # pragma: no cover
                from typing import _get_protocol_attrs

                for attr in _get_protocol_attrs(self):
                    try:
                        inspect.getattr_static(instance, attr)
                    except AttributeError:
                        return False
                return True
            return super().__instancecheck__(instance)


class DataExtractionWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.data_extraction``."""

    def extract_records_by_criteria(
        self,
        study_key: str,
        record_filter: dict[str, Any] | None = None,
        subject_filter: dict[str, Any] | None = None,
        visit_filter: dict[str, Any] | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        include_deleted: bool = False,
        **other_filters: Any,
    ) -> list[_models.Record]:
        """Extract study records matching combined subject, visit, and record filters."""
        ...

    def extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any] | None = None,
        **filters: Any,
    ) -> list[_models.RecordRevision]:
        """Extract record revisions bounded by optional UTC dates and filters."""
        ...


class DuckDBIngestionWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.duckdb_centralizer``."""

    def ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
    ) -> int:
        """Ingest study record revisions into the DuckDB bronze table."""
        ...

    def build_silver_view(self, study_key: str) -> None:
        """Create or replace the deduplicated DuckDB silver view for a study."""
        ...


class TriageStoreProtocol(Protocol):
    """Structural contract for triage stores used by ``sdk.workflows.query_management``."""

    def upsert_item(self, item: _models.TriageItem) -> None:
        """Insert or update a triage item while preserving annotations and history."""
        ...

    def get_triage_item(self, item_id: str) -> _models.TriageItem | None:
        """Fetch a single triage item by ID."""
        ...

    def get_queue(
        self, study_key: str, status: _models.TriageStatus | None = None
    ) -> list[_models.TriageItem]:
        """Fetch the triage queue for a study, optionally filtered by status."""
        ...


class QueryManagementWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.query_management``."""

    def get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any] | None = None,
        include_answered: bool = False,
        **kwargs: Any,
    ) -> list[_models.Query]:
        """Retrieve open queries for a study, optionally including answered queries."""
        ...

    def get_unresolved_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[_models.Query]:
        """Retrieve all unresolved (OPEN and ANSWERED) queries for a study."""
        ...

    def get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[_models.Query]:
        """Retrieve queries associated with a specific subject."""
        ...

    def get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[_models.Query]:
        """Retrieve queries associated with a specific site."""
        ...

    def get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Count queries grouped by query status."""
        ...

    def sync_queries_to_triage(
        self,
        study_key: str,
        store: TriageStoreProtocol | str | Path | None = None,
        *,
        additional_filter: dict[str, Any] | None = None,
        unresolved_only: bool = False,
        default_severity: str = "medium",
        **kwargs: Any,
    ) -> list[_models.TriageItem]:
        """Synchronize clinical queries for a study into a local triage queue."""
        ...


class RecordMapperProtocol(Protocol):
    """Structural contract for ``sdk.workflows.record_mapper``."""

    def dataframe(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> _typing.DataFrame:
        """Return mapped study records as a pandas DataFrame."""
        ...

    def iter_dataframes(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> Iterator[_typing.DataFrame]:
        """Yield mapped study record DataFrames in bounded chunks."""
        ...

    def build_hierarchy(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_keys: bool = False,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        records: Iterable[_models.Record] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate a nested JSON hierarchy for study records."""
        ...

    def transform_to_sdtm(
        self,
        records: Iterable[Any],
        domain: str,
        study_key: str,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        seq_counters: dict[str, int] | None = None,
    ) -> _typing.DataFrame:
        """Transform records into a normalized SDTM domain DataFrame."""
        ...

    def sdtm_dataframe(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> _typing.DataFrame:
        """Return study records mapped to a CDISC SDTM domain DataFrame."""
        ...

    def iter_sdtm_dataframes(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> Iterator[_typing.DataFrame]:
        """Yield chunked DataFrames mapped to a CDISC SDTM domain."""
        ...


class SyncRecordUpdateWorkflowProtocol(Protocol):
    """Synchronous contract for ``sdk.workflows.record_update`` on ``ImednetSDK``."""

    def create_or_update_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        chunk_size: int | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _spi.utils.BatchSubmissionSummary:
        """Validate once, submit records in bounded chunks, and return a batch summary."""
        ...

    def create_or_update_records(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _models.Job:
        """Submit records for creation or update and return the resulting Job."""
        ...

    def register_subject(
        self,
        study_key: str,
        form_identifier: str | int,
        site_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        site_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
    ) -> _models.Job:
        """Register a new subject by submitting a single enrollment record."""
        ...

    def update_scheduled_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        interval_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
    ) -> _models.Job:
        """Update an existing scheduled record for a subject."""
        ...

    def create_new_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
    ) -> _models.Job:
        """Create a new unscheduled record for an existing subject."""
        ...


class AsyncRecordUpdateWorkflowProtocol(Protocol):
    """Asynchronous contract for ``sdk.workflows.record_update`` on ``AsyncImednetSDK``."""

    async def async_create_or_update_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        chunk_size: int | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _spi.utils.BatchSubmissionSummary:
        """Asynchronously validate once, submit chunks, and return a batch summary."""
        ...

    async def async_create_or_update_records(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _models.Job:
        """Asynchronously submit records for creation or update and return the Job."""
        ...

    async def async_register_subject(
        self,
        study_key: str,
        form_identifier: str | int,
        site_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        site_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
    ) -> _models.Job:
        """Asynchronously register a new subject via the Records API."""
        ...

    async def async_update_scheduled_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        interval_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
    ) -> _models.Job:
        """Asynchronously update an existing scheduled record for a subject."""
        ...

    async def async_create_new_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
    ) -> _models.Job:
        """Asynchronously create a new unscheduled record for an existing subject."""
        ...

    @deprecated(
        "create_or_update_records_batch() is synchronous and raises TypeError on AsyncImednetSDK; "
        "use await async_create_or_update_records_batch(...) instead."
    )
    def create_or_update_records_batch(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous batch method stub on AsyncImednetSDK."""
        ...

    @deprecated(
        "create_or_update_records() is synchronous and raises TypeError on AsyncImednetSDK; "
        "use await async_create_or_update_records(...) instead."
    )
    def create_or_update_records(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous record submission stub on AsyncImednetSDK."""
        ...

    @deprecated(
        "register_subject() is synchronous and raises TypeError on AsyncImednetSDK; "
        "use await async_register_subject(...) instead."
    )
    def register_subject(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous subject registration stub on AsyncImednetSDK."""
        ...

    @deprecated(
        "update_scheduled_record() is synchronous and raises TypeError on AsyncImednetSDK; "
        "use await async_update_scheduled_record(...) instead."
    )
    def update_scheduled_record(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous scheduled record update stub on AsyncImednetSDK."""
        ...

    @deprecated(
        "create_new_record() is synchronous and raises TypeError on AsyncImednetSDK; "
        "use await async_create_new_record(...) instead."
    )
    def create_new_record(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous unscheduled record creation stub on AsyncImednetSDK."""
        ...


class SyncRegisterSubjectsWorkflowProtocol(Protocol):
    """Synchronous contract for ``sdk.workflows.register_subjects`` on ``ImednetSDK``."""

    def register_subjects_batch(
        self,
        study_key: str,
        subjects: Sequence[_models.RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _spi.utils.BatchSubmissionSummary:
        """Validate sites and submit subject registrations in bounded chunks."""
        ...

    def register_subjects(
        self,
        study_key: str,
        subjects: Sequence[_models.RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _models.Job:
        """Register one or more subjects in a study."""
        ...

    async def async_register_subjects_batch(
        self,
        study_key: str,
        subjects: Sequence[_models.RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _spi.utils.BatchSubmissionSummary:
        """Asynchronously register subjects in bounded chunks."""
        ...

    async def async_register_subjects(
        self,
        study_key: str,
        subjects: Sequence[_models.RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _models.Job:
        """Asynchronously register one or more subjects in a study."""
        ...


RegisterSubjectsWorkflowProtocol = SyncRegisterSubjectsWorkflowProtocol


class AsyncRegisterSubjectsWorkflowProtocol(Protocol):
    """Asynchronous contract for ``sdk.workflows.register_subjects`` on ``AsyncImednetSDK``."""

    async def async_register_subjects_batch(
        self,
        study_key: str,
        subjects: Sequence[_models.RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _spi.utils.BatchSubmissionSummary:
        """Asynchronously validate sites and submit subject registrations in bounded chunks."""
        ...

    async def async_register_subjects(
        self,
        study_key: str,
        subjects: Sequence[_models.RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        on_progress: _spi.utils.JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> _models.Job:
        """Asynchronously register one or more subjects in a study."""
        ...

    @deprecated(
        "register_subjects_batch() is synchronous and raises TypeError on AsyncImednetSDK; "
        "use await async_register_subjects_batch(...) instead."
    )
    def register_subjects_batch(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous batch subject registration stub on AsyncImednetSDK."""
        ...

    @deprecated(
        "register_subjects() is synchronous and raises TypeError on AsyncImednetSDK; "
        "use await async_register_subjects(...) instead."
    )
    def register_subjects(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous subject registration stub on AsyncImednetSDK."""
        ...


class SubjectComprehensiveDataProtocol(Protocol):
    """Structural contract for aggregated subject data returned by ``subject_data``."""

    @property
    def subject_details(self) -> _models.Subject | None:
        """Core details of the subject."""
        ...

    @property
    def visits(self) -> list[_models.Visit]:
        """Visits associated with the subject."""
        ...

    @property
    def records(self) -> list[_models.Record]:
        """Records associated with the subject."""
        ...

    @property
    def queries(self) -> list[_models.Query]:
        """Queries associated with the subject."""
        ...


class SubjectDataWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.subject_data``."""

    def get_all_subject_data(
        self,
        study_key: str,
        subject_key: str,
        *,
        include_deleted: bool = False,
    ) -> SubjectComprehensiveDataProtocol:
        """Retrieve subject details, visits, records, and queries for a subject."""
        ...


class StudySnapshotProtocol(Protocol):
    """Structural contract for a study metadata snapshot."""

    @property
    def study_key(self) -> str:
        """Study key identifier."""
        ...

    @property
    def captured_at(self) -> datetime:
        """UTC timestamp when the snapshot was captured."""
        ...

    @property
    def forms(self) -> list[_models.Form]:
        """Study form definitions."""
        ...

    @property
    def variables(self) -> list[_models.Variable]:
        """Study variable definitions."""
        ...

    @property
    def intervals(self) -> list[_models.Interval]:
        """Study interval definitions."""
        ...

    @property
    def sites(self) -> list[_models.Site]:
        """Study site definitions."""
        ...

    @property
    def forms_by_key(self) -> dict[str, _models.Form]:
        """Lookup dictionary mapping form keys to Form models."""
        ...

    @property
    def variables_by_form(self) -> dict[str, list[_models.Variable]]:
        """Lookup dictionary mapping form keys to Variable lists."""
        ...

    @property
    def intervals_by_name(self) -> dict[str, _models.Interval]:
        """Lookup dictionary mapping interval names to Interval models."""
        ...

    def enrollment_forms(self) -> list[_models.Form]:
        """Return forms configured for subject enrollment/registration."""
        ...

    def scheduled_forms(self) -> list[_models.Form]:
        """Return scheduled non-enrollment forms."""
        ...

    def unscheduled_forms(self) -> list[_models.Form]:
        """Return unscheduled forms."""
        ...

    def active_sites(self) -> list[_models.Site]:
        """Return actively enrolling sites."""
        ...


class SyncStudySchemaInspectorProtocol(Protocol):
    """Synchronous contract for ``sdk.workflows.uat_inspector`` on ``ImednetSDK``."""

    def inspect(self, study_key: str, *, force_refresh: bool = False) -> StudySnapshotProtocol:
        """Synchronously fetch and index structural study metadata."""
        ...

    def clear_cache(self, study_key: str | None = None) -> None:
        """Clear cached study snapshots."""
        ...


class AsyncStudySchemaInspectorProtocol(Protocol):
    """Asynchronous contract for ``sdk.workflows.uat_inspector`` on ``AsyncImednetSDK``."""

    async def async_inspect(
        self, study_key: str, *, force_refresh: bool = False
    ) -> StudySnapshotProtocol:
        """Asynchronously fetch and index structural study metadata concurrently."""
        ...

    def clear_cache(self, study_key: str | None = None) -> None:
        """Clear cached study snapshots."""
        ...

    @deprecated(
        "inspect() is synchronous and raises TypeError on AsyncImednetSDK; "
        "use await async_inspect(...) instead."
    )
    def inspect(self, study_key: str, *, force_refresh: bool = False) -> Never:
        """Rejected synchronous inspect stub on AsyncImednetSDK."""
        ...


class UATRunResultProtocol(Protocol):
    """Structural contract for ``UATWorkflow.run`` execution results."""

    @property
    def study_key(self) -> str:
        """Study key tested."""
        ...

    @property
    def run_id(self) -> str:
        """Unique execution identifier."""
        ...

    @property
    def started_at(self) -> datetime:
        """UTC timestamp when the UAT run started."""
        ...

    @property
    def completed_at(self) -> datetime | None:
        """UTC timestamp when the UAT run finished, if completed."""
        ...

    @property
    def state(self) -> str:
        """Current or final lifecycle state value."""
        ...

    @property
    def state_history(self) -> Sequence[str]:
        """Ordered sequence of visited lifecycle states."""
        ...

    @property
    def failure_phase(self) -> str | None:
        """Phase in which execution failed, if any."""
        ...

    @property
    def failed_phase(self) -> str | None:
        """Alias for ``failure_phase``."""
        ...

    @property
    def error_message(self) -> str | None:
        """Failure message captured when a phase errors."""
        ...

    @property
    def is_terminal(self) -> bool:
        """Whether the run reached a terminal lifecycle state."""
        ...

    @property
    def overall_success(self) -> bool:
        """Whether all UAT batches completed without failure."""
        ...

    @property
    def spec(self) -> Any:
        """Generated or supplied UAT specification."""
        ...

    @property
    def record_sets(self) -> Sequence[Any]:
        """Generated synthetic record sets submitted during the run."""
        ...

    @property
    def submission_result(self) -> Any:
        """Bulk submission result produced by the submit phase."""
        ...

    @property
    def poll_summary(self) -> Any:
        """Job polling summary produced by the monitor phase."""
        ...

    @property
    def dry_run(self) -> bool:
        """Whether the run executed in local dry-run validation mode."""
        ...

    @property
    def validation_report(self) -> Any:
        """Local batch validation report captured during dry-run execution, if any."""
        ...

    @property
    def phase_durations(self) -> Mapping[str, float]:
        """Elapsed seconds recorded per execution phase."""
        ...

    def summary(self) -> str:
        """Format a human-readable execution summary."""
        ...


class UATWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.uat``."""

    @property
    def state(self) -> str:
        """Current lifecycle state of the UAT workflow."""
        ...

    @property
    def state_history(self) -> Sequence[str]:
        """Recorded lifecycle state transitions for the current or latest run."""
        ...

    @property
    def last_result(self) -> UATRunResultProtocol | None:
        """Result from the most recent ``run()`` invocation, if any."""
        ...

    def transition_to(self, target: str, *, dry_run: bool = False) -> str:
        """Transition workflow state along a valid lifecycle edge."""
        ...

    def run(
        self,
        study_key: str,
        *,
        spec: Any = None,
        subject_count: int = 2,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        email_notify: bool | str | None = None,
        data_dictionary: _spi.validation.DataDictionary | None = None,
        schema: _validation.ValidationSchemaProtocol | None = None,
        seed: int | None = None,
        dry_run: bool = False,
    ) -> UATRunResultProtocol:
        """Execute the end-to-end UAT pipeline and return its run result."""
        ...

    def inspect(self, study_key: str) -> StudySnapshotProtocol:
        """Inspect study metadata via StudySchemaInspector."""
        ...

    def build_spec(self, snapshot: Any, **kwargs: Any) -> Any:
        """Auto-generate a UAT specification from a study snapshot."""
        ...

    def generate(
        self,
        spec: Any,
        snapshot: Any,
        *,
        data_dictionary: _spi.validation.DataDictionary | None = None,
        seed: int | None = None,
    ) -> list[Any]:
        """Generate synthetic record sets from a UAT specification and snapshot."""
        ...

    def submit(
        self,
        study_key: str,
        record_sets: list[Any],
        email_notify: bool | str | None = None,
        *,
        schema: _validation.ValidationSchemaProtocol | None = None,
    ) -> Any:
        """Submit generated UAT record sets in two phases."""
        ...

    def monitor(self, study_key: str, submission_result: Any) -> Any:
        """Poll submitted UAT job batches to completion."""
        ...

    def reset(self) -> None:
        """Reset the UAT workflow state machine."""
        ...


@runtime_checkable
class SyncWorkflowsNamespaceProtocol(Protocol):
    """Synchronous workflows namespace contract exposed on ``ImednetSDK.workflows``."""

    @property
    def data_extraction(self) -> DataExtractionWorkflowProtocol:
        """Access the synchronous data extraction workflow."""
        ...

    @property
    def duckdb_centralizer(self) -> DuckDBIngestionWorkflowProtocol:
        """Access the lazy DuckDB bronze/silver ingestion workflow."""
        ...

    @property
    def query_management(self) -> QueryManagementWorkflowProtocol:
        """Access the query management workflow."""
        ...

    @property
    def record_mapper(self) -> RecordMapperProtocol:
        """Access the tabular and SDTM record mapper workflow."""
        ...

    @property
    def record_update(self) -> SyncRecordUpdateWorkflowProtocol:
        """Access the synchronous record submission and update workflow."""
        ...

    @property
    def register_subjects(self) -> SyncRegisterSubjectsWorkflowProtocol:
        """Access the synchronous bulk subject registration workflow."""
        ...

    @property
    def subject_data(self) -> SubjectDataWorkflowProtocol:
        """Access the comprehensive subject data retrieval workflow."""
        ...

    @property
    def uat(self) -> UATWorkflowProtocol:
        """Access the end-to-end UAT orchestration workflow."""
        ...

    @property
    def uat_inspector(self) -> SyncStudySchemaInspectorProtocol:
        """Access the synchronous study schema inspector."""
        ...


@runtime_checkable
class AsyncWorkflowsNamespaceProtocol(Protocol):
    """Asynchronous workflows namespace contract exposed on ``AsyncImednetSDK.workflows``."""

    @property
    def record_update(self) -> AsyncRecordUpdateWorkflowProtocol:
        """Access the async-capable record submission and update workflow."""
        ...

    @property
    def register_subjects(self) -> AsyncRegisterSubjectsWorkflowProtocol:
        """Access the async-capable bulk subject registration workflow."""
        ...

    @property
    def uat_inspector(self) -> AsyncStudySchemaInspectorProtocol:
        """Access the async-capable study schema inspector."""
        ...

    @property
    @deprecated(
        "Workflow 'data_extraction' is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def data_extraction(self) -> Never:
        """Rejected synchronous-only workflow stub on AsyncImednetSDK."""
        ...

    @property
    @deprecated(
        "Workflow 'duckdb_centralizer' is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def duckdb_centralizer(self) -> Never:
        """Rejected synchronous-only workflow stub on AsyncImednetSDK."""
        ...

    @property
    @deprecated(
        "Workflow 'query_management' is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def query_management(self) -> Never:
        """Rejected synchronous-only workflow stub on AsyncImednetSDK."""
        ...

    @property
    @deprecated(
        "Workflow 'record_mapper' is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def record_mapper(self) -> Never:
        """Rejected synchronous-only workflow stub on AsyncImednetSDK."""
        ...

    @property
    @deprecated(
        "Workflow 'subject_data' is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def subject_data(self) -> Never:
        """Rejected synchronous-only workflow stub on AsyncImednetSDK."""
        ...

    @property
    @deprecated(
        "Workflow 'uat' is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def uat(self) -> Never:
        """Rejected synchronous-only workflow stub on AsyncImednetSDK."""
        ...


WorkflowsNamespaceProtocol = SyncWorkflowsNamespaceProtocol

if not TYPE_CHECKING:
    SyncWorkflowsNamespaceProtocol.__annotations__ = {
        "data_extraction": DataExtractionWorkflowProtocol,
        "duckdb_centralizer": DuckDBIngestionWorkflowProtocol,
        "query_management": QueryManagementWorkflowProtocol,
        "record_mapper": RecordMapperProtocol,
        "record_update": SyncRecordUpdateWorkflowProtocol,
        "register_subjects": SyncRegisterSubjectsWorkflowProtocol,
        "subject_data": SubjectDataWorkflowProtocol,
        "uat": UATWorkflowProtocol,
        "uat_inspector": SyncStudySchemaInspectorProtocol,
    }
    SyncWorkflowsNamespaceProtocol.__class__ = _WorkflowsRuntimeProtocolMeta
    AsyncWorkflowsNamespaceProtocol.__class__ = _WorkflowsRuntimeProtocolMeta


class MongoDbSinkConfigProtocol(Protocol):
    """Structural contract for ``MongoDbSinkConfig`` instances."""

    study_key: str
    uri: str
    database: str
    collection: str
    batch_size: int
    max_buffered_records: int | None
    as_sdtm: bool
    sdtm_domain: str | None
    standards_profile: str | Any | None
    min_standards_score: float | None
    naming_convention: str | None
    idempotent: bool
    mask_phi: bool
    include_deleted: bool
    incremental: bool
    modified_since: datetime | str | None
    record_filter: dict[str, Any] | str | None


class MongoDbSinkConfigFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.MongoDbSinkConfig``."""

    def __call__(
        self,
        study_key: str,
        batch_size: int = 500,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        idempotent: bool = True,
        mask_phi: bool = True,
        extra: dict[str, Any] = ...,
        quality_gate_enabled: bool = False,
        min_schema_readiness_score: float = 100.0,
        tracer: Any | None = None,
        modified_since: datetime | str | None = None,
        record_filter: dict[str, Any] | str | None = None,
        record_data_filter: str | None = None,
        include_deleted: bool = False,
        incremental: bool = False,
        stream_name: str | None = None,
        ledger_path: str | Path | None = None,
        state_provider: Any | None = None,
        max_buffered_records: int | None = None,
        as_sdtm: bool = False,
        sdtm_domain: str | None = None,
        standards_profile: str | Any | None = None,
        min_standards_score: float | None = None,
        naming_convention: str | None = None,
        uri: str = "",
        database: str = "",
        collection: str = "",
    ) -> MongoDbSinkConfigProtocol:
        """Instantiate a MongoDB sink configuration."""
        ...


class MongoDbExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.MongoDbExportSink``."""

    def __call__(self, config: Any) -> _spi.sink_base.ExportSink:
        """Instantiate a MongoDB export sink."""
        ...


class BoundExportToMongoDbProtocol(Protocol):
    """Bound callable contract for ``sdk.sinks.export_to_mongodb``."""

    @overload
    def __call__(
        self,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_mongodb() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def __call__(
        self,
        sdk: _spi.facade.ImednetFacade,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to MongoDB using the bound SDK client."""
        ...


class Neo4jSinkConfigProtocol(Protocol):
    """Structural contract for ``Neo4jSinkConfig`` instances."""

    study_key: str
    uri: str
    auth: tuple[str, str]
    database: str
    batch_size: int
    max_buffered_records: int | None
    as_sdtm: bool
    sdtm_domain: str | None
    standards_profile: str | Any | None
    min_standards_score: float | None
    naming_convention: str | None
    idempotent: bool
    mask_phi: bool
    include_deleted: bool
    incremental: bool
    modified_since: datetime | str | None
    record_filter: dict[str, Any] | str | None


class Neo4jSinkConfigFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.Neo4jSinkConfig``."""

    def __call__(
        self,
        study_key: str,
        batch_size: int = 500,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        idempotent: bool = True,
        mask_phi: bool = True,
        extra: dict[str, Any] = ...,
        quality_gate_enabled: bool = False,
        min_schema_readiness_score: float = 100.0,
        tracer: Any | None = None,
        modified_since: datetime | str | None = None,
        record_filter: dict[str, Any] | str | None = None,
        record_data_filter: str | None = None,
        include_deleted: bool = False,
        incremental: bool = False,
        stream_name: str | None = None,
        ledger_path: str | Path | None = None,
        state_provider: Any | None = None,
        max_buffered_records: int | None = None,
        as_sdtm: bool = False,
        sdtm_domain: str | None = None,
        standards_profile: str | Any | None = None,
        min_standards_score: float | None = None,
        naming_convention: str | None = None,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        database: str = "neo4j",
    ) -> Neo4jSinkConfigProtocol:
        """Instantiate a Neo4j sink configuration."""
        ...


class Neo4jExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.Neo4jExportSink``."""

    def __call__(self, config: Any) -> _spi.sink_base.ExportSink:
        """Instantiate a Neo4j export sink."""
        ...


class BoundExportToNeo4jProtocol(Protocol):
    """Bound callable contract for ``sdk.sinks.export_to_neo4j``."""

    @overload
    def __call__(
        self,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_neo4j() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def __call__(
        self,
        sdk: _spi.facade.ImednetFacade,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to Neo4j using the bound SDK client."""
        ...


class SnowflakeSinkConfigProtocol(Protocol):
    """Structural contract for ``SnowflakeSinkConfig`` instances."""

    study_key: str
    account: str
    user: str
    database: str
    schema: str
    warehouse: str
    stage: str
    table: str
    stage_prefix: str
    batch_size: int
    max_buffered_records: int | None
    as_sdtm: bool
    sdtm_domain: str | None
    standards_profile: str | Any | None
    min_standards_score: float | None
    naming_convention: str | None
    idempotent: bool
    mask_phi: bool
    include_deleted: bool
    incremental: bool
    modified_since: datetime | str | None
    record_filter: dict[str, Any] | str | None


class SnowflakeSinkConfigFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.SnowflakeSinkConfig``."""

    def __call__(
        self,
        study_key: str,
        batch_size: int = 500,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        idempotent: bool = True,
        mask_phi: bool = True,
        extra: dict[str, Any] = ...,
        quality_gate_enabled: bool = False,
        min_schema_readiness_score: float = 100.0,
        tracer: Any | None = None,
        modified_since: datetime | str | None = None,
        record_filter: dict[str, Any] | str | None = None,
        record_data_filter: str | None = None,
        include_deleted: bool = False,
        incremental: bool = False,
        stream_name: str | None = None,
        ledger_path: str | Path | None = None,
        state_provider: Any | None = None,
        max_buffered_records: int | None = None,
        as_sdtm: bool = False,
        sdtm_domain: str | None = None,
        standards_profile: str | Any | None = None,
        min_standards_score: float | None = None,
        naming_convention: str | None = None,
        account: str = "",
        user: str = "",
        password: str = "",
        database: str = "",
        schema: str = "PUBLIC",
        warehouse: str = "",
        stage: str = "",
        table: str = "",
        stage_prefix: str = "imednet",
        prefix: str | None = None,
        local_staging_dir: str | os.PathLike[str] | None = None,
        manifest_path: str | os.PathLike[str] | None = None,
        hard_delete: bool = False,
    ) -> SnowflakeSinkConfigProtocol:
        """Instantiate a Snowflake sink configuration."""
        ...


class SnowflakeExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.SnowflakeExportSink``."""

    def __call__(self, config: Any) -> _spi.sink_base.ExportSink:
        """Instantiate a Snowflake export sink."""
        ...


class BoundExportToSnowflakeProtocol(Protocol):
    """Bound callable contract for ``sdk.sinks.export_to_snowflake``."""

    @overload
    def __call__(
        self,
        study_key: str,
        *,
        config: Any,
        mask_phi: bool | None = None,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_snowflake() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def __call__(
        self,
        sdk: _spi.facade.ImednetFacade,
        study_key: str,
        *,
        config: Any,
        mask_phi: bool | None = None,
    ) -> int: ...

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to Snowflake using the bound SDK client."""
        ...


@runtime_checkable
class SinksNamespaceProtocol(Protocol):
    """Synchronous sinks namespace contract exposed on ``ImednetSDK.sinks``."""

    @property
    def export_to_mongodb(self) -> BoundExportToMongoDbProtocol:
        """Bound helper to export study records to MongoDB."""
        ...

    @property
    def MongoDbSinkConfig(self) -> MongoDbSinkConfigFactoryProtocol:
        """Configuration class for MongoDB export sinks."""
        ...

    @property
    def MongoDbExportSink(self) -> MongoDbExportSinkFactoryProtocol:
        """Sink class for MongoDB record exports."""
        ...

    @property
    def export_to_neo4j(self) -> BoundExportToNeo4jProtocol:
        """Bound helper to export study records to Neo4j."""
        ...

    @property
    def Neo4jSinkConfig(self) -> Neo4jSinkConfigFactoryProtocol:
        """Configuration class for Neo4j export sinks."""
        ...

    @property
    def Neo4jExportSink(self) -> Neo4jExportSinkFactoryProtocol:
        """Sink class for Neo4j record exports."""
        ...

    @property
    def export_to_snowflake(self) -> BoundExportToSnowflakeProtocol:
        """Bound helper to export study records to Snowflake."""
        ...

    @property
    def SnowflakeSinkConfig(self) -> SnowflakeSinkConfigFactoryProtocol:
        """Configuration class for Snowflake export sinks."""
        ...

    @property
    def SnowflakeExportSink(self) -> SnowflakeExportSinkFactoryProtocol:
        """Sink class for Snowflake record exports."""
        ...


SyncSinksNamespaceProtocol = SinksNamespaceProtocol


@runtime_checkable
class AsyncSinksNamespaceProtocol(Protocol):
    """Asynchronous sinks namespace contract exposed on ``AsyncImednetSDK.sinks``."""

    @deprecated(
        "export_to_mongodb() is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def export_to_mongodb(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous MongoDB export stub on AsyncImednetSDK."""
        ...

    @property
    def MongoDbSinkConfig(self) -> MongoDbSinkConfigFactoryProtocol:
        """Configuration class for MongoDB export sinks."""
        ...

    @property
    def MongoDbExportSink(self) -> MongoDbExportSinkFactoryProtocol:
        """Sink class for MongoDB record exports."""
        ...

    @deprecated(
        "export_to_neo4j() is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def export_to_neo4j(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous Neo4j export stub on AsyncImednetSDK."""
        ...

    @property
    def Neo4jSinkConfig(self) -> Neo4jSinkConfigFactoryProtocol:
        """Configuration class for Neo4j export sinks."""
        ...

    @property
    def Neo4jExportSink(self) -> Neo4jExportSinkFactoryProtocol:
        """Sink class for Neo4j record exports."""
        ...

    @deprecated(
        "export_to_snowflake() is synchronous-only and raises TypeError on AsyncImednetSDK; "
        "use ImednetSDK instead."
    )
    def export_to_snowflake(self, *args: Any, **kwargs: Any) -> Never:
        """Rejected synchronous Snowflake export stub on AsyncImednetSDK."""
        ...

    @property
    def SnowflakeSinkConfig(self) -> SnowflakeSinkConfigFactoryProtocol:
        """Configuration class for Snowflake export sinks."""
        ...

    @property
    def SnowflakeExportSink(self) -> SnowflakeExportSinkFactoryProtocol:
        """Sink class for Snowflake record exports."""
        ...


@runtime_checkable
class PluginProtocol(Protocol):
    """Protocol that every iMednet plugin factory must satisfy.

    A conforming factory is a callable that accepts an SDK instance and returns
    an object implementing :class:`WorkflowsNamespaceProtocol`.

    Example::

        from imednet.plugins import PluginProtocol
        from imednet.sdk import ImednetSDK

        class MyWorkflows:
            def __init__(self, sdk: ImednetSDK) -> None:
                self.data_extraction = ...
                self.duckdb_centralizer = ...
                self.query_management = ...
                self.record_mapper = ...
                self.record_update = ...
                self.register_subjects = ...
                self.subject_data = ...
                self.uat = ...
                self.uat_inspector = ...

        def create_workflows(sdk: ImednetSDK) -> MyWorkflows:
            return MyWorkflows(sdk)

        # Verify conformance at import time (optional, for development use):
        assert isinstance(create_workflows, PluginProtocol)
    """

    def __call__(self, sdk_instance: _spi.facade.ImednetFacade, /) -> WorkflowsNamespaceProtocol:
        """Initialize the workflows plugin with an SDK instance."""
        ...


@runtime_checkable
class SinksPluginProtocol(Protocol):
    """Protocol for the sinks plugin factory."""

    def __call__(
        self, sdk_instance: _spi.facade.ImednetFacade | _spi.facade.AsyncImednetFacade, /
    ) -> SinksNamespaceProtocol | AsyncSinksNamespaceProtocol:
        """Initialize the sinks plugin with an SDK instance."""
        ...


__all__ = [
    "AsyncRecordUpdateWorkflowProtocol",
    "AsyncRegisterSubjectsWorkflowProtocol",
    "AsyncSinksNamespaceProtocol",
    "AsyncStudySchemaInspectorProtocol",
    "AsyncWorkflowsNamespaceProtocol",
    "BoundExportToMongoDbProtocol",
    "BoundExportToNeo4jProtocol",
    "BoundExportToSnowflakeProtocol",
    "DataExtractionWorkflowProtocol",
    "DuckDBIngestionWorkflowProtocol",
    "MongoDbExportSinkFactoryProtocol",
    "MongoDbSinkConfigFactoryProtocol",
    "MongoDbSinkConfigProtocol",
    "Neo4jExportSinkFactoryProtocol",
    "Neo4jSinkConfigFactoryProtocol",
    "Neo4jSinkConfigProtocol",
    "PluginProtocol",
    "QueryManagementWorkflowProtocol",
    "RecordMapperProtocol",
    "RegisterSubjectsWorkflowProtocol",
    "SinksNamespaceProtocol",
    "SinksPluginProtocol",
    "SnowflakeExportSinkFactoryProtocol",
    "SnowflakeSinkConfigFactoryProtocol",
    "SnowflakeSinkConfigProtocol",
    "StudySnapshotProtocol",
    "SubjectComprehensiveDataProtocol",
    "SubjectDataWorkflowProtocol",
    "SyncRecordUpdateWorkflowProtocol",
    "SyncRegisterSubjectsWorkflowProtocol",
    "SyncSinksNamespaceProtocol",
    "SyncStudySchemaInspectorProtocol",
    "SyncWorkflowsNamespaceProtocol",
    "TriageStoreProtocol",
    "UATRunResultProtocol",
    "UATWorkflowProtocol",
    "WorkflowsNamespaceProtocol",
]
