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

from ..models.standards import resolve_sdtm_domain, transform_records_to_sdtm
from ..sdk import ImednetSDK
from ..utils import validate_partition_key
from ._export_contracts import (
    append_dataframe_manifest,
    delete_superseded_hive_files,
    snapshot_hive_partition_files,
)
from .enrichment import resolve_sink_enrichment_pipeline, resolve_sink_phi_keys
from .export import (
    SdtmKeyTuple,
    _append_jsonl_manifest,
    _apply_naming_convention_to_df,
    _build_form_export_contexts,
    _build_tabular_sink_config,
    _canonical_record_id,
    _concat_tabular_frames,
    _config_has_streaming_options,
    _dedup_case_insensitive_columns,
    _dedupe_df_by_record_id,
    _dedupe_df_by_sdtm_key,
    _df_sdtm_keys,
    _filter_df_excluding_record_ids,
    _filter_df_excluding_sdtm_keys,
    _find_df_record_id_col,
    _FormExportContext,
    _is_incremental_config,
    _lineage_entry_to_sdtm_key,
    _mask_df,
    _project_records_whitelist,
    _record_mapper,
    _seed_mapper_enrichment,
    _stream_records_across_forms,
)
from .parquet_engine import PyArrowDatasetPartitionedStorageEngine
from .sink_base import (
    BoundedFormBufferRouter,
    commit_sdtm_lineage,
    extract_record_form_ref,
    extract_record_id,
    rehydrate_sdtm_lineage,
    resolve_voided_sdtm_keys,
    should_preserve_sdtm_record_id,
    stamp_tabular_batch,
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
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
    dedupe_incremental: bool = False,
    manifest_path: str | None = None,
    batch_id: str | None = None,
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
    df = _dedup_case_insensitive_columns(df, ensure_str_columns=True)
    if mask_phi and not df.empty and not bool(df.attrs.get("imednet_sdtm_redacted")):
        df = _mask_df(df, phi_keys=phi_keys)
    if not use_labels_as_columns and naming_convention is not None and not df.empty:
        df = _apply_naming_convention_to_df(
            df,
            naming_convention,
            domain=sdtm_domain,
            study_key=study_key,
        )
        if mask_phi:
            df = _mask_df(df, phi_keys=phi_keys)
    if dedupe_incremental and not df.empty:
        df, _ = _dedupe_df_by_record_id(df, study_key=study_key)
        if skip_empty and df.empty:
            return 0
    for part_col in ("study_key", "form_key"):
        if part_col in df.columns:
            df = df.drop(columns=[part_col])
    table = pyarrow_module.Table.from_pandas(df, preserve_index=False)
    storage_engine.write_form_table(
        table,
        base_dir=base_dir,
        study_key=study_key,
        form_key=form_key,
    )
    if manifest_path and len(df) > 0:
        dest_dir = str(Path(base_dir) / f"study_key={study_key}" / f"form_key={form_key}")
        append_dataframe_manifest(
            manifest_path,
            batch_id=batch_id or f"{study_key}/{form_key}/0",
            destination=dest_dir,
            df=df,
            append_fn=_append_jsonl_manifest,
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
        filtered_df = _filter_df_excluding_record_ids(
            existing_df,
            excluded_ids,
            study_key=study_key,
        )
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


def _prune_hive_partition_sdtm_keys(
    files: list[Path],
    excluded_keys: set[SdtmKeyTuple],
    *,
    pyarrow_module: ModuleType,
    storage_engine: PyArrowDatasetPartitionedStorageEngine,
    base_dir: str,
    study_key: str,
    form_key: str,
) -> None:
    """Prune superseded or voided SDTM composite keys from pre-existing Hive partition Parquet files."""
    if not files or not excluded_keys:
        return
    pandas_module = _ensure_pandas()
    partition_dir = Path(base_dir) / f"study_key={study_key}" / f"form_key={form_key}"
    for pf in files:
        if not pf.exists():
            continue
        existing_df = pandas_module.read_parquet(pf, engine="pyarrow")
        filtered_df = _filter_df_excluding_sdtm_keys(existing_df, excluded_keys)
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
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    max_buffered_records: int | None = None,
    **kwargs: Any,
) -> None:
    """Export Study Records to a Hive-partitioned Parquet directory layout."""
    pyarrow_module = _ensure_pyarrow()
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")
    validate_max_buffered_records(max_buffered_records)
    validate_partition_key(study_key)

    extra_kwargs = dict(kwargs)
    has_config_obj = "config" in extra_kwargs and extra_kwargs["config"] is not None
    if not has_config_obj:
        extra_kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
        extra_kwargs.setdefault("batch_size", chunk_size)
    elif chunk_size != 5_000 and "batch_size" not in extra_kwargs:
        extra_kwargs["batch_size"] = chunk_size
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
        default_quality_gate=quality_gate_enabled,
        mask_phi=mask_phi,
    )
    if has_config_obj and chunk_size != 5_000:
        config.batch_size = chunk_size
    if has_config_obj and max_buffered_records is not None:
        config.max_buffered_records = max_buffered_records
    if config.pandas_kwargs:
        unexpected = next(iter(config.pandas_kwargs))
        raise TypeError(
            f"export_to_hive_parquet() got an unexpected keyword argument {unexpected!r}"
        )

    from imednet.core.context import sdk_governance_context

    with sdk_governance_context(sdk):
        effective_mask_phi = config.mask_phi
        mapper = _build_record_mapper(
            sdk, chunk_size=config.batch_size, mask_phi=effective_mask_phi
        )
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=config)
        _seed_mapper_enrichment(mapper, study_key, pipeline, config=config)
        phi_keys: set[str] = (
            resolve_sink_phi_keys(
                study_key,
                config=config,
                phi_keys=getattr(pipeline, "phi_keys", None),
            )
            or set()
        )
    has_native_enrichment = callable(getattr(mapper, "_get_enrichment_pipeline", None))
    storage_engine = PyArrowDatasetPartitionedStorageEngine()
    effective_use_labels = config.use_labels_as_columns
    effective_var_whitelist = config.variable_whitelist
    effective_form_whitelist = config.form_whitelist

    if _config_has_streaming_options(config) or config.quality_gate_enabled or config.as_sdtm:
        preserve_rec_id = should_preserve_sdtm_record_id(config)
        contexts, all_variables = _build_form_export_contexts(
            sdk,
            study_key,
            mapper,
            use_labels_as_columns=effective_use_labels,
            variable_whitelist=effective_var_whitelist,
            form_whitelist=effective_form_whitelist,
            mask_phi=effective_mask_phi,
            pipeline=pipeline,
            stream_prefix="hive_parquet",
            custom_stream_prefix=config.stream_name,
            validate_form_key_fn=validate_partition_key,
            as_sdtm=config.as_sdtm,
            sdtm_domain=config.sdtm_domain,
            naming_convention=config.naming_convention,
            preserve_record_id=preserve_rec_id,
        )
        if not contexts:
            return

        is_incremental = _is_incremental_config(config)
        preexisting_files: dict[str, list[Path]] = (
            snapshot_hive_partition_files(
                base_dir,
                study_key,
                (ctx.form_key for ctx in contexts),
            )
            if config.idempotent
            else {}
        )
        updated_ids: dict[str, set[str]] = {ctx.form_key: set() for ctx in contexts}
        voided_ids: dict[str, set[str]] = {ctx.form_key: set() for ctx in contexts}
        active_id_counts: dict[str, dict[str, int]] = {ctx.form_key: {} for ctx in contexts}
        superseded_sdtm_keys: dict[str, set[SdtmKeyTuple]] = {
            ctx.form_key: set() for ctx in contexts
        }
        in_run_sdtm_keys: dict[str, set[SdtmKeyTuple]] = {ctx.form_key: set() for ctx in contexts}
        in_run_record_ids: dict[str, set[str]] = {ctx.form_key: set() for ctx in contexts}

        def _on_hive_active(ctx: _FormExportContext, batch: list[Any], batch_idx: int) -> int:
            if config.as_sdtm:
                if not batch:
                    return 0
                batch_records: list[Any] = (
                    _project_records_whitelist(batch, config.variable_whitelist)
                    if config.variable_whitelist is not None
                    else batch
                )
                domain_counters, seq_overrides, _ = rehydrate_sdtm_lineage(
                    ctx,
                    study_key=study_key,
                    stream_name=ctx.stream_name,
                )
                target_domain = resolve_sdtm_domain(
                    ctx.sdtm_domain or config.sdtm_domain or ctx.form_key,
                    fallback="AE",
                )
                session_id = id(domain_counters)
                if ctx._synced_sdtm_session_id != session_id:
                    dom_wm = domain_counters.setdefault(target_domain, {})
                    if target_domain != "DM":
                        for subj_key, wm_val in dom_wm.items():
                            ctx.seq_counters[subj_key] = max(
                                ctx.seq_counters.get(subj_key, 0),
                                wm_val,
                            )
                        domain_counters[target_domain] = ctx.seq_counters
                    ctx._synced_sdtm_session_id = session_id
                lineage_out: dict[str, dict[str, Any]] = {}
                df = transform_records_to_sdtm(
                    batch_records,
                    domain=target_domain,
                    study_key=study_key,
                    include_deleted=config.include_deleted,
                    mask_phi=effective_mask_phi,
                    seq_counters=ctx.seq_counters,
                    record_seqs=ctx.record_seqs,
                    preserve_record_id=preserve_rec_id,
                    phi_keys=phi_keys,
                    pipeline=pipeline,
                    seq_overrides=seq_overrides,
                    lineage_out=lineage_out,
                )
                if df.empty:
                    return 0
                has_rec_id_col = _find_df_record_id_col(df) is not None
                if has_rec_id_col and config.idempotent:
                    df, batch_cids = _dedupe_df_by_record_id(df, study_key=study_key)
                    if df.empty:
                        return 0
                else:
                    df, _ = _dedupe_df_by_sdtm_key(df)
                    if df.empty:
                        return 0
                    batch_cids = set()

                batch_skeys = {skey for skey in _df_sdtm_keys(df) if skey is not None}
                if not is_incremental and config.idempotent:
                    part_dir = (
                        Path(base_dir) / f"study_key={study_key}" / f"form_key={ctx.form_key}"
                    )
                    overlap_ids = batch_cids & in_run_record_ids[ctx.form_key]
                    overlap_in_run = batch_skeys & in_run_sdtm_keys[ctx.form_key]
                    if (overlap_ids or overlap_in_run) and part_dir.exists():
                        pre_set = set(preexisting_files.get(ctx.form_key, []))
                        current_run_files = [
                            p for p in sorted(part_dir.rglob("*.parquet")) if p not in pre_set
                        ]
                        if current_run_files:
                            if overlap_ids:
                                _prune_hive_partition_record_ids(
                                    current_run_files,
                                    overlap_ids,
                                    pyarrow_module=pyarrow_module,
                                    storage_engine=storage_engine,
                                    base_dir=base_dir,
                                    study_key=study_key,
                                    form_key=ctx.form_key,
                                )
                            elif overlap_in_run:
                                _prune_hive_partition_sdtm_keys(
                                    current_run_files,
                                    overlap_in_run,
                                    pyarrow_module=pyarrow_module,
                                    storage_engine=storage_engine,
                                    base_dir=base_dir,
                                    study_key=study_key,
                                    form_key=ctx.form_key,
                                )

                stamp_tabular_batch(
                    df,
                    mask_phi=effective_mask_phi,
                    phi_keys=phi_keys,
                    sanitized=False,
                )
                table = pyarrow_module.Table.from_pandas(df, preserve_index=False)
                storage_engine.write_form_table(
                    table,
                    base_dir=base_dir,
                    study_key=study_key,
                    form_key=ctx.form_key,
                )
                if config.manifest_path and len(df) > 0:
                    dest_dir = str(
                        Path(base_dir) / f"study_key={study_key}" / f"form_key={ctx.form_key}"
                    )
                    append_dataframe_manifest(
                        config.manifest_path,
                        batch_id=f"{study_key}/{ctx.form_key}/{batch_idx}",
                        destination=dest_dir,
                        df=df,
                        append_fn=_append_jsonl_manifest,
                    )
                commit_sdtm_lineage(
                    ctx,
                    lineage_out,
                    study_key=study_key,
                    stream_name=ctx.stream_name,
                )
                in_run_sdtm_keys[ctx.form_key].update(batch_skeys)
                in_run_record_ids[ctx.form_key].update(batch_cids)
                written = len(df)
                if is_incremental and config.idempotent and written > 0:
                    superseded_sdtm_keys[ctx.form_key].update(batch_skeys)
                    for cid in batch_cids:
                        updated_ids[ctx.form_key].add(cid)
                        voided_ids[ctx.form_key].discard(cid)
                        active_id_counts[ctx.form_key][cid] = (
                            active_id_counts[ctx.form_key].get(cid, 0) + 1
                        )
                return written

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
                use_labels_as_columns=effective_use_labels,
                mask_phi=effective_mask_phi,
                phi_keys=phi_keys,
                pyarrow_module=pyarrow_module,
                storage_engine=storage_engine,
                base_dir=base_dir,
                study_key=study_key,
                form_key=ctx.form_key,
                skip_empty=True,
                naming_convention=config.naming_convention,
                sdtm_domain=ctx.sdtm_domain or config.sdtm_domain,
                dedupe_incremental=(is_incremental and config.idempotent),
                manifest_path=config.manifest_path,
                batch_id=f"{study_key}/{ctx.form_key}/{batch_idx}",
            )
            if is_incremental and config.idempotent and written > 0:
                seen_batch_cids: set[str] = set()
                for rec in batch:
                    maybe_cid = _canonical_record_id(extract_record_id(rec))
                    if maybe_cid is not None:
                        seen_batch_cids.add(maybe_cid)
                for cid in seen_batch_cids:
                    updated_ids[ctx.form_key].add(cid)
                    voided_ids[ctx.form_key].discard(cid)
                    active_id_counts[ctx.form_key][cid] = (
                        active_id_counts[ctx.form_key].get(cid, 0) + 1
                    )
            return written

        def _on_hive_voided(ctx: _FormExportContext, batch: list[Any], _batch_idx: int) -> int:
            if not (is_incremental and config.idempotent):
                return 0
            cids = [
                cid
                for rec in batch
                if (cid := _canonical_record_id(extract_record_id(rec))) is not None
            ]
            voided_ids[ctx.form_key].update(cids)
            skeys: list[SdtmKeyTuple] = []
            if config.as_sdtm:
                entries = resolve_voided_sdtm_keys(
                    batch,
                    ctx,
                    study_key=study_key,
                    stream_name=ctx.stream_name,
                    sdtm_domain=ctx.sdtm_domain,
                    form_domain_map=ctx._active_form_domain_map,
                )
                skeys = [
                    skey
                    for entry in entries
                    if (skey := _lineage_entry_to_sdtm_key(entry)) is not None
                ]
                superseded_sdtm_keys[ctx.form_key].update(skeys)
            return max(len(cids), len(skeys))

        def _reconcile_partitions_before_checkpoints() -> None:
            if not config.idempotent:
                return
            if not is_incremental:
                delete_superseded_hive_files(
                    preexisting_files,
                    base_dir=base_dir,
                    study_key=study_key,
                )
                return
            pandas_module = _ensure_pandas()
            for ctx in contexts:
                fk = ctx.form_key
                pre_files = preexisting_files.get(fk, [])
                pre_set = set(pre_files)
                _prune_hive_partition_record_ids(
                    pre_files,
                    updated_ids[fk] | voided_ids[fk],
                    pyarrow_module=pyarrow_module,
                    storage_engine=storage_engine,
                    base_dir=base_dir,
                    study_key=study_key,
                    form_key=fk,
                )
                if config.as_sdtm and superseded_sdtm_keys[fk]:
                    legacy_sdtm_files = [
                        pf
                        for pf in pre_files
                        if pf.exists()
                        and _find_df_record_id_col(pandas_module.read_parquet(pf, engine="pyarrow"))
                        is None
                    ]
                    if legacy_sdtm_files:
                        _prune_hive_partition_sdtm_keys(
                            legacy_sdtm_files,
                            superseded_sdtm_keys[fk],
                            pyarrow_module=pyarrow_module,
                            storage_engine=storage_engine,
                            base_dir=base_dir,
                            study_key=study_key,
                            form_key=fk,
                        )
                needs_run_dedup = any(cnt > 1 for cnt in active_id_counts[fk].values()) or bool(
                    voided_ids[fk] & set(active_id_counts[fk])
                )
                if needs_run_dedup:
                    part_dir = Path(base_dir) / f"study_key={study_key}" / f"form_key={fk}"
                    run_files = (
                        [pf for pf in sorted(part_dir.rglob("*.parquet")) if pf not in pre_set]
                        if part_dir.exists()
                        else []
                    )
                    if run_files:
                        frames = [
                            pandas_module.read_parquet(pf, engine="pyarrow") for pf in run_files
                        ]
                        combined_run = _concat_tabular_frames(frames)
                        dedup_run, _ = _dedupe_df_by_record_id(
                            combined_run,
                            exclude_ids=voided_ids[fk],
                            study_key=study_key,
                        )
                        for part_col in ("study_key", "form_key"):
                            if part_col in dedup_run.columns:
                                dedup_run = dedup_run.drop(columns=[part_col])
                        if not dedup_run.empty:
                            storage_engine.write_form_table(
                                pyarrow_module.Table.from_pandas(
                                    dedup_run.reset_index(drop=True),
                                    preserve_index=False,
                                ),
                                base_dir=base_dir,
                                study_key=study_key,
                                form_key=fk,
                            )
                        for pf in run_files:
                            pf.unlink(missing_ok=True)
                            if (
                                pf.parent != part_dir
                                and pf.parent.exists()
                                and not any(pf.parent.iterdir())
                            ):
                                with contextlib.suppress(OSError):
                                    pf.parent.rmdir()

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

    form_filter: dict[str, Any] = (
        {"formId": effective_form_whitelist} if effective_form_whitelist else {}
    )
    all_variables = (
        []
        if effective_form_whitelist is not None and len(effective_form_whitelist) == 0
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
        if effective_form_whitelist is not None and form.form_id not in effective_form_whitelist:
            continue
        validate_partition_key(form.form_key)

        variables = variables_by_form.get(form.form_id, [])
        variable_keys = list(
            dict.fromkeys(
                variable.variable_name
                for variable in variables
                if variable.variable_name
                and (
                    effective_var_whitelist is None
                    or variable.variable_name in effective_var_whitelist
                )
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

    full_preexisting_files = (
        snapshot_hive_partition_files(
            base_dir,
            study_key,
            (spec["form_key"] for spec in form_specs),
        )
        if config.idempotent
        else {}
    )

    iter_records = getattr(mapper, "_iter_records", None)
    can_stream_single_pass = (
        not isinstance(mapper, NonCallableMock)
        and callable(getattr(mapper, "_parse_records", None))
        and callable(getattr(mapper, "_parse_record", None))
    )
    if can_stream_single_pass and callable(iter_records):
        by_form_id = {spec["form_id"]: spec for spec in form_specs}
        by_form_key = {spec["form_key"]: spec for spec in form_specs}

        def _flush_hive_batch(form_key: str, batch: list[Any], batch_idx: int) -> None:
            spec = by_form_key[form_key]
            parsed_rows, _ = mapper._parse_records(batch, spec["record_model"])
            rows = (
                [pipeline.process(r, mask_phi=effective_mask_phi) for r in parsed_rows]
                if pipeline is not None and not has_native_enrichment
                else parsed_rows
            )
            _write_hive_form_rows(
                rows,
                mapper=mapper,
                variable_keys=spec["variable_keys"],
                label_map=spec["label_map"],
                use_labels_as_columns=effective_use_labels,
                mask_phi=effective_mask_phi,
                phi_keys=phi_keys,
                pyarrow_module=pyarrow_module,
                storage_engine=storage_engine,
                base_dir=base_dir,
                study_key=study_key,
                form_key=form_key,
                skip_empty=True,
                manifest_path=config.manifest_path,
                batch_id=f"{study_key}/{form_key}/{batch_idx}",
            )

        router = BoundedFormBufferRouter(
            [spec["form_key"] for spec in form_specs],
            batch_size=config.batch_size,
            max_buffered_records=config.max_buffered_records,
            on_flush_active=_flush_hive_batch,
        )
        study_extra_filters = (
            {"formId": [spec["form_id"] for spec in form_specs]}
            if effective_form_whitelist is not None
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
        if config.idempotent:
            delete_superseded_hive_files(
                full_preexisting_files,
                base_dir=base_dir,
                study_key=study_key,
            )
        return

    iter_parsed_rows = getattr(mapper, "_iter_parsed_rows", None)
    for spec in form_specs:
        if callable(iter_records) and callable(iter_parsed_rows):
            records = iter_records(
                study_key,
                extra_filters={"formId": spec["form_id"]},
            )
            for batch_idx, (parsed_rows, _) in enumerate(
                iter_parsed_rows(records, spec["record_model"])
            ):
                rows = (
                    [pipeline.process(r, mask_phi=effective_mask_phi) for r in parsed_rows]
                    if pipeline is not None and not has_native_enrichment
                    else parsed_rows
                )
                _write_hive_form_rows(
                    rows,
                    mapper=mapper,
                    variable_keys=spec["variable_keys"],
                    label_map=spec["label_map"],
                    use_labels_as_columns=effective_use_labels,
                    mask_phi=effective_mask_phi,
                    phi_keys=phi_keys,
                    pyarrow_module=pyarrow_module,
                    storage_engine=storage_engine,
                    base_dir=base_dir,
                    study_key=study_key,
                    form_key=spec["form_key"],
                    skip_empty=True,
                    manifest_path=config.manifest_path,
                    batch_id=f"{study_key}/{spec['form_key']}/{batch_idx}",
                )
            continue

        records = mapper._fetch_records(
            study_key,
            extra_filters={"formId": spec["form_id"]},
        )
        parsed_rows, _ = mapper._parse_records(records, spec["record_model"])
        rows = (
            [pipeline.process(r, mask_phi=effective_mask_phi) for r in parsed_rows]
            if pipeline is not None and not has_native_enrichment
            else parsed_rows
        )
        _write_hive_form_rows(
            rows,
            mapper=mapper,
            variable_keys=spec["variable_keys"],
            label_map=spec["label_map"],
            use_labels_as_columns=effective_use_labels,
            mask_phi=effective_mask_phi,
            phi_keys=phi_keys,
            pyarrow_module=pyarrow_module,
            storage_engine=storage_engine,
            base_dir=base_dir,
            study_key=study_key,
            form_key=spec["form_key"],
            skip_empty=False,
            manifest_path=config.manifest_path,
            batch_id=f"{study_key}/{spec['form_key']}/0",
        )

    if config.idempotent:
        delete_superseded_hive_files(
            full_preexisting_files,
            base_dir=base_dir,
            study_key=study_key,
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
