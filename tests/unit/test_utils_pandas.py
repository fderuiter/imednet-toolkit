"""Unit tests for utils pandas."""

import ast
import csv
from unittest.mock import MagicMock

import pandas as pd
import pytest

from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.queries import Query, QueryStatus
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.utils.pandas import (
    MASKED_SENTINEL,
    _is_custom_phi_column,
    _models_to_dataframe,
    check_pandas,
    export_records_csv,
    forms_to_dataframe,
    intervals_to_dataframe,
    queries_to_dataframe,
    records_to_dataframe,
    sites_to_dataframe,
    subjects_to_dataframe,
    users_to_dataframe,
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


def _sample_user() -> User:
    """Helper function to sample user."""
    return User(
        user_id="1",
        login="jdoe",
        first_name="John",
        last_name="Doe",
        email="jdoe@example.com",
        user_active_in_study=True,
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


def test_users_to_dataframe_success() -> None:
    """Test converting users to a pandas DataFrame."""
    user = _sample_user()
    df = users_to_dataframe([user])
    assert not df.empty
    assert len(df) == 1
    assert df.loc[0, "user_id"] == "1"
    assert df.loc[0, "login"] == "jdoe"
    assert df.loc[0, "first_name"] == "John"
    assert df.loc[0, "last_name"] == "Doe"
    assert df.loc[0, "email"] == "jdoe@example.com"
    assert bool(df.loc[0, "user_active_in_study"]) is True
    assert df.loc[0, "name"] == "John Doe"


def test_users_to_dataframe_empty() -> None:
    """Test converting empty users collection to DataFrame."""
    df = users_to_dataframe([])
    assert df.empty


def test_users_to_dataframe_raises_importerror_when_pandas_missing(monkeypatch):
    """Test that users_to_dataframe raises ImportError when pandas is missing."""
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    with pytest.raises(ImportError, match="pandas is required for users_to_dataframe"):
        users_to_dataframe([])


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


def _sample_phi_record() -> Record:
    """Create a sample Record containing sensitive clinical fields and clinical vitals."""
    return Record(
        record_id=101,
        study_key="STUDY1",
        subject_key="SUBJ-001",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        record_data={
            "patientName": "Jane Doe",
            "DOB": "1980-01-01",
            "SSN": "123-45-6789",
            "email": "jane@example.org",
            "systolicBp": 120,
        },
    )


def test_records_to_dataframe_masks_phi_by_default_and_honors_opt_out() -> None:
    """Verify records_to_dataframe masks PHI in flat and nested modes and supports mask_phi=False."""
    from imednet.utils.security import global_sensitivity_registry

    rec = _sample_phi_record()
    snapshot_keywords = set(global_sensitivity_registry._sensitive_keys)

    flat_df = records_to_dataframe([rec], flatten=True)
    assert flat_df.loc[0, "patientName"] == "***MASKED***"
    assert flat_df.loc[0, "DOB"] == "***MASKED***"
    assert flat_df.loc[0, "SSN"] == "***MASKED***"
    assert flat_df.loc[0, "email"] == "***MASKED***"
    assert flat_df.loc[0, "systolicBp"] == 120
    assert flat_df.loc[0, "subject_key"] == "SUBJ-001"
    assert flat_df.loc[0, "study_key"] == "STUDY1"
    assert flat_df.loc[0, "record_id"] == 101

    nested_df = records_to_dataframe([rec], flatten=False)
    assert nested_df.loc[0, "record_data"] == {
        "patientName": "***MASKED***",
        "DOB": "***MASKED***",
        "SSN": "***MASKED***",
        "email": "***MASKED***",
        "systolicBp": 120,
    }
    # Original Record model was not mutated in-place
    assert rec.record_data["patientName"] == "Jane Doe"

    unmasked_df = records_to_dataframe([rec], flatten=True, mask_phi=False)
    assert unmasked_df.loc[0, "patientName"] == "Jane Doe"
    assert unmasked_df.loc[0, "DOB"] == "1980-01-01"
    assert unmasked_df.loc[0, "SSN"] == "123-45-6789"
    assert unmasked_df.loc[0, "email"] == "jane@example.org"
    assert unmasked_df.loc[0, "systolicBp"] == 120
    assert global_sensitivity_registry._sensitive_keys == snapshot_keywords


def test_export_records_csv_masks_phi_by_default_and_honors_opt_out(tmp_path) -> None:
    """Verify export_records_csv masks PHI by default and supports mask_phi=False."""
    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: [_sample_phi_record()]

    masked_path = tmp_path / "masked_records.csv"
    export_records_csv(sdk, "STUDY1", str(masked_path))
    masked_df = pd.read_csv(masked_path)
    assert masked_df.loc[0, "patientName"] == "***MASKED***"
    assert masked_df.loc[0, "DOB"] == "***MASKED***"
    assert masked_df.loc[0, "SSN"] == "***MASKED***"
    assert masked_df.loc[0, "email"] == "***MASKED***"
    assert masked_df.loc[0, "systolicBp"] == 120
    assert masked_df.loc[0, "subject_key"] == "SUBJ-001"

    unmasked_path = tmp_path / "unmasked_records.csv"
    export_records_csv(sdk, "STUDY1", str(unmasked_path), mask_phi=False)
    unmasked_df = pd.read_csv(unmasked_path)
    assert unmasked_df.loc[0, "patientName"] == "Jane Doe"
    assert unmasked_df.loc[0, "DOB"] == "1980-01-01"
    assert unmasked_df.loc[0, "SSN"] == "123-45-6789"
    assert unmasked_df.loc[0, "email"] == "jane@example.org"
    assert unmasked_df.loc[0, "systolicBp"] == 120


def test_export_records_csv_applies_study_config_labels_and_formula_sanitization(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Verify export_records_csv masks default + StudyConfiguration PHI before applying Variable labels and sanitizes CSV formulas."""
    from types import SimpleNamespace

    from imednet.integrations import enrichment as enrichment_mod
    from imednet.models.study_config import StudyConfiguration

    study_cfg = StudyConfiguration(
        study_key="STUDY_PANDAS",
        phi_fields=["localPatientTag"],
        terminology_lookups={"gender": {"2": "Female"}},
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == "STUDY_PANDAS" else None,
    )

    rec = Record(
        record_id=808,
        study_key="STUDY_PANDAS",
        subject_key="SUBJ-808",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        record_data={
            "SSN": "123-45-6789",
            "DOB": "1977-08-20",
            "patientName": "Ada Lovelace",
            "email": "ada@example.org",
            "phone": "555-0177",
            "address": "12 Analytical Way",
            "localPatientTag": "TAG-808",
            "gender": "2",
            "comment": "=HYPERLINK(\"http://evil\")",
        },
    )
    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: [rec.model_copy(deep=True)]
    sdk.variables.list.return_value = [
        SimpleNamespace(variable_name="SSN", label="Government Identifier"),
        SimpleNamespace(variable_name="DOB", label="Birth Calendar"),
        SimpleNamespace(variable_name="patientName", label="Participant Alias"),
        SimpleNamespace(variable_name="email", label="Contact Address"),
        SimpleNamespace(variable_name="phone", label="Telephone Line"),
        SimpleNamespace(variable_name="address", label="Street Residence"),
        SimpleNamespace(variable_name="localPatientTag", label="Site Tag"),
        SimpleNamespace(variable_name="gender", label="Gender Label"),
        SimpleNamespace(variable_name="comment", label="Clinical Comment"),
    ]

    out_csv = tmp_path / "labeled_pandas.csv"
    export_records_csv(
        sdk,
        "STUDY_PANDAS",
        str(out_csv),
        use_labels_as_columns=True,
    )
    df = pd.read_csv(out_csv)
    for col in (
        "Government Identifier",
        "Birth Calendar",
        "Participant Alias",
        "Contact Address",
        "Telephone Line",
        "Street Residence",
        "Site Tag",
    ):
        assert df.loc[0, col] == "***MASKED***"
    assert df.loc[0, "Gender Label"] == "Female"
    assert str(df.loc[0, "Clinical Comment"]).startswith("'=")
    assert df.loc[0, "subject_key"] == "SUBJ-808"
    assert df.loc[0, "study_key"] == "STUDY_PANDAS"
    assert df.loc[0, "record_id"] == 808


def test_is_custom_phi_column_matches_collapsed_keys_and_preserves_exemptions() -> None:
    """Verify _is_custom_phi_column matches normalized and collapsed Study PHI keys while exempting structural IDs."""
    from imednet.integrations.enrichment import EnrichmentPipeline
    from imednet.models.study_config import StudyConfiguration

    cfg = StudyConfiguration(
        studyKey="STUDY_PHI_COLLAPSE",
        version="1.0",
        phiFields=["medicalRecordNumber", "siteChartId", "study_key", "subject_key", "record_id"],
    )
    pipeline = EnrichmentPipeline(cfg)

    assert _is_custom_phi_column("Medical Record Number", pipeline.phi_keys) is True
    assert _is_custom_phi_column("medicalrecordnumber", pipeline.phi_keys) is True
    assert _is_custom_phi_column("Site Chart ID", pipeline.phi_keys) is True
    assert _is_custom_phi_column("siteChartId", pipeline.phi_keys) is True
    assert _is_custom_phi_column("demographics_siteChartId", pipeline.phi_keys) is True
    assert _is_custom_phi_column("Site Chart ID", {"sitechartid"}) is True
    assert _is_custom_phi_column("systolicBp", pipeline.phi_keys) is False
    assert _is_custom_phi_column("", pipeline.phi_keys) is False
    assert _is_custom_phi_column("---", pipeline.phi_keys) is False
    assert _is_custom_phi_column("Medical Record Number", set()) is False

    for exempt_col in (
        "study_key",
        "subject_key",
        "record_id",
        "Study Key",
        "Subject Key",
        "Record ID",
    ):
        assert _is_custom_phi_column(exempt_col, pipeline.phi_keys) is False
        assert _is_custom_phi_column(exempt_col, {"study_key", "subject_key", "record_id"}) is False


def test_records_to_dataframe_masks_sensitive_and_brthdtc_variable_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify records_to_dataframe masks coded variables whose mapped Variable.label is sensitive, BRTHDTC, or custom PHI."""
    from imednet.integrations import enrichment as enrichment_mod
    from imednet.models.study_config import StudyConfiguration

    study_key = "STUDY_LABEL_DF"
    cfg = StudyConfiguration(
        studyKey=study_key,
        version="1.0",
        phiFields=["medicalRecordNumber", "siteChartId"],
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: cfg if sk == study_key else None,
    )

    rec = Record(
        record_id=501,
        study_key=study_key,
        subject_key="SUBJ-501",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        record_data={
            "Q_CODE_01": "SYNTH_PHI_NAME_MARKER",
            "Q_CODE_02": "1979-05-14",
            "Q_CODE_03": "1984-11-02",
            "Q_CODE_05": "SYNTH_CUSTOM_MRN_MARKER",
            "Q_DUP_A": "SYNTH_DUP_NAME_A",
            "Q_DUP_B": "SYNTH_DUP_NAME_B",
            "Q_META_COLLIDE": "SAFE_CODE_VAL",
            "Q_SAFE": 118,
        },
    )
    label_map = {
        "Q_CODE_01": "Patient Name",
        "Q_CODE_02": "Date of Birth",
        "Q_CODE_03": "BRTHDTC",
        "Q_CODE_05": "Medical Record Number",
        "Q_DUP_A": "Patient Initials",
        "Q_DUP_B": "Patient Initials",
        "Q_META_COLLIDE": "subject_key",
        "Q_SAFE": "Systolic BP",
        "subject_key": "Should Not Rename Metadata",
    }

    masked_df = records_to_dataframe(
        [rec],
        flatten=True,
        mask_phi=True,
        study_key=study_key,
        label_map=label_map,
    )
    assert masked_df.loc[0, "Patient Name"] == MASKED_SENTINEL
    assert masked_df.loc[0, "Date of Birth"] == MASKED_SENTINEL
    assert masked_df.loc[0, "BRTHDTC"] == MASKED_SENTINEL
    assert masked_df.loc[0, "Medical Record Number"] == MASKED_SENTINEL
    dup_cols = masked_df.loc[:, "Patient Initials"]
    assert isinstance(dup_cols, pd.DataFrame)
    assert list(dup_cols.iloc[0]) == [MASKED_SENTINEL, MASKED_SENTINEL]
    assert masked_df.loc[0, "Systolic BP"] == 118
    assert masked_df.loc[0, "subject_key"] == "SUBJ-501"
    assert masked_df.loc[0, "Q_META_COLLIDE"] == "SAFE_CODE_VAL"
    assert masked_df.loc[0, "study_key"] == study_key
    assert masked_df.loc[0, "record_id"] == 501

    unmasked_df = records_to_dataframe(
        [rec],
        flatten=True,
        mask_phi=False,
        study_key=study_key,
        label_map=label_map,
    )
    assert unmasked_df.loc[0, "Patient Name"] == "SYNTH_PHI_NAME_MARKER"
    assert unmasked_df.loc[0, "Date of Birth"] == "1979-05-14"
    assert unmasked_df.loc[0, "BRTHDTC"] == "1984-11-02"
    assert unmasked_df.loc[0, "Medical Record Number"] == "SYNTH_CUSTOM_MRN_MARKER"
    assert unmasked_df.loc[0, "Systolic BP"] == 118
    assert unmasked_df.loc[0, "subject_key"] == "SUBJ-501"


def test_export_records_csv_masks_sensitive_variable_labels_and_prevents_phi_leaks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Verify export_records_csv masks coded Variables whose Variable.label matches SensitivityRegistry, BRTHDTC, or StudyConfiguration.phi_fields."""
    from types import SimpleNamespace

    from imednet.integrations import enrichment as enrichment_mod
    from imednet.models.study_config import StudyConfiguration

    study_key = "STUDY_CSV_LABEL_PHI"
    phi_variables = [
        ("Q_CODE_01", "Patient Name", "SYNTH_PHI_NAME_MARKER"),
        ("Q_CODE_02", "Date of Birth", "1979-05-14"),
        ("Q_CODE_03", "SSN", "000-00-9999"),
        ("Q_CODE_04", "Email", "synth-marker@example.invalid"),
        ("Q_CODE_05", "Medical Record Number", "SYNTH_CUSTOM_MRN_MARKER"),
        ("Q_CODE_06", "siteChartId", "SYNTH_CUSTOM_CHART_MARKER"),
        ("Q_CODE_07", "BRTHDTC", "1979-05-14"),
        ("Q_CODE_08", "Phone", "+1-555-0199"),
    ]
    cfg = StudyConfiguration(
        studyKey=study_key,
        version="1.0",
        phiFields=["medicalRecordNumber", "siteChartId"],
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: cfg if sk == study_key else None,
    )

    rec = Record(
        record_id=909,
        study_key=study_key,
        subject_key="SUBJ-909",
        visit_id=1,
        form_id=10,
        record_status="Complete",
        record_data={
            **{vcode: marker for vcode, _, marker in phi_variables},
            "Q_VITAL_HR": 72,
            "Q_FORMULA_NOTE": "=SUM(A1:A2)",
        },
    )
    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: [rec.model_copy(deep=True)]
    sdk.variables.list.return_value = [
        *[SimpleNamespace(variable_name=vcode, label=vlabel) for vcode, vlabel, _ in phi_variables],
        SimpleNamespace(variable_name="Q_VITAL_HR", label="Heart Rate"),
        SimpleNamespace(variable_name="Q_FORMULA_NOTE", label="Investigator Note"),
        SimpleNamespace(variable_name="Q_EMPTY_LABEL", label="   "),
    ]

    csv_path = tmp_path / "masked_labeled_export.csv"
    export_records_csv(
        sdk,
        study_key,
        str(csv_path),
        flatten=True,
        mask_phi=True,
        use_labels_as_columns=True,
    )
    with open(csv_path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    csv_leaks = [
        f"{vlabel}={rows[0].get(vlabel)!r}"
        for _, vlabel, marker in phi_variables
        if rows[0].get(vlabel) == marker
    ]
    assert not csv_leaks
    for _, vlabel, _ in phi_variables:
        assert rows[0][vlabel] == MASKED_SENTINEL
    assert rows[0]["Heart Rate"] == "72"
    assert rows[0]["Investigator Note"] == "'=SUM(A1:A2)"
    assert rows[0]["study_key"] == study_key
    assert rows[0]["subject_key"] == "SUBJ-909"
    assert rows[0]["record_id"] == "909"

    unmasked_csv_path = tmp_path / "unmasked_labeled_export.csv"
    export_records_csv(
        sdk,
        study_key,
        str(unmasked_csv_path),
        flatten=True,
        mask_phi=False,
        use_labels_as_columns=True,
    )
    with open(unmasked_csv_path, encoding="utf-8", newline="") as fh:
        unmasked_rows = list(csv.DictReader(fh))
    assert unmasked_rows[0]["Patient Name"] == "SYNTH_PHI_NAME_MARKER"
    assert unmasked_rows[0]["Date of Birth"] == "1979-05-14"
    assert unmasked_rows[0]["BRTHDTC"] == "1979-05-14"
    assert unmasked_rows[0]["Medical Record Number"] == "SYNTH_CUSTOM_MRN_MARKER"
    assert unmasked_rows[0]["siteChartId"] == "SYNTH_CUSTOM_CHART_MARKER"
    assert unmasked_rows[0]["Phone"] == "'+1-555-0199"
    assert unmasked_rows[0]["Investigator Note"] == "'=SUM(A1:A2)"

    sdk_no_vars = MagicMock(spec=["records"])
    sdk_no_vars.records.list.return_value = [rec.model_copy(deep=True)]
    fallback_csv_path = tmp_path / "no_vars_export.csv"
    export_records_csv(
        sdk_no_vars,
        study_key,
        str(fallback_csv_path),
        flatten=True,
        mask_phi=True,
        use_labels_as_columns=True,
    )
    with open(fallback_csv_path, encoding="utf-8", newline="") as fh:
        fallback_rows = list(csv.DictReader(fh))
    assert fallback_rows[0]["Q_VITAL_HR"] == "72"
    assert fallback_rows[0]["subject_key"] == "SUBJ-909"
