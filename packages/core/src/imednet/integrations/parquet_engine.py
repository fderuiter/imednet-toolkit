"""Partitioned Parquet storage engines."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from errno import ENOENT, ENOTEMPTY
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


def _import_pyarrow() -> tuple[Any, Any]:
    """Import and return the pyarrow and pyarrow.dataset modules.

    Returns:
        tuple[Any, Any]: A tuple containing (pyarrow, pyarrow.dataset).

    Raises:
        ImportError: If pyarrow is not installed.
    """
    try:
        pyarrow_module = import_module("pyarrow")
        dataset_module = import_module("pyarrow.dataset")
    except ImportError as error:
        raise ImportError(
            "PyArrow is required for partitioned Parquet storage. "
            "Install with \"pip install 'imednet[export]'\"."
        ) from error
    return pyarrow_module, dataset_module


@runtime_checkable
class PartitionedStorageEngine(Protocol):
    """Interface for writing tables into a partitioned dataset."""

    def write_form_table(
        self,
        table: Any,
        *,
        base_dir: str,
        study_key: str,
        form_key: str,
    ) -> None:
        """Persist a form table into a partitioned dataset layout."""


@dataclass(frozen=True)
class PyArrowDatasetPartitionedStorageEngine(PartitionedStorageEngine):
    """Partitioned storage engine powered by ``pyarrow.dataset.write_dataset``."""

    compression: str = "snappy"
    use_dictionary: bool = True
    existing_data_behavior: str = "overwrite_or_ignore"
    staging_dir_name: str = ".imednet_staging"

    def _table_with_metadata(
        self,
        table: Any,
        *,
        study_key: str,
        form_key: str,
        commit_id: str,
    ) -> Any:
        """Inject iMednet-specific metadata into the table schema.

        Args:
            table: The pyarrow table to modify.
            study_key: The iMednet study key.
            form_key: The iMednet form key.
            commit_id: The unique commit identifier for this write operation.

        Returns:
            Any: The table with updated schema metadata.
        """
        if not hasattr(table, "replace_schema_metadata"):
            return table
        schema = getattr(table, "schema", None)
        existing_metadata = dict(getattr(schema, "metadata", {}) or {})
        existing_metadata.update(
            {
                b"imednet.writer": b"pyarrow.dataset",
                b"imednet.commit_id": commit_id.encode("utf-8"),
                b"imednet.study_key": study_key.encode("utf-8"),
                b"imednet.form_key": form_key.encode("utf-8"),
                b"imednet.written_at_utc": datetime.now(UTC).isoformat().encode("utf-8"),
            }
        )
        return table.replace_schema_metadata(existing_metadata)

    def write_form_table(
        self,
        table: Any,
        *,
        base_dir: str,
        study_key: str,
        form_key: str,
    ) -> None:
        """Persist a form table into a partitioned dataset layout using PyArrow.

        This implementation uses a staging directory and atomic renames to ensure
        that data is written consistently.

        Args:
            table: The pyarrow table to write.
            base_dir: The base directory for the Hive dataset.
            study_key: The iMednet study key (used for partitioning).
            form_key: The iMednet form key (used for partitioning).

        Raises:
            RuntimeError: If the write operation fails or produces unexpected output.
        """
        pyarrow_module, dataset_module = _import_pyarrow()
        commit_id = uuid4().hex
        base_path = Path(base_dir)
        staging_root = base_path / self.staging_dir_name
        staging_base_dir = staging_root / commit_id
        staged_partition_dir = staging_base_dir / f"study_key={study_key}" / f"form_key={form_key}"
        final_partition_dir = base_path / f"study_key={study_key}" / f"form_key={form_key}"
        committed_batch_dir = final_partition_dir / f"_batch_{commit_id}"
        commit_succeeded = False

        staging_base_dir.mkdir(parents=True, exist_ok=False)

        partition_schema = pyarrow_module.schema(
            [
                ("study_key", pyarrow_module.string()),
                ("form_key", pyarrow_module.string()),
            ]
        )
        partitioned_table = table.append_column(
            "study_key",
            pyarrow_module.array([study_key] * table.num_rows, type=pyarrow_module.string()),
        ).append_column(
            "form_key",
            pyarrow_module.array([form_key] * table.num_rows, type=pyarrow_module.string()),
        )
        partitioned_table = self._table_with_metadata(
            partitioned_table,
            study_key=study_key,
            form_key=form_key,
            commit_id=commit_id,
        )
        parquet_format = dataset_module.ParquetFileFormat()
        parquet_options = parquet_format.make_write_options(
            compression=self.compression,
            use_dictionary=self.use_dictionary,
        )
        try:
            dataset_module.write_dataset(
                partitioned_table,
                base_dir=str(staging_base_dir),
                basename_template=f"{commit_id}-{{i}}.parquet",
                partitioning=dataset_module.partitioning(flavor="hive", schema=partition_schema),
                format=parquet_format,
                file_options=parquet_options,
                existing_data_behavior=self.existing_data_behavior,
            )

            if not staged_partition_dir.exists():
                raise RuntimeError(
                    "Partition write did not produce staged output for "
                    f"study_key={study_key!r}, form_key={form_key!r}, "
                    f"commit_id={commit_id!r}, staged_partition_dir={staged_partition_dir!s}."
                )

            final_partition_dir.mkdir(parents=True, exist_ok=True)
            # Where the filesystem supports atomic rename (e.g., local POSIX),
            # os.replace ensures readers observe a fully committed batch dir.
            os.replace(staged_partition_dir, committed_batch_dir)
            commit_succeeded = True
        finally:
            shutil.rmtree(staging_base_dir, ignore_errors=True)
            if not commit_succeeded:
                # Roll back any empty, reader-visible partition directories that
                # may have been created before the atomic move failed.
                current_path = final_partition_dir
                while current_path != base_path:
                    try:
                        current_path.rmdir()
                    except OSError as error:
                        if error.errno in (ENOENT, ENOTEMPTY):
                            break
                        raise
                    current_path = current_path.parent
            if staging_root.exists():
                try:
                    staging_root.rmdir()
                except OSError as error:
                    if error.errno not in (ENOENT, ENOTEMPTY):
                        raise


__all__ = ["PartitionedStorageEngine", "PyArrowDatasetPartitionedStorageEngine"]
