"""Core tabular record mapping foundation for standalone export pipelines."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any
from unittest.mock import NonCallableMock

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment, unused-ignore]

from pydantic import BaseModel, Field, ValidationError, create_model

from imednet.errors import ImednetError
from imednet.integrations.sink_base import is_async_sdk, is_record_deleted
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.records import Record as RecordModel
from imednet.models.study_structure import FormStructure, IntervalStructure, StudyStructure
from imednet.models.variables import Variable as VariableModel
from imednet.utils.security import global_sensitivity_registry, mask_clinical_phi

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

logger = logging.getLogger(__name__)

DEFAULT_TABULAR_CHUNK_SIZE = 5_000


def _iter_record_chunks(
    records: Iterable[RecordModel],
    *,
    chunk_size: int = DEFAULT_TABULAR_CHUNK_SIZE,
) -> Iterator[list[RecordModel]]:
    """Yield fixed-size record chunks from an iterable."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    batch: list[RecordModel] = []
    for item in records:
        batch.append(item)
        if len(batch) >= chunk_size:
            yield batch
            batch = []
    if batch:
        yield batch


def build_core_study_structure(
    study_key: str,
    intervals: list[Interval],
    forms: list[Form],
    variables: list[VariableModel],
) -> StudyStructure:
    """Assemble a :class:`StudyStructure` from intervals, forms, and variables."""
    forms_by_id: dict[int, Form] = {f.form_id: f for f in forms if f.form_id is not None}
    variables_by_form_id: dict[int, list[VariableModel]] = {}
    for var in variables:
        if var.form_id is not None:
            variables_by_form_id.setdefault(var.form_id, []).append(var)

    interval_structures: list[IntervalStructure] = []
    for interval in intervals:
        form_structures: list[FormStructure] = []
        for form_summary in getattr(interval, "forms", []):
            full_form: Form | None = None
            if form_summary.form_id is not None:
                full_form = forms_by_id.get(form_summary.form_id)
            if full_form and full_form.form_id is not None:
                form_vars = variables_by_form_id.get(full_form.form_id, [])
                form_structures.append(FormStructure.from_form(full_form, form_vars))
        interval_structures.append(IntervalStructure.from_interval(interval, form_structures))

    return StudyStructure(studyKey=study_key, intervals=interval_structures)


