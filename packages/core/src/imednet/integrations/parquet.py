# pylint: disable=duplicate-code
"""Hive-partitioned Parquet integration helpers."""

from __future__ import annotations

import inspect
from importlib import import_module
from types import ModuleType
from typing import Any

from ..sdk import ImednetSDK
from ..utils import validate_partition_key
from .export import _mask_df, _record_mapper
from .parquet_engine import PyArrowDatasetPartitionedStorageEngine


def _ensure_pyarrow() -> ModuleType:
    """Ensure PyArrow is installed and return the module.

    Returns:
        ModuleType: The pyarrow module.

    Raises:
        ImportError: If pyarrow is not installed.
    """
    try:
        return import_module("pyarrow")
    except ImportError as error:
        raise ImportError(
            "PyArrow is required for Hive Parquet export. Install with "
            "\"pip install 'imednet[export]'\"."
        ) from error


def _cached_records_loader() -> Any:
    """Lazy import and return the CachedRecordsLoader from plugins.

    Returns:
        Any: The CachedRecordsLoader class.

    Raises:
        ImportError: If no loader plugin is installed.
    """
    from importlib.metadata import entry_points

    loaders = list(entry_points(group="imednet.loaders", name="CachedRecordsLoader"))
    if not loaders:
        raise ImportError(
            "Record export requires an installed loader plugin. "
            "Please install a package that provides this capability."
        )
    return loaders[0].load()


def _build_record_mapper(
    sdk: ImednetSDK,
    *,
    chunk_size: int,
    mask_phi: bool = True,
) -> Any:
    """Build a workflows ``RecordMapper`` without importing the plugin at module import time."""
    mapper_cls = _record_mapper()
    parameters = inspect.signature(mapper_cls).parameters
    kwargs: dict[str, Any] = {}
    if "chunk_size" in parameters:
        kwargs["chunk_size"] = chunk_size
    if "loader" in parameters:
        kwargs["loader"] = _cached_records_loader()(sdk)
    if "mask_phi" in parameters:
        kwargs["mask_phi"] = mask_phi
    return mapper_cls(sdk, **kwargs)


def export_to_hive_parquet(
    sdk: ImednetSDK,
    study_key: str,
    base_dir: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    chunk_size: int = 5_000,
    mask_phi: bool = True,
) -> None:
    """Export study records to a Hive-partitioned Parquet directory layout."""
    pyarrow_module = _ensure_pyarrow()
    validate_partition_key(study_key)

    mapper = _build_record_mapper(sdk, chunk_size=chunk_size, mask_phi=mask_phi)
    storage_engine = PyArrowDatasetPartitionedStorageEngine()
    forms = sdk.forms.list(study_key=study_key)

    form_filter: dict[str, Any] = {"formId": form_whitelist} if form_whitelist else {}
    all_variables = (
        []
        if form_whitelist is not None and len(form_whitelist) == 0
        else sdk.variables.list(study_key=study_key, **form_filter)
    )
    variables_by_form: dict[int, list[Any]] = {}
    for variable in all_variables:
        if variable.form_id is not None:
            variables_by_form.setdefault(variable.form_id, []).append(variable)

    for form in forms:
        if form.form_id is None or form.form_key is None:
            continue
        if form_whitelist is not None and form.form_id not in form_whitelist:
            continue
        validate_partition_key(form.form_key)

        variables = variables_by_form.get(form.form_id, [])
        variable_keys = [
            variable.variable_name
            for variable in variables
            if variable_whitelist is None or variable.variable_name in variable_whitelist
        ]
        label_map = {
            variable.variable_name: variable.label
            for variable in variables
            if variable.variable_name in variable_keys
        }

        record_model = mapper._build_record_model(variable_keys, label_map)
        iter_records = getattr(mapper, "_iter_records", None)
        iter_parsed_rows = getattr(mapper, "_iter_parsed_rows", None)
        if callable(iter_records) and callable(iter_parsed_rows):
            records = iter_records(
                study_key,
                extra_filters={"formId": form.form_id},
            )
            for rows, _ in iter_parsed_rows(records, record_model):
                df = mapper._build_dataframe(
                    rows,
                    variable_keys,
                    label_map,
                    use_labels_as_columns,
                )
                if df.empty:
                    continue
                if mask_phi:
                    df = _mask_df(df)
                table = pyarrow_module.Table.from_pandas(df, preserve_index=False)
                storage_engine.write_form_table(
                    table,
                    base_dir=base_dir,
                    study_key=study_key,
                    form_key=form.form_key or "",
                )
            continue

        records = mapper._fetch_records(
            study_key,
            extra_filters={"formId": form.form_id},
        )
        rows, _ = mapper._parse_records(records, record_model)
        df = mapper._build_dataframe(
            rows,
            variable_keys,
            label_map,
            use_labels_as_columns,
        )
        if mask_phi and not df.empty:
            df = _mask_df(df)
        table = pyarrow_module.Table.from_pandas(df, preserve_index=False)
        storage_engine.write_form_table(
            table,
            base_dir=base_dir,
            study_key=study_key,
            form_key=form.form_key or "",
        )


def _validate_directory_path(path: str) -> None:
    """Validate directory path to reject null bytes and control characters."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Directory path must be a non-empty string.")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError(
            f"Invalid directory path: {path!r}. Path must not contain null bytes or control characters."
        )


def hive_parquet_query(base_dir: str) -> str:
    """Return the DuckDB read_parquet query string for the given Hive base directory."""
    _validate_directory_path(base_dir)
    clean_base_dir = base_dir.rstrip("/\\")
    if not clean_base_dir and base_dir.startswith(("/", "\\")):
        clean_base_dir = "/"
    escaped_base_dir = clean_base_dir.replace("'", "''")
    return (
        f"SELECT * FROM read_parquet('{escaped_base_dir}/**/*.parquet', "
        "hive_partitioning = true, union_by_name = true)"
    )


__all__ = ["export_to_hive_parquet", "hive_parquet_query"]
