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
from imednet.utils.security import global_sensitivity_registry, mask_clinical_phi
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
