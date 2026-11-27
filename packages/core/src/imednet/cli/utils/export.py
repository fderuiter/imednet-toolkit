"""CLI utilities for exporting data to files."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import imednet.integrations.enrichment as _enrichment
from imednet.core.context import sdk_governance_context
from imednet.integrations.enrichment import (
    _matches_custom_phi_key,
    _to_snake_case,
    mask_custom_phi_recursive,
    resolve_enrichment_pipeline,
    resolve_study_configuration,
)
from imednet.utils import sanitize_csv_formula
from imednet.utils.security import (
    SensitivityRegistry,
    _normalize_key,
    build_study_sensitivity_registry,
    global_sensitivity_registry,
    mask_clinical_phi,
    sanitize_diagnostic_text,
)
from imednet.utils.serialization import flatten

from .output import print_error

_MASKED_VALUE = "***MASKED***"
_ORIG_RESOLVE_STUDY_CONFIG = resolve_study_configuration
_ORIG_RESOLVE_PIPELINE = resolve_enrichment_pipeline
_QUERY_MAPPING_KEYS = frozenset(
    {
        "query_comments",
        "queryComments",
        "annotation_id",
        "annotationId",
        "annotation_type",
        "annotationType",
    }
)


def _is_query_mapping(mapping: Mapping[Any, Any]) -> bool:
    """Return True if *mapping* represents a serialized Query payload."""
    return any(k in mapping for k in _QUERY_MAPPING_KEYS)


def _is_diagnostic_text_model_field(
    fname: str,
    model_obj: Any,
    model_fields: Mapping[str, Any],
) -> bool:
    """Return True if *fname* on *model_obj* holds clinical diagnostic/comment free text."""
    if fname in {"reason_for_change", "comment"}:
        return True
    if fname == "description":
        return type(model_obj).__name__ == "Query" or "query_comments" in model_fields
    return False


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
    phi_cache: dict[str, set[str]] | None = None,
    sdk: Any = None,
) -> dict[str, Any]:
    """Apply optional PHI masking and ``recordData`` flattening to a dumped model row."""
    row = dict(raw_row)
    effective_study = study_key or row.get("studyKey") or row.get("study_key")
    active_pipeline: Any = None
    resolved_phi_keys: set[str] = set()
    if isinstance(effective_study, str) and effective_study.strip():
        sk = effective_study.strip()
        if sk not in pipelines:
            pipelines[sk] = resolve_enrichment_pipeline(sk)
        active_pipeline = pipelines[sk]
        if mask_phi:
            if phi_cache is not None:
                if sk not in phi_cache:
                    phi_cache[sk] = resolve_cli_phi_fields(sk, sdk=sdk)
                resolved_phi_keys = set(phi_cache[sk])
            else:
                resolved_phi_keys = resolve_cli_phi_fields(sk, sdk=sdk)

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
            if resolved_phi_keys:
                masked_rec = mask_custom_phi_recursive(rec_dict, resolved_phi_keys)
                if isinstance(masked_rec, dict):
                    rec_dict = masked_rec
        elif not mask_phi and rec_dict and active_pipeline is not None:
            rec_dict = active_pipeline.process(rec_dict, mask_phi=False)
        if rec_dict:
            flat_record_data = flatten(rec_dict)

    if mask_phi:
        row = mask_clinical_phi(row, registry=global_sensitivity_registry)
        custom_phi_keys: set[str] = set(resolved_phi_keys)
        if active_pipeline is not None and getattr(active_pipeline, "phi_keys", None):
            custom_phi_keys.update(_normalize_custom_phi_keys(active_pipeline.phi_keys))
        study_registry = build_study_sensitivity_registry(custom_phi_keys)
        if custom_phi_keys:
            for key, val in list(row.items()):
                if global_sensitivity_registry.is_exempt(key):
                    continue
                if _matches_custom_phi_key(key, custom_phi_keys):
                    row[key] = _MASKED_VALUE
                elif isinstance(val, (Mapping, list, tuple)):
                    row[key] = mask_custom_phi_recursive(val, custom_phi_keys)
        diag_keys = ["reasonForChange", "reason_for_change", "comment"]
        if _is_query_mapping(row):
            diag_keys.append("description")
        for diag_key in diag_keys:
            diag_val = row.get(diag_key)
            if isinstance(diag_val, str) and diag_val != _MASKED_VALUE:
                row[diag_key] = sanitize_diagnostic_text(diag_val, registry=study_registry)
        for qc_key in ("queryComments", "query_comments"):
            qc_val = row.get(qc_key)
            if isinstance(qc_val, list):
                row[qc_key] = [
                    _mask_record_tree(
                        entry,
                        custom_phi_keys=custom_phi_keys,
                        active_registry=study_registry,
                    )
                    for entry in qc_val
                ]

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
    sdk: Any = None,
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
    phi_cache: dict[str, set[str]] = {}
    with sdk_governance_context(sdk):
        data = [
            _prepare_export_row(
                item.model_dump(by_alias=True),
                study_key=study_key,
                mask_phi=mask_phi,
                pipelines=pipelines,
                phi_cache=phi_cache,
                sdk=sdk,
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


def _normalize_custom_phi_keys(phi_fields: Collection[str] | None) -> set[str]:
    """Normalize study-configured ``phi_fields`` into canonical and collapsed keys."""
    if not phi_fields:
        return set()
    phi_keys: set[str] = set()
    for raw_key in phi_fields:
        if global_sensitivity_registry.is_exempt(raw_key):
            continue
        for candidate in (_normalize_key(raw_key), _normalize_key(_to_snake_case(str(raw_key)))):
            if candidate:
                phi_keys.add(candidate)
                phi_keys.add(candidate.replace("_", ""))
    return phi_keys


def resolve_cli_phi_fields(
    study_key: str | None,
    *,
    sdk: Any = None,
) -> set[str]:
    """Resolve normalized ``StudyConfiguration.phi_fields`` under *sdk* governance context."""
    if not isinstance(study_key, str) or not study_key.strip():
        return set()
    sk = study_key.strip()
    with sdk_governance_context(sdk):
        cfg = (
            resolve_study_configuration(sk)
            if resolve_study_configuration is not _ORIG_RESOLVE_STUDY_CONFIG
            else _enrichment.resolve_study_configuration(sk)
        )
        if cfg is not None:
            return _normalize_custom_phi_keys(getattr(cfg, "phi_fields", None))
        if resolve_enrichment_pipeline is not _ORIG_RESOLVE_PIPELINE:
            pipe = resolve_enrichment_pipeline(sk)
            if pipe is not None and getattr(pipe, "phi_keys", None):
                return _normalize_custom_phi_keys(pipe.phi_keys)
    return set()


def _copy_model_with_masked_extra(
    item: Any,
    model_copy: Any,
    updates: dict[str, Any],
    *,
    active_registry: SensitivityRegistry,
    custom_phi_keys: set[str],
) -> Any:
    """Copy *item* with *updates* and masked ``__pydantic_extra__``, keeping identity if clean."""
    extra = getattr(item, "__pydantic_extra__", None)
    masked_extra: dict[str, Any] | None = None
    if isinstance(extra, dict) and extra:
        cand = mask_clinical_phi(extra, registry=active_registry)
        if custom_phi_keys:
            cand = mask_custom_phi_recursive(cand, custom_phi_keys)
        diag_keys = ["reasonForChange", "reason_for_change", "comment"]
        if type(item).__name__ == "Query" or _is_query_mapping(extra):
            diag_keys.append("description")
        for rkey in diag_keys:
            rval = cand.get(rkey)
            if isinstance(rval, str) and rval != _MASKED_VALUE:
                cand[rkey] = sanitize_diagnostic_text(rval, registry=active_registry)
        if cand != extra:
            masked_extra = dict(cand)

    if not updates and masked_extra is None:
        return item

    copied = model_copy(update=updates)
    if masked_extra is not None:
        copied_extra = getattr(copied, "__pydantic_extra__", None)
        if isinstance(copied_extra, dict):
            object.__setattr__(copied, "__pydantic_extra__", {**copied_extra, **masked_extra})
    return copied


def _is_field_phi_sensitive(
    fname: str,
    alias: Any,
    *,
    active_registry: SensitivityRegistry,
    custom_phi_keys: set[str],
) -> bool:
    """Return True if a Pydantic field name or alias matches active PHI rules."""
    return bool(
        active_registry.is_sensitive(fname)
        or (isinstance(alias, str) and active_registry.is_sensitive(alias))
        or _matches_custom_phi_key(fname, custom_phi_keys)
        or (isinstance(alias, str) and _matches_custom_phi_key(alias, custom_phi_keys))
    )


def _mask_record_tree(
    value: Any,
    *,
    custom_phi_keys: set[str],
    active_registry: SensitivityRegistry,
) -> Any:
    """Mask PHI across nested record mappings, sequences, and models without flattening."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        rebuilt: dict[Any, Any] = {}
        changed = False
        is_query_map = _is_query_mapping(value)
        for k, v in value.items():
            key_str = str(k)
            if global_sensitivity_registry.is_sensitive(key_str) or _matches_custom_phi_key(
                key_str, custom_phi_keys
            ):
                rebuilt[k] = _MASKED_VALUE
                if type(v) is not str or v != _MASKED_VALUE:
                    changed = True
            elif isinstance(v, str) and (
                key_str in {"reason_for_change", "reasonForChange", "comment"}
                or (key_str == "description" and is_query_map)
            ):
                scrubbed = sanitize_diagnostic_text(v, registry=active_registry)
                rebuilt[k] = scrubbed
                if scrubbed != v:
                    changed = True
            else:
                masked_v = _mask_record_tree(
                    v,
                    custom_phi_keys=custom_phi_keys,
                    active_registry=active_registry,
                )
                rebuilt[k] = masked_v
                if masked_v is not v:
                    changed = True
        if custom_phi_keys:
            post_custom = mask_custom_phi_recursive(rebuilt, custom_phi_keys)
            if isinstance(post_custom, dict) and post_custom != rebuilt:
                rebuilt = post_custom
                changed = True
        return value if not changed and isinstance(value, dict) else rebuilt

    if isinstance(value, (list, tuple)):
        elems = [
            _mask_record_tree(
                elem, custom_phi_keys=custom_phi_keys, active_registry=active_registry
            )
            for elem in value
        ]
        if all(new_el is old_el for new_el, old_el in zip(elems, value, strict=False)):
            return value
        return tuple(elems) if isinstance(value, tuple) else elems

    model_fields = getattr(type(value), "model_fields", None)
    model_copy = getattr(value, "model_copy", None)
    if not isinstance(model_fields, Mapping) or not callable(model_copy):
        return value

    updates: dict[str, Any] = {}
    for fname, finfo in model_fields.items():
        raw_val = getattr(value, fname, None)
        if raw_val is None or active_registry.is_exempt(fname):
            continue
        if _is_field_phi_sensitive(
            fname,
            getattr(finfo, "alias", None),
            active_registry=active_registry,
            custom_phi_keys=custom_phi_keys,
        ):
            if isinstance(raw_val, str) and raw_val != _MASKED_VALUE:
                updates[fname] = _MASKED_VALUE
        elif isinstance(raw_val, str) and _is_diagnostic_text_model_field(
            fname, value, model_fields
        ):
            scrubbed = sanitize_diagnostic_text(raw_val, registry=active_registry)
            if scrubbed != raw_val:
                updates[fname] = scrubbed
        elif not isinstance(raw_val, (str, int, float, bool)):
            masked_val = _mask_record_tree(
                raw_val,
                custom_phi_keys=custom_phi_keys,
                active_registry=active_registry,
            )
            if masked_val is not raw_val:
                updates[fname] = masked_val

    return _copy_model_with_masked_extra(
        value,
        model_copy,
        updates,
        active_registry=active_registry,
        custom_phi_keys=custom_phi_keys,
    )


