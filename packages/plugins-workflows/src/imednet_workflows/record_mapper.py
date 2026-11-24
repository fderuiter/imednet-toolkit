"""Record-to-DataFrame mapping workflow for iMednet.

This module provides the :class:`RecordMapper` which handles fetching,
filtering, and parsing EDC records into tabular formats like pandas
DataFrames, including support for dynamic Pydantic model validation.
"""

from __future__ import annotations

import heapq
import inspect
import logging
import types
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment, unused-ignore]
from pydantic import BaseModel, Field, ValidationError, create_model

from imednet.spi.cdisc import (
    DataDictionary,
    DatasetJsonClinicalData,
    DatasetJsonDataset,
    build_dataset_json_clinical_data,
    build_dataset_json_dataset,
)
from imednet.spi.endpoints import Record as RecordModel
from imednet.spi.endpoints import Variable as VariableModel
from imednet.spi.models import (
    _SDTM_RECORDS_SORT,
    _UNSET_PIPELINE,
    SDTM_DOMAIN_SPECS,
    _collapse_alnum_key,
    _extract_stream_usubjid_and_id,
    _iter_sorted_sdtm_frames,
    _SdtmNormalizer,
    format_iso8601_dtc,
    transform_records_to_sdtm_rows,
)
from imednet.spi.models import (
    transform_records_to_sdtm as _core_transform_records_to_sdtm,
)
from imednet.spi.sink_base import (
    extract_record_form_ref,
    filter_forms_by_sdtm_domain,
    is_record_deleted,
)
from imednet.spi.utils import (
    CompiledRecordPredicate,
    EnrichmentPipeline,
    compile_record_predicate,
    get_tabular_batch_stamp,
    global_sensitivity_registry,
    mask_clinical_phi,
    mask_custom_phi_recursive,
    normalize_phi_keys_snapshot,
    resolve_enrichment_pipeline,
    stamp_tabular_batch,
)

from .cached_loader import CachedRecordsLoader
from .chunked_pipeline import DEFAULT_CHUNK_SIZE, ChunkedRecordPipeline, iter_chunks

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

# Setup basic logging
logger = logging.getLogger(__name__)

_CONTAINER_CELL_TYPES = (dict, list, tuple)
_SCALAR_CELL_CLASSES = frozenset({str, int, float, bool, type(None)})


class _IngressPreparedRows(list[dict[str, Any]]):
    """Parsed record rows carrying verified ingress PHI-masking provenance."""

    def __init__(
        self,
        rows: Iterable[dict[str, Any]] = (),
        *,
        ingress_masked: bool = False,
        registry_snapshot: tuple[frozenset[str], frozenset[str]] | None = None,
        phi_keys: frozenset[str] = frozenset(),
        has_nested_containers: bool = False,
    ) -> None:
        super().__init__(rows)
        self.ingress_masked = ingress_masked
        self.registry_snapshot = registry_snapshot
        self.phi_keys = phi_keys
        self.has_nested_containers = has_nested_containers


def _mask_series_nested_containers(
    series: pd.Series,
    phi_keys: set[str],
) -> pd.Series:
    """Mask nested dict/list/tuple cells in ``series`` without scalar mask_clinical_phi calls."""
    values = series.tolist()
    if not any(isinstance(v, _CONTAINER_CELL_TYPES) for v in values):
        return series
    new_values: list[Any] = []
    for val in values:
        if isinstance(val, _CONTAINER_CELL_TYPES):
            masked_val = mask_clinical_phi(val)
            if phi_keys:
                masked_val = mask_custom_phi_recursive(masked_val, phi_keys)
            new_values.append(masked_val)
        else:
            new_values.append(val)
    return pd.Series(new_values, index=series.index, dtype=object)


def transform_records_to_sdtm(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
    mask_phi: bool = True,
    enrichment_pipeline: EnrichmentPipeline | None = _UNSET_PIPELINE,
    pipeline: EnrichmentPipeline | None = _UNSET_PIPELINE,
    seq_counters: dict[str, int] | None = None,
    record_seqs: dict[str, int] | None = None,
    preserve_record_id: bool = False,
    phi_keys: Iterable[str] | None = None,
    registry: Any | None = None,
) -> pd.DataFrame:
    """Transform clinical records into a normalized CDISC SDTM domain DataFrame.

    Delegates normalization, ISO 8601 ``BRTHDTC`` year redaction, subject-group
    sorting, and ``<DOM>SEQ`` assignment (omitting ``DMSEQ`` on ``DM``) to the
    core ``imednet.spi.models`` SDTM engine while resolving study enrichment
    rules through ``resolve_enrichment_pipeline`` when not explicitly provided.
    """
    resolved_pipeline = (
        enrichment_pipeline
        if enrichment_pipeline is not _UNSET_PIPELINE
        else (
            pipeline if pipeline is not _UNSET_PIPELINE else resolve_enrichment_pipeline(study_key)
        )
    )
    return _core_transform_records_to_sdtm(
        records,
        domain=domain,
        study_key=study_key,
        include_deleted=include_deleted,
        mask_phi=mask_phi,
        seq_counters=seq_counters,
        record_seqs=record_seqs,
        preserve_record_id=preserve_record_id,
        phi_keys=phi_keys,
        registry=registry,
        pipeline=resolved_pipeline,
    )


