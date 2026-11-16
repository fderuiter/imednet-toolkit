"""Unit tests for utils pandas."""

import ast
from unittest.mock import MagicMock

import pandas as pd
import pytest

from imednet.models.records import Record
from imednet.models.subjects import Subject
from imednet.models.variables import Variable
from imednet.utils.pandas import (
    export_records_csv,
    records_to_dataframe,
    subjects_to_dataframe,
    variables_to_dataframe,
)


def _sample_variable() -> Variable:
    """Helper function to sample variable."""
    return Variable(
        label="Subject Initials",
        variable_oid="SUBJ_INIT",
        variable_id=1,
        study_key="S1",
    )


def _sample_record() -> Record:
    """Helper function to sample record."""
    return Record(
        record_id=1,
        subject_key="S1",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        record_data={"AGE": 30},
    )


def _sample_subject() -> Subject:
    """Helper function to sample subject."""
    return Subject(
        subject_key="SUB1",
        subject_id=101,
        site_id=5,
    )


def test_subjects_to_dataframe_success() -> None:
    """Test converting subjects to a pandas DataFrame."""
    subj = _sample_subject()
    df = subjects_to_dataframe([subj])
    assert not df.empty
    assert len(df) == 1
    assert df.loc[0, "subject_key"] == "SUB1"
    assert df.loc[0, "subject_id"] == 101
    assert df.loc[0, "site_id"] == 5


def test_subjects_to_dataframe_empty() -> None:
    """Test converting empty subjects collection to DataFrame."""
    df = subjects_to_dataframe([])
    assert df.empty


def test_subjects_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that subjects_to_dataframe raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for subjects_to_dataframe"):
        subjects_to_dataframe([])


def test_variables_to_dataframe_success() -> None:
    """Test converting variables to a pandas DataFrame."""
    var = _sample_variable()
    df = variables_to_dataframe([var])
    assert not df.empty
    assert len(df) == 1
    assert df.loc[0, "label"] == "Subject Initials"
    assert df.loc[0, "variable_oid"] == "SUBJ_INIT"
    assert df.loc[0, "variable_id"] == 1
    assert df.loc[0, "study_key"] == "S1"


def test_variables_to_dataframe_empty() -> None:
    """Test converting empty variables collection to DataFrame."""
    df = variables_to_dataframe([])
    assert df.empty


def test_variables_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that variables_to_dataframe raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for variables_to_dataframe"):
        variables_to_dataframe([])


def test_records_to_dataframe_flatten() -> None:
    """Test that records to dataframe flatten."""
    rec = _sample_record()
    df = records_to_dataframe([rec], flatten=True)
    assert "record_data" not in df.columns
    assert df.loc[0, "AGE"] == 30


def test_records_to_dataframe_no_flatten() -> None:
    """Test that records to dataframe no flatten."""
    rec = _sample_record()
    df = records_to_dataframe([rec], flatten=False)
    assert "record_data" in df.columns
    assert df.loc[0, "record_data"] == {"AGE": 30}


def test_records_to_dataframe_empty() -> None:
    """Test that records to dataframe empty."""
    df = records_to_dataframe([], flatten=False)
    assert df.empty


def test_export_records_csv(tmp_path) -> None:
    """Test that export records csv."""
    sdk = MagicMock()
    sdk.records.list.return_value = [_sample_record()]
    out_path = tmp_path / "records.csv"

    export_records_csv(sdk, "STUDY", str(out_path))

    assert out_path.exists()
    df = pd.read_csv(out_path)
    assert df.loc[0, "AGE"] == 30
    sdk.records.list.assert_called_once_with(study_key="STUDY")


def test_export_records_csv_no_flatten(tmp_path) -> None:
    """Test that export records csv no flatten."""
    sdk = MagicMock()
    sdk.records.list.return_value = [_sample_record()]
    out_path = tmp_path / "records.csv"

    export_records_csv(sdk, "STUDY", str(out_path), flatten=False)

    assert out_path.exists()
    df = pd.read_csv(out_path)
    assert "record_data" in df.columns
    assert ast.literal_eval(str(df.loc[0, "record_data"])) == {"AGE": 30}
    sdk.records.list.assert_called_once_with(study_key="STUDY")


def test_records_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that records to dataframe raises importerror when pandas missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for records_to_dataframe"):
        records_to_dataframe([])


def test_export_records_csv_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that export records csv raises importerror when pandas missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for export_records_csv"):
        export_records_csv(None, "STUDY", "path.csv")
