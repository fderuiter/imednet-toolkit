"""Unit tests for chunked pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from imednet.models.queries import Query, QueryStatus
from imednet_workflows.chunked_pipeline import (
    ChunkedRecordPipeline,
    apply_global_sort_and_page,
    iter_chunks,
    prepare_deferred_controls,
)


def test_iter_chunks_splits_batches() -> None:
    """Test that iter chunks splits batches."""
    chunks = list(iter_chunks(range(5), chunk_size=2))
    assert chunks == [[0, 1], [2, 3], [4]]


def test_iter_chunks_rejects_invalid_chunk_size() -> None:
    """Test that iter chunks rejects invalid chunk size."""
    with pytest.raises(ValueError, match="chunk_size must be greater than zero"):
        list(iter_chunks([1, 2], chunk_size=0))


def test_chunked_record_pipeline_maps_in_chunks() -> None:
    """Test that chunked record pipeline maps in chunks."""
    pipeline = ChunkedRecordPipeline(chunk_size=2)
    mapped = list(pipeline.map_chunks([1, 2, 3], lambda value: value * 10))
    assert mapped == [[10, 20], [30]]


def test_prepare_deferred_controls_strips_page_and_validates_bounds() -> None:
    """prepare_deferred_controls omits page/size when paging globally and keeps unpaged size/sort."""
    assert prepare_deferred_controls({"page": 1, "size": 25, "sort": "annotationId,desc"}) == {
        "sort": "annotationId,desc"
    }
    assert prepare_deferred_controls({"size": 25, "sort": None}) == {"size": 25}
    with pytest.raises(ValueError, match="page"):
        prepare_deferred_controls({"page": -1, "size": 5})
    with pytest.raises(ValueError, match="size"):
        prepare_deferred_controls({"size": 0})


def test_apply_global_sort_and_page_handles_models_dicts_types_and_bounds() -> None:
    """apply_global_sort_and_page orders models and dicts across types and slices global pages."""
    queries = [
        Query(annotation_id=30, record_id=2, date_modified="2026-01-02T00:00:00Z"),
        Query(annotation_id=10, record_id=1, date_modified="2026-01-03T00:00:00Z"),
        Query(annotation_id=20, record_id=2, date_modified="2026-01-01T00:00:00Z"),
    ]
    default_sorted = apply_global_sort_and_page(
        queries,
        {},
        default_sort_field="annotation_id",
        enforce_default_sort=True,
    )
    assert [q.annotation_id for q in default_sorted] == [10, 20, 30]

    multi_sorted = apply_global_sort_and_page(
        queries,
        {"sort": "recordId,asc;annotationId,desc", "page": 0, "limit": 2},
    )
    assert [q.annotation_id for q in multi_sorted] == [10, 30]

    rows = [
        {"id": 2, "flag": True, "ts": datetime(2026, 1, 2, tzinfo=UTC), "status": QueryStatus.OPEN},
        {"id": None, "flag": False, "ts": None, "status": QueryStatus.CLOSED},
        {"id": 1, "flag": False, "ts": datetime(2026, 1, 3), "status": QueryStatus.ANSWERED},
    ]
    ts_sorted = apply_global_sort_and_page(rows, {"sort": ["ts,desc"]})
    assert [r["id"] for r in ts_sorted] == [1, 2, None]

    flag_sorted = apply_global_sort_and_page(rows, {"sort": "flag,id"})
    assert [r["id"] for r in flag_sorted] == [1, None, 2]

    with pytest.raises(ValueError, match="page"):
        apply_global_sort_and_page(rows, {"page": True, "size": 2})
    with pytest.raises(ValueError, match="size"):
        apply_global_sort_and_page(rows, {"page": 0, "size": False})
