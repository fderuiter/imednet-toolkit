"""CLI utilities for exporting data to files."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeVar

from imednet.integrations.enrichment import (
    _matches_custom_phi_key,
    mask_custom_phi_recursive,
    resolve_enrichment_pipeline,
)
from imednet.utils import sanitize_csv_formula
from imednet.utils.security import (
    _normalize_key,
    global_sensitivity_registry,
    mask_clinical_phi,
)
from imednet.utils.serialization import flatten

from .output import print_error

_MASKED_VALUE = "***MASKED***"
_T = TypeVar("_T")


def _normalize_custom_phi_keys(raw_fields: Collection[str]) -> set[str]:
    """Normalize custom ``phiFields`` into canonical snake_case and collapsed keys."""
    normalized: set[str] = set()
    for raw_key in raw_fields:
        if global_sensitivity_registry.is_exempt(raw_key):
            continue
        norm_key = _normalize_key(raw_key)
        if norm_key:
            normalized.add(norm_key)
            normalized.add(norm_key.replace("_", ""))
    return normalized


def _resolve_study_phi_keys(
    study_key: str | None,
    pipelines: dict[str, Any],
) -> set[str]:
    """Resolve and cache study-configured ``phiFields`` keys for *study_key*."""
    if not isinstance(study_key, str) or not study_key.strip():
        return set()
    sk = study_key.strip()
    if sk not in pipelines:
        pipelines[sk] = resolve_enrichment_pipeline(sk)
    active_pipeline = pipelines[sk]
    if active_pipeline is None:
        return set()
    return set(getattr(active_pipeline, "phi_keys", ()) or ())


def _is_phi_key(key: str, custom_phi_keys: set[str]) -> bool:
    """Return True if *key* matches default clinical PHI or study-configured ``phiFields``."""
    if global_sensitivity_registry.is_exempt(key):
        return False
    if global_sensitivity_registry.is_sensitive(key):
        return True
    return bool(custom_phi_keys and _matches_custom_phi_key(key, custom_phi_keys))


def _is_plain_container(value: Any) -> bool:
    """Return True if *value* is a dict/list/tuple tree with no nested Pydantic models."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, dict):
        return all(_is_plain_container(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_is_plain_container(item) for item in value)
    return False


def _sanitize_value_recursive(value: Any, *, custom_phi_keys: set[str]) -> Any:
    """Recursively mask PHI across Pydantic models, mappings, and sequences."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        if isinstance(value, dict) and _is_plain_container(value):
            masked_dict = mask_clinical_phi(value, registry=global_sensitivity_registry)
            if custom_phi_keys:
                masked_dict = mask_custom_phi_recursive(masked_dict, custom_phi_keys)
            return value if masked_dict == value else masked_dict

        changed = False
        rebuilt: dict[Any, Any] = {}
        for k, v in value.items():
            if _is_phi_key(str(k), custom_phi_keys):
                rebuilt[k] = _MASKED_VALUE
                if v != _MASKED_VALUE:
                    changed = True
            else:
                masked_v = _sanitize_value_recursive(v, custom_phi_keys=custom_phi_keys)
                rebuilt[k] = masked_v
                if masked_v is not v and masked_v != v:
                    changed = True
        if not changed and isinstance(value, dict):
            return value
        return rebuilt

    if isinstance(value, list):
        masked_list = [
            _sanitize_value_recursive(item, custom_phi_keys=custom_phi_keys) for item in value
        ]
        if all(
            new_item is old_item for new_item, old_item in zip(masked_list, value, strict=False)
        ):
            return value
        return masked_list

    if isinstance(value, tuple):
        masked_tuple = tuple(
            _sanitize_value_recursive(item, custom_phi_keys=custom_phi_keys) for item in value
        )
        if all(
            new_item is old_item for new_item, old_item in zip(masked_tuple, value, strict=False)
        ):
            return value
        return masked_tuple

    model_fields = getattr(type(value), "model_fields", None)
    model_copy = getattr(value, "model_copy", None)
    if isinstance(model_fields, Mapping) and callable(model_copy):
        updates: dict[str, Any] = {}
        for fname, finfo in model_fields.items():
            raw_val = getattr(value, fname, None)
            if raw_val is None:
                continue
            alias = getattr(finfo, "alias", None)
            if _is_phi_key(fname, custom_phi_keys) or (
                isinstance(alias, str) and _is_phi_key(alias, custom_phi_keys)
            ):
                if isinstance(raw_val, str) and raw_val != _MASKED_VALUE:
                    updates[fname] = _MASKED_VALUE
                continue
            masked_val = _sanitize_value_recursive(raw_val, custom_phi_keys=custom_phi_keys)
            if masked_val is not raw_val and masked_val != raw_val:
                updates[fname] = masked_val

        extra = getattr(value, "__pydantic_extra__", None)
        masked_extra: dict[str, Any] | None = None
        if isinstance(extra, dict) and extra:
            candidate_extra = _sanitize_value_recursive(extra, custom_phi_keys=custom_phi_keys)
            if candidate_extra is not extra and candidate_extra != extra:
                masked_extra = dict(candidate_extra)

        if not updates and masked_extra is None:
            return value

        copied = model_copy(update=updates)
        if masked_extra is not None:
            copied_extra = getattr(copied, "__pydantic_extra__", None)
            if isinstance(copied_extra, dict):
                copied_extra = dict(copied_extra)
                copied_extra.update(masked_extra)
                object.__setattr__(copied, "__pydantic_extra__", copied_extra)
        return copied

    return value


def sanitize_cli_phi(
    payload: _T,
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
    custom_phi_fields: Collection[str] | None = None,
) -> _T:
    """Apply default ``mask_clinical_phi`` and ``StudyConfiguration`` ``phiFields`` to *payload*.

    Preserves Pydantic model instances and object identity when unchanged, and returns
    *payload* untouched when ``mask_phi=False``.
    """
    if not mask_phi:
        return payload
    pipelines: dict[str, Any] = {}
    effective_study = study_key
    if effective_study is None:
        if isinstance(payload, Mapping):
            raw_sk = payload.get("studyKey") or payload.get("study_key")
            if isinstance(raw_sk, str):
                effective_study = raw_sk
        else:
            raw_sk = getattr(payload, "study_key", None) or getattr(payload, "studyKey", None)
            if isinstance(raw_sk, str):
                effective_study = raw_sk
    custom_keys = (
        _normalize_custom_phi_keys(custom_phi_fields)
        if custom_phi_fields is not None
        else _resolve_study_phi_keys(effective_study, pipelines)
    )
    return _sanitize_value_recursive(payload, custom_phi_keys=custom_keys)  # type: ignore[no-any-return]


def sanitize_cli_records(
    records: Sequence[_T],
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
) -> list[_T]:
    """Sanitize a sequence of ``Record`` models or dicts for CLI JSON and interactive views."""
    if not mask_phi:
        return list(records)
    pipelines: dict[str, Any] = {}
    base_phi_keys = _resolve_study_phi_keys(study_key, pipelines)
    sanitized: list[_T] = []
    for item in records:
        item_phi_keys = base_phi_keys
        if not study_key:
            raw_sk = (
                (item.get("studyKey") or item.get("study_key"))
                if isinstance(item, Mapping)
                else (getattr(item, "study_key", None) or getattr(item, "studyKey", None))
            )
            if isinstance(raw_sk, str):
                item_phi_keys = _resolve_study_phi_keys(raw_sk, pipelines)
        sanitized.append(_sanitize_value_recursive(item, custom_phi_keys=item_phi_keys))
    return sanitized


class Model(Protocol):
    """Protocol for objects that can be dumped to a dictionary."""

    def model_dump(self, *, by_alias: bool = False) -> dict[str, Any]:
        """Dump the model to a dictionary."""
        ...


def resolve_records_export_target(
    output: str,
    filename_prefix: str = "records",
) -> tuple[str, Path] | None:
    """Resolve an ``-o/--output`` token to ``(format_name, destination_path)``."""
    raw = output.strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower in {"csv", "json"}:
        return lower, Path(f"{filename_prefix}.{lower}")
    candidate = Path(raw)
    suffix = candidate.suffix.lower()
    if suffix in {".csv", ".json"} and candidate.name != candidate.suffix:
        if candidate.exists() and candidate.is_dir():
            return None
        return suffix.lstrip("."), candidate
    return None


def _prepare_export_row(
    raw_row: dict[str, Any],
    *,
    study_key: str | None,
    mask_phi: bool,
    pipelines: dict[str, Any],
) -> dict[str, Any]:
    """Apply optional PHI masking and ``recordData`` flattening to a dumped model row."""
    row = dict(raw_row)
    effective_study = study_key or row.get("studyKey") or row.get("study_key")
    active_pipeline: Any = None
    if isinstance(effective_study, str) and effective_study.strip():
        sk = effective_study.strip()
        if sk not in pipelines:
            pipelines[sk] = resolve_enrichment_pipeline(sk)
        active_pipeline = pipelines[sk]

    record_data_key: str | None = None
    if "recordData" in row and (
        row["recordData"] is None or isinstance(row["recordData"], Mapping)
    ):
        record_data_key = "recordData"
    elif "record_data" in row and (
        row["record_data"] is None or isinstance(row["record_data"], Mapping)
    ):
        record_data_key = "record_data"

    flat_record_data: dict[str, Any] = {}
    if record_data_key is not None:
        raw_record_data = row.pop(record_data_key)
        rec_dict = dict(raw_record_data) if isinstance(raw_record_data, Mapping) else {}
        if mask_phi and rec_dict:
            rec_dict = mask_clinical_phi(rec_dict, registry=global_sensitivity_registry)
            if active_pipeline is not None:
                rec_dict = active_pipeline.process(rec_dict, mask_phi=True)
        elif not mask_phi and rec_dict and active_pipeline is not None:
            rec_dict = active_pipeline.process(rec_dict, mask_phi=False)
        if rec_dict:
            flat_record_data = flatten(rec_dict)

    if mask_phi:
        row = mask_clinical_phi(row, registry=global_sensitivity_registry)
        custom_phi_keys: set[str] = (
            set(active_pipeline.phi_keys) if active_pipeline is not None else set()
        )
        if custom_phi_keys:
            for key, val in list(row.items()):
                if global_sensitivity_registry.is_exempt(key):
                    continue
                if _matches_custom_phi_key(key, custom_phi_keys):
                    row[key] = _MASKED_VALUE
                elif isinstance(val, (Mapping, list, tuple)):
                    row[key] = mask_custom_phi_recursive(val, custom_phi_keys)

    for var_key, var_val in flat_record_data.items():
        target_key = f"recordData.{var_key}" if var_key in row else var_key
        row[target_key] = var_val

    return row


def export_list_to_file(
    items: Sequence[Model],
    filename_prefix: str,
    output_format: str,
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
) -> None:
    """Export a list of Pydantic models to a CSV or JSON file."""
    resolved = resolve_records_export_target(output_format, filename_prefix)
    if resolved is None:
        print_error(
            f"Error: Invalid output format: '{output_format.lower()}'. Supported: csv, json. "
            "Explicit .csv and .json file paths are also supported."
        )
        sys.exit(1)

    fmt, path = resolved
    path.parent.mkdir(parents=True, exist_ok=True)

    pipelines: dict[str, Any] = {}
    data = [
        _prepare_export_row(
            item.model_dump(by_alias=True),
            study_key=study_key,
            mask_phi=mask_phi,
            pipelines=pipelines,
        )
        for item in items
    ]

    if fmt == "csv":
        if data:
            for row in data:
                for k, v in row.items():
                    row[k] = sanitize_csv_formula(v)

            keys = list(dict.fromkeys(k for row in data for k in row))
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(data)
        else:
            path.touch()
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

    print(f"Saved {len(items)} {filename_prefix} to {path}")