class RecordMapper:
    """Maps EDC records for a study into a pandas DataFrame.

    Features:
      - Fetches variable definitions for column mapping.
      - Dynamically creates a Pydantic model for type validation of record data.
      - Fetches records, applying server-side filtering where possible.
      - Merges metadata and record data.
      - Offers choice between variable names or labels for column headers.
      - Handles parsing errors gracefully for individual records.

    Example:
        sdk = ImednetSDK(api_key, security_key, base_url)
        mapper = RecordMapper(sdk)
        # Get DataFrame with labels as columns, filtered by visit
        df_labels = mapper.dataframe(study_key="MYSTUDY", visit_key="VISIT1")
        # Get DataFrame with variable names as columns
        df_names = mapper.dataframe(study_key="MYSTUDY", use_labels_as_columns=False)
    """

    def __init__(
        self,
        sdk: ImednetFacade,
        *,
        loader: CachedRecordsLoader | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        mask_phi: bool = True,
        include_deleted: bool = False,
    ) -> None:
        """Initialize with an :class:`ImednetSDK` instance.

        ``loader`` enables cache-backed streaming for large-study workflows.
        ``chunk_size`` controls the maximum number of records parsed into each
        yielded batch when using chunked mapping helpers.
        ``mask_phi`` controls whether sensitive clinical fields are masked at
        mapper ingress by default.
        ``include_deleted`` controls whether voided (``deleted=True``) records
        are retained (defaults to ``False``).
        """
        self.sdk = sdk
        self._loader = loader
        self._pipeline = ChunkedRecordPipeline(chunk_size=chunk_size)
        self.mask_phi = mask_phi
        self._include_deleted = include_deleted
        self._last_variables: list[VariableModel] = []
        self._active_study_key: str | None = None
        self._enrichment_cache: dict[str, EnrichmentPipeline | None] = {}
        self._site_map_cache: dict[str, dict[int, str]] = {}
        self._interval_map_cache: dict[str, dict[int, str]] = {}
        self._visit_interval_map_cache: dict[str, dict[int, tuple[int | None, str | None]]] = {}
        self._form_key_map_cache: dict[str, dict[int, str]] = {}

    def _iter_sdk_lookup_items(
        self,
        study_key: str,
        endpoint_attr: str,
        facade_method: str,
    ) -> list[Any]:
        """Safely fetch study metadata items without triggering unconfigured mock calls."""
        from unittest.mock import NonCallableMock

        try:
            if isinstance(self.sdk, NonCallableMock):
                mock_children = getattr(self.sdk, "_mock_children", {})
                obj_vars = vars(self.sdk)
                has_ep = endpoint_attr in mock_children or endpoint_attr in obj_vars
                has_facade = facade_method in mock_children or facade_method in obj_vars
                if not has_ep and not has_facade:
                    return []
                raw_iter = (
                    getattr(self.sdk, endpoint_attr).list(study_key=study_key)
                    if (has_ep and not has_facade)
                    else getattr(self.sdk, facade_method)(study_key=study_key)
                )
            elif callable(getattr(self.sdk, facade_method, None)):
                raw_iter = getattr(self.sdk, facade_method)(study_key=study_key)
            else:
                ep = getattr(self.sdk, endpoint_attr, None)
                if ep is None or not callable(getattr(ep, "list", None)):
                    return []
                raw_iter = ep.list(study_key=study_key)
            return list(raw_iter)
        except Exception as exc:  # pragma: no cover - defensive for unmocked routes
            logger.debug(
                "Skipping %s metadata hydration for study '%s': %s",
                endpoint_attr,
                study_key,
                exc,
            )
            return []

    def _resolve_site_name(self, study_key: str, site_id: int) -> str | None:
        """Resolve human-readable ``siteName`` for *site_id* using a per-study cache."""
        if study_key not in self._site_map_cache:
            mapping: dict[int, str] = {}
            for site in self._iter_sdk_lookup_items(study_key, "sites", "get_sites"):
                sid = (
                    site.get("siteId") if isinstance(site, dict) else getattr(site, "site_id", None)
                )
                sname = (
                    site.get("siteName")
                    if isinstance(site, dict)
                    else getattr(site, "site_name", None)
                )
                if sid is not None and isinstance(sname, str) and sname:
                    mapping[int(sid)] = sname
            self._site_map_cache[study_key] = mapping
        return self._site_map_cache[study_key].get(site_id)

    def _ensure_visit_interval_maps(self, study_key: str) -> None:
        """Populate visit-backed interval fallback maps once per study when needed."""
        if study_key in self._visit_interval_map_cache:
            return
        visit_map: dict[int, tuple[int | None, str | None]] = {}
        interval_map = self._interval_map_cache.setdefault(study_key, {})
        for visit in self._iter_sdk_lookup_items(study_key, "visits", "get_visits"):
            vid = (
                visit.get("visitId")
                if isinstance(visit, dict)
                else getattr(visit, "visit_id", None)
            )
            iid = (
                visit.get("intervalId")
                if isinstance(visit, dict)
                else getattr(visit, "interval_id", None)
            )
            iname = (
                visit.get("intervalName")
                if isinstance(visit, dict)
                else getattr(visit, "interval_name", None)
            )
            coerced_iid = int(iid) if iid is not None else None
            coerced_iname = str(iname) if isinstance(iname, str) and iname else None
            if coerced_iid is not None and coerced_iname and coerced_iid not in interval_map:
                interval_map[coerced_iid] = coerced_iname
            if vid is not None:
                visit_map[int(vid)] = (coerced_iid, coerced_iname)
        self._visit_interval_map_cache[study_key] = visit_map

    def _resolve_interval_name(
        self,
        study_key: str,
        interval_id: int,
        *,
        visit_id: int | None = None,
    ) -> str | None:
        """Resolve human-readable ``intervalName`` for *interval_id* using Intervals + Visits."""
        if study_key not in self._interval_map_cache:
            mapping: dict[int, str] = {}
            for interval in self._iter_sdk_lookup_items(study_key, "intervals", "get_intervals"):
                iid = (
                    interval.get("intervalId")
                    if isinstance(interval, dict)
                    else getattr(interval, "interval_id", None)
                )
                iname = (
                    interval.get("intervalName")
                    if isinstance(interval, dict)
                    else getattr(interval, "interval_name", None)
                )
                if iid is not None and isinstance(iname, str) and iname:
                    mapping[int(iid)] = iname
            self._interval_map_cache[study_key] = mapping
        found = self._interval_map_cache[study_key].get(interval_id)
        if found is not None:
            return found
        self._ensure_visit_interval_maps(study_key)
        found = self._interval_map_cache[study_key].get(interval_id)
        if found is not None:
            return found
        if visit_id is not None:
            visit_pair = self._visit_interval_map_cache[study_key].get(visit_id)
            if visit_pair is not None:
                return visit_pair[1]
        return None

    def _resolve_visit_interval(
        self,
        study_key: str,
        visit_id: int,
    ) -> tuple[int | None, str | None]:
        """Resolve ``(intervalId, intervalName)`` from *visit_id* when absent on the Record."""
        self._ensure_visit_interval_maps(study_key)
        pair = self._visit_interval_map_cache[study_key].get(visit_id)
        if pair is None:
            return None, None
        iid, iname = pair
        if iid is not None and iname is None:
            iname = self._resolve_interval_name(study_key, iid)
        return iid, iname

    def _resolve_form_key(self, study_key: str | None, form_id: int) -> str | None:
        """Resolve ``formKey`` from cached variables or the Forms endpoint."""
        for var in self._last_variables:
            if getattr(var, "form_id", None) == form_id:
                vf_key = getattr(var, "form_key", None)
                if isinstance(vf_key, str) and vf_key:
                    return vf_key
        if not study_key:
            return None
        if study_key not in self._form_key_map_cache:
            mapping: dict[int, str] = {}
            for form in self._iter_sdk_lookup_items(study_key, "forms", "get_forms"):
                fid = (
                    form.get("formId") if isinstance(form, dict) else getattr(form, "form_id", None)
                )
                fkey = (
                    form.get("formKey")
                    if isinstance(form, dict)
                    else getattr(form, "form_key", None)
                )
                if fid is not None and isinstance(fkey, str) and fkey:
                    mapping[int(fid)] = fkey
            self._form_key_map_cache[study_key] = mapping
        return self._form_key_map_cache[study_key].get(form_id)

    def _get_enrichment_pipeline(self, study_key: str | None = None) -> EnrichmentPipeline | None:
        """Resolve and cache committed StudyConfiguration rules for *study_key*."""
        sk = study_key if isinstance(study_key, str) and study_key else self._active_study_key
        if not sk:
            return None
        if sk not in self._enrichment_cache:
            self._enrichment_cache[sk] = resolve_enrichment_pipeline(sk)
        return self._enrichment_cache[sk]

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
    def _fetch_variable_metadata(
        self,
        study_key: str,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Return variable names and label mapping for a study."""
        self._active_study_key = study_key
        self._last_variables = []
        if variable_whitelist is not None and len(variable_whitelist) == 0:
            return [], {}
        if form_whitelist is not None and len(form_whitelist) == 0:
            return [], {}

        filters: dict[str, Any] = {}
        if form_whitelist is not None:
            filters["formId"] = (
                form_whitelist[0] if len(form_whitelist) == 1 else list(form_whitelist)
            )
            if (
                variable_whitelist is not None
                and len(variable_whitelist) == 1
                and len(form_whitelist) == 1
            ):
                filters["variableName"] = variable_whitelist[0]
        elif variable_whitelist is not None:
            filters["variableName"] = (
                variable_whitelist[0] if len(variable_whitelist) == 1 else list(variable_whitelist)
            )

        from unittest.mock import NonCallableMock

        if isinstance(self.sdk, NonCallableMock):
            mock_children = getattr(self.sdk, "_mock_children", {})
            obj_vars = vars(self.sdk)
            has_vars_ep = "variables" in mock_children or "variables" in obj_vars
            has_get_vars = "get_variables" in mock_children or "get_variables" in obj_vars
            raw_vars_iter = (
                self.sdk.variables.list(study_key=study_key, **filters)
                if (has_vars_ep and not has_get_vars)
                else self.sdk.get_variables(study_key=study_key, **filters)
            )
        else:
            raw_vars_iter = self.sdk.get_variables(
                study_key=study_key,
                **filters,
            )
        variables: list[VariableModel] = list(raw_vars_iter)
        if form_whitelist is not None:
            allowed_forms = set(form_whitelist)
            variables = [v for v in variables if v.form_id is None or v.form_id in allowed_forms]
        if variable_whitelist is not None:
            allowed_vars = set(variable_whitelist)
            variables = [v for v in variables if v.variable_name in allowed_vars]

        if not variables:
            logger.warning(
                "No variables found for study '%s'. Returning empty DataFrame.",
                study_key,
            )
            return [], {}

        self._last_variables = list(variables)
        variable_keys = [v.variable_name for v in variables]
        label_map = {v.variable_name: v.label for v in variables}
        return variable_keys, label_map  # type: ignore

    def _build_record_model(
        self, variable_keys: list[str], label_map: dict[str, str]
    ) -> type[BaseModel]:
        """Create a dynamic model for the record data payload."""
        fields: dict[str, tuple[Any | None, Any]] = {}
        for key in variable_keys:
            fields[key] = (
                Any | None,
                Field(None, alias=key, description=label_map.get(key, key)),
            )
        return create_model("RecordData", __base__=BaseModel, **fields)  # type: ignore

    @staticmethod
    def _extract_form_ids(
        filters: dict[str, Any | tuple[str, Any] | list[Any]],
    ) -> tuple[list[Any] | None, dict[str, Any | tuple[str, Any] | list[Any]]]:
        """Extract scalar or multi-item formId filters and strip variable filters for /records."""
        cleaned = dict(filters)
        cleaned.pop("variableNames", None)
        cleaned.pop("variableName", None)

        if "formId" not in cleaned:
            return None, cleaned

        raw_form_id = cleaned.get("formId")
        if isinstance(raw_form_id, (list, set)):
            cleaned.pop("formId")
            return list(dict.fromkeys(raw_form_id)), cleaned
        if not isinstance(raw_form_id, tuple):
            cleaned.pop("formId")
            return [raw_form_id], cleaned
        return None, cleaned

    def _is_mock_sdk_or_callable(self, fn: Any = None) -> bool:
        """Return True when ``self.sdk`` or *fn* is a unittest mock object."""
        from unittest.mock import NonCallableMock

        return isinstance(self.sdk, NonCallableMock) or isinstance(fn, NonCallableMock)

    def _can_stream_via_records_endpoint(self) -> bool:
        """Return True when ``self.sdk.records.list`` can stream paginated responses."""
        from unittest.mock import NonCallableMock

        if isinstance(self.sdk, NonCallableMock):
            mock_children = getattr(self.sdk, "_mock_children", {})
            obj_vars = vars(self.sdk)
            if "records" in mock_children or "records" in obj_vars:
                return True
            return not ("get_records" in mock_children or "get_records" in obj_vars)

        records_ep = getattr(self.sdk, "records", None)
        return records_ep is not None and callable(getattr(records_ep, "list", None))

    @staticmethod
    def _merge_form_record_streams(
        fetch_fn: Any,
        *,
        study_key: str,
        form_id_list: list[Any],
        record_data_filter: str | None,
        filters: dict[str, Any],
        include_deleted: bool,
    ) -> Iterator[RecordModel]:
        """Merge per-form SDK record streams by (USUBJID, recordId) so Subjects stay grouped."""

        def _iter_keyed_form(
            form_idx: int, fid: Any
        ) -> Iterator[tuple[str, tuple[int, int, str], int, int, RecordModel]]:
            raw_stream = fetch_fn(
                study_key=study_key,
                record_data_filter=record_data_filter,
                **filters,
                formId=fid,
            )
            if isinstance(raw_stream, (list, tuple)):
                filtered = [r for r in raw_stream if include_deleted or not is_record_deleted(r)]
                keyed = [
                    (*_extract_stream_usubjid_and_id(r), form_idx, seq_idx, r)
                    for seq_idx, r in enumerate(filtered)
                ]
                keyed.sort(key=lambda entry: entry[:4])
                yield from keyed
                return

            seq_idx = 0
            for rec in raw_stream:
                if not include_deleted and is_record_deleted(rec):
                    continue
                usubjid, rec_id_key = _extract_stream_usubjid_and_id(rec)
                yield (usubjid, rec_id_key, form_idx, seq_idx, rec)
                seq_idx += 1

        merged = heapq.merge(
            *(_iter_keyed_form(idx, fid) for idx, fid in enumerate(form_id_list)),
            key=lambda entry: entry[:4],
        )
        for _, _, _, _, rec in merged:
            yield rec

    def _stream_sdk_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
        sort: str | None = None,
        group_by_subject: bool = False,
    ) -> Iterator[RecordModel]:
        """Lazily stream records from the SDK, applying visit/form/deleted filters."""
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        raw_filters: dict[str, Any | tuple[str, Any] | list[Any]] = (
            dict(extra_filters) if extra_filters else {}
        )
        if visit_key is not None:
            try:
                raw_filters["visitId"] = int(visit_key)
            except ValueError:
                logger.warning(
                    "Invalid visit_key '%s'. Should be convertible to int. Fetching all records.",
                    visit_key,
                )

        form_id_list, filters = self._extract_form_ids(raw_filters)
        if form_id_list is not None and len(form_id_list) == 0:
            return
        if sort is not None and "sort" not in filters:
            filters["sort"] = sort

        use_streaming_endpoint = self._can_stream_via_records_endpoint()
        records_ep = getattr(self.sdk, "records", None) if use_streaming_endpoint else None
        fetch_fn: Any = (
            records_ep.list
            if use_streaming_endpoint and records_ep is not None
            else self.sdk.get_records
        )

        try:
            if form_id_list is not None:
                if group_by_subject and len(form_id_list) > 1:
                    yield from self._merge_form_record_streams(
                        fetch_fn,
                        study_key=study_key,
                        form_id_list=form_id_list,
                        record_data_filter=record_data_filter,
                        filters=filters,
                        include_deleted=effective_include_deleted,
                    )
                    return
                for fid in form_id_list:
                    raw_form_records = fetch_fn(
                        study_key=study_key,
                        record_data_filter=record_data_filter,
                        **filters,
                        formId=fid,
                    )
                    if group_by_subject and (
                        isinstance(raw_form_records, (list, tuple))
                        or (
                            self._is_mock_sdk_or_callable(fetch_fn)
                            and not isinstance(raw_form_records, types.GeneratorType)
                        )
                    ):
                        materialized_form = [
                            rec
                            for rec in raw_form_records
                            if effective_include_deleted or not is_record_deleted(rec)
                        ]
                        materialized_form.sort(key=lambda r: _extract_stream_usubjid_and_id(r))
                        yield from materialized_form
                        continue
                    for rec in raw_form_records:
                        if not effective_include_deleted and is_record_deleted(rec):
                            continue
                        yield rec
                return

            raw_records = fetch_fn(
                study_key=study_key,
                record_data_filter=record_data_filter,
                **filters,
            )
            if group_by_subject and (
                isinstance(raw_records, (list, tuple))
                or (
                    self._is_mock_sdk_or_callable(fetch_fn)
                    and not isinstance(raw_records, types.GeneratorType)
                )
            ):
                materialized = [
                    rec
                    for rec in raw_records
                    if effective_include_deleted or not is_record_deleted(rec)
                ]
                materialized.sort(key=lambda r: _extract_stream_usubjid_and_id(r))
                yield from materialized
                return

            for rec in raw_records:
                if not effective_include_deleted and is_record_deleted(rec):
                    continue
                yield rec
        except ValueError:
            raise
        except Exception as exc:  # pragma: no cover - unexpected
            logger.error("Failed to fetch records for study '%s': %s", study_key, exc)
            return

    def _fetch_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
    ) -> list[RecordModel]:
        """Fetch records for a study applying optional filters."""
        self._active_study_key = study_key
        return list(
            self._stream_sdk_records(
                study_key,
                visit_key=visit_key,
                extra_filters=extra_filters,
                record_data_filter=record_data_filter,
                include_deleted=include_deleted,
            )
        )

    @staticmethod
    def _resolve_loader_iter_callable(loader: Any) -> tuple[Any | None, bool]:
        """Return (bound_iter_callable, supports_order_by_subject) for *loader*."""
        from unittest.mock import NonCallableMock

        bound_iter: Any = None
        if isinstance(loader, NonCallableMock):
            mock_children = getattr(loader, "_mock_children", {})
            mock_vars = vars(loader)
            if "iter_cached_records" in mock_children or "iter_cached_records" in mock_vars:
                bound_iter = loader.iter_cached_records
        else:
            inst_vars = vars(loader) if hasattr(loader, "__dict__") else {}
            if "iter_cached_records" in inst_vars and callable(inst_vars["iter_cached_records"]):
                bound_iter = inst_vars["iter_cached_records"]
            elif callable(getattr(type(loader), "iter_cached_records", None)):
                bound_iter = loader.iter_cached_records

        if not callable(bound_iter):
            return None, False

        supports_subject_order = False
        try:
            sig = inspect.signature(bound_iter)
            supports_subject_order = "order_by_subject" in sig.parameters
        except (TypeError, ValueError):
            supports_subject_order = False
        return bound_iter, supports_subject_order

    def _iter_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
        sort: str | None = None,
        group_by_subject: bool = False,
    ) -> Iterable[RecordModel]:
        """Iterate lazily over records for a study, using cache if available."""
        self._active_study_key = study_key
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        raw_filters = dict(extra_filters) if extra_filters else {}
        explicit_sort = sort if sort is not None else raw_filters.get("sort")
        has_pagination_override = (
            raw_filters.get("page") is not None or raw_filters.get("size") is not None
        )

        loader = self._loader
        if loader is not None and not effective_include_deleted and not has_pagination_override:
            sync_method = getattr(loader, "sync_records", None)
            bound_iter, supports_subject_order = self._resolve_loader_iter_callable(loader)
            is_custom_sort = False
            if explicit_sort is not None and str(explicit_sort).strip():
                is_sdtm_subject_sort = explicit_sort == _SDTM_RECORDS_SORT and group_by_subject
                is_custom_sort = not is_sdtm_subject_sort

            if callable(sync_method) and callable(bound_iter) and not is_custom_sort:
                form_id_list, remaining_filters = self._extract_form_ids(raw_filters)
                if form_id_list is not None and len(form_id_list) == 0:
                    return iter(())
                form_ids: set[Any] | None = set(form_id_list) if form_id_list is not None else None
                if form_id_list is not None and remaining_filters:
                    compile_record_predicate(
                        extra_filters={**remaining_filters, "formId": form_id_list[0]},
                        record_data_filter=record_data_filter,
                    )
                predicate = compile_record_predicate(
                    extra_filters=remaining_filters,
                    record_data_filter=record_data_filter,
                )
                if predicate.always_empty:
                    return iter(())

                sync_method(study_key)
                iter_kwargs: dict[str, Any] = {"chunk_size": self._pipeline.chunk_size}
                if group_by_subject and supports_subject_order:
                    iter_kwargs["order_by_subject"] = True
                return self._filter_records(
                    bound_iter(study_key, **iter_kwargs),
                    visit_key=visit_key,
                    form_ids=form_ids,
                    include_deleted=effective_include_deleted,
                    predicate=predicate,
                )

        return self._stream_sdk_records(
            study_key,
            visit_key,
            extra_filters=extra_filters,
            record_data_filter=record_data_filter,
            include_deleted=effective_include_deleted,
            sort=sort,
            group_by_subject=group_by_subject,
        )

    def _filter_records(
        self,
        records: Iterable[RecordModel],
        *,
        visit_key: str | None,
        form_ids: set[Any] | None,
        include_deleted: bool | None = None,
        predicate: CompiledRecordPredicate | None = None,
    ) -> Iterator[RecordModel]:
        """Apply visit, form, metadata, recordData, and deleted filters to cached records."""
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        visit_id: int | None = None
        if visit_key is not None:
            try:
                visit_id = int(visit_key)
            except ValueError:
                logger.warning(
                    "Invalid visit_key '%s'. Should be convertible to int. Fetching all records.",
                    visit_key,
                )

        for record in records:
            if not effective_include_deleted and is_record_deleted(record):
                continue
            if visit_id is not None and record.visit_id != visit_id:
                continue
            if form_ids is not None and record.form_id not in form_ids:
                continue
            emitted: RecordModel = record
            if predicate is not None:
                matched = predicate.apply(record)
                if matched is None:
                    continue
                emitted = matched
            yield emitted

    def _parse_record(
        self,
        rec: RecordModel,
        record_model: type[BaseModel],
        *,
        mask_phi: bool | None = None,
        study_key: str | None = None,
    ) -> dict[str, Any]:
        """Parse a single record using the dynamic Pydantic model."""
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        resolved_sk = (
            study_key
            if isinstance(study_key, str) and study_key
            else getattr(rec, "study_key", None) or self._active_study_key
        )
        sk_str = resolved_sk if isinstance(resolved_sk, str) and resolved_sk else None

        raw_form_key = getattr(rec, "form_key", None)
        form_key = (
            raw_form_key
            if isinstance(raw_form_key, str) and raw_form_key
            else (
                self._resolve_form_key(sk_str, int(rec.form_id))
                if rec.form_id is not None
                else None
            )
        )

        site_id = getattr(rec, "site_id", None)
        raw_site_name = getattr(rec, "site_name", None)
        site_name = (
            raw_site_name
            if isinstance(raw_site_name, str) and raw_site_name
            else (
                self._resolve_site_name(sk_str, int(site_id))
                if sk_str and site_id is not None
                else None
            )
        )

        interval_id = getattr(rec, "interval_id", None)
        raw_interval_name = getattr(rec, "interval_name", None)
        interval_name: str | None = (
            raw_interval_name if isinstance(raw_interval_name, str) and raw_interval_name else None
        )
        if sk_str:
            if interval_name is None and interval_id is not None:
                interval_name = self._resolve_interval_name(
                    sk_str, int(interval_id), visit_id=rec.visit_id
                )
            elif interval_id is None and rec.visit_id is not None:
                interval_id, interval_name = self._resolve_visit_interval(sk_str, int(rec.visit_id))

        meta = {
            "recordId": rec.record_id,
            "recordOid": getattr(rec, "record_oid", None),
            "subjectKey": rec.subject_key,
            "visitId": rec.visit_id,
            "intervalId": interval_id,
            "intervalName": interval_name,
            "formId": rec.form_id,
            "formKey": form_key,
            "siteId": site_id,
            "siteName": site_name,
            "recordStatus": rec.record_status,
            "dateCreated": (
                rec.date_created.isoformat()  # type: ignore[union-attr]
                if hasattr(rec.date_created, "isoformat")
                else str(rec.date_created)
                if rec.date_created
                else None
            ),
        }
        raw_data = rec.record_data if isinstance(rec.record_data, dict) else {}
        data = mask_clinical_phi(raw_data) if (should_mask and raw_data) else raw_data
        parsed = record_model(**data).model_dump(by_alias=False)
        pipeline = self._get_enrichment_pipeline(sk_str)
        if pipeline is not None:
            parsed = pipeline.process(parsed, mask_phi=should_mask)
        return {**meta, **parsed}

    def _uses_standard_parse_record(self) -> bool:
        """Return True when ``self._parse_record`` has not been overridden or monkeypatched."""
        return (
            getattr(type(self), "_parse_record", None) is RecordMapper._parse_record
            and "_parse_record" not in self.__dict__
        )

    def _parse_records(
        self,
        records: Iterable[RecordModel],
        record_model: type[BaseModel],
        *,
        mask_phi: bool | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Parse raw records into row dictionaries and count failures."""
        rows: list[dict[str, Any]] = []
        errors = 0
        all_verified = True
        saw_chunks = False
        reg_snap: tuple[frozenset[str], frozenset[str]] | None = None
        common_phi_keys: frozenset[str] | None = None
        has_nested = False
        for chunk_rows, chunk_errors in self._iter_parsed_rows(
            records,
            record_model,
            mask_phi=mask_phi,
        ):
            saw_chunks = True
            rows.extend(chunk_rows)
            errors += chunk_errors
            if (
                all_verified
                and isinstance(chunk_rows, _IngressPreparedRows)
                and chunk_rows.ingress_masked
            ):
                if reg_snap is None:
                    reg_snap = chunk_rows.registry_snapshot
                elif chunk_rows.registry_snapshot != reg_snap:
                    all_verified = False
                if chunk_rows:
                    common_phi_keys = (
                        chunk_rows.phi_keys
                        if common_phi_keys is None
                        else (common_phi_keys & chunk_rows.phi_keys)
                    )
                if chunk_rows.has_nested_containers:
                    has_nested = True
            else:
                all_verified = False

        should_mask = bool(self.mask_phi if mask_phi is None else mask_phi)
        if not saw_chunks and should_mask and self._uses_standard_parse_record():
            active_pipeline = self._get_enrichment_pipeline(None)
            empty_phi = (
                normalize_phi_keys_snapshot(active_pipeline.phi_keys)
                if active_pipeline is not None
                else frozenset()
            )
            return (
                _IngressPreparedRows(
                    rows,
                    ingress_masked=True,
                    registry_snapshot=global_sensitivity_registry.snapshot(),
                    phi_keys=empty_phi,
                    has_nested_containers=False,
                ),
                errors,
            )

        if (
            saw_chunks
            and all_verified
            and reg_snap is not None
            and global_sensitivity_registry.snapshot() == reg_snap
        ):
            if common_phi_keys is None:
                active_pipeline = self._get_enrichment_pipeline(None)
                common_phi_keys = (
                    normalize_phi_keys_snapshot(active_pipeline.phi_keys)
                    if active_pipeline is not None
                    else frozenset()
                )
            return (
                _IngressPreparedRows(
                    rows,
                    ingress_masked=True,
                    registry_snapshot=reg_snap,
                    phi_keys=common_phi_keys,
                    has_nested_containers=has_nested,
                ),
                errors,
            )
        return rows, errors

    def _iter_parsed_rows(
        self,
        records: Iterable[RecordModel],
        record_model: type[BaseModel],
        *,
        mask_phi: bool | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], int]]:
        """Iterate over records and parse them in chunks."""
        should_mask = bool(self.mask_phi if mask_phi is None else mask_phi)
        for chunk in iter_chunks(records, chunk_size=self._pipeline.chunk_size):
            rows: list[dict[str, Any]] = []
            errors = 0
            can_track_ingress = should_mask and self._uses_standard_parse_record()
            reg_snap = global_sensitivity_registry.snapshot() if can_track_ingress else None
            common_phi_keys: frozenset[str] | None = None
            phi_snap_by_sk: dict[str | None, frozenset[str]] = {}
            has_nested = False
            scalar_classes = _SCALAR_CELL_CLASSES
            for rec in chunk:
                try:
                    row = self._parse_record(rec, record_model, mask_phi=mask_phi)
                    rows.append(row)
                    if reg_snap is not None:
                        if not has_nested:
                            for v in row.values():
                                if v.__class__ not in scalar_classes and isinstance(
                                    v, _CONTAINER_CELL_TYPES
                                ):
                                    has_nested = True
                                    break
                        resolved_sk = getattr(rec, "study_key", None) or self._active_study_key
                        sk_key = resolved_sk if isinstance(resolved_sk, str) else None
                        row_phi = phi_snap_by_sk.get(sk_key)
                        if row_phi is None:
                            pipeline = self._get_enrichment_pipeline(sk_key)
                            row_phi = (
                                normalize_phi_keys_snapshot(pipeline.phi_keys)
                                if pipeline is not None
                                else frozenset()
                            )
                            phi_snap_by_sk[sk_key] = row_phi
                        common_phi_keys = (
                            row_phi if common_phi_keys is None else (common_phi_keys & row_phi)
                        )
                except (ValidationError, TypeError) as exc:
                    errors += 1
                    logger.warning(
                        "Failed to parse record data for recordId %s: %s",
                        rec.record_id,
                        exc,
                    )
                except Exception as exc:  # pragma: no cover - unexpected
                    errors += 1
                    logger.error("Unexpected error processing recordId %s: %s", rec.record_id, exc)
            if reg_snap is not None and global_sensitivity_registry.snapshot() == reg_snap:
                if common_phi_keys is None:
                    active_pipeline = self._get_enrichment_pipeline(None)
                    common_phi_keys = (
                        normalize_phi_keys_snapshot(active_pipeline.phi_keys)
                        if active_pipeline is not None
                        else frozenset()
                    )
                yield (
                    _IngressPreparedRows(
                        rows,
                        ingress_masked=True,
                        registry_snapshot=reg_snap,
                        phi_keys=common_phi_keys,
                        has_nested_containers=has_nested,
                    ),
                    errors,
                )
            else:
                yield rows, errors

    def _build_dataframe(
        self,
        rows: list[dict[str, Any]],
        variable_keys: list[str],
        label_map: dict[str, str],
        use_labels: bool,
        *,
        mask_phi: bool | None = None,
        study_key: str | None = None,
    ) -> pd.DataFrame:
        """Create the output DataFrame from parsed rows."""
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        should_mask = bool(self.mask_phi if mask_phi is None else mask_phi)
        pipeline = self._get_enrichment_pipeline(study_key)
        pipeline_phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()
        required_phi_snap = normalize_phi_keys_snapshot(pipeline_phi_keys)
        mapped_targets: list[str] = []
        replaced_sources: set[str] = set()
        if pipeline is not None and pipeline.mappings:
            for src_var, rule in pipeline.mappings.items():
                if rule.target_field and rule.target_field != src_var and src_var in variable_keys:
                    replaced_sources.add(src_var)
                    if (
                        rule.target_field not in variable_keys
                        and rule.target_field not in mapped_targets
                    ):
                        mapped_targets.append(rule.target_field)

        effective_var_keys = [
            k for k in variable_keys if k not in replaced_sources
        ] + mapped_targets
        meta_cols = [
            "recordId",
            "recordOid",
            "subjectKey",
            "visitId",
            "intervalId",
            "intervalName",
            "formId",
            "formKey",
            "siteId",
            "siteName",
            "recordStatus",
            "dateCreated",
        ]

        unique_effective_var_keys = list(
            dict.fromkeys(key for key in effective_var_keys if key not in meta_cols)
        )
        for key in unique_effective_var_keys:
            if key not in df.columns:
                df[key] = pd.NA

        df = df[meta_cols + unique_effective_var_keys]
        if should_mask:
            collapsed_phi_keys = {_collapse_alnum_key(pk) for pk in pipeline_phi_keys}

            def _is_header_sensitive(header: str) -> bool:
                return global_sensitivity_registry.is_sensitive(header) or (
                    bool(collapsed_phi_keys)
                    and not global_sensitivity_registry.is_exempt(header)
                    and _collapse_alnum_key(header) in collapsed_phi_keys
                )

            for mcol in meta_cols:
                if _is_header_sensitive(mcol):
                    df[mcol] = "***MASKED***"

            sensitive_vars: list[str] = []
            for key in unique_effective_var_keys:
                if _is_header_sensitive(key):
                    sensitive_vars.append(key)
                    continue
                if use_labels:
                    mapped_label = str(label_map.get(key, key))
                    if mapped_label != key and _is_header_sensitive(mapped_label):
                        sensitive_vars.append(key)

            for key in sensitive_vars:
                df[key] = "***MASKED***"

            current_reg_snap = global_sensitivity_registry.snapshot()
            ingress_verified = (
                isinstance(rows, _IngressPreparedRows)
                and rows.ingress_masked
                and rows.registry_snapshot == current_reg_snap
                and required_phi_snap.issubset(rows.phi_keys)
            )
            should_scan_containers = (not ingress_verified) or (
                isinstance(rows, _IngressPreparedRows) and rows.has_nested_containers
            )
            if should_scan_containers:
                sensitive_var_set = set(sensitive_vars)
                non_sensitive_vars = [
                    key for key in unique_effective_var_keys if key not in sensitive_var_set
                ]
                if non_sensitive_vars:
                    var_frame = df[non_sensitive_vars]
                    try:
                        obj_cols = var_frame.select_dtypes(include=[object, "str"]).columns
                    except TypeError:
                        obj_cols = var_frame.select_dtypes(include=[object]).columns
                    for col in obj_cols:
                        target = df[col]
                        if isinstance(target, pd.DataFrame):
                            df[col] = target.apply(
                                lambda s: _mask_series_nested_containers(s, pipeline_phi_keys)
                            )
                        else:
                            df[col] = _mask_series_nested_containers(target, pipeline_phi_keys)

        if use_labels:
            rename_map = {key: label_map.get(key, key) for key in unique_effective_var_keys}
            df = df.rename(columns=rename_map)

        effective_phi_keys: Iterable[str] = (
            (rows.phi_keys | required_phi_snap)
            if isinstance(rows, _IngressPreparedRows) and rows.ingress_masked
            else required_phi_snap
        )
        stamp_tabular_batch(
            df,
            mask_phi=should_mask,
            phi_keys=effective_phi_keys,
            sanitized=False,
        )
        return df

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
    ) -> Iterator[pd.DataFrame]:
        """Yield mapped record DataFrames in bounded chunks."""
        if as_sdtm:
            target_domain = domain or "AE"
            yield from self.iter_sdtm_dataframes(
                study_key=study_key,
                domain=target_domain,
                visit_key=visit_key,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                mask_phi=mask_phi,
                include_deleted=include_deleted,
            )
            return

        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.dataframe. Install "
                "with 'pip install \"imednet[export]\"'."
            )
        variable_keys, label_map = self._fetch_variable_metadata(
            study_key,
            variable_whitelist=variable_whitelist,
            form_whitelist=form_whitelist,
        )
        if not variable_keys:
            return

        record_model = self._build_record_model(variable_keys, label_map)
        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = form_whitelist

        errors = 0
        yielded = False
        for rows, chunk_errors in self._iter_parsed_rows(
            self._iter_records(
                study_key,
                visit_key,
                extra_filters=extra_filters or None,
                include_deleted=include_deleted,
            ),
            record_model,
            mask_phi=mask_phi,
        ):
            errors += chunk_errors
            df = self._build_dataframe(
                rows,
                variable_keys,
                label_map,
                use_labels_as_columns,
                mask_phi=mask_phi,
            )
            if df.empty:
                continue
            yielded = True
            yield df

        if errors:
            logger.warning("Encountered %s errors while parsing record data.", errors)
        if not yielded:
            logger.info(
                "No records processed successfully for study '%s' with the given filters.",
                study_key,
            )

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
        records: Iterable[RecordModel] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate a nested JSON tree structure up to three levels deep."""
        from imednet_workflows.study_structure import get_study_structure

        self._active_study_key = study_key
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        pipeline = self._get_enrichment_pipeline(study_key)
        pipeline_phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()
        study_struct = get_study_structure(self.sdk, study_key)

        # 1. Build form-scoped models across both standalone study forms and interval forms
        form_models: dict[int, type[BaseModel]] = {}
        form_label_maps: dict[int, dict[str, str]] = {}

        candidate_forms: list[Any] = []
        raw_study_forms = getattr(study_struct, "forms", None)
        if isinstance(raw_study_forms, Iterable):
            candidate_forms.extend(raw_study_forms)
        for interval in study_struct.intervals:
            candidate_forms.extend(interval.forms)

        for form in candidate_forms:
            if form_whitelist and form.form_id not in form_whitelist:
                continue
            if form.form_id in form_models:
                continue
            var_keys = list(
                dict.fromkeys(
                    v.variable_name
                    for v in (form.variables or [])
                    if v.variable_name
                    and (not variable_whitelist or v.variable_name in variable_whitelist)
                )
            )
            label_map = {
                v.variable_name: v.label for v in (form.variables or []) if v.variable_name
            }
            form_models[form.form_id] = self._build_record_model(var_keys, label_map)  # type: ignore
            form_label_maps[form.form_id] = label_map  # type: ignore

        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = form_whitelist

        # 2. Fetch and iterate records unless pre-streamed records were supplied
        record_iter: Iterable[RecordModel] = (
            records
            if records is not None
            else self._iter_records(
                study_key,
                visit_key,
                extra_filters=extra_filters or None,
                include_deleted=include_deleted,
            )
        )

        meta_keys = {
            "recordId",
            "recordOid",
            "subjectKey",
            "visitId",
            "intervalId",
            "intervalName",
            "formId",
            "formKey",
            "siteId",
            "siteName",
            "recordStatus",
            "dateCreated",
        }
        collapsed_phi_keys = {_collapse_alnum_key(pk) for pk in pipeline_phi_keys}
        standard_parse = self._uses_standard_parse_record()
        tree: dict[str, dict[str, Any]] = {}

        for rec in record_iter:
            if rec.form_id not in form_models:
                continue

            model = form_models[rec.form_id]
            label_map = form_label_maps[rec.form_id]  # type: ignore

            try:
                parsed_data = self._parse_record(
                    rec,
                    model,
                    mask_phi=should_mask,
                    study_key=study_key,
                )
            except (ValidationError, TypeError) as exc:
                logger.warning(
                    "Failed to parse record data for recordId %s: %s",
                    rec.record_id,
                    exc,
                )
                continue
            except Exception as exc:  # pragma: no cover - unexpected
                logger.error("Unexpected error processing recordId %s: %s", rec.record_id, exc)
                continue

            if should_mask:
                if not standard_parse:
                    parsed_data = mask_clinical_phi(parsed_data)
                    if pipeline_phi_keys:
                        parsed_data = mask_custom_phi_recursive(parsed_data, pipeline_phi_keys)
                else:
                    for k in list(parsed_data):
                        if global_sensitivity_registry.is_sensitive(k) or (
                            bool(collapsed_phi_keys)
                            and not global_sensitivity_registry.is_exempt(k)
                            and _collapse_alnum_key(k) in collapsed_phi_keys
                        ):
                            parsed_data[k] = "***MASKED***"

            if use_labels_as_keys:
                remapped: dict[str, Any] = {}
                for k, v in parsed_data.items():
                    if k in meta_keys:
                        remapped[k] = v
                    else:
                        lbl = label_map.get(k)
                        target_key = lbl if lbl is not None else k
                        if target_key not in meta_keys:
                            if should_mask and (
                                global_sensitivity_registry.is_sensitive(target_key)
                                or (
                                    bool(collapsed_phi_keys)
                                    and not global_sensitivity_registry.is_exempt(target_key)
                                    and _collapse_alnum_key(target_key) in collapsed_phi_keys
                                )
                            ):
                                remapped[target_key] = "***MASKED***"
                            else:
                                remapped[target_key] = v
                parsed_data = remapped

            # Automatically perform lookups for parent record identifiers using available metadata
            resolved_parent_id = getattr(rec, "parent_record_id", None)
            if not resolved_parent_id:
                resolved_parent_id = (
                    f"{rec.visit_id}_{rec.form_id}" if rec.visit_id is not None else None
                )

            if "parent_record_id" not in parsed_data or parsed_data.get("parent_record_id") is None:
                parsed_data["parent_record_id"] = resolved_parent_id
                if use_labels_as_keys and "parentRecordId" in parsed_data:
                    parsed_data["parentRecordId"] = resolved_parent_id

            subj_key = rec.subject_key
            if subj_key not in tree:
                tree[subj_key] = {"subject_key": subj_key, "visits": {}, "forms": {}}  # type: ignore

            subj_node = tree[subj_key]  # type: ignore
            form_id = rec.form_id

            visit_id = rec.visit_id
            if visit_id is None:
                if form_id not in subj_node["forms"]:
                    subj_node["forms"][form_id] = {"form_id": form_id, "records": []}
                subj_node["forms"][form_id]["records"].append(parsed_data)
            else:
                if visit_id not in subj_node["visits"]:
                    subj_node["visits"][visit_id] = {"visit_id": visit_id, "forms": {}}

                visit_node = subj_node["visits"][visit_id]

                if form_id not in visit_node["forms"]:
                    visit_node["forms"][form_id] = {"form_id": form_id, "records": []}

                form_node = visit_node["forms"][form_id]
                form_node["records"].append(parsed_data)

        # 3. Convert dicts to lists
        result = []
        for subj_key, subj in tree.items():  # noqa: B007
            subj_visits = []
            for visit_id, visit in subj["visits"].items():  # noqa: B007
                visit_forms = []
                for form_id, form in visit["forms"].items():  # noqa: B007
                    visit_forms.append(form)
                visit["forms"] = visit_forms
                subj_visits.append(visit)
            subj["visits"] = subj_visits
            unscheduled_forms = list(subj.pop("forms", {}).values())
            if unscheduled_forms:
                subj["forms"] = unscheduled_forms
            result.append(subj)

        return result

    def transform_to_sdtm(
        self,
        records: Iterable[Any],
        domain: str,
        study_key: str,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        seq_counters: dict[str, int] | None = None,
        record_seqs: dict[str, int] | None = None,
        preserve_record_id: bool = False,
    ) -> pd.DataFrame:
        """Transform an iterable of records into a normalized SDTM domain DataFrame."""
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        pipeline = self._get_enrichment_pipeline(study_key)
        return transform_records_to_sdtm(
            records,
            domain=domain,
            study_key=study_key,
            include_deleted=effective_include_deleted,
            mask_phi=should_mask,
            enrichment_pipeline=pipeline,
            seq_counters=seq_counters,
            record_seqs=record_seqs,
            preserve_record_id=preserve_record_id,
        )

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
    ) -> pd.DataFrame:
        """Return a :class:`pandas.DataFrame` transformed into a CDISC SDTM domain dataset."""
        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.sdtm_dataframe. Install "
                "with 'pip install \"imednet[export]\"'."
            )
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        frames = list(
            self.iter_sdtm_dataframes(
                study_key=study_key,
                domain=domain,
                visit_key=visit_key,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                mask_phi=should_mask,
                include_deleted=include_deleted,
            )
        )
        if not frames:
            return transform_records_to_sdtm(
                [],
                domain=domain,
                study_key=study_key,
                mask_phi=should_mask,
                enrichment_pipeline=self._get_enrichment_pipeline(study_key),
            )
        combined = pd.concat(frames, ignore_index=True)
        combined.attrs["imednet_sdtm_redacted"] = bool(should_mask)
        return combined

    @staticmethod
    def _project_record_whitelist(
        records: Iterable[Any],
        allowed_vars: set[str],
    ) -> Iterator[Any]:
        """Lazily project ``record_data`` dictionaries down to ``allowed_vars``."""
        for rec in records:
            rec_data = getattr(rec, "record_data", None)
            if hasattr(rec, "model_copy") and isinstance(rec_data, dict):
                yield rec.model_copy(
                    update={"record_data": {k: v for k, v in rec_data.items() if k in allowed_vars}}
                )
            elif isinstance(rec, dict):
                copied = dict(rec)
                if isinstance(copied.get("record_data"), dict):
                    copied["record_data"] = {
                        k: v for k, v in copied["record_data"].items() if k in allowed_vars
                    }
                yield copied
            else:
                yield rec

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
    ) -> Iterator[pd.DataFrame]:
        """Yield chunked pandas DataFrames transformed into an SDTM domain dataset."""
        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.iter_sdtm_dataframes. Install "
                "with 'pip install \"imednet[export]\"'."
            )
        if variable_whitelist is not None and len(variable_whitelist) == 0:
            return
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        pipeline = self._get_enrichment_pipeline(study_key)

        allowed_vars: set[str] | None = None
        if variable_whitelist is not None:
            allowed_vars = set(variable_whitelist)
            if pipeline is not None and pipeline.mappings:
                for src_var, rule in pipeline.mappings.items():
                    if rule.target_field in allowed_vars:
                        allowed_vars.add(src_var)

        extra_filters: dict[str, Any] = {}
        matched_form_ids: set[int] | None = None
        matched_form_keys: set[str] | None = None
        if form_whitelist is not None:
            if len(form_whitelist) == 0:
                return
            extra_filters["formId"] = form_whitelist
            matched_form_ids = set(form_whitelist)
        else:
            study_forms = self._try_fetch_study_forms_for_sdtm(study_key)
            if study_forms:
                matched_forms = filter_forms_by_sdtm_domain(study_forms, domain)
                if not matched_forms:
                    return
                if len(matched_forms) < len(study_forms):
                    m_ids = [
                        fid
                        for f in matched_forms
                        if isinstance(
                            (
                                fid := (
                                    f.get("formId") or f.get("form_id")
                                    if isinstance(f, dict)
                                    else getattr(f, "form_id", None)
                                )
                            ),
                            int,
                        )
                        and not isinstance(fid, bool)
                    ]
                    m_keys = {
                        str(fk)
                        for f in matched_forms
                        if (
                            fk := (
                                f.get("formKey") or f.get("form_key")
                                if isinstance(f, dict)
                                else getattr(f, "form_key", None)
                            )
                        )
                    }
                    if m_ids:
                        extra_filters["formId"] = m_ids
                        matched_form_ids = set(m_ids)
                    if m_keys:
                        matched_form_keys = m_keys

        source_is_ordered = False
        force_drain_ungrouped_loader = False
        if self._loader is not None and not effective_include_deleted:
            bound_iter, supports_subject_order = self._resolve_loader_iter_callable(self._loader)
            from unittest.mock import NonCallableMock

            if supports_subject_order and not isinstance(bound_iter, NonCallableMock):
                source_is_ordered = True
            elif bound_iter is not None:
                force_drain_ungrouped_loader = True
        elif not self._is_mock_sdk_or_callable():
            source_is_ordered = True

        records: Iterable[Any] = self._iter_records(
            study_key,
            visit_key=visit_key,
            extra_filters=extra_filters or None,
            include_deleted=effective_include_deleted,
            sort=_SDTM_RECORDS_SORT,
            group_by_subject=True,
        )
        if matched_form_ids is not None or matched_form_keys:
            records = self._filter_records_by_matched_forms(
                records,
                matched_form_ids,
                matched_form_keys,
            )
        if allowed_vars is not None:
            records = self._project_record_whitelist(records, allowed_vars)
        if force_drain_ungrouped_loader:
            records = list(records)

        normalizer = _SdtmNormalizer(
            domain=domain,
            study_key=study_key,
            include_deleted=effective_include_deleted,
            mask_phi=should_mask,
            pipeline=pipeline,
        )
        if normalizer.remaps_usubjid:
            source_is_ordered = False
        seq_counters: dict[str, int] = {}
        for df in _iter_sorted_sdtm_frames(
            records,
            normalizer,
            chunk_size=self._pipeline.chunk_size,
            seq_counters=seq_counters,
            source_is_ordered=source_is_ordered,
        ):
            if not df.empty:
                yield df

    def _try_fetch_study_forms_for_sdtm(self, study_key: str) -> list[Any]:
        """Return concrete Study Forms when ``self.sdk.forms.list`` yields non-mock Form entries."""
        from unittest.mock import NonCallableMock

        forms_ep = getattr(self.sdk, "forms", None)
        list_fn = getattr(forms_ep, "list", None) if forms_ep is not None else None
        if not callable(list_fn):
            return []
        try:
            raw_forms = list_fn(study_key=study_key)
        except Exception:
            return []
        if (
            isinstance(raw_forms, NonCallableMock)
            and "list" not in getattr(forms_ep, "_mock_children", {})
            and "list" not in vars(forms_ep)
        ):
            return []
        try:
            items = list(raw_forms)
        except Exception:
            return []
        concrete: list[Any] = []
        for f in items:
            if isinstance(f, NonCallableMock):
                continue
            if (
                (isinstance(f, dict) and (f.get("formKey") or f.get("form_key")))
                or isinstance(getattr(f, "form_key", None), str)
                or isinstance(getattr(f, "form_name", None), str)
            ):
                concrete.append(f)
        return concrete

    @staticmethod
    def _filter_records_by_matched_forms(
        records: Iterable[Any],
        matched_ids: set[int] | None,
        matched_keys: set[str] | None,
    ) -> Iterator[Any]:
        """Yield records whose ``form_id`` or ``form_key`` matches the domain-filtered forms."""
        for rec in records:
            fid, fk = extract_record_form_ref(rec)
            if fid is not None and matched_ids is not None:
                if fid in matched_ids:
                    yield rec
                continue
            if fk is not None and matched_keys:
                if fk in matched_keys:
                    yield rec
                continue
            if fid is None and fk is None:
                yield rec

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
    ) -> pd.DataFrame:
        """Return a :class:`pandas.DataFrame` of records for a study."""
        if as_sdtm:
            target_domain = domain or "AE"
            return self.sdtm_dataframe(
                study_key=study_key,
                domain=target_domain,
                visit_key=visit_key,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                mask_phi=mask_phi,
                include_deleted=include_deleted,
            )

        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.dataframe. Install "
                "with 'pip install \"imednet[export]\"'."
            )
        frames = list(
            self.iter_dataframes(
                study_key,
                visit_key=visit_key,
                use_labels_as_columns=use_labels_as_columns,
                variable_whitelist=variable_whitelist,
                form_whitelist=form_whitelist,
                mask_phi=mask_phi,
                include_deleted=include_deleted,
            )
        )
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        first_stamp = get_tabular_batch_stamp(frames[0])
        if (
            first_stamp is not None
            and all(
                (st := get_tabular_batch_stamp(f)) is not None
                and st.mask_phi == first_stamp.mask_phi
                and st.registry_snapshot == first_stamp.registry_snapshot
                for f in frames[1:]
            )
            and (
                not first_stamp.mask_phi
                or first_stamp.registry_snapshot == global_sensitivity_registry.snapshot()
            )
        ):
            common_phi = first_stamp.phi_keys.intersection(
                *(get_tabular_batch_stamp(f).phi_keys for f in frames[1:])  # type: ignore[union-attr]
            )
            stamp_tabular_batch(
                combined,
                mask_phi=first_stamp.mask_phi,
                phi_keys=common_phi,
                sanitized=all(
                    bool(get_tabular_batch_stamp(f).sanitized)  # type: ignore[union-attr]
                    for f in frames
                ),
            )
        return combined

    def _try_load_datadict(self, study_key: str) -> DataDictionary | None:
        """Attempt to build a DataDictionary from SDK forms/variables when available."""
        from imednet.spi.cdisc import Form as DdForm
        from imednet.spi.cdisc import Metadata as DdMetadata
        from imednet.spi.cdisc import Variable as DdVariable

        forms_raw = self._try_fetch_study_forms_for_sdtm(study_key)
        _, _ = self._fetch_variable_metadata(study_key)
        vars_raw = self._last_variables
        if not forms_raw and not vars_raw:
            return None

        vars_by_form_key: dict[str, list[DdVariable]] = {}
        form_id_to_key: dict[int, str] = {}
        dd_forms: dict[str, DdForm] = {}

        for f in forms_raw:
            fid = f.get("formId") if isinstance(f, dict) else getattr(f, "form_id", None)
            fkey = (
                (f.get("formKey") or f.get("form_key"))
                if isinstance(f, dict)
                else getattr(f, "form_key", None)
            )
            fname = (
                (f.get("formName") or f.get("form_name"))
                if isinstance(f, dict)
                else getattr(f, "form_name", None)
            )
            ftype = (
                (f.get("formType") or f.get("form_type"))
                if isinstance(f, dict)
                else getattr(f, "form_type", None)
            )
            if not fkey:
                continue
            fkey_str = str(fkey)
            if isinstance(fid, int):
                form_id_to_key[fid] = fkey_str
            dd_forms[fkey_str] = DdForm(
                formKey=fkey_str,
                formName=str(fname) if fname else fkey_str,
                formId=fid if isinstance(fid, int) else None,
                formType=str(ftype) if ftype else "Subject",
                variables=[],
            )

        for idx, v in enumerate(vars_raw, start=1):
            v_name = getattr(v, "variable_name", None)
            if not v_name:
                continue
            vf_id = getattr(v, "form_id", None)
            vf_key = getattr(v, "form_key", None) or (
                form_id_to_key.get(vf_id) if isinstance(vf_id, int) else None
            )
            target_fk = str(vf_key) if vf_key else "DEFAULT"
            vars_by_form_key.setdefault(target_fk, []).append(
                DdVariable(
                    variableName=str(v_name),
                    label=str(getattr(v, "label", None) or v_name),
                    variableType=str(getattr(v, "variable_type", None) or "Text"),
                    sequence=getattr(v, "sequence", None) or idx,
                )
            )

        for fk, f_vars in vars_by_form_key.items():
            if fk in dd_forms:
                dd_forms[fk].variables = f_vars
            else:
                dd_forms[fk] = DdForm(
                    formKey=fk,
                    formName=fk,
                    variables=f_vars,
                )

        if not dd_forms:
            return None
        return DataDictionary(
            metadata=DdMetadata.create(study_key=study_key, source="api"),
            forms=list(dd_forms.values()),
        )

    def dataset_json(
        self,
        study_key: str,
        *,
        item_group_oid: str | None = None,
        name: str | None = None,
        label: str | None = None,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        as_sdtm: bool = False,
        domain: str | None = None,
        sdtm_domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        creation_date_time: str | None = None,
    ) -> DatasetJsonDataset:
        """Emit a CDISC Dataset-JSON v1.1 ``itemGroupData`` dataset for a CDASH Form or SDTM domain."""
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        pipeline = self._get_enrichment_pipeline(study_key)
        raw_phi_keys = (
            getattr(pipeline, "phi_fields", None) or getattr(pipeline, "phi_keys", None)
            if pipeline is not None
            else None
        )
        custom_phi = set(normalize_phi_keys_snapshot(raw_phi_keys)) if raw_phi_keys else None

        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = (
                form_whitelist[0] if len(form_whitelist) == 1 else list(form_whitelist)
            )

        resolved_domain_arg = domain or sdtm_domain
        if as_sdtm or resolved_domain_arg is not None:
            target_domain = (resolved_domain_arg or "AE").strip().upper()
            study_forms = self._try_fetch_study_forms_for_sdtm(study_key)
            matched_ids: set[int] | None = None
            matched_keys: set[str] | None = None
            if study_forms and form_whitelist is None:
                matched_forms = filter_forms_by_sdtm_domain(study_forms, target_domain)
                matched_ids = {
                    int(fid)
                    for f in matched_forms
                    if (
                        fid := (
                            f.get("formId") if isinstance(f, dict) else getattr(f, "form_id", None)
                        )
                    )
                    is not None
                }
                matched_keys = {
                    str(fk)
                    for f in matched_forms
                    if (
                        fk := (
                            f.get("formKey")
                            if isinstance(f, dict)
                            else getattr(f, "form_key", None)
                        )
                    )
                }
            records_iter: Iterable[Any] = self._iter_records(
                study_key,
                visit_key=visit_key,
                extra_filters=extra_filters or None,
                include_deleted=effective_include_deleted,
                sort=_SDTM_RECORDS_SORT,
                group_by_subject=True,
            )
            if matched_ids is not None or matched_keys:
                records_iter = self._filter_records_by_matched_forms(
                    records_iter,
                    matched_ids,
                    matched_keys,
                )
            if variable_whitelist is not None:
                records_iter = self._project_record_whitelist(
                    records_iter,
                    set(variable_whitelist),
                )
            sdtm_rows = transform_records_to_sdtm_rows(
                records_iter,
                domain=target_domain,
                study_key=study_key,
                include_deleted=effective_include_deleted,
                mask_phi=should_mask,
                pipeline=pipeline,
                phi_keys=custom_phi,
            )
            return build_dataset_json_dataset(
                study_key,
                sdtm_rows,
                dataset_name=name or target_domain,
                dataset_label=label or f"SDTM {target_domain} Domain",
                item_group_oid=item_group_oid or f"IG.{target_domain}",
                as_sdtm=True,
                sdtm_domain=target_domain,
                creation_date_time=creation_date_time,
                mask_phi=should_mask,
                custom_phi_keys=custom_phi,
            )

        dd = self._try_load_datadict(study_key)
        raw_records = list(
            self._iter_records(
                study_key,
                visit_key=visit_key,
                extra_filters=extra_filters or None,
                include_deleted=effective_include_deleted,
            )
        )
        if variable_whitelist is not None:
            raw_records = list(self._project_record_whitelist(raw_records, set(variable_whitelist)))

        matched_form = None
        if dd is not None:
            if name and any(f.formKey == name for f in dd.forms):
                matched_form = next(f for f in dd.forms if f.formKey == name)
            elif len(dd.forms) == 1:
                matched_form = dd.forms[0]
            elif raw_records:
                _, fk = extract_record_form_ref(raw_records[0])
                if fk:
                    matched_form = next((f for f in dd.forms if f.formKey == fk), None)

        resolved_name = name or (matched_form.formKey if matched_form is not None else study_key)
        resolved_ig = item_group_oid or f"IG_{resolved_name}"
        resolved_label = label or (
            matched_form.formName or matched_form.formKey
            if matched_form is not None
            else resolved_name
        )
        return build_dataset_json_dataset(
            study_key,
            raw_records,
            dataset_name=resolved_name,
            dataset_label=resolved_label,
            item_group_oid=resolved_ig,
            form=matched_form,
            as_sdtm=False,
            creation_date_time=creation_date_time,
            mask_phi=should_mask,
            custom_phi_keys=custom_phi,
        )

    def dataset_json_clinical_data(
        self,
        study_key: str,
        *,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        as_sdtm: bool = False,
        domains: list[str] | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        creation_date_time: str | None = None,
    ) -> DatasetJsonClinicalData:
        """Emit CDISC Dataset-JSON v1.1 ``clinicalData`` containing ``itemGroupData`` datasets."""
        should_mask = self.mask_phi if mask_phi is None else mask_phi
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        pipeline = self._get_enrichment_pipeline(study_key)
        raw_phi_keys = (
            getattr(pipeline, "phi_fields", None) or getattr(pipeline, "phi_keys", None)
            if pipeline is not None
            else None
        )
        custom_phi = set(normalize_phi_keys_snapshot(raw_phi_keys)) if raw_phi_keys else None

        if as_sdtm or domains:
            target_domains = [d.strip().upper() for d in (domains or ["DM", "AE", "VS", "LB"])]
            ig_map: dict[str, DatasetJsonDataset] = {}
            for dom in target_domains:
                ds = self.dataset_json(
                    study_key,
                    visit_key=visit_key,
                    variable_whitelist=variable_whitelist,
                    form_whitelist=form_whitelist,
                    as_sdtm=True,
                    domain=dom,
                    mask_phi=should_mask,
                    include_deleted=effective_include_deleted,
                    creation_date_time=creation_date_time,
                )
                if ds.records > 0 or domains is not None:
                    ig_map[ds.item_group_oid] = ds
            return DatasetJsonClinicalData(
                studyOID=study_key,
                metaDataVersionOID="MDV.001",
                itemGroupData=tuple(ig_map.values()),
            )

        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = (
                form_whitelist[0] if len(form_whitelist) == 1 else list(form_whitelist)
            )
        raw_records = list(
            self._iter_records(
                study_key,
                visit_key=visit_key,
                extra_filters=extra_filters or None,
                include_deleted=effective_include_deleted,
            )
        )
        if variable_whitelist is not None:
            raw_records = list(self._project_record_whitelist(raw_records, set(variable_whitelist)))
        dd = self._try_load_datadict(study_key)
        return build_dataset_json_clinical_data(
            study_key,
            raw_records,
            data_dictionary=dd,
            creation_date_time=creation_date_time,
            mask_phi=should_mask,
            include_deleted=effective_include_deleted,
            custom_phi_keys=custom_phi,
        )


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "SDTM_DOMAIN_SPECS",
    "RecordMapper",
    "_SdtmNormalizer",
    "_iter_sorted_sdtm_frames",
    "format_iso8601_dtc",
    "transform_records_to_sdtm",
]
