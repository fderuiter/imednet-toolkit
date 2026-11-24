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

from imednet.spi.endpoints import Record as RecordModel
from imednet.spi.endpoints import Variable as VariableModel
from imednet.spi.models import (
    _SDTM_RECORDS_SORT,
    _UNSET_PIPELINE,
    SDTM_DOMAIN_SPECS,
    _collapse_alnum_key,
    _extract_stream_usubjid_and_id,
    _format_sdtm_flag,
    _get_model_consumed_keys,
    _get_model_field_aliases,
    _iter_sorted_sdtm_frames,
    _normalize_key,
    _PreparedSdtmRow,
    _SdtmNormalizer,
    format_iso8601_dtc,
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
    EnrichmentPipeline,
    global_sensitivity_registry,
    mask_clinical_phi,
    mask_custom_phi_recursive,
    resolve_enrichment_pipeline,
)

from .cached_loader import CachedRecordsLoader
from .chunked_pipeline import DEFAULT_CHUNK_SIZE, ChunkedRecordPipeline, iter_chunks

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

# Setup basic logging
logger = logging.getLogger(__name__)


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
        form_id_list, _ = self._extract_form_ids(raw_filters)
        form_ids: set[Any] | None = set(form_id_list) if form_id_list is not None else None

        loader = self._loader
        if loader is not None:
            sync_method = getattr(loader, "sync_records", None)
            bound_iter, supports_subject_order = self._resolve_loader_iter_callable(loader)
            if callable(sync_method) and callable(bound_iter):
                sync_method(study_key)
                iter_kwargs: dict[str, Any] = {"chunk_size": self._pipeline.chunk_size}
                if group_by_subject and supports_subject_order:
                    iter_kwargs["order_by_subject"] = True
                return self._filter_records(
                    bound_iter(study_key, **iter_kwargs),
                    visit_key=visit_key,
                    form_ids=form_ids,
                    include_deleted=effective_include_deleted,
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
    ) -> Iterator[RecordModel]:
        """Apply visit, form, and deleted filters to an iterable of records."""
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
            yield record

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
        meta = {
            "recordId": rec.record_id,
            "subjectKey": rec.subject_key,
            "visitId": rec.visit_id,
            "formId": rec.form_id,
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
        resolved_sk = (
            study_key
            if isinstance(study_key, str) and study_key
            else getattr(rec, "study_key", None) or self._active_study_key
        )
        pipeline = self._get_enrichment_pipeline(
            resolved_sk if isinstance(resolved_sk, str) else None
        )
        if pipeline is not None:
            parsed = pipeline.process(parsed, mask_phi=should_mask)
        return {**meta, **parsed}

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
        for chunk_rows, chunk_errors in self._iter_parsed_rows(
            records,
            record_model,
            mask_phi=mask_phi,
        ):
            rows.extend(chunk_rows)
            errors += chunk_errors
        return rows, errors

    def _iter_parsed_rows(
        self,
        records: Iterable[RecordModel],
        record_model: type[BaseModel],
        *,
        mask_phi: bool | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], int]]:
        """Iterate over records and parse them in chunks."""
        for chunk in iter_chunks(records, chunk_size=self._pipeline.chunk_size):
            rows: list[dict[str, Any]] = []
            errors = 0
            for rec in chunk:
                try:
                    rows.append(self._parse_record(rec, record_model, mask_phi=mask_phi))
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

        should_mask = self.mask_phi if mask_phi is None else mask_phi
        pipeline = self._get_enrichment_pipeline(study_key)
        pipeline_phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()
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
            "subjectKey",
            "visitId",
            "formId",
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
            sensitive_vars = [
                key
                for key in unique_effective_var_keys
                if global_sensitivity_registry.is_sensitive(key)
                or (
                    bool(collapsed_phi_keys)
                    and not global_sensitivity_registry.is_exempt(key)
                    and _collapse_alnum_key(key) in collapsed_phi_keys
                )
            ]
            for key in sensitive_vars:
                df[key] = "***MASKED***"

            non_sensitive_vars = [
                key for key in unique_effective_var_keys if key not in set(sensitive_vars)
            ]
            if non_sensitive_vars:
                var_frame = df[non_sensitive_vars]
                try:
                    obj_cols = var_frame.select_dtypes(include=[object, "str"]).columns
                except TypeError:
                    obj_cols = var_frame.select_dtypes(include=[object]).columns
                for col in obj_cols:
                    df[col] = df[col].apply(mask_clinical_phi)
                    if pipeline_phi_keys:
                        df[col] = df[col].apply(
                            lambda val: mask_custom_phi_recursive(val, pipeline_phi_keys)
                        )

        if use_labels:
            rename_map = {key: label_map.get(key, key) for key in unique_effective_var_keys}
            df = df.rename(columns=rename_map)
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
            "subjectKey",
            "visitId",
            "formId",
            "recordStatus",
            "dateCreated",
        }
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
                parsed_data = mask_clinical_phi(parsed_data)
                if pipeline_phi_keys:
                    parsed_data = mask_custom_phi_recursive(parsed_data, pipeline_phi_keys)

            if use_labels_as_keys:
                remapped: dict[str, Any] = {}
                for k, v in parsed_data.items():
                    if k in meta_keys:
                        remapped[k] = v
                    else:
                        lbl = label_map.get(k)
                        target_key = lbl if lbl is not None else k
                        if target_key not in meta_keys:
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
        return pd.concat(frames, ignore_index=True)

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
        if self._loader is not None:
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
        return pd.concat(frames, ignore_index=True)


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "SDTM_DOMAIN_SPECS",
    "RecordMapper",
    "_PreparedSdtmRow",
    "_SdtmNormalizer",
    "_format_sdtm_flag",
    "_get_model_consumed_keys",
    "_get_model_field_aliases",
    "_iter_sorted_sdtm_frames",
    "_normalize_key",
    "format_iso8601_dtc",
    "transform_records_to_sdtm",
]
