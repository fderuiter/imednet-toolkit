"""CLI utilities for exporting data to files."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from imednet.integrations.enrichment import (
    _matches_custom_phi_key,
    mask_custom_phi_recursive,
    resolve_enrichment_pipeline,
)
from imednet.utils import sanitize_csv_formula
from imednet.utils.security import (
    SensitivityRegistry,
    global_sensitivity_registry,
    mask_clinical_phi,
    sanitize_diagnostic_text,
)
from imednet.utils.serialization import flatten

from .output import print_error

_MASKED_VALUE = "***MASKED***"


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
        study_registry = global_sensitivity_registry
        if custom_phi_keys:
            study_registry = SensitivityRegistry()
            sens_keys, exempt_keys = global_sensitivity_registry.snapshot()
            for ex_key in exempt_keys:
                study_registry.add_exempt_key(ex_key)
            for sens_key in sens_keys:
                study_registry.add_sensitive_key(sens_key)
            for custom_key in custom_phi_keys:
                study_registry.add_sensitive_key(custom_key)
            for key, val in list(row.items()):
                if global_sensitivity_registry.is_exempt(key):
                    continue
                if _matches_custom_phi_key(key, custom_phi_keys):
                    row[key] = _MASKED_VALUE
                elif isinstance(val, (Mapping, list, tuple)):
                    row[key] = mask_custom_phi_recursive(val, custom_phi_keys)
        for reason_key in ("reasonForChange", "reason_for_change"):
            reason_val = row.get(reason_key)
            if isinstance(reason_val, str) and reason_val != _MASKED_VALUE:
                row[reason_key] = sanitize_diagnostic_text(reason_val, registry=study_registry)

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


def mask_cli_models(
    items: Sequence[Any],
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
) -> list[Any]:
    """Return PHI-masked copies of model items for terminal rendering when *mask_phi* is True."""
    if not mask_phi or not items:
        return list(items)

    pipelines: dict[str, Any] = {}
    registries: dict[str, SensitivityRegistry] = {}
    out: list[Any] = []

    for item in items:
        model_fields = getattr(type(item), "model_fields", None)
        model_copy = getattr(item, "model_copy", None)
        if not isinstance(model_fields, Mapping) or not callable(model_copy):
            out.append(item)
            continue

        eff_study = study_key or getattr(item, "study_key", None)
        custom_phi_keys: set[str] = set()
        active_registry = global_sensitivity_registry
        if isinstance(eff_study, str) and eff_study.strip():
            sk = eff_study.strip()
            if sk not in pipelines:
                pipelines[sk] = resolve_enrichment_pipeline(sk)
            pipe = pipelines[sk]
            if pipe is not None and pipe.phi_keys:
                custom_phi_keys = set(pipe.phi_keys)
                if sk not in registries:
                    reg = SensitivityRegistry()
                    sens_keys, exempt_keys = global_sensitivity_registry.snapshot()
                    for ex_key in exempt_keys:
                        reg.add_exempt_key(ex_key)
                    for sens_key in sens_keys:
                        reg.add_sensitive_key(sens_key)
                    for custom_key in custom_phi_keys:
                        reg.add_sensitive_key(custom_key)
                    registries[sk] = reg
                active_registry = registries[sk]

        updates: dict[str, Any] = {}
        for fname, finfo in model_fields.items():
            raw_val = getattr(item, fname, None)
            if raw_val is None or active_registry.is_exempt(fname):
                continue
            alias = getattr(finfo, "alias", None)
            is_sens = (
                active_registry.is_sensitive(fname)
                or (isinstance(alias, str) and active_registry.is_sensitive(alias))
                or _matches_custom_phi_key(fname, custom_phi_keys)
                or (isinstance(alias, str) and _matches_custom_phi_key(alias, custom_phi_keys))
            )
            if is_sens:
                if isinstance(raw_val, str) and raw_val != _MASKED_VALUE:
                    updates[fname] = _MASKED_VALUE
            elif fname == "reason_for_change" and isinstance(raw_val, str):
                scrubbed = sanitize_diagnostic_text(raw_val, registry=active_registry)
                if scrubbed != raw_val:
                    updates[fname] = scrubbed

        extra = getattr(item, "__pydantic_extra__", None)
        masked_extra: dict[str, Any] | None = None
        if isinstance(extra, dict) and extra:
            cand = mask_clinical_phi(extra, registry=active_registry)
            if custom_phi_keys:
                cand = mask_custom_phi_recursive(cand, custom_phi_keys)
            for rkey in ("reasonForChange", "reason_for_change"):
                rval = cand.get(rkey)
                if isinstance(rval, str) and rval != _MASKED_VALUE:
                    cand[rkey] = sanitize_diagnostic_text(rval, registry=active_registry)
            if cand != extra:
                masked_extra = dict(cand)

        if not updates and masked_extra is None:
            out.append(item)
            continue

        copied = model_copy(update=updates)
        if masked_extra is not None:
            copied_extra = getattr(copied, "__pydantic_extra__", None)
            if isinstance(copied_extra, dict):
                merged_extra = dict(copied_extra)
                merged_extra.update(masked_extra)
                object.__setattr__(copied, "__pydantic_extra__", merged_extra)
        out.append(copied)

    return out
