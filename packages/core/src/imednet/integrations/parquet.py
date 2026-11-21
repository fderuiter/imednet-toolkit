# pylint: disable=duplicate-code
"""Hive-partitioned Parquet integration helpers."""

from __future__ import annotations

import contextlib
import inspect
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import NonCallableMock

from ..sdk import ImednetSDK
from ..utils import validate_partition_key
from .enrichment import resolve_enrichment_pipeline
from .export import (
    _build_form_export_contexts,
    _build_tabular_sink_config,
    _canonical_record_id,
    _config_has_streaming_options,
    _filter_df_excluding_record_ids,
    _FormExportContext,
    _is_incremental_config,
    _mask_df,
    _record_mapper,
    _stream_records_across_forms,
)
from .parquet_engine import PyArrowDatasetPartitionedStorageEngine
from .sink_base import (
    BoundedFormBufferRouter,
    extract_record_form_ref,
    extract_record_id,
    validate_max_buffered_records,
)


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


def _ensure_pandas() -> ModuleType:
    """Ensure pandas is installed and return the module.

    Returns:
        ModuleType: The pandas module.

    Raises:
        ImportError: If pandas is not installed.
    """
    try:
        return import_module("pandas")
    except ImportError as error:
        raise ImportError(
            "pandas is required for Hive Parquet export. Install with "
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


def _write_hive_form_rows(
    rows: list[dict[str, Any]],
    *,
    mapper: Any,
    variable_keys: list[str],
    label_map: dict[str, Any],
    use_labels_as_columns: bool,
    mask_phi: bool,
    phi_keys: set[str],
    pyarrow_module: ModuleType,
    storage_engine: PyArrowDatasetPartitionedStorageEngine,
    base_dir: str,
    study_key: str,
    form_key: str,
    skip_empty: bool,
) -> int:
    """Convert parsed form rows into an Arrow table and commit to the Hive partition."""
    df = mapper._build_dataframe(
        rows,
        variable_keys,
        label_map,
        use_labels_as_columns,
    )
    if skip_empty and df.empty:
        return 0
    df.columns = df.columns.astype(str)
    df = df.loc[:, ~df.columns.str.lower().duplicated()]
    if mask_phi and not df.empty:
        df = _mask_df(df, phi_keys=phi_keys)
    table = pyarrow_module.Table.from_pandas(df, preserve_index=False)
    storage_engine.write_form_table(
        table,
        base_dir=base_dir,
        study_key=study_key,
        form_key=form_key,
    )
    return len(df)


def _prune_hive_partition_record_ids(
    files: list[Path],
    excluded_ids: set[str],
    *,
    pyarrow_module: ModuleType,
    storage_engine: PyArrowDatasetPartitionedStorageEngine,
    base_dir: str,
    study_key: str,
    form_key: str,
) -> None:
    """Prune superseded or voided record IDs from pre-existing Hive partition Parquet files."""
    if not files or not excluded_ids:
        return
    pandas_module = _ensure_pandas()
    partition_dir = Path(base_dir) / f"study_key={study_key}" / f"form_key={form_key}"
    for pf in files:
        if not pf.exists():
            continue
        existing_df = pandas_module.read_parquet(pf, engine="pyarrow")
        filtered_df = _filter_df_excluding_record_ids(existing_df, excluded_ids)
        if len(filtered_df) == len(existing_df):
            continue
        if not filtered_df.empty:
            for part_col in ("study_key", "form_key"):
                if part_col in filtered_df.columns:
                    filtered_df = filtered_df.drop(columns=[part_col])
            replacement_table = pyarrow_module.Table.from_pandas(
                filtered_df.reset_index(drop=True),
                preserve_index=False,
            )
            storage_engine.write_form_table(
                replacement_table,
                base_dir=base_dir,
                study_key=study_key,
                form_key=form_key,
            )
        pf.unlink(missing_ok=True)
        if pf.parent != partition_dir and pf.parent.exists() and not any(pf.parent.iterdir()):
            with contextlib.suppress(OSError):
                pf.parent.rmdir()
    if partition_dir.exists() and not any(partition_dir.rglob("*.parquet")):
        for child in sorted(partition_dir.rglob("*"), reverse=True):
            if child.is_dir():
                with contextlib.suppress(OSError):
                    child.rmdir()
        with contextlib.suppress(OSError):
            partition_dir.rmdir()


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
    max_buffered_records: int | None = None,
    **kwargs: Any,
) -> None:
    """Export study records to a Hive-partitioned Parquet directory layout."""
    pyarrow_module = _ensure_pyarrow()
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")
    validate_max_buffered_records(max_buffered_records)
    validate_partition_key(study_key)

    extra_kwargs = dict(kwargs)
    extra_kwargs.setdefault("batch_size", chunk_size)
    if max_buffered_records is not None:
        extra_kwargs["max_buffered_records"] = max_buffered_records
    manifest_path = extra_kwargs.pop("manifest_path", None)
    config = _build_tabular_sink_config(
        study_key,
        extra_kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        mask_phi=mask_phi,
    )
    if config.pandas_kwargs:
        unexpected = next(iter(config.pandas_kwargs))
        raise TypeError(
            f"export_to_hive_parquet() got an unexpected keyword argument {unexpected!r}"
        )

    effective_mask_phi = config.mask_phi
    mapper = _build_record_mapper(sdk, chunk_size=config.batch_size, mask_phi=effective_mask_phi)
    if hasattr(mapper, "_active_study_key"):
        mapper._active_study_key = study_key
    pipeline = resolve_enrichment_pipeline(study_key)
    phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()
    has_native_enrichment = callable(getattr(mapper, "_get_enrichment_pipeline", None))
    storage_engine = PyArrowDatasetPartitionedStorageEngine()

    if _config_has_streaming_options(config) or config.quality_gate_enabled:
        contexts, all_variables = _build_form_export_contexts(
            sdk,
            study_key,
            mapper,
            use_labels_as_columns=use_labels_as_columns,
            variable_whitelist=variable_whitelist,
            form_whitelist=form_whitelist,
            mask_phi=effective_mask_phi,
            pipeline=pipeline,
            stream_prefix="hive_parquet",
            custom_stream_prefix=config.stream_name,
            validate_form_key_fn=validate_partition_key,
            as_sdtm=config.as_sdtm,
            sdtm_domain=config.sdtm_domain,
            naming_convention=config.naming_convention,
        )
        if not contexts:
            return

        is_incremental = _is_incremental_config(config)
        preexisting_files: dict[str, list[Path]] = {}
        superseded_ids: dict[str, set[str]] = {ctx.form_key: set() for ctx in contexts}
        if is_incremental and config.idempotent:
            for ctx in contexts:
                part_dir = Path(base_dir) / f"study_key={study_key}" / f"form_key={ctx.form_key}"
                preexisting_files[ctx.form_key] = (
                    sorted(part_dir.rglob("*.parquet")) if part_dir.exists() else []
                )

        def _on_hive_active(ctx: _FormExportContext, batch: list[Any], _batch_idx: int) -> int:
            parsed_rows, _ = mapper._parse_records(batch, ctx.record_model)
            rows = (
                [pipeline.process(r, mask_phi=effective_mask_phi) for r in parsed_rows]
                if pipeline is not None and not has_native_enrichment
                else parsed_rows
            )
            written = _write_hive_form_rows(
                rows,
                mapper=mapper,
                variable_keys=ctx.variable_keys,
                label_map=ctx.label_map,
                use_labels_as_columns=use_labels_as_columns,
                mask_phi=effective_mask_phi,
                phi_keys=phi_keys,
                pyarrow_module=pyarrow_module,
                storage_engine=storage_engine,
                base_dir=base_dir,
                study_key=study_key,
                form_key=ctx.form_key,
                skip_empty=True,
            )
            if is_incremental and config.idempotent and written > 0:
                for rec in batch:
                    cid = _canonical_record_id(extract_record_id(rec))
                    if cid is not None:
                        superseded_ids[ctx.form_key].add(cid)
            return written

        def _on_hive_voided(ctx: _FormExportContext, batch: list[Any], _batch_idx: int) -> int:
            if not (is_incremental and config.idempotent):
                return 0
            cids = [
                cid
                for rec in batch
                if (cid := _canonical_record_id(extract_record_id(rec))) is not None
            ]
            superseded_ids[ctx.form_key].update(cids)
            return len(cids)

        def _reconcile_partitions_before_checkpoints() -> None:
            if not (is_incremental and config.idempotent):
                return
            for ctx in contexts:
                _prune_hive_partition_record_ids(
                    preexisting_files.get(ctx.form_key, []),
                    superseded_ids[ctx.form_key],
                    pyarrow_module=pyarrow_module,
                    storage_engine=storage_engine,
                    base_dir=base_dir,
                    study_key=study_key,
                    form_key=ctx.form_key,
                )

        _stream_records_across_forms(
            sdk,
            study_key,
            config,
            contexts,
            all_variables,
            on_active_batch=_on_hive_active,
            on_voided_batch=_on_hive_voided,
            on_before_checkpoints=_reconcile_partitions_before_checkpoints,
        )
        return

    forms = list(sdk.forms.list(study_key=study_key))

    form_filter: dict[str, Any] = {"formId": form_whitelist} if form_whitelist else {}
    all_variables = (
        []
        if form_whitelist is not None and len(form_whitelist) == 0
        else list(sdk.variables.list(study_key=study_key, **form_filter))
    )
    variables_by_form: dict[int, list[Any]] = {}
    for variable in all_variables:
        if variable.form_id is not None:
            variables_by_form.setdefault(variable.form_id, []).append(variable)

    form_specs: list[dict[str, Any]] = []
    for form in forms:
        if form.form_id is None or form.form_key is None:
            continue
        if form_whitelist is not None and form.form_id not in form_whitelist:
            continue
        validate_partition_key(form.form_key)

        variables = variables_by_form.get(form.form_id, [])
        variable_keys = list(
            dict.fromkeys(
                variable.variable_name
                for variable in variables
                if variable.variable_name
                and (variable_whitelist is None or variable.variable_name in variable_whitelist)
            )
        )
        label_map = {
            variable.variable_name: variable.label
            for variable in variables
            if variable.variable_name in variable_keys
        }
        form_specs.append(
            {
                "form_id": form.form_id,
                "form_key": form.form_key,
                "variable_keys": variable_keys,
                "label_map": label_map,
                "record_model": mapper._build_record_model(variable_keys, label_map),
            }
        )

    if not form_specs:
        return

    iter_records = getattr(mapper, "_iter_records", None)
    can_stream_single_pass = (
        not isinstance(mapper, NonCallableMock)
        and callable(getattr(mapper, "_parse_records", None))
        and callable(getattr(mapper, "_parse_record", None))
    )
    if can_stream_single_pass and callable(iter_records):
        by_form_id = {spec["form_id"]: spec for spec in form_specs}
        by_form_key = {spec["form_key"]: spec for spec in form_specs}

        def _flush_hive_batch(form_key: str, batch: list[Any], _batch_idx: int) -> None:
            spec = by_form_key[form_key]
            parsed_rows, _ = mapper._parse_records(batch, spec["record_model"])
            rows = (
                [pipeline.process(r, mask_phi=mask_phi) for r in parsed_rows]
                if pipeline is not None and not has_native_enrichment
                else parsed_rows
            )
            _write_hive_form_rows(
                rows,
                mapper=mapper,
                variable_keys=spec["variable_keys"],
                label_map=spec["label_map"],
                use_labels_as_columns=use_labels_as_columns,
                mask_phi=mask_phi,
                phi_keys=phi_keys,
                pyarrow_module=pyarrow_module,
                storage_engine=storage_engine,
                base_dir=base_dir,
                study_key=study_key,
                form_key=form_key,
                skip_empty=True,
            )

        router = BoundedFormBufferRouter(
            [spec["form_key"] for spec in form_specs],
            batch_size=chunk_size,
            max_buffered_records=max_buffered_records,
            on_flush_active=_flush_hive_batch,
        )
        study_extra_filters = (
            {"formId": [spec["form_id"] for spec in form_specs]}
            if form_whitelist is not None
            else None
        )
        for rec in iter_records(study_key, extra_filters=study_extra_filters):
            fid, fk = extract_record_form_ref(rec)
            matched = (
                by_form_id.get(fid)
                if fid is not None and fid in by_form_id
                else (by_form_key.get(fk) if fk is not None else None)
            )
            if matched is not None:
                router.add_active(matched["form_key"], rec)
        router.flush_all()
        return

    iter_parsed_rows = getattr(mapper, "_iter_parsed_rows", None)
    for spec in form_specs:
        if callable(iter_records) and callable(iter_parsed_rows):
            records = iter_records(
                study_key,
                extra_filters={"formId": spec["form_id"]},
            )
            for parsed_rows, _ in iter_parsed_rows(records, spec["record_model"]):
                rows = (
                    [pipeline.process(r, mask_phi=mask_phi) for r in parsed_rows]
                    if pipeline is not None and not has_native_enrichment
                    else parsed_rows
                )
                _write_hive_form_rows(
                    rows,
                    mapper=mapper,
                    variable_keys=spec["variable_keys"],
                    label_map=spec["label_map"],
                    use_labels_as_columns=use_labels_as_columns,
                    mask_phi=mask_phi,
                    phi_keys=phi_keys,
                    pyarrow_module=pyarrow_module,
                    storage_engine=storage_engine,
                    base_dir=base_dir,
                    study_key=study_key,
                    form_key=spec["form_key"],
                    skip_empty=True,
                )
            continue

        records = mapper._fetch_records(
            study_key,
            extra_filters={"formId": spec["form_id"]},
        )
        parsed_rows, _ = mapper._parse_records(records, spec["record_model"])
        rows = (
            [pipeline.process(r, mask_phi=mask_phi) for r in parsed_rows]
            if pipeline is not None and not has_native_enrichment
            else parsed_rows
        )
        _write_hive_form_rows(
            rows,
            mapper=mapper,
            variable_keys=spec["variable_keys"],
            label_map=spec["label_map"],
            use_labels_as_columns=use_labels_as_columns,
            mask_phi=mask_phi,
            phi_keys=phi_keys,
            pyarrow_module=pyarrow_module,
            storage_engine=storage_engine,
            base_dir=base_dir,
            study_key=study_key,
            form_key=spec["form_key"],
            skip_empty=False,
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
