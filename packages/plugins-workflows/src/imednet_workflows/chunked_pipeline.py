"""Utilities for processing large datasets in memory-bounded chunks."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from imednet.spi.utils import parse_iso_datetime

T = TypeVar("T")
R = TypeVar("R")

# Keep per-chunk memory bounded for large-study payloads while still
# amortizing DB/API overhead across each batch.
DEFAULT_CHUNK_SIZE = 5_000

# Keep server-side OR filter strings well within URL query limits while avoiding
# excessive sequential round-trips on non-selective full-study cohorts.
DEFAULT_OR_FILTER_BATCH_SIZE = 50
MAX_OR_FILTER_PUSHDOWN_ITEMS = 250
DEFAULT_PAGE_SIZE = 100
_MISSING = object()


def _field_name_variants(field_name: str) -> list[str]:
    """Build exact, snake_case, and camelCase lookup candidates for a field name."""
    snake_chars: list[str] = []
    for idx, ch in enumerate(field_name):
        if ch.isupper() and idx > 0 and not field_name[idx - 1].isupper():
            snake_chars.append("_")
        snake_chars.append(ch.lower())
    snake = "".join(snake_chars).lstrip("_")
    segments = [seg for seg in snake.split("_") if seg]
    camel = segments[0] + "".join(seg.capitalize() for seg in segments[1:]) if segments else snake
    return list(dict.fromkeys((field_name, snake, camel)))


def _resolve_item_field(item: Any, candidates: Sequence[str], raw_field: str) -> Any:
    """Extract a field value from a dict or SDK model across naming and alias variants."""
    if isinstance(item, Mapping):
        for name in candidates:
            if name in item:
                return item[name]
        return None

    for name in candidates:
        val = getattr(item, name, _MISSING)
        if val is not _MISSING:
            return val

    model_fields = getattr(type(item), "model_fields", None)
    if isinstance(model_fields, Mapping):
        for attr_name, meta in model_fields.items():
            if getattr(meta, "alias", None) == raw_field:
                return getattr(item, attr_name, None)
    return None


def _comparable_sort_key(raw_value: Any) -> tuple[int, Any] | None:
    """Convert a non-null field value into a type-tagged tuple suitable for ordering."""
    if raw_value is None:
        return None
    if isinstance(raw_value, Enum):
        raw_value = raw_value.value
    if isinstance(raw_value, bool):
        return (0, int(raw_value))
    if isinstance(raw_value, (int, float)):
        return (1, float(raw_value))
    if isinstance(raw_value, datetime):
        utc_dt = (
            raw_value.replace(tzinfo=UTC) if raw_value.tzinfo is None else raw_value.astimezone(UTC)
        )
        return (2, utc_dt.timestamp())

    text = str(raw_value).strip()
    if len(text) >= 10 and text[:4].isdigit() and text[4] == "-":
        try:
            parsed_dt = parse_iso_datetime(text)
        except ValueError:
            pass
        else:
            utc_dt = (
                parsed_dt.replace(tzinfo=UTC)
                if parsed_dt.tzinfo is None
                else parsed_dt.astimezone(UTC)
            )
            return (2, utc_dt.timestamp())

    return (3, (text.lower(), text))


def _parse_sort_spec(sort_spec: Any) -> list[tuple[str, bool]]:
    """Parse single or ';'-separated sort clauses into ``(field, descending)`` pairs."""
    if sort_spec is None:
        return []
    raw_segments: list[str]
    if isinstance(sort_spec, str):
        raw_segments = [seg.strip() for seg in sort_spec.split(";") if seg.strip()]
    elif isinstance(sort_spec, Sequence):
        raw_segments = [
            seg.strip() for entry in sort_spec for seg in str(entry).split(";") if seg.strip()
        ]
    else:
        raw_segments = [str(sort_spec).strip()]

    clauses: list[tuple[str, bool]] = []
    for segment in raw_segments:
        tokens = [tok.strip() for tok in segment.split(",") if tok.strip()]
        if not tokens:
            continue
        if len(tokens) >= 2 and tokens[-1].lower() in ("asc", "desc"):
            descending = tokens[-1].lower() == "desc"
            for field_token in tokens[:-1]:
                clauses.append((field_token, descending))
        else:
            for field_token in tokens:
                clauses.append((field_token, False))
    return clauses


def _sort_items_by_clause(items: list[T], field_name: str, *, descending: bool) -> list[T]:
    """Stably sort ``items`` by ``field_name``, keeping missing/None values at the end."""
    candidates = _field_name_variants(field_name)
    keyed_present: list[tuple[tuple[int, Any], T]] = []
    missing: list[T] = []
    for item in items:
        key = _comparable_sort_key(_resolve_item_field(item, candidates, field_name))
        if key is None:
            missing.append(item)
        else:
            keyed_present.append((key, item))

    keyed_present.sort(key=lambda entry: entry[0], reverse=descending)
    return [entry[1] for entry in keyed_present] + missing


def _resolve_page_bounds(controls: Mapping[str, Any]) -> tuple[int, int] | None:
    """Validate pagination controls and return ``(start, end)`` slice bounds if ``page`` is set."""
    raw_size = controls.get("size")
    if raw_size is None:
        raw_size = controls.get("limit")
    page_size = DEFAULT_PAGE_SIZE
    if raw_size is not None:
        if isinstance(raw_size, bool):
            raise ValueError("size must be a positive integer")
        page_size = int(raw_size)
        if page_size <= 0:
            raise ValueError("size must be greater than zero")

    raw_page = controls.get("page")
    if raw_page is None:
        return None
    if isinstance(raw_page, bool):
        raise ValueError("page must be a non-negative integer")
    page_index = int(raw_page)
    if page_index < 0:
        raise ValueError("page must be non-negative")

    start = page_index * page_size
    return start, start + page_size


def prepare_deferred_controls(controls: Mapping[str, Any]) -> dict[str, Any]:
    """Validate pagination bounds and strip per-request ``page`` controls for global merging."""
    has_explicit_page = _resolve_page_bounds(controls) is not None
    forwarded: dict[str, Any] = {}
    for key, value in controls.items():
        if value is None or key == "page":
            continue
        if has_explicit_page and key in ("size", "limit"):
            continue
        forwarded[key] = value
    return forwarded


def apply_global_sort_and_page(
    items: Sequence[T],
    controls: Mapping[str, Any],
    *,
    default_sort_field: str | None = None,
    enforce_default_sort: bool = False,
) -> list[T]:
    """Apply global sort order and optional zero-based page slicing to a complete cohort."""
    page_bounds = _resolve_page_bounds(controls)
    ordered = list(items)
    sort_clauses = _parse_sort_spec(controls.get("sort"))

    if enforce_default_sort and default_sort_field is not None:
        ordered = _sort_items_by_clause(ordered, default_sort_field, descending=False)

    for field_name, descending in reversed(sort_clauses):
        ordered = _sort_items_by_clause(ordered, field_name, descending=descending)

    if page_bounds is not None:
        start, end = page_bounds
        return ordered[start:end]

    return ordered


def iter_chunks(items: Iterable[T], *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[list[T]]:
    """Yield ``items`` in bounded chunks."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    chunk: list[T] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


class ChunkedRecordPipeline:
    """Chunked iteration utilities for large-study workflows."""

    def __init__(self, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        """Initialize the chunked record pipeline.

        Args:
            chunk_size: The number of items per chunk.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        self.chunk_size = chunk_size

    def map_chunks(self, items: Iterable[T], mapper: Callable[[T], R]) -> Iterator[list[R]]:
        """Apply ``mapper`` to ``items`` and yield mapped chunks."""
        for chunk in iter_chunks(items, chunk_size=self.chunk_size):
            mapped = [mapper(item) for item in chunk]
            yield mapped

    def write_parquet_chunks(
        self,
        rows: Iterable[dict[str, object]],
        *,
        output_dir: str | Path,
        filename_prefix: str = "records",
    ) -> list[Path]:
        """Write chunked parquet files and return written file paths."""
        import pandas as pd

        target_dir = Path(output_dir).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for index, chunk in enumerate(iter_chunks(rows, chunk_size=self.chunk_size), start=1):
            frame = pd.DataFrame(chunk)
            destination = target_dir / f"{filename_prefix}_chunk_{index:05d}.parquet"
            frame.to_parquet(destination, index=False)
            written.append(destination)
        return written
