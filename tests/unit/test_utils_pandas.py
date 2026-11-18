"""Unit tests for utils pandas."""

import ast
from unittest.mock import MagicMock

import pandas as pd
import pytest

from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.queries import Query, QueryStatus
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.subjects import Subject
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.utils.pandas import (
    _models_to_dataframe,
    check_pandas,
    export_records_csv,
    forms_to_dataframe,
    intervals_to_dataframe,
    queries_to_dataframe,
    records_to_dataframe,
    sites_to_dataframe,
    subjects_to_dataframe,
    variables_to_dataframe,
    visits_to_dataframe,
)


def _sample_query() -> Query:
    """Helper function to sample query."""
    return Query(
        status=QueryStatus.OPEN,
        query_comments=[],
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


def _sample_form() -> Form:
    """Helper function to sample form."""
    return Form(
        form_id=1,
        form_name="Demographics",
        form_key="DEMO",
        subject_record_report=True,
        unscheduled_visit=False,
        disabled=False,
    )


def _sample_visit() -> Visit:
    """Helper function to sample visit."""
    return Visit(
        visit_id=1,
        subject_id=101,
        interval_id=5,
    )


def _sample_interval() -> Interval:
    """Helper function to sample interval."""
    return Interval(
        interval_id=5,
        interval_name="Baseline",
        forms=[FormSummary(form_id=1, form_key="DEMO", form_name="Demographics")],
    )


def _sample_site() -> Site:
    """Helper function to sample site."""
    return Site(
        site_id=10,
        site_name="General Hospital",
    )


def test_forms_to_dataframe_success() -> None:
    """Test converting forms to a pandas DataFrame."""
    form = _sample_form()
    df = forms_to_dataframe([form])
    assert not df.empty
    assert len(df) == 1
    assert df.loc[0, "form_id"] == 1
    assert df.loc[0, "form_name"] == "Demographics"
    assert df.loc[0, "form_key"] == "DEMO"
    assert bool(df.loc[0, "subject_record_report"]) is True
    assert bool(df.loc[0, "unscheduled_visit"]) is False
    assert bool(df.loc[0, "disabled"]) is False


def test_forms_to_dataframe_empty() -> None:
    """Test converting empty forms collection to DataFrame."""
    df = forms_to_dataframe([])
    assert df.empty


def test_forms_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that forms_to_dataframe raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for forms_to_dataframe"):
        forms_to_dataframe([])


def test_visits_to_dataframe_success() -> None:
    """Test converting visits to a pandas DataFrame."""
    visit = _sample_visit()
    df = visits_to_dataframe([visit])
    assert not df.empty
    assert len(df) == 1
    assert df.loc[0, "visit_id"] == 1
    assert df.loc[0, "subject_id"] == 101
    assert df.loc[0, "interval_id"] == 5


def test_visits_to_dataframe_empty() -> None:
    """Test converting empty visits collection to DataFrame."""
    df = visits_to_dataframe([])
    assert df.empty


def test_visits_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that visits_to_dataframe raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for visits_to_dataframe"):
        visits_to_dataframe([])


def test_intervals_to_dataframe_success() -> None:
    """Test converting intervals to a pandas DataFrame including nested structures."""
    interval = _sample_interval()
    df = intervals_to_dataframe([interval])
    assert not df.empty
    assert len(df) == 1
    assert df.loc[0, "interval_id"] == 5
    assert df.loc[0, "interval_name"] == "Baseline"
    forms = df.loc[0, "forms"]
    assert isinstance(forms, list)
    assert len(forms) == 1
    assert forms[0]["form_id"] == 1
    assert forms[0]["form_key"] == "DEMO"
    assert forms[0]["form_name"] == "Demographics"


def test_intervals_to_dataframe_empty() -> None:
    """Test converting empty intervals collection to DataFrame."""
    df = intervals_to_dataframe([])
    assert df.empty


def test_intervals_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that intervals_to_dataframe raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for intervals_to_dataframe"):
        intervals_to_dataframe([])


def test_sites_to_dataframe_success() -> None:
    """Test converting sites to a pandas DataFrame."""
    site = _sample_site()
    df = sites_to_dataframe([site])
    assert not df.empty
    assert len(df) == 1
    assert df.loc[0, "site_id"] == 10
    assert df.loc[0, "site_name"] == "General Hospital"


def test_sites_to_dataframe_empty() -> None:
    """Test converting empty sites collection to DataFrame."""
    df = sites_to_dataframe([])
    assert df.empty


def test_sites_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that sites_to_dataframe raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for sites_to_dataframe"):
        sites_to_dataframe([])


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


def test_queries_to_dataframe_success() -> None:
    """Test converting queries to a pandas DataFrame."""
    query = _sample_query()
    df = queries_to_dataframe([query])
    assert not df.empty
    assert len(df) == 1
    assert df.loc[0, "status"] == "OPEN"
    assert df.loc[0, "query_comments"] == []


def test_queries_to_dataframe_empty() -> None:
    """Test converting empty queries collection to DataFrame."""
    df = queries_to_dataframe([])
    assert df.empty


def test_queries_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that queries_to_dataframe raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for queries_to_dataframe"):
        queries_to_dataframe([])


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


def test_check_pandas_success() -> None:
    """Test that check_pandas succeeds when pandas is installed."""
    assert check_pandas() is None
    assert check_pandas("test_feature") is None


def test_check_pandas_raises_when_missing(monkeypatch) -> None:
    """Test that check_pandas raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(
        ImportError,
        match=r"pandas is required\. Install with \"pip install 'imednet\[export\]'\"\.",
    ):
        check_pandas()


def test_check_pandas_raises_with_feature_when_missing(monkeypatch) -> None:
    """Test that check_pandas raises ImportError with feature name when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(
        ImportError,
        match=r"pandas is required for test_feature\. Install with \"pip install 'imednet\[export\]'\"\.",
    ):
        check_pandas("test_feature")


def test_models_to_dataframe_helper_serializes_iterables() -> None:
    """Test that _models_to_dataframe serializes an iterable of models with by_alias=False."""
    gen = (item for item in [_sample_site(), _sample_site()])
    df = _models_to_dataframe(gen, "custom_feature")

    assert len(df) == 2
    assert list(df["site_id"]) == [10, 10]
    assert list(df["site_name"]) == ["General Hospital", "General Hospital"]


def test_models_to_dataframe_helper_raises_when_pandas_missing(monkeypatch) -> None:
    """Test that _models_to_dataframe raises ImportError with feature name when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for custom_feature"):
        _models_to_dataframe([], "custom_feature")
