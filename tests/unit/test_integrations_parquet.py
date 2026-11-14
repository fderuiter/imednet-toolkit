"""Unit tests for integrations parquet."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import imednet.integrations.parquet as parquet_mod
from imednet.errors import PathTraversalValidationError


def _read_partition_dataframe(path: Path) -> pd.DataFrame:
    """Helper function to  read partition dataframe."""
    parquet_files = sorted(path.glob("**/*.parquet"))
    assert parquet_files
    return pd.read_parquet(parquet_files[0], engine="pyarrow")


def test_export_creates_hive_layout_and_contents(tmp_path, monkeypatch) -> None:
    """Test that export creates hive layout and contents."""
    pytest.importorskip("pyarrow")

    sdk = MagicMock()
    sdk.forms.list.return_value = [
        SimpleNamespace(form_id=1, form_key="DEMOGRAPHICS"),
        SimpleNamespace(form_id=2, form_key="VITALS"),
    ]
    sdk.variables.list.return_value = [
        SimpleNamespace(form_id=1, variable_name="age", label="Age"),
        SimpleNamespace(form_id=2, variable_name="weight", label="Weight"),
    ]

    class FakeMapper:
        """Test suite for FakeMapper."""

        def __init__(self, _sdk) -> None:
            """Initialize the test object."""
            self._sdk = _sdk

        def _build_record_model(self, variable_keys, label_map):
            """Helper function to  build record model."""
            return (variable_keys, label_map)

        def _fetch_records(self, _study_key, extra_filters):
            """Helper function to  fetch records."""
            return [extra_filters["formId"]]

        def _parse_records(self, records, _record_model):
            """Helper function to  parse records."""
            return records, len(records)

        def _build_dataframe(self, rows, _variable_keys, _label_map, _use_labels_as_columns):
            """Helper function to  build dataframe."""
            form_id = rows[0]
            if form_id == 1:
                return pd.DataFrame([{"age": 42}])
            return pd.DataFrame([{"weight": 73.5}])

    monkeypatch.setattr(parquet_mod, "_record_mapper", lambda: FakeMapper)

    parquet_mod.export_to_hive_parquet(sdk, "STUDY_A", str(tmp_path))

    demographics_path = tmp_path / "study_key=STUDY_A" / "form_key=DEMOGRAPHICS"
    vitals_path = tmp_path / "study_key=STUDY_A" / "form_key=VITALS"
    assert demographics_path.exists()
    assert vitals_path.exists()

    demographics_df = _read_partition_dataframe(demographics_path)
    vitals_df = _read_partition_dataframe(vitals_path)
    assert demographics_df.to_dict("records") == [{"age": 42}]
    assert vitals_df.to_dict("records") == [{"weight": 73.5}]


def test_export_isolates_studies(tmp_path, monkeypatch) -> None:
    """Test that export isolates studies."""
    pytest.importorskip("pyarrow")

    sdk = MagicMock()
    sdk.forms.list.return_value = [SimpleNamespace(form_id=1, form_key="DEMOGRAPHICS")]
    sdk.variables.list.return_value = [SimpleNamespace(form_id=1, variable_name="age", label="Age")]

    class FakeMapper:
        """Test suite for FakeMapper."""

        def __init__(self, _sdk) -> None:
            """Initialize the test object."""
            self._sdk = _sdk

        def _build_record_model(self, variable_keys, label_map):
            """Helper function to  build record model."""
            return (variable_keys, label_map)

        def _fetch_records(self, study_key, extra_filters):
            """Helper function to  fetch records."""
            return [(study_key, extra_filters["formId"])]

        def _parse_records(self, records, _record_model):
            """Helper function to  parse records."""
            return records, len(records)

        def _build_dataframe(self, rows, _variable_keys, _label_map, _use_labels_as_columns):
            """Helper function to  build dataframe."""
            study_key, _form_id = rows[0]
            return pd.DataFrame([{"study": study_key}])

    monkeypatch.setattr(parquet_mod, "_record_mapper", lambda: FakeMapper)

    parquet_mod.export_to_hive_parquet(sdk, "STUDY_A", str(tmp_path))
    parquet_mod.export_to_hive_parquet(sdk, "STUDY_B", str(tmp_path))

    study_a_path = tmp_path / "study_key=STUDY_A" / "form_key=DEMOGRAPHICS"
    study_b_path = tmp_path / "study_key=STUDY_B" / "form_key=DEMOGRAPHICS"
    assert study_a_path.exists()
    assert study_b_path.exists()

    assert _read_partition_dataframe(study_a_path).to_dict("records") == [{"study": "STUDY_A"}]
    assert _read_partition_dataframe(study_b_path).to_dict("records") == [{"study": "STUDY_B"}]


def test_hive_parquet_query() -> None:
    """Test that hive parquet query."""
    assert (
        parquet_mod.hive_parquet_query("/tmp/lake")
        == "SELECT * FROM read_parquet('/tmp/lake/**/*.parquet', "
        "hive_partitioning = true, union_by_name = true)"
    )


def test_export_to_hive_parquet_missing_pyarrow(monkeypatch) -> None:
    """Test that export to hive parquet missing pyarrow."""

    def _raise_import_error(module_name: str):
        """Helper function to  raise import error."""
        if module_name == "pyarrow":
            raise ImportError("missing pyarrow")
        return __import__(module_name)

    monkeypatch.setattr(parquet_mod, "import_module", _raise_import_error)

    with pytest.raises(ImportError, match=r"pip install 'imednet\[export\]'"):
        parquet_mod.export_to_hive_parquet(MagicMock(), "STUDY_A", "/tmp/lake")


def test_export_to_hive_parquet_rejects_malicious_study_key(monkeypatch) -> None:
    """Test that export to hive parquet rejects malicious study key."""
    monkeypatch.setattr(parquet_mod, "_ensure_pyarrow", lambda: None)

    sdk = MagicMock()

    with pytest.raises(PathTraversalValidationError):
        parquet_mod.export_to_hive_parquet(sdk, "../STUDY_A", "/tmp/lake")

    sdk.forms.list.assert_not_called()


def test_export_to_hive_parquet_rejects_malicious_form_key(monkeypatch) -> None:
    """Test that export to hive parquet rejects malicious form key."""
    monkeypatch.setattr(parquet_mod, "_ensure_pyarrow", lambda: None)

    sdk = MagicMock()
    sdk.forms.list.return_value = [SimpleNamespace(form_id=1, form_key="../DEMOGRAPHICS")]
    sdk.variables.list.return_value = []
    mapper_factory = MagicMock()
    mapper = mapper_factory.return_value
    monkeypatch.setattr(parquet_mod, "_record_mapper", lambda: mapper_factory)

    with pytest.raises(PathTraversalValidationError):
        parquet_mod.export_to_hive_parquet(sdk, "STUDY_A", "/tmp/lake")

    mapper._fetch_records.assert_not_called()


def test_export_to_hive_parquet_flushes_form_batches(monkeypatch, tmp_path) -> None:
    """Test that export to hive parquet flushes form batches."""
    sdk = MagicMock()
    sdk.forms.list.return_value = [SimpleNamespace(form_id=1, form_key="DEMOGRAPHICS")]
    sdk.variables.list.return_value = [SimpleNamespace(form_id=1, variable_name="age", label="Age")]

    class FakeMapper:
        """Test suite for FakeMapper."""

        def __init__(self, _sdk) -> None:
            """Initialize the test object."""
            self._sdk = _sdk

        def _build_record_model(self, variable_keys, label_map):
            """Helper function to  build record model."""
            return (variable_keys, label_map)

        def _iter_records(self, _study_key, extra_filters):
            """Helper function to  iter records."""
            assert extra_filters == {"formIds": [1]}
            return iter([1, 2, 3])

        def _iter_parsed_rows(self, records, _record_model):
            """Helper function to  iter parsed rows."""
            values = list(records)
            yield values[:2], 0
            yield values[2:], 0

        def _build_dataframe(self, rows, _variable_keys, _label_map, _use_labels_as_columns):
            """Helper function to  build dataframe."""
            return pd.DataFrame([{"age": value} for value in rows])

    writes: list[list[dict[str, int]]] = []

    class FakeEngine:
        """Test suite for FakeEngine."""

        def write_form_table(self, table, **kwargs) -> None:
            """Helper function to write form table."""
            assert kwargs == {
                "base_dir": str(tmp_path),
                "study_key": "STUDY_A",
                "form_key": "DEMOGRAPHICS",
            }
            writes.append(table.to_dict("records"))

    class _FakeTable:
        """Test suite for  FakeTable."""

        @staticmethod
        def from_pandas(df: pd.DataFrame, preserve_index: bool = False) -> pd.DataFrame:
            """Helper function to from pandas."""
            assert preserve_index is False
            return df

    monkeypatch.setattr(parquet_mod, "_record_mapper", lambda: FakeMapper)
    monkeypatch.setattr(parquet_mod, "_ensure_pyarrow", lambda: SimpleNamespace(Table=_FakeTable))
    monkeypatch.setattr(parquet_mod, "PyArrowDatasetPartitionedStorageEngine", lambda: FakeEngine())

    parquet_mod.export_to_hive_parquet(sdk, "STUDY_A", str(tmp_path), chunk_size=2)

    assert writes == [[{"age": 1}, {"age": 2}], [{"age": 3}]]


def test_hive_parquet_query_trailing_slash_clean_glob() -> None:
    """Test that hive_parquet_query handles trailing slashes without double slashes."""
    assert (
        parquet_mod.hive_parquet_query("/tmp/lake/")
        == "SELECT * FROM read_parquet('/tmp/lake/**/*.parquet', "
        "hive_partitioning = true, union_by_name = true)"
    )


@pytest.mark.parametrize(
    "invalid_path",
    [
        "/tmp/lake\x00/extra",
        "/tmp/lake\n/extra",
        "/tmp/lake\r/extra",
        "/tmp/lake\x1f/extra",
        "/tmp/lake\x7f/extra",
        "",
        "   ",
    ],
)
def test_hive_parquet_query_rejects_control_chars_and_null_bytes(invalid_path: str) -> None:
    """Test that hive_parquet_query rejects null bytes, newlines, and control characters."""
    with pytest.raises(ValueError, match=r"(?i)directory path"):
        parquet_mod.hive_parquet_query(invalid_path)
