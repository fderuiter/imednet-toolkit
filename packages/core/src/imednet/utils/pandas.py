"""Pandas helpers for working with iMednet models."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

try:
    import pandas as pd
except ImportError:
    pd: Any = None  # type: ignore[no-redef]
from ..core.context import get_study_context, sdk_governance_context
from ..integrations.enrichment import resolve_enrichment_pipeline
from ..models.base import ImednetBaseModel
from ..models.forms import Form
from ..models.intervals import Interval
from ..models.queries import Query
from ..models.records import Record
from ..models.sites import Site
from ..models.subjects import Subject
from ..models.users import User
from ..models.variables import Variable
from ..models.visits import Visit
from .batch_coordinator import DEFAULT_BATCH_CHUNK_SIZE, BatchSubmissionSummary
from .job_poller import JobProgressHandler
from .security import (
    SensitivityRegistry,
    _normalize_key,
    build_study_sensitivity_registry,
    global_sensitivity_registry,
    sanitize_csv_formula,
    sanitize_diagnostic_payload,
)
from .tabular import MASKED_SENTINEL, csv_to_records, dataframe_rows_to_records
from .typing import JsonDict

if TYPE_CHECKING:  # pragma: no cover - only for type checking
    from ..sdk import ImednetSDK
    from ..validation.cache import ValidationSchemaProtocol


def check_pandas(feature_name: str | None = None) -> None:
    """Validate that pandas is installed, raising an ImportError if missing.

    Args:
        feature_name: Optional name of the feature or function requiring pandas.

    Raises:
        ImportError: If pandas is not installed.
    """
    if pd is None:
        target = f" for {feature_name}" if feature_name else ""
        raise ImportError(
            f"pandas is required{target}. Install with \"pip install 'imednet[export]'\"."
        )


def _resolve_study_registry(study_key: str | None) -> SensitivityRegistry:
    """Resolve a study-aware SensitivityRegistry, failing closed to global defaults."""
    pipeline = resolve_enrichment_pipeline(study_key)
    if pipeline is not None and pipeline.phi_keys:
        return build_study_sensitivity_registry(pipeline.phi_keys)
    return global_sensitivity_registry


def _models_to_dataframe(
    models: Iterable[ImednetBaseModel],
    feature_name: str | None = None,
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert an iterable of :class:`~imednet.models.base.ImednetBaseModel` to a DataFrame.

    Each model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`. When
    ``mask_phi`` is ``True`` (default), sensitive PHI keys, inline text PHI,
    and committed ``StudyConfiguration`` PHI rules are redacted with
    ``***MASKED***``.
    """
    check_pandas(feature_name)
    rows = [model.model_dump(by_alias=False) for model in models]
    if not rows or not mask_phi:
        return pd.DataFrame(rows)

    scalar_study_key = study_key if isinstance(study_key, str) and study_key else None
    fallback_study_key = scalar_study_key or get_study_context()
    registries: dict[str | None, SensitivityRegistry] = {}
    sanitized_rows: list[dict[str, Any]] = []
    for row in rows:
        raw_row_sk = row.get("study_key")
        row_sk = (
            scalar_study_key
            or (raw_row_sk if isinstance(raw_row_sk, str) and raw_row_sk else None)
            or fallback_study_key
        )
        reg = registries.get(row_sk)
        if reg is None:
            reg = _resolve_study_registry(row_sk)
            registries[row_sk] = reg
        sanitized_rows.append(sanitize_diagnostic_payload(row, registry=reg))
    return pd.DataFrame(sanitized_rows)


