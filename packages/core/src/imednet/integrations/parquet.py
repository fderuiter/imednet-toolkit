# pylint: disable=duplicate-code
"""Hive-partitioned Parquet integration helpers."""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import NonCallableMock

from ..errors import ExportBatchError, ImednetError
from ..models.sdtm import resolve_sdtm_domain, transform_records_to_sdtm_rows
from ..sdk import ImednetSDK
from ..utils import validate_partition_key
from .enrichment import resolve_enrichment_pipeline
from .export import (
    _apply_naming_convention_to_df,
    _build_form_export_contexts,
    _build_tabular_sink_config,
    _canonical_record_id,
    _config_has_streaming_options,
    _df_sdtm_key_tuples,
    _filter_df_excluding_record_ids,
    _filter_df_excluding_sdtm_keys,
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
    hydrate_sdtm_checkpoint_state,
    persist_sdtm_checkpoint_state,
    pop_deleted_sdtm_provenance_keys,
    reconcile_sdtm_provenance_rows,
    sdtm_natural_key_tuple,
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
    use_cache: bool = True,
) -> Any:
    """Build a workflows ``RecordMapper`` without importing the plugin at module import time."""
    mapper_cls = _record_mapper()
    parameters = inspect.signature(mapper_cls).parameters
    kwargs: dict[str, Any] = {}
    if "chunk_size" in parameters:
        kwargs["chunk_size"] = chunk_size
    if use_cache and "loader" in parameters:
        loader_cls = _cached_records_loader()
        loader_params: Mapping[str, inspect.Parameter] = {}
        with contextlib.suppress(TypeError, ValueError):
            loader_params = inspect.signature(loader_cls).parameters
        if "mask_phi" in loader_params:
            kwargs["loader"] = loader_cls(sdk, mask_phi=mask_phi)
        else:
            kwargs["loader"] = loader_cls(sdk)
    if "mask_phi" in parameters:
        kwargs["mask_phi"] = mask_phi
    return mapper_cls(sdk, **kwargs)


def _commit_hive_df(
    df: Any,
    *,
    pyarrow_module: ModuleType,
    storage_engine: PyArrowDatasetPartitionedStorageEngine,
    base_dir: str,
    study_key: str,
    form_key: str,
    batch_id: str,
) -> int:
    """Drop conflicting Hive partition columns and commit ``df`` as a partitioned Arrow table."""
    for part_col in ("study_key", "form_key"):
        if part_col in df.columns:
            df = df.drop(columns=[part_col])
    pa_err_cls = getattr(pyarrow_module, "ArrowException", None)
    driver_errs: tuple[type[BaseException], ...] = (
        (OSError, pa_err_cls)
        if isinstance(pa_err_cls, type) and issubclass(pa_err_cls, BaseException)
        else (OSError,)
    )
    try:
        table = pyarrow_module.Table.from_pandas(df, preserve_index=False)
        storage_engine.write_form_table(
            table,
            base_dir=base_dir,
            study_key=study_key,
            form_key=form_key,
        )
    except ImednetError:
        raise
    except driver_errs as exc:
        raise ExportBatchError(
            f"Hive Parquet batch {batch_id!r} failed: {exc}",
            batch_id=batch_id,
        ) from exc
    return len(df)


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
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
    batch_id: str = "",
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
        df = _mask_df(df, phi_keys=phi_keys, as_sdtm=False)
    if not use_labels_as_columns and naming_convention is not None:
        df = _apply_naming_convention_to_df(
            df,
            naming_convention,
            domain=sdtm_domain,
            study_key=study_key,
        )
    return _commit_hive_df(
        df,
        pyarrow_module=pyarrow_module,
        storage_engine=storage_engine,
        base_dir=base_dir,
        study_key=study_key,
        form_key=form_key,
        batch_id=batch_id or f"{study_key}/{form_key}/0",
    )


