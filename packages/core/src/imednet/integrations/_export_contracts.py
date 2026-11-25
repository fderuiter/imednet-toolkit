"""Shared contract helpers for callable export barrels, manifests, whitelists, and Hive partitions."""

from __future__ import annotations

import contextlib
import hashlib
import sys
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .sink_base import SinkConfig, extract_record_form_ref

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK


class _CallableExportModule(types.ModuleType):
    """Module subclass delegating direct calls to :func:`imednet.integrations.dispatcher.export`."""

    def __call__(
        self,
        target: str,
        sdk: ImednetSDK,
        study_key: str,
        *,
        config: SinkConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        """Invoke the unified export dispatcher while preserving submodule attributes."""
        from .dispatcher import export as _dispatch_export

        return _dispatch_export(target, sdk, study_key, config=config, **kwargs)


def install_callable_export_module(module_name: str) -> None:
    """Upgrade ``sys.modules[module_name]`` in place to :class:`_CallableExportModule`."""
    mod = sys.modules.get(module_name)
    if mod is not None and not isinstance(mod, _CallableExportModule):
        mod.__class__ = _CallableExportModule


def resolve_config_manifest_path(
    config: Any,
    explicit_manifest_path: str | None = None,
) -> str | None:
    """Resolve ``manifest_path`` from explicit args, ``config.manifest_path``, or ``config.extra``."""
    if explicit_manifest_path is not None:
        return explicit_manifest_path
    cfg_manifest = getattr(config, "manifest_path", None)
    if cfg_manifest:
        return str(cfg_manifest)
    extra = getattr(config, "extra", None)
    if isinstance(extra, Mapping) and extra.get("manifest_path"):
        return str(extra["manifest_path"])
    return None


def record_matches_form_whitelist(record: Any, allowed_forms: set[int]) -> bool:
    """Return True when ``record`` belongs to one of the integer Form IDs in ``allowed_forms``."""
    fid_norm, _ = extract_record_form_ref(record)
    if fid_norm is not None:
        return fid_norm in allowed_forms
    if isinstance(record, Mapping):
        raw_fid = record.get("form_id") if "form_id" in record else record.get("formId")
    else:
        raw_fid = getattr(record, "form_id", getattr(record, "formId", None))
    return raw_fid in allowed_forms


def filter_records_by_form_whitelist(
    records: Sequence[Any],
    form_whitelist: Sequence[int] | set[int] | None,
) -> Sequence[Any]:
    """Filter ``records`` to ``form_whitelist`` when configured, returning ``records`` unchanged otherwise."""
    if form_whitelist is None:
        return records
    allowed_forms = set(form_whitelist)
    if not allowed_forms:
        return []
    return [r for r in records if record_matches_form_whitelist(r, allowed_forms)]


def append_dataframe_manifest(
    manifest_path: str | None,
    *,
    batch_id: str,
    destination: str,
    df: Any,
    append_fn: Callable[..., None],
) -> None:
    """Compute SHA-256 CSV checksum for a non-empty DataFrame and emit a JSONL manifest entry."""
    if not manifest_path:
        return
    row_count = len(df)
    if row_count <= 0:
        return
    csv_payload = df.to_csv(index=False) if hasattr(df, "to_csv") else str(df)
    checksum = hashlib.sha256(csv_payload.encode("utf-8")).hexdigest()
    append_fn(
        manifest_path,
        batch_id=batch_id,
        destination=destination,
        row_count=row_count,
        checksum=checksum,
    )


def snapshot_hive_partition_files(
    base_dir: str,
    study_key: str,
    form_keys: Iterable[str],
) -> dict[str, list[Path]]:
    """Snapshot pre-existing Parquet files per form partition prior to an idempotent run."""
    base_path = Path(base_dir)
    snapshots: dict[str, list[Path]] = {}
    for fk in form_keys:
        part_dir = base_path / f"study_key={study_key}" / f"form_key={fk}"
        snapshots[fk] = sorted(part_dir.rglob("*.parquet")) if part_dir.exists() else []
    return snapshots


def delete_superseded_hive_files(
    preexisting_files: Mapping[str, list[Path]],
    *,
    base_dir: str,
    study_key: str,
) -> None:
    """Delete superseded pre-existing partition files after full idempotent commits succeed."""
    base_path = Path(base_dir)
    for fk, files in preexisting_files.items():
        if not files:
            continue
        partition_dir = base_path / f"study_key={study_key}" / f"form_key={fk}"
        for pf in files:
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
