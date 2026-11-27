"""Provides workflows for managing queries within iMednet studies."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

from imednet.spi.cdisc import OdmAnnotation, map_queries_to_annotations
from imednet.spi.models import (
    Query,
    QueryStatus,
    TriageAnnotation,
    TriageItem,
    TriageStatus,
    calculate_legacy_status,
)
from imednet.spi.utils import (
    SensitivityRegistry,
    SyncToAsyncFacadeAdapter,
    build_study_sensitivity_registry,
    call_async_facade_method,
    extract_sdk_base_url,
    is_async_facade,
    maybe_to_thread,
    normalize_iso_date,
    parse_iso_datetime,
    reset_base_url_context,
    resolve_enrichment_pipeline,
    resolve_sdk_base_url,
    resolve_study_configuration,
    run_sync_over_async,
    sanitize_diagnostic_text,
    sdk_governance_context,
    set_base_url_context,
)

from ._filter_bounds import filter_queries_by_subject_keys, plan_site_subject_query_filters
from ._workflow_helpers import ensure_sync_workflow_mode
from .triage_store import TriageStore

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade, TriageStoreProtocol


class QueryStateCounts(dict[str, int]):
    """State counts dictionary supporting case-insensitive and enum key access."""

    def __getitem__(self, key: Any) -> int:
        """Retrieve state count supporting case-insensitive and enum keys."""
        if isinstance(key, str) and key not in self:
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
        if hasattr(key, "value") and key.value in self:
            return self[key.value]
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = 0) -> Any:  # type: ignore[override]
        """Get state count with fallback default on missing keys."""
        try:
            return self[key]
        except KeyError:
            return default


class _QueryManagementBase:
    """Shared discrepancy lifecycle classification and triage mapping helpers."""

    @staticmethod
    def _resolve_query_status(query: Any) -> QueryStatus:
        """Resolve a query's lifecycle status from explicit attribute or comments."""
        status = getattr(query, "status", None)
        if isinstance(status, QueryStatus):
            return status
        if status is not None:
            try:
                return QueryStatus(str(status).strip().upper())
            except ValueError:
                return QueryStatus.OPEN
        comments = getattr(query, "query_comments", None)
        if comments is None:
            comments = getattr(query, "queryComments", None)
        return calculate_legacy_status(comments)

    @classmethod
    def _is_query_open(cls, query: Query, *, include_answered: bool = False) -> bool:
        """Determines if a query is open (or unresolved when include_answered is True)."""
        resolved = cls._resolve_query_status(query)
        if include_answered:
            return resolved in (QueryStatus.OPEN, QueryStatus.ANSWERED)
        return resolved == QueryStatus.OPEN

    @contextmanager
    def _governance_context(self) -> Iterator[None]:
        """Bind active SDK ``base_url`` and ``security_key`` governance context."""
        raw_sdk = getattr(self, "_sdk", None)
        if isinstance(raw_sdk, SyncToAsyncFacadeAdapter):
            raw_sdk = raw_sdk.sync_sdk
        if raw_sdk is None:
            yield
            return
        with sdk_governance_context(raw_sdk):
            token_norm = (
                set_base_url_context(resolve_sdk_base_url(raw_sdk))
                if extract_sdk_base_url(raw_sdk) is not None
                else None
            )
            try:
                yield
            finally:
                if token_norm is not None:
                    reset_base_url_context(token_norm)

    @staticmethod
    def _resolve_study_sensitivity_registry(study_key: str) -> SensitivityRegistry:
        """Resolve a study-aware ``SensitivityRegistry`` from committed ``StudyConfiguration``."""
        study_cfg = resolve_study_configuration(study_key)
        phi_fields = study_cfg.phi_fields if study_cfg is not None else None
        return build_study_sensitivity_registry(phi_fields)

    def _resolve_triage_store(
        self,
        store: TriageStoreProtocol | str | Path | None,
    ) -> TriageStoreProtocol:
        """Resolve a TriageStore instance bound to the active SDK governance context."""
        if store is not None and not isinstance(store, (str, Path)):
            return store
        with self._governance_context():
            eps = list(entry_points(group="imednet.stores", name="TriageStore"))
            store_cls: Any = eps[0].load() if eps else TriageStore
            if store is None:
                return store_cls()  # type: ignore[no-any-return]
            return store_cls(store)  # type: ignore[no-any-return]

    @staticmethod
    def _parse_comment_timestamp(raw_date: Any) -> datetime:
        """Parse a query comment timestamp into a timezone-aware UTC datetime."""
        if isinstance(raw_date, datetime):
            return raw_date if raw_date.tzinfo is not None else raw_date.replace(tzinfo=UTC)
        if isinstance(raw_date, str) and raw_date.strip():
            normalized = normalize_iso_date(raw_date.strip())
            candidate = normalized if isinstance(normalized, str) else raw_date.strip()
            if len(candidate) == 10 and candidate[4:5] == "-" and candidate[7:8] == "-":
                candidate = f"{candidate}T00:00:00Z"
            try:
                dt = parse_iso_datetime(candidate)
                return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
            except ValueError:
                pass
        return datetime.now(UTC)

    @staticmethod
    def _build_triage_item_id(query: Any, study_key: str, index: int) -> str:
        """Construct a deterministic triage item identifier for a clinical query."""
        ann_id = getattr(query, "annotation_id", None)
        if ann_id is not None and str(ann_id).strip():
            return f"{study_key}:{str(ann_id).strip()}"
        subj_key = getattr(query, "subject_key", None)
        rec_id = getattr(query, "record_id", None)
        var_name = getattr(query, "variable", None)
        subj_part = (
            subj_key.strip() if isinstance(subj_key, str) and subj_key.strip() else "unkeyed"
        )
        rec_part = str(rec_id) if rec_id is not None else str(index)
        var_part = (
            var_name.strip() if isinstance(var_name, str) and var_name.strip() else f"q{index}"
        )
        return f"{study_key}/{subj_part}/{rec_part}/{var_part}"

    @classmethod
    def _query_to_triage_item(
        cls,
        query: Any,
        *,
        study_key: str,
        index: int,
        existing: TriageItem | None = None,
        default_severity: str = "medium",
        registry: SensitivityRegistry | None = None,
    ) -> TriageItem:
        """Map a clinical Query model into a TriageItem with scrubbed comments."""
        active_registry = (
            registry if registry is not None else cls._resolve_study_sensitivity_registry(study_key)
        )
        item_id = cls._build_triage_item_id(query, study_key, index)
        query_status = cls._resolve_query_status(query)
        if query_status == QueryStatus.ANSWERED:
            target_status = TriageStatus.UNDER_REVIEW
        elif query_status in (QueryStatus.CLOSED, QueryStatus.CANCELLED):
            target_status = TriageStatus.RESOLVED
        elif existing is not None and existing.status in (
            TriageStatus.UNDER_REVIEW,
            TriageStatus.RESOLVED,
        ):
            target_status = TriageStatus.UNDER_REVIEW
        else:
            target_status = TriageStatus.NEW

        comments = getattr(query, "query_comments", None)
        if comments is None:
            comments = getattr(query, "queryComments", None)
        annotations: list[TriageAnnotation] = []
        seen_ids: set[str] = set()
        for c_idx, comment_obj in enumerate(comments or [], start=1):
            if isinstance(comment_obj, dict):
                seq = comment_obj.get("sequence")
                raw_user = comment_obj.get("user")
                raw_text = comment_obj.get("comment")
                raw_ann_status = comment_obj.get("annotation_status") or comment_obj.get(
                    "annotationStatus"
                )
                raw_date = comment_obj.get("date")
            else:
                seq = getattr(comment_obj, "sequence", None)
                raw_user = getattr(comment_obj, "user", None)
                raw_text = getattr(comment_obj, "comment", None)
                raw_ann_status = getattr(comment_obj, "annotation_status", None) or getattr(
                    comment_obj, "annotationStatus", None
                )
                raw_date = getattr(comment_obj, "date", None)

            text = (
                raw_text.strip()
                if isinstance(raw_text, str) and raw_text.strip()
                else (
                    raw_ann_status.strip()
                    if isinstance(raw_ann_status, str) and raw_ann_status.strip()
                    else ""
                )
            )
            if not text:
                continue

            base_id = f"comment-{seq}" if seq is not None else f"comment-idx-{c_idx}"
            ann_id_str = base_id if base_id not in seen_ids else f"{base_id}-{c_idx}"
            seen_ids.add(ann_id_str)

            user_id = (
                raw_user.strip() if isinstance(raw_user, str) and raw_user.strip() else "system"
            )
            scrubbed_text = sanitize_diagnostic_text(text, registry=active_registry)
            annotations.append(
                TriageAnnotation(
                    annotation_id=ann_id_str,
                    user_id=user_id,
                    comment=scrubbed_text,
                    timestamp=cls._parse_comment_timestamp(raw_date),
                )
            )

        if not annotations:
            raw_desc = getattr(query, "description", None)
            if isinstance(raw_desc, str) and raw_desc.strip():
                scrubbed_desc = sanitize_diagnostic_text(
                    raw_desc.strip(),
                    registry=active_registry,
                )
                annotations.append(
                    TriageAnnotation(
                        annotation_id="description",
                        user_id="system",
                        comment=scrubbed_desc,
                        timestamp=cls._parse_comment_timestamp(
                            getattr(query, "date_created", None)
                        ),
                    )
                )

        return TriageItem(
            item_id=item_id,
            study_key=study_key,
            status=target_status,
            assignee=existing.assignee if existing is not None else None,
            severity=existing.severity if existing is not None else default_severity,
            annotations=annotations,
        )

    def _persist_triage_queries_sync(
        self,
        study_key: str,
        queries: Iterable[Query],
        store: TriageStoreProtocol | str | Path | None,
        *,
        default_severity: str,
    ) -> list[TriageItem]:
        """Persist fetched queries into the resolved TriageStore synchronously."""
        with self._governance_context():
            target_store = self._resolve_triage_store(store)
            registry = self._resolve_study_sensitivity_registry(study_key)
            for idx, query in enumerate(queries, start=1):
                item_id = self._build_triage_item_id(query, study_key, idx)
                existing = target_store.get_triage_item(item_id)
                triage_item = self._query_to_triage_item(
                    query,
                    study_key=study_key,
                    index=idx,
                    existing=existing,
                    default_severity=default_severity,
                    registry=registry,
                )
                target_store.upsert_item(triage_item)
            return target_store.get_queue(study_key)

    @staticmethod
    def _queries_to_odm_annotations(
        study_key: str,
        queries: Iterable[Query],
        *,
        mask_phi: bool,
        phi_keys: Collection[str] | None,
    ) -> list[OdmAnnotation]:
        """Map fetched study Query records to deterministically ordered ODM 1.3.2 annotations."""
        resolved_phi: set[str] = set(phi_keys) if phi_keys is not None else set()
        if mask_phi:
            pipeline = resolve_enrichment_pipeline(study_key)
            if pipeline is not None and getattr(pipeline, "phi_keys", None):
                resolved_phi.update(pipeline.phi_keys)

        by_rec_var, by_rec, by_subj, unattached = map_queries_to_annotations(
            queries,
            mask_phi=mask_phi,
            custom_phi_keys=resolved_phi or None,
        )
        ordered: list[OdmAnnotation] = []
        for _key, anns in sorted(by_rec_var.items()):
            ordered.extend(anns)
        for _rid, anns in sorted(by_rec.items()):
            ordered.extend(anns)
        for _sk, anns in sorted(by_subj.items()):
            ordered.extend(anns)
        ordered.extend(unattached)
        ordered.sort(key=lambda ann: ann.seq_num)
        return ordered


