"""Unit tests for chunked pipeline."""

from __future__ import annotations

import pytest

from imednet_workflows.chunked_pipeline import ChunkedRecordPipeline, iter_chunks


def test_iter_chunks_splits_batches() -> None:
    """Test that iter chunks splits batches."""
    chunks = list(iter_chunks(range(5), chunk_size=2))
    assert chunks == [[0, 1], [2, 3], [4]]


def test_iter_chunks_rejects_invalid_chunk_size() -> None:
    """Test that iter chunks rejects invalid chunk size."""
    with pytest.raises(ValueError, match="chunk_size must be greater than zero"):
        list(iter_chunks([1, 2], chunk_size=0))


def test_chunked_record_pipeline_maps_in_chunks(tmp_path) -> None:
    """Test that chunked record pipeline maps in chunks and writes parquet chunks."""
    with pytest.raises(ValueError, match="chunk_size must be greater than zero"):
        ChunkedRecordPipeline(chunk_size=0)

    pipeline = ChunkedRecordPipeline(chunk_size=2)
    mapped = list(pipeline.map_chunks([1, 2, 3], lambda value: value * 10))
    assert mapped == [[10, 20], [30]]

    rows = [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}, {"id": 3, "val": "c"}]
    written = pipeline.write_parquet_chunks(
        rows, output_dir=tmp_path / "out", filename_prefix="batch"
    )
    assert len(written) == 2
    assert [p.name for p in written] == ["batch_chunk_00001.parquet", "batch_chunk_00002.parquet"]
    assert all(p.is_file() for p in written)