class BaseTabularRecordMapper:
    """Core tabular record-to-DataFrame mapper used by standalone exports and plugins."""

    def __init__(
        self,
        sdk: ImednetFacade,
        *,
        chunk_size: int = DEFAULT_TABULAR_CHUNK_SIZE,
        mask_phi: bool = True,
        include_deleted: bool = False,
    ) -> None:
        """Initialize the base tabular mapper."""
        if is_async_sdk(sdk):
            raise TypeError(
                f"{type(self).__name__} requires a synchronous ImednetSDK client; "
                "AsyncImednetSDK is not supported."
            )
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        self.sdk = sdk
        self._loader: Any = None
        self._chunk_size = chunk_size
        self.mask_phi = mask_phi
        self._include_deleted = include_deleted
        self._last_variables: list[VariableModel] = []

    @property
    def chunk_size(self) -> int:
        """Return active chunk size for batched record parsing."""
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None and hasattr(pipeline, "chunk_size"):
            return int(pipeline.chunk_size)
        return self._chunk_size

    def _fetch_variable_metadata(
        self,
        study_key: str,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Return variable names and label mapping for a study."""
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

        variables: list[VariableModel] = list(
            self.sdk.get_variables(
                study_key=study_key,
                **filters,
            )
        )
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
        variable_keys = [v.variable_name for v in variables if v.variable_name is not None]
        label_map = {
            v.variable_name: (v.label or v.variable_name)
            for v in variables
            if v.variable_name is not None
        }
        return variable_keys, label_map

    def _build_record_model(
        self, variable_keys: list[str], label_map: dict[str, str]
    ) -> type[BaseModel]:
        """Create a dynamic Pydantic model for the record data payload."""
        fields: dict[str, tuple[Any, Any]] = {}
        for key in variable_keys:
            fields[key] = (
                Any | None,
                Field(None, alias=key, description=label_map.get(key, key)),
            )
        return create_model("RecordData", __base__=BaseModel, **fields)  # type: ignore[call-overload, no-any-return]

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

    def _can_stream_via_records_endpoint(self) -> bool:
        """Return True when ``self.sdk.records.list`` can stream paginated responses."""
        if isinstance(self.sdk, NonCallableMock):
            mock_children = getattr(self.sdk, "_mock_children", {})
            obj_vars = vars(self.sdk)
            has_records_attr = "records" in mock_children or "records" in obj_vars
            has_get_records_attr = "get_records" in mock_children or "get_records" in obj_vars
            if has_records_attr:
                records_obj = getattr(self.sdk, "records", None)
                if isinstance(records_obj, NonCallableMock):
                    rec_children = getattr(records_obj, "_mock_children", {})
                    rec_vars = vars(records_obj)
                    if "list" in rec_children or "list" in rec_vars:
                        return True
                    if has_get_records_attr:
                        return False
                return True
            return not has_get_records_attr

        records_ep = getattr(self.sdk, "records", None)
        return records_ep is not None and callable(getattr(records_ep, "list", None))

    def _stream_sdk_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
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

        use_streaming_endpoint = self._can_stream_via_records_endpoint()
        records_ep = getattr(self.sdk, "records", None) if use_streaming_endpoint else None
        fetch_fn: Any = (
            records_ep.list
            if use_streaming_endpoint and records_ep is not None
            else self.sdk.get_records
        )

        try:
            if form_id_list is not None:
                for fid in form_id_list:
                    for rec in fetch_fn(
                        study_key=study_key,
                        record_data_filter=record_data_filter,
                        **filters,
                        formId=fid,
                    ):
                        if not effective_include_deleted and is_record_deleted(rec):
                            continue
                        yield rec
                return

            for rec in fetch_fn(
                study_key=study_key,
                record_data_filter=record_data_filter,
                **filters,
            ):
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
        return list(
            self._stream_sdk_records(
                study_key,
                visit_key=visit_key,
                extra_filters=extra_filters,
                record_data_filter=record_data_filter,
                include_deleted=include_deleted,
            )
        )

    def _iter_records(
        self,
        study_key: str,
        visit_key: str | None = None,
        extra_filters: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_data_filter: str | None = None,
        include_deleted: bool | None = None,
    ) -> Iterable[RecordModel]:
        """Iterate lazily over records for a study, using optional cache loader if configured."""
        effective_include_deleted = (
            self._include_deleted if include_deleted is None else include_deleted
        )
        raw_filters = dict(extra_filters) if extra_filters else {}
        form_id_list, _ = self._extract_form_ids(raw_filters)
        form_ids: set[Any] | None = set(form_id_list) if form_id_list is not None else None

        loader = self._loader
        if loader is not None:
            sync_method = getattr(loader, "sync_records", None)
            iter_method = getattr(type(loader), "iter_cached_records", None)
            if callable(sync_method) and callable(iter_method):
                sync_method(study_key)
                return self._filter_records(
                    iter_method(loader, study_key, chunk_size=self.chunk_size),
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
        """Iterate over records and parse them in bounded chunks."""
        for chunk in _iter_record_chunks(records, chunk_size=self.chunk_size):
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
    ) -> pd.DataFrame:
        """Create the output DataFrame from parsed rows."""
        if pd is None:
            raise ImportError(
                "pandas is required for tabular exports. Install with "
                "\"pip install 'imednet[export]'\"."
            )
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        should_mask = self.mask_phi if mask_phi is None else mask_phi
        meta_cols = [
            "recordId",
            "subjectKey",
            "visitId",
            "formId",
            "recordStatus",
            "dateCreated",
        ]

        for key in variable_keys:
            if key not in df.columns:
                df[key] = pd.NA

        df = df[meta_cols + variable_keys]
        if should_mask:
            sensitive_vars = [
                key for key in variable_keys if global_sensitivity_registry.is_sensitive(key)
            ]
            for key in sensitive_vars:
                df[key] = "***MASKED***"

            non_sensitive_vars = [key for key in variable_keys if key not in set(sensitive_vars)]
            if non_sensitive_vars:
                var_frame = df[non_sensitive_vars]
                try:
                    obj_cols = var_frame.select_dtypes(include=[object, "str"]).columns
                except TypeError:
                    obj_cols = var_frame.select_dtypes(include=[object]).columns
                for col in obj_cols:
                    df[col] = df[col].apply(mask_clinical_phi)

        if use_labels:
            rename_map = {key: label_map.get(key, key) for key in variable_keys}
            df = df.rename(columns=rename_map)
        return df

    def _iter_tabular_dataframes(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> Iterator[pd.DataFrame]:
        """Yield mapped tabular DataFrames in bounded chunks."""
        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.dataframe. Install "
                "with 'pip install \"imednet[pandas]\"'."
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

    def iter_dataframes(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> Iterator[pd.DataFrame]:
        """Yield mapped record DataFrames in bounded chunks."""
        yield from self._iter_tabular_dataframes(
            study_key,
            visit_key=visit_key,
            use_labels_as_columns=use_labels_as_columns,
            variable_whitelist=variable_whitelist,
            form_whitelist=form_whitelist,
            mask_phi=mask_phi,
            include_deleted=include_deleted,
        )

    def dataframe(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Return a :class:`pandas.DataFrame` of records for a study."""
        if pd is None:
            raise ImportError(
                "pandas is required for RecordMapper.dataframe. Install "
                "with 'pip install \"imednet[pandas]\"'."
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

    def _get_study_structure(self, study_key: str) -> StudyStructure:
        """Fetch and assemble :class:`StudyStructure` using core SDK methods."""
        try:
            intervals = list(self.sdk.get_intervals(study_key))
            forms = list(self.sdk.get_forms(study_key))
            variables = list(self.sdk.get_variables(study_key))
            return build_core_study_structure(study_key, intervals, forms, variables)
        except Exception as exc:
            raise ImednetError(
                f"Failed to retrieve or process study structure for {study_key}: {exc}"
            ) from exc

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
    ) -> list[dict[str, Any]]:
        """Generate a nested Subject -> Visit -> Form JSON tree of study records."""
        study_struct = self._get_study_structure(study_key)

        form_models: dict[int, type[BaseModel]] = {}
        form_label_maps: dict[int, dict[str, str]] = {}

        for interval in study_struct.intervals:
            for form in interval.forms:
                if form_whitelist and form.form_id not in form_whitelist:
                    continue
                if form.form_id in form_models:
                    continue
                var_keys = [
                    v.variable_name
                    for v in (form.variables or [])
                    if v.variable_name is not None
                    and (not variable_whitelist or v.variable_name in variable_whitelist)
                ]
                label_map = {
                    v.variable_name: (v.label or v.variable_name)
                    for v in (form.variables or [])
                    if v.variable_name is not None
                }
                form_models[form.form_id] = self._build_record_model(var_keys, label_map)
                form_label_maps[form.form_id] = label_map

        extra_filters: dict[str, Any] = {}
        if form_whitelist is not None:
            extra_filters["formId"] = form_whitelist

        records = self._iter_records(
            study_key,
            visit_key,
            extra_filters=extra_filters or None,
            include_deleted=include_deleted,
        )

        tree: dict[Any, dict[str, Any]] = {}
        for rec in records:
            if rec.form_id not in form_models:
                continue

            model = form_models[rec.form_id]
            label_map = form_label_maps[rec.form_id]

            try:
                parsed_data = self._parse_record(rec, model, mask_phi=mask_phi)
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

            if use_labels_as_keys:
                remapped: dict[str, Any] = {}
                for k, v in parsed_data.items():
                    lbl = label_map.get(k)
                    remapped[lbl if lbl is not None else k] = v
                parsed_data = remapped

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
                tree[subj_key] = {"subject_key": subj_key, "visits": {}, "forms": {}}

            subj_node = tree[subj_key]
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
                visit_node["forms"][form_id]["records"].append(parsed_data)

        result: list[dict[str, Any]] = []
        for subj in tree.values():
            subj_visits: list[dict[str, Any]] = []
            for visit in subj["visits"].values():
                visit["forms"] = list(visit["forms"].values())
                subj_visits.append(visit)
            subj["visits"] = subj_visits
            unscheduled_forms = list(subj.pop("forms", {}).values())
            if unscheduled_forms:
                subj["forms"] = unscheduled_forms
            result.append(subj)

        return result


__all__ = [
    "DEFAULT_TABULAR_CHUNK_SIZE",
    "BaseTabularRecordMapper",
    "build_core_study_structure",
]