def queries_to_dataframe(
    queries: list[Query],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.queries.Query` to a DataFrame.

    Each query model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(
        queries, "queries_to_dataframe", mask_phi=mask_phi, study_key=study_key
    )


def variables_to_dataframe(
    variables: list[Variable],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.variables.Variable` to a DataFrame.

    Each variable model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(
        variables, "variables_to_dataframe", mask_phi=mask_phi, study_key=study_key
    )


def subjects_to_dataframe(
    subjects: list[Subject],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.subjects.Subject` to a DataFrame.

    Each subject model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(
        subjects, "subjects_to_dataframe", mask_phi=mask_phi, study_key=study_key
    )


def forms_to_dataframe(
    forms: list[Form],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.forms.Form` to a DataFrame.

    Each form model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(forms, "forms_to_dataframe", mask_phi=mask_phi, study_key=study_key)


def visits_to_dataframe(
    visits: list[Visit],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.visits.Visit` to a DataFrame.

    Each visit model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(
        visits, "visits_to_dataframe", mask_phi=mask_phi, study_key=study_key
    )


def intervals_to_dataframe(
    intervals: list[Interval],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.intervals.Interval` to a DataFrame.

    Each interval model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(
        intervals, "intervals_to_dataframe", mask_phi=mask_phi, study_key=study_key
    )


def sites_to_dataframe(
    sites: list[Site],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.sites.Site` to a DataFrame.

    Each site model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(sites, "sites_to_dataframe", mask_phi=mask_phi, study_key=study_key)


def users_to_dataframe(
    users: list[User],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.users.User` to a DataFrame.

    Each user model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(users, "users_to_dataframe", mask_phi=mask_phi, study_key=study_key)


def _is_custom_phi_column(col: str, phi_keys: set[str]) -> bool:
    """Return True if *col* matches a custom study PHI field (top-level or json_normalize path)."""
    if not phi_keys or not col or global_sensitivity_registry.is_exempt(col):
        return False
    norm = _normalize_key(col)
    if not norm:
        return False
    collapsed_phi_keys = {
        norm_pk.replace("_", "") for pk in phi_keys if (norm_pk := _normalize_key(pk))
    } | phi_keys
    if norm in collapsed_phi_keys or norm.replace("_", "") in collapsed_phi_keys:
        return True
    return any(
        not global_sensitivity_registry.is_exempt(part)
        and (part_norm := _normalize_key(part))
        and (part_norm in collapsed_phi_keys or part_norm.replace("_", "") in collapsed_phi_keys)
        for part in col.split("_")
        if part
    )


def records_to_dataframe(
    records: list[Record],
    *,
    flatten: bool = False,
    mask_phi: bool = True,
    study_key: str | None = None,
    label_map: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.records.Record` to a DataFrame.

    Each record is converted using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False``. If ``flatten`` is ``True`` the ``record_data`` column is
    expanded using :func:`pandas.json_normalize` so that each variable becomes a
    column in the resulting DataFrame. When ``label_map`` is provided with
    ``flatten=True``, flattened variable columns are renamed to their mapped
    labels after PHI checks. When ``mask_phi`` is ``True`` (default), sensitive
    PHI fields and mapped variable labels inside ``record_data`` are replaced
    with ``***MASKED***``.
    """
    resolved_study_key = study_key if isinstance(study_key, str) and study_key else None
    if not resolved_study_key:
        for rec in records:
            cand = getattr(rec, "study_key", None)
            if isinstance(cand, str) and cand:
                resolved_study_key = cand
                break
    if not resolved_study_key:
        resolved_study_key = get_study_context()

    df = _models_to_dataframe(
        records,
        "records_to_dataframe",
        mask_phi=mask_phi,
        study_key=resolved_study_key,
    )
    if df.empty:
        return df

    pipeline = resolve_enrichment_pipeline(resolved_study_key)
    phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()

    if "record_data" in df.columns and pipeline is not None:
        df["record_data"] = df["record_data"].apply(
            lambda val: (
                pipeline.process(val, mask_phi=mask_phi, _skip_default_phi=mask_phi)
                if isinstance(val, dict)
                else val
            )
        )

    if flatten:
        base_df = df.drop(columns=["record_data"])
        record_df = pd.json_normalize(df["record_data"].tolist(), sep="_")
        clean_label_map = (
            {str(k): str(v) for k, v in label_map.items() if k and v and str(v).strip()}
            if label_map
            else {}
        )
        if mask_phi and not record_df.empty:
            for col in record_df.columns:
                col_str = str(col)
                mapped_label = clean_label_map.get(col_str)
                is_col_phi = global_sensitivity_registry.is_sensitive(
                    col_str
                ) or _is_custom_phi_column(col_str, phi_keys)
                is_label_phi = bool(
                    mapped_label
                    and (
                        global_sensitivity_registry.is_sensitive(mapped_label)
                        or _is_custom_phi_column(mapped_label, phi_keys)
                    )
                )
                if is_col_phi or is_label_phi:
                    record_df[col] = MASKED_SENTINEL
        if clean_label_map and not record_df.empty:
            meta_cols = set(base_df.columns)
            rename_targets = {
                col_str: lbl for col_str, lbl in clean_label_map.items() if lbl not in meta_cols
            }
            if rename_targets:
                record_df = record_df.rename(columns=rename_targets)
        df = pd.concat([base_df, record_df], axis=1)
    return df


def dataframe_to_records(
    df: pd.DataFrame,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Convert a ``pandas.DataFrame`` into REST create-records payload dicts.

    Strips read-only :class:`~imednet.models.records.Record` system audit columns,
    preserves record update identifiers and metadata (``recordId``, ``recordOid``,
    ``visitId``, ``recordStatus``, ``reasonForChange``), maps snake_case and
    camelCase identifiers (``formKey``/``formId``,
    ``subjectKey``/``subjectOid``/``subjectId``, ``siteName``/``siteId``,
    ``intervalName``/``intervalId``), drops ``NaN`` and blank cells, coerces
    integer-valued floats (``45.0 -> 45``), and raises
    :class:`~imednet.errors.ValidationError` if any cell contains ``***MASKED***``.
    """
    check_pandas("dataframe_to_records")
    return dataframe_rows_to_records(
        df,
        schema=schema,
        study_key=study_key,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )


def import_records_csv(
    sdk: ImednetSDK,
    study_key: str,
    file_path: str | os.PathLike[str],
    *,
    email_notify: bool | str | None = None,
    schema: ValidationSchemaProtocol | None = None,
    chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
    skip_validation: bool = False,
    wait_for_completion: bool = False,
    timeout: float = 300.0,
    interval: float = 5.0,
    on_progress: JobProgressHandler | None = None,
    dry_run: bool = False,
    collect_errors: bool = False,
) -> BatchSubmissionSummary:
    """Import records from a CSV file into ``study_key`` using stdlib CSV parsing.

    Preserves ``recordId``, ``recordOid``, ``visitId``, ``recordStatus``, and
    ``reasonForChange`` for symmetric round-trip updates. Works in
    ``packages/core`` even when ``pandas`` is not installed, and rejects
    ``***MASKED***`` sentinels before any HTTP request is sent.
    """
    records = csv_to_records(file_path, schema=schema, study_key=study_key)
    effective_schema = schema
    if records and effective_schema is None and not skip_validation:
        from imednet.datadict.loaders.api import can_load_from_api, load_from_api

        if can_load_from_api(sdk):
            effective_schema = load_from_api(sdk, study_key)
            records = csv_to_records(file_path, schema=effective_schema, study_key=study_key)
    return sdk.records.create_batch(
        study_key,
        records,
        email_notify=email_notify,
        schema=effective_schema,
        chunk_size=chunk_size,
        skip_validation=skip_validation,
        wait_for_completion=wait_for_completion,
        timeout=timeout,
        interval=interval,
        on_progress=on_progress,
        dry_run=dry_run,
        collect_errors=collect_errors,
    )


def export_records_csv(
    sdk: ImednetSDK,
    study_key: str,
    file_path: str | os.PathLike[str],
    *,
    flatten: bool = True,
    mask_phi: bool = True,
    use_labels_as_columns: bool = False,
) -> None:
    """Fetch all records for ``study_key`` and write them to ``file_path``.

    Parameters are passed to :func:`records_to_dataframe` and the resulting
    DataFrame is written with :meth:`pandas.DataFrame.to_csv` using
    ``index=False``.
    """
    check_pandas("export_records_csv")

    with sdk_governance_context(sdk):
        records = list(sdk.records.list(study_key=study_key))
        rename_map: dict[str, str] | None = None
        if use_labels_as_columns and flatten and records:
            variables_ep = getattr(sdk, "variables", None)
            if variables_ep is not None and callable(getattr(variables_ep, "list", None)):
                variables = list(variables_ep.list(study_key=study_key))
                rename_map = {
                    v.variable_name: v.label
                    for v in variables
                    if getattr(v, "variable_name", None)
                    and getattr(v, "label", None)
                    and str(v.label).strip()
                } or None

        df = records_to_dataframe(
            records,
            flatten=flatten,
            mask_phi=mask_phi,
            study_key=study_key,
            label_map=rename_map,
        )

    # Sanitize data to prevent CSV injection
    df = df.map(sanitize_csv_formula)

    df.to_csv(file_path, index=False)


__all__ = [
    "MASKED_SENTINEL",
    "check_pandas",
    "csv_to_records",
    "dataframe_to_records",
    "export_records_csv",
    "forms_to_dataframe",
    "import_records_csv",
    "intervals_to_dataframe",
    "queries_to_dataframe",
    "records_to_dataframe",
    "sites_to_dataframe",
    "subjects_to_dataframe",
    "users_to_dataframe",
    "variables_to_dataframe",
    "visits_to_dataframe",
]
