"""Pandas helpers for working with iMednet models."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

try:
    import pandas as pd
except ImportError:
    pd: Any = None  # type: ignore[no-redef]
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
    _normalize_key,
    global_sensitivity_registry,
    mask_clinical_phi,
    sanitize_csv_formula,
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


def _models_to_dataframe(
    models: Iterable[ImednetBaseModel],
    feature_name: str | None = None,
) -> pd.DataFrame:
    """Convert an iterable of :class:`~imednet.models.base.ImednetBaseModel` to a DataFrame.

    Each model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas(feature_name)
    rows = [model.model_dump(by_alias=False) for model in models]
    return pd.DataFrame(rows)


def queries_to_dataframe(queries: list[Query]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.queries.Query` to a DataFrame.

    Each query model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(queries, "queries_to_dataframe")


def variables_to_dataframe(variables: list[Variable]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.variables.Variable` to a DataFrame.

    Each variable model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(variables, "variables_to_dataframe")


def subjects_to_dataframe(subjects: list[Subject]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.subjects.Subject` to a DataFrame.

    Each subject model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(subjects, "subjects_to_dataframe")


def forms_to_dataframe(forms: list[Form]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.forms.Form` to a DataFrame.

    Each form model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(forms, "forms_to_dataframe")


def visits_to_dataframe(visits: list[Visit]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.visits.Visit` to a DataFrame.

    Each visit model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(visits, "visits_to_dataframe")


def intervals_to_dataframe(intervals: list[Interval]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.intervals.Interval` to a DataFrame.

    Each interval model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(intervals, "intervals_to_dataframe")


def sites_to_dataframe(sites: list[Site]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.sites.Site` to a DataFrame.

    Each site model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    return _models_to_dataframe(sites, "sites_to_dataframe")


def users_to_dataframe(users: list[User]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.users.User` to a DataFrame.

    Each user model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas("users_to_dataframe")

    user_dicts = [u.model_dump(by_alias=False) for u in users]
    return pd.DataFrame(user_dicts)


def _is_custom_phi_column(col: str, phi_keys: set[str]) -> bool:
    """Return True if *col* matches a custom study PHI field (top-level or json_normalize path)."""
    if not phi_keys or not col or global_sensitivity_registry.is_exempt(col):
        return False
    if _normalize_key(col) in phi_keys:
        return True
    return any(
        not global_sensitivity_registry.is_exempt(part) and _normalize_key(part) in phi_keys
        for part in col.split("_")
        if part
    )


def records_to_dataframe(
    records: list[Record],
    *,
    flatten: bool = False,
    mask_phi: bool = True,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.records.Record` to a DataFrame.

    Each record is converted using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False``. If ``flatten`` is ``True`` the ``record_data`` column is
    expanded using :func:`pandas.json_normalize` so that each variable becomes a
    column in the resulting DataFrame. When ``mask_phi`` is ``True`` (default),
    sensitive PHI fields inside ``record_data`` are replaced with ``***MASKED***``.
    """
    df = _models_to_dataframe(records, "records_to_dataframe")
    if df.empty:
        return df

    resolved_study_key = study_key
    if not resolved_study_key:
        for rec in records:
            cand = getattr(rec, "study_key", None)
            if isinstance(cand, str) and cand:
                resolved_study_key = cand
                break

    pipeline = resolve_enrichment_pipeline(resolved_study_key)
    phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()

    if "record_data" in df.columns:
        if mask_phi:
            df["record_data"] = df["record_data"].apply(mask_clinical_phi)
        if pipeline is not None:
            df["record_data"] = df["record_data"].apply(
                lambda val: (
                    pipeline.process(val, mask_phi=mask_phi, _skip_default_phi=mask_phi)
                    if isinstance(val, dict)
                    else val
                )
            )

    if flatten:
        record_df = pd.json_normalize(df["record_data"].tolist(), sep="_")
        if mask_phi and not record_df.empty:
            for col in record_df.columns:
                col_str = str(col)
                if global_sensitivity_registry.is_sensitive(col_str) or _is_custom_phi_column(
                    col_str, phi_keys
                ):
                    record_df[col] = MASKED_SENTINEL
        df = pd.concat([df.drop(columns=["record_data"]), record_df], axis=1)
    return df


def dataframe_to_records(
    df: pd.DataFrame,
    *,
    schema: ValidationSchemaProtocol | None = None,
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
    return dataframe_rows_to_records(df, schema=schema)


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
    records = csv_to_records(file_path, schema=schema)
    effective_schema = schema
    if dry_run and effective_schema is None and not skip_validation:
        from imednet.datadict.loaders.api import can_load_from_api, load_from_api

        if can_load_from_api(sdk):
            effective_schema = load_from_api(sdk, study_key)
            records = csv_to_records(file_path, schema=effective_schema)
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
    file_path: str,
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

    records = list(sdk.records.list(study_key=study_key))
    df = records_to_dataframe(
        records,
        flatten=flatten,
        mask_phi=mask_phi,
        study_key=study_key,
    )

    if use_labels_as_columns and flatten and not df.empty:
        variables_ep = getattr(sdk, "variables", None)
        if variables_ep is not None and callable(getattr(variables_ep, "list", None)):
            variables = list(variables_ep.list(study_key=study_key))
            rename_map = {
                v.variable_name: v.label
                for v in variables
                if getattr(v, "variable_name", None) and getattr(v, "label", None)
            }
            if rename_map:
                df = df.rename(columns=rename_map)

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
