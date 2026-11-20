"""Example showing safe analytical reads from a partitioned Hive Parquet lakehouse."""

from __future__ import annotations

import os
import tempfile
from contextlib import ExitStack
from pathlib import Path

import duckdb

from imednet.integrations.parquet import hive_parquet_query


def _seed_sample_lakehouse(root: Path) -> None:
    """Create minimal Hive-partitioned Parquet files for offline demonstration."""
    part_dir = root / "study_key=STUDY" / "form_key=F_DM"
    part_dir.mkdir(parents=True, exist_ok=True)
    part_file = (part_dir / "part-0.parquet").as_posix()
    con = duckdb.connect(":memory:")
    con.execute(
        f"COPY (SELECT 601 AS record_id, 'SUB1' AS subject_key, 45 AS v_age) "
        f"TO '{part_file}' (FORMAT PARQUET)"
    )
    con.close()


with ExitStack() as stack:
    configured_dir = os.getenv("IMEDNET_LAKEHOUSE_DIR")
    if configured_dir and list(Path(configured_dir).rglob("*.parquet")):
        base_dir = configured_dir
    else:
        tmp_lakehouse = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        _seed_sample_lakehouse(tmp_lakehouse)
        base_dir = str(tmp_lakehouse)

    query = (
        "SELECT study_key, form_key, COUNT(*) AS row_count "
        f"FROM ({hive_parquet_query(base_dir)}) records "
        "GROUP BY study_key, form_key "
        "ORDER BY study_key, form_key"
    )

    rows = duckdb.connect(":memory:").execute(query).fetchall()
    for row in rows:
        print(row)