def _prune_hive_partition_files(
    files: list[Path],
    *,
    excluded_ids: set[str] | None = None,
    excluded_sdtm_keys: set[tuple[tuple[str, Any], ...]] | None = None,
    sdtm_domain: str | None = None,
    pyarrow_module: ModuleType,
    storage_engine: PyArrowDatasetPartitionedStorageEngine,
    base_dir: str,
    study_key: str,
    form_key: str,
) -> None:
    """Prune superseded or voided rows from pre-existing Hive partition Parquet files."""
    if not files or (not excluded_ids and not excluded_sdtm_keys):
        return
    pandas_module = _ensure_pandas()
    partition_dir = Path(base_dir) / f"study_key={study_key}" / f"form_key={form_key}"
    pa_err_cls = getattr(pyarrow_module, "ArrowException", None)
    driver_errs: tuple[type[BaseException], ...] = (
        (OSError, pa_err_cls)
        if isinstance(pa_err_cls, type) and issubclass(pa_err_cls, BaseException)
        else (OSError,)
    )
    try:
        for pf in files:
            if not pf.exists():
                continue
            existing_df = pandas_module.read_parquet(pf, engine="pyarrow")
            if excluded_sdtm_keys is not None:
                filtered_df = _filter_df_excluding_sdtm_keys(
                    existing_df,
                    excluded_sdtm_keys,
                    default_domain=sdtm_domain,
                )
            else:
                filtered_df = _filter_df_excluding_record_ids(existing_df, excluded_ids or set())
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
    except ImednetError:
        raise
    except driver_errs as exc:
        raise ExportBatchError(
            f"Hive Parquet prune for '{study_key}/{form_key}' failed: {exc}",
            batch_id=f"{study_key}/{form_key}/reconcile",
        ) from exc


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
    """Backward-compatible wrapper pruning superseded/voided ``recordId`` rows from Hive partitions."""
    _prune_hive_partition_files(
        files,
        excluded_ids=excluded_ids,
        pyarrow_module=pyarrow_module,
        storage_engine=storage_engine,
        base_dir=base_dir,
        study_key=study_key,
        form_key=form_key,
    )


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
    is_streaming_mode = _config_has_streaming_options(config) or config.quality_gate_enabled
    mapper = _build_record_mapper(
        sdk,
        chunk_size=config.batch_size,
        mask_phi=effective_mask_phi,
        use_cache=not is_streaming_mode,
    )
    if hasattr(mapper, "_active_study_key"):
        mapper._active_study_key = study_key
    pipeline = resolve_enrichment_pipeline(study_key)
    phi_keys: set[str] = set(pipeline.phi_keys) if pipeline is not None else set()
    has_native_enrichment = callable(getattr(mapper, "_get_enrichment_pipeline", None))
    storage_engine = PyArrowDatasetPartitionedStorageEngine()

    if is_streaming_mode:
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
        superseded_sdtm_keys: dict[str, set[tuple[tuple[str, Any], ...]]] = {
            ctx.form_key: set() for ctx in contexts
        }
        sdtm_state_by_form: dict[
            str, tuple[dict[str, dict[str, int]], dict[str, dict[str, Any]]]
        ] = {}
        if is_incremental and config.idempotent:
            for ctx in contexts:
                part_dir = Path(base_dir) / f"study_key={study_key}" / f"form_key={ctx.form_key}"
                preexisting_files[ctx.form_key] = (
                    sorted(part_dir.rglob("*.parquet")) if part_dir.exists() else []
                )

        def _get_form_sdtm_state(
            ctx: _FormExportContext,
        ) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, Any]]]:
            if ctx.form_key not in sdtm_state_by_form:
                raw_meta = ctx.tx.get("metadata") if isinstance(ctx.tx, dict) else None
                sdtm_state_by_form[ctx.form_key] = hydrate_sdtm_checkpoint_state(
                    raw_meta if isinstance(raw_meta, dict) else None
                )
            return sdtm_state_by_form[ctx.form_key]

        def _persist_form_sdtm_state(ctx: _FormExportContext) -> None:
            if ctx.form_key not in sdtm_state_by_form:
                return
            seq_by_dom, prov = sdtm_state_by_form[ctx.form_key]
            raw_meta = ctx.tx.get("metadata") if isinstance(ctx.tx, dict) else None
            if isinstance(raw_meta, dict):
                persist_sdtm_checkpoint_state(raw_meta, seq_by_dom, prov)

        def _on_hive_active(ctx: _FormExportContext, batch: list[Any], batch_idx: int) -> int:
            batch_id_str = f"{study_key}/{ctx.form_key}/{batch_idx}"
            if config.as_sdtm:
                pandas_mod = _ensure_pandas()
                seq_by_dom, prov = _get_form_sdtm_state(ctx)
                target_domain = resolve_sdtm_domain(ctx.sdtm_domain, fallback="AE")
                domain_seq = seq_by_dom.setdefault(target_domain, {})
                batch_records: Sequence[Any] = batch
                if variable_whitelist is not None:
                    allowed = set(variable_whitelist)
                    if pipeline is not None and pipeline.mappings:
                        for src_var, rule in pipeline.mappings.items():
                            if rule.target_field in allowed:
                                allowed.add(src_var)
                    projected: list[Any] = []
                    for rec in batch:
                        rec_data = getattr(rec, "record_data", None)
                        if hasattr(rec, "model_copy") and isinstance(rec_data, dict):
                            projected.append(
                                rec.model_copy(
                                    update={
                                        "record_data": {
                                            k: v for k, v in rec_data.items() if k in allowed
                                        }
                                    }
                                )
                            )
                        elif isinstance(rec, Mapping) and isinstance(
                            rec.get("record_data"), Mapping
                        ):
                            cloned = dict(rec)
                            cloned["record_data"] = {
                                k: v for k, v in rec["record_data"].items() if k in allowed
                            }
                            projected.append(cloned)
                        else:
                            projected.append(rec)
                    batch_records = projected

                raw_sdtm_rows = transform_records_to_sdtm_rows(
                    batch_records,
                    domain=target_domain,
                    study_key=study_key,
                    include_deleted=config.include_deleted,
                    mask_phi=effective_mask_phi,
                    seq_counters=domain_seq,
                    phi_keys=phi_keys,
                    pipeline=pipeline,
                    filter_by_domain=bool(config.sdtm_domain),
                    include_record_id_provenance=True,
                    provenance=prov,
                )
                cleaned_rows, all_replaced_keys, _ = reconcile_sdtm_provenance_rows(
                    raw_sdtm_rows,
                    prov,
                    default_domain=target_domain,
                )
                _persist_form_sdtm_state(ctx)
                if is_incremental and config.idempotent:
                    for kd in all_replaced_keys:
                        superseded_sdtm_keys[ctx.form_key].add(sdtm_natural_key_tuple(kd))
                if not cleaned_rows:
                    return 0
                df = pandas_mod.DataFrame(cleaned_rows)
                if df.empty:
                    return 0
                if effective_mask_phi:
                    df = _mask_df(df, phi_keys=phi_keys, as_sdtm=True)
                if is_incremental and config.idempotent:
                    for kt in _df_sdtm_key_tuples(df, default_domain=target_domain):
                        if kt is not None:
                            superseded_sdtm_keys[ctx.form_key].add(kt)
                return _commit_hive_df(
                    df,
                    pyarrow_module=pyarrow_module,
                    storage_engine=storage_engine,
                    base_dir=base_dir,
                    study_key=study_key,
                    form_key=ctx.form_key,
                    batch_id=batch_id_str,
                )

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
                naming_convention=config.naming_convention,
                sdtm_domain=config.sdtm_domain,
                batch_id=batch_id_str,
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
            if config.as_sdtm:
                _seq_by_dom, prov = _get_form_sdtm_state(ctx)
                deleted_keys = pop_deleted_sdtm_provenance_keys(batch, prov)
                _persist_form_sdtm_state(ctx)
                for kd in deleted_keys:
                    superseded_sdtm_keys[ctx.form_key].add(sdtm_natural_key_tuple(kd))
                return len(deleted_keys)
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
                if config.as_sdtm:
                    _prune_hive_partition_files(
                        preexisting_files.get(ctx.form_key, []),
                        excluded_sdtm_keys=superseded_sdtm_keys[ctx.form_key],
                        sdtm_domain=ctx.sdtm_domain,
                        pyarrow_module=pyarrow_module,
                        storage_engine=storage_engine,
                        base_dir=base_dir,
                        study_key=study_key,
                        form_key=ctx.form_key,
                    )
                else:
                    _prune_hive_partition_files(
                        preexisting_files.get(ctx.form_key, []),
                        excluded_ids=superseded_ids[ctx.form_key],
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