class AsyncQueryManagementWorkflow(_QueryManagementBase):
    """Canonical asynchronous workflow for clinical discrepancy management."""

    supports_async = True

    def __init__(self, sdk: AsyncImednetFacade) -> None:
        """Initialize the asynchronous query management workflow."""
        self._sdk = sdk

    async def get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        include_answered: bool = False,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronously retrieve open queries (and optionally answered queries)."""
        filters = dict(additional_filter) if additional_filter else {}
        all_matching_queries = await call_async_facade_method(
            self._sdk, "get_queries", study_key, **filters, **kwargs
        )
        return [
            query
            for query in all_matching_queries
            if self._is_query_open(query, include_answered=include_answered)
        ]

    async def get_unresolved_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronously retrieve all unresolved (OPEN and ANSWERED) queries."""
        return await self.get_open_queries(
            study_key,
            additional_filter=additional_filter,
            include_answered=True,
            **kwargs,
        )

    async def get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronously retrieve all queries for a specific subject within a study."""
        final_filter_dict: dict[str, Any] = {"subject_key": subject_key}
        if additional_filter:
            final_filter_dict.update(additional_filter)
        return list(
            await call_async_facade_method(
                self._sdk, "get_queries", study_key, **final_filter_dict, **kwargs
            )
        )

    async def get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronously retrieve all queries for active, keyed subjects at a site."""
        subjects = await call_async_facade_method(
            self._sdk, "get_subjects", study_key, site_name=site_key
        )
        subject_keys, final_filter_dict, filter_locally = plan_site_subject_query_filters(
            subjects,
            additional_filter,
            kwargs,
        )
        if not subject_keys:
            return []

        queries = await call_async_facade_method(
            self._sdk, "get_queries", study_key, **final_filter_dict, **kwargs
        )
        if not filter_locally:
            return list(queries)
        return filter_queries_by_subject_keys(queries, subject_keys)

    async def get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Asynchronously count queries grouped by their current QueryStatus values."""
        all_queries = await call_async_facade_method(self._sdk, "get_queries", study_key, **kwargs)
        state_counts = QueryStateCounts({s.value: 0 for s in QueryStatus})
        for query in all_queries:
            status_key = self._resolve_query_status(query).value
            state_counts[status_key] += 1
        return state_counts

    async def sync_queries_to_triage(
        self,
        study_key: str,
        store: TriageStoreProtocol | str | Path | None = None,
        *,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        unresolved_only: bool = False,
        default_severity: str = "medium",
        **kwargs: Any,
    ) -> list[TriageItem]:
        """Asynchronously synchronize clinical queries into a local TriageStore queue."""
        with self._governance_context():
            if unresolved_only:
                queries = await self.get_unresolved_queries(
                    study_key,
                    additional_filter=additional_filter,
                    **kwargs,
                )
            else:
                filters = dict(additional_filter) if additional_filter else {}
                queries = list(
                    await call_async_facade_method(
                        self._sdk, "get_queries", study_key, **filters, **kwargs
                    )
                )
            return await maybe_to_thread(
                self._persist_triage_queries_sync,
                study_key,
                queries,
                store,
                default_severity=default_severity,
            )

    async def extract_odm_annotations(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        open_only: bool = False,
        include_answered: bool = False,
        mask_phi: bool = True,
        phi_keys: Collection[str] | None = None,
        **kwargs: Any,
    ) -> list[OdmAnnotation]:
        """Asynchronously extract study Query histories mapped to ODM 1.3.2 ``<Annotation>`` models."""
        if open_only:
            queries = await self.get_open_queries(
                study_key,
                additional_filter=additional_filter,
                include_answered=include_answered,
                **kwargs,
            )
        else:
            filters = dict(additional_filter) if additional_filter else {}
            queries = list(
                await call_async_facade_method(
                    self._sdk, "get_queries", study_key, **filters, **kwargs
                )
            )
        return self._queries_to_odm_annotations(
            study_key,
            queries,
            mask_phi=mask_phi,
            phi_keys=phi_keys,
        )


class QueryManagementWorkflow(_QueryManagementBase):
    """Provides methods for common query management tasks.

    Args:
        sdk: An instance of the synchronous ImednetSDK.
    """

    def __init__(self, sdk: ImednetFacade) -> None:
        """Initialize the query management workflow."""
        self._sdk = sdk
        self._async_core = AsyncQueryManagementWorkflow(SyncToAsyncFacadeAdapter(sdk))

    def get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        include_answered: bool = False,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves open queries for a given study, optionally including answered queries."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "QueryManagementWorkflow",
            "get_open_queries",
            "get_open_queries",
        )
        return run_sync_over_async(
            lambda: self._async_core.get_open_queries(
                study_key,
                additional_filter=additional_filter,
                include_answered=include_answered,
                **kwargs,
            )
        )

    def get_unresolved_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all unresolved (``OPEN`` and ``ANSWERED``) queries for a given study."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "QueryManagementWorkflow",
            "get_unresolved_queries",
            "get_unresolved_queries",
        )
        return self.get_open_queries(
            study_key,
            additional_filter=additional_filter,
            include_answered=True,
            **kwargs,
        )

    def get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all queries for a specific subject within a study."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "QueryManagementWorkflow",
            "get_queries_for_subject",
            "get_queries_for_subject",
        )
        return run_sync_over_async(
            lambda: self._async_core.get_queries_for_subject(
                study_key,
                subject_key,
                additional_filter=additional_filter,
                **kwargs,
            )
        )

    def get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all queries for active, keyed subjects at a specific site within a study."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "QueryManagementWorkflow",
            "get_queries_by_site",
            "get_queries_by_site",
        )
        return run_sync_over_async(
            lambda: self._async_core.get_queries_by_site(
                study_key,
                site_key,
                additional_filter=additional_filter,
                **kwargs,
            )
        )

    def get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Counts queries grouped by their current QueryStatus values."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "QueryManagementWorkflow",
            "get_query_state_counts",
            "get_query_state_counts",
        )
        return run_sync_over_async(
            lambda: self._async_core.get_query_state_counts(study_key, **kwargs)
        )

    def sync_queries_to_triage(
        self,
        study_key: str,
        store: TriageStoreProtocol | str | Path | None = None,
        *,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        unresolved_only: bool = False,
        default_severity: str = "medium",
        **kwargs: Any,
    ) -> list[TriageItem]:
        """Synchronize clinical queries for a study into a local TriageStore queue."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "QueryManagementWorkflow",
            "sync_queries_to_triage",
            "sync_queries_to_triage",
        )
        with self._governance_context():
            if unresolved_only and "get_unresolved_queries" in vars(self):
                queries = self.get_unresolved_queries(
                    study_key,
                    additional_filter=additional_filter,
                    **kwargs,
                )
                return self._persist_triage_queries_sync(
                    study_key,
                    queries,
                    store,
                    default_severity=default_severity,
                )
            return run_sync_over_async(
                lambda: self._async_core.sync_queries_to_triage(
                    study_key,
                    store=store,
                    additional_filter=additional_filter,
                    unresolved_only=unresolved_only,
                    default_severity=default_severity,
                    **kwargs,
                )
            )

    def extract_odm_annotations(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        open_only: bool = False,
        include_answered: bool = False,
        mask_phi: bool = True,
        phi_keys: Collection[str] | None = None,
        **kwargs: Any,
    ) -> list[OdmAnnotation]:
        """Extract study Query and QueryComment histories mapped to ODM 1.3.2 ``<Annotation>`` models."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "QueryManagementWorkflow",
            "extract_odm_annotations",
            "extract_odm_annotations",
        )
        if open_only and "get_open_queries" in vars(self):
            queries = self.get_open_queries(
                study_key,
                additional_filter=additional_filter,
                include_answered=include_answered,
                **kwargs,
            )
            return self._queries_to_odm_annotations(
                study_key,
                queries,
                mask_phi=mask_phi,
                phi_keys=phi_keys,
            )
        return run_sync_over_async(
            lambda: self._async_core.extract_odm_annotations(
                study_key,
                additional_filter=additional_filter,
                open_only=open_only,
                include_answered=include_answered,
                mask_phi=mask_phi,
                phi_keys=phi_keys,
                **kwargs,
            )
        )


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.query_management.get_open_queries(...)`).
# - Provides convenient ways to access query information without manually constructing
#   complex filters.