def _mask_cli_items(
    items: Sequence[Any],
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
    sdk: Any = None,
    custom_phi_fields: Collection[str] | None = None,
) -> list[Any]:
    """Mask PHI across model, mapping, or sequence *items* under *sdk* governance context."""
    if not mask_phi or not items:
        return list(items)

    precomputed = (
        _normalize_custom_phi_keys(custom_phi_fields) if custom_phi_fields is not None else None
    )
    phi_cache: dict[str, set[str]] = {}
    registries: dict[str, SensitivityRegistry] = {}
    out: list[Any] = []

    with sdk_governance_context(sdk):
        for item in items:
            if precomputed is not None:
                phi_keys = precomputed
                active_registry = build_study_sensitivity_registry(phi_keys)
            else:
                eff_study = study_key or (
                    (item.get("study_key") or item.get("studyKey"))
                    if isinstance(item, Mapping)
                    else getattr(item, "study_key", None)
                )
                phi_keys = set()
                active_registry = global_sensitivity_registry
                if isinstance(eff_study, str) and eff_study.strip():
                    sk = eff_study.strip()
                    if sk not in phi_cache:
                        phi_cache[sk] = resolve_cli_phi_fields(sk, sdk=sdk)
                    phi_keys = phi_cache[sk]
                    if phi_keys:
                        if sk not in registries:
                            registries[sk] = build_study_sensitivity_registry(phi_keys)
                        active_registry = registries[sk]

            out.append(
                _mask_record_tree(
                    item,
                    custom_phi_keys=phi_keys,
                    active_registry=active_registry,
                )
            )
    return out


def mask_cli_records(
    items: Sequence[Any],
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
    sdk: Any = None,
    custom_phi_fields: Collection[str] | None = None,
) -> list[Any]:
    """Return non-flattened PHI-masked copies of ``Record`` items when *mask_phi* is True."""
    return _mask_cli_items(
        items,
        study_key=study_key,
        mask_phi=mask_phi,
        sdk=sdk,
        custom_phi_fields=custom_phi_fields,
    )


def mask_cli_models(
    items: Sequence[Any],
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
    sdk: Any = None,
    custom_phi_fields: Collection[str] | None = None,
) -> list[Any]:
    """Return PHI-masked copies of model items for terminal rendering when *mask_phi* is True."""
    return _mask_cli_items(
        items,
        study_key=study_key,
        mask_phi=mask_phi,
        sdk=sdk,
        custom_phi_fields=custom_phi_fields,
    )
