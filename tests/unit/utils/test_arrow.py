"""Unit tests for arrow."""

from datetime import datetime, timezone

import pyarrow as pa
from pydantic import BaseModel

from imednet.utils.arrow import to_arrow_table


def test_to_arrow_table_empty_records_returns_empty_table() -> None:
    """Test that to arrow table empty records returns empty table."""
    table = to_arrow_table([])

    assert table.num_rows == 0
    assert table.num_columns == 0


def test_to_arrow_table_handles_key_variations_with_nulls() -> None:
    """Test that to arrow table handles key variations with nulls."""
    table = to_arrow_table([{"subject_key": "S1", "weight": 73.5}, {"subject_key": "S2"}])

    assert table.column("subject_key").to_pylist() == ["S1", "S2"]
    assert table.column("weight").to_pylist() == [73.5, None]
    assert table.schema.field("weight").type == pa.float64()


def test_to_arrow_table_preserves_datetime_bool_and_float_types() -> None:
    """Test that to arrow table preserves datetime bool and float types."""
    recorded_at = datetime(2025, 1, 1, 8, 30, tzinfo=timezone.utc)
    table = to_arrow_table(
        [
            {"recorded_at": recorded_at, "is_complete": True, "score": 1.25},
            {"recorded_at": None, "is_complete": False, "score": 2.5},
        ]
    )

    assert pa.types.is_timestamp(table.schema.field("recorded_at").type)
    assert table.schema.field("is_complete").type == pa.bool_()
    assert table.schema.field("score").type == pa.float64()


def test_to_arrow_table_accepts_pydantic_like_records() -> None:
    """Test that to arrow table accepts pydantic like records."""

    class Visit(BaseModel):
        """Test suite for Visit."""

        subject_key: str
        completed: bool
        systolic: float | None = None

    schema = pa.schema(
        [
            pa.field("subject_key", pa.string()),
            pa.field("completed", pa.bool_()),
            pa.field("systolic", pa.float64()),
            pa.field("missing_vector", pa.null()),
        ]
    )
    table = to_arrow_table(
        [
            Visit(subject_key="S1", completed=True, systolic=120.0),
            Visit(subject_key="S2", completed=False),
        ],
        schema=schema,
    )

    assert table.column("subject_key").to_pylist() == ["S1", "S2"]
    assert table.column("completed").to_pylist() == [True, False]
    assert table.column("systolic").to_pylist() == [120.0, None]
    assert table.column("missing_vector").to_pylist() == [None, None]
    assert table.schema.field("missing_vector").type == pa.null()


def test_to_arrow_table_schema_coerces_bool_strings_invalid_floats_and_timestamps() -> None:
    """Test explicit schema coercion for boolean strings, invalid floats, and non-datetimes."""
    recorded_at = datetime(2025, 1, 1, 8, 30, tzinfo=timezone.utc)
    naive_at = datetime(2025, 1, 2, 9, 15)
    schema = pa.schema(
        [
            pa.field("is_active", pa.bool_()),
            pa.field("measurement", pa.float64()),
            pa.field("recorded_at", pa.timestamp("us", tz="UTC")),
        ]
    )
    table = to_arrow_table(
        [
            {"is_active": "true", "measurement": "72.5", "recorded_at": recorded_at},
            {"is_active": "0", "measurement": "not-a-float", "recorded_at": "2025-01-01T00:00:00Z"},
            {"is_active": "yes", "measurement": 10, "recorded_at": naive_at},
            {"is_active": "invalid", "measurement": object(), "recorded_at": 1735689600},
            {"is_active": "", "measurement": "", "recorded_at": None},
        ],
        schema=schema,
    )

    assert table.column("is_active").to_pylist() == [True, False, True, None, None]
    assert table.column("measurement").to_pylist() == [72.5, None, 10.0, None, None]
    assert table.column("recorded_at").to_pylist() == [
        recorded_at,
        None,
        datetime(2025, 1, 2, 9, 15, tzinfo=timezone.utc),
        None,
        None,
    ]
