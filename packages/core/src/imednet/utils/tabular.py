"""Standard-library CSV and tabular record normalization utilities."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from imednet.utils.typing import JsonDict, RecordsInput
from imednet.utils.write_pipeline import (
    MASKED_SENTINEL,
    WriteIngestionPipeline,
    _build_tabular_raw_envelope,
    _contains_masked_sentinel,
    _is_dataframe_instance,
    ensure_no_masked_sentinel,
)

if TYPE_CHECKING:
    from imednet.validation.cache import ValidationSchemaProtocol


def normalize_tabular_row(
    raw_row: Mapping[str, Any],
    *,
    row_index: int = 0,
    from_csv_str: bool = False,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> JsonDict | None:
    """Normalize a single DataFrame or CSV row into a REST create-records payload dict.

    Returns ``None`` if every cell in the row is empty.
    """
    env = _build_tabular_raw_envelope(
        raw_row,
        row_index=row_index,
        source_kind="csv" if from_csv_str else "tabular",
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )
    if env is None:
        return None
    bound = WriteIngestionPipeline.bind([env], schema=schema, study_key=study_key)
    return bound[0] if bound else None


def _read_csv_stream_records(
    stream: Any,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    envelopes = WriteIngestionPipeline.read_csv_stream(
        stream,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )
    return WriteIngestionPipeline.bind(envelopes, schema=schema, study_key=study_key)


def csv_text_to_records(
    raw_csv: str,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Parse CSV text into REST create-records payload dicts."""
    envelopes = WriteIngestionPipeline.read_csv_text(
        raw_csv,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )
    return WriteIngestionPipeline.bind(envelopes, schema=schema, study_key=study_key)


def csv_to_records(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8-sig",
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Load a CSV file into REST create-records payload dicts using the standard library.

    Works in ``packages/core`` even when ``pandas`` is not installed.
    """
    envelopes = WriteIngestionPipeline.read_csv(
        path,
        encoding=encoding,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )
    return WriteIngestionPipeline.bind(envelopes, schema=schema, study_key=study_key)


def dataframe_rows_to_records(
    df: Any,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Convert a ``pandas.DataFrame`` into REST create-records payload dicts."""
    envelopes = WriteIngestionPipeline.read_dataframe(
        df,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )
    return WriteIngestionPipeline.bind(envelopes, schema=schema, study_key=study_key)


def dataframe_to_records(
    df: Any,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Thin wrapper over :class:`WriteIngestionPipeline` converting a DataFrame into records."""
    return dataframe_rows_to_records(
        df,
        schema=schema,
        study_key=study_key,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )


def normalize_records_input(
    records_data: RecordsInput,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Normalize ``DataFrame``, CSV/JSON/Parquet path, or ``list[JsonDict]`` into payload dicts."""
    envelopes = WriteIngestionPipeline.read_input(
        records_data,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )
    return WriteIngestionPipeline.bind(envelopes, schema=schema, study_key=study_key)


__all__ = [
    "MASKED_SENTINEL",
    "_contains_masked_sentinel",
    "_is_dataframe_instance",
    "csv_text_to_records",
    "csv_to_records",
    "dataframe_rows_to_records",
    "dataframe_to_records",
    "ensure_no_masked_sentinel",
    "normalize_records_input",
    "normalize_tabular_row",
]
