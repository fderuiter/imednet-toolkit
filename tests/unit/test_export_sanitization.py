"""Unit tests for export sanitization."""

import warnings
from unittest.mock import MagicMock

import pandas as pd
import pytest

import imednet.integrations.export as export_mod


@pytest.fixture
def mock_record_mapper(monkeypatch):
    """Fixture to mock RecordMapper with a configurable DataFrame."""
    mapper_inst = MagicMock()

    def _setup(df: pd.DataFrame):
        mapper_inst.dataframe.return_value = df
        mapper_inst._fetch_variable_metadata.return_value = (
            list(df.columns),
            {c: c for c in df.columns},
        )
        mapper_inst._iter_records.return_value = ["mock_record"]
        mapper_inst._parse_records.return_value = ([{}], 0)
        mapper_inst._build_dataframe.return_value = df
        monkeypatch.setattr(
            export_mod, "apply_quality_gate", MagicMock(return_value=["mock_record"])
        )
        monkeypatch.setattr(
            export_mod,
            "_record_mapper",
            MagicMock(return_value=MagicMock(return_value=mapper_inst)),
        )
        return mapper_inst

    return _setup


def test_export_to_csv_sanitization(tmp_path, mock_record_mapper, monkeypatch):
    """Test that CSV export sanitizes formulas and raises no warnings."""
    df = pd.DataFrame({"safe": ["hello"], "unsafe": ["=cmd"]})
    mock_record_mapper(df)
    monkeypatch.setattr(export_mod, "apply_quality_gate", lambda s, sk, rr, c: rr)

    sdk = MagicMock()
    path = tmp_path / "out.csv"

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")  # Catch all warnings
        export_mod.export_to_csv(sdk, "STUDY", str(path))

        # Ensure no Pandas4Warning or other warnings related to sanitization
        relevant_warnings = [
            warning
            for warning in w
            if "Pandas4Warning" in str(warning.category.__name__)
            or "select_dtypes" in str(warning.message)
        ]
        assert not relevant_warnings, (
            f"Caught unexpected warnings: {[str(warn.message) for warn in relevant_warnings]}"
        )

    content = path.read_text()
    # Check that unsafe content is prefixed with a single quote
    assert "'=cmd" in content
    # Check that safe content is untouched (though to_csv might quote/escape, expected 'hello')
    assert "hello" in content


def test_export_to_excel_sanitization(tmp_path, mock_record_mapper, monkeypatch):
    """Test that Excel export sanitizes formulas."""
    df = pd.DataFrame({"safe": ["hello"], "unsafe": ["=cmd"], "num": [123]})
    mock_record_mapper(df)
    monkeypatch.setattr(export_mod, "apply_quality_gate", lambda s, sk, rr, c: rr)

    sdk = MagicMock()
    path = tmp_path / "out.xlsx"

    # We need to inspect the DataFrame passed to to_excel, because reading xlsx requires openpyxl
    # and might be complex to verify directly in test environment without full setup.
    # Instead, we patch pd.DataFrame.to_excel to verify the dataframe state just before export.

    captured_df = None

    def mock_to_excel(self, excel_writer, index=False, **kwargs):
        """Helper function to mock to excel."""
        nonlocal captured_df
        captured_df = self.copy()
        # Call original to generate file if needed, but we can skip file generation
        # for speed/dependency. Just creating an empty file to satisfy existence
        # checks if any. However, export_mod doesn't check existence after.
        # But we need to ensure the method signature matches.

    monkeypatch.setattr(pd.DataFrame, "to_excel", mock_to_excel)

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        export_mod.export_to_excel(sdk, "STUDY", str(path))

        relevant_warnings = [
            warning
            for warning in w
            if "Pandas4Warning" in str(warning.category.__name__)
            or "select_dtypes" in str(warning.message)
        ]
        assert not relevant_warnings, (
            f"Caught unexpected warnings: {[str(warn.message) for warn in relevant_warnings]}"
        )

    assert captured_df is not None
    assert captured_df["unsafe"].iloc[0] == "'=cmd"
    assert captured_df["safe"].iloc[0] == "hello"
    assert captured_df["num"].iloc[0] == 123


def test_sanitization_does_not_affect_non_strings(tmp_path, mock_record_mapper, monkeypatch):
    """Test that sanitization ignores non-string columns."""
    df = pd.DataFrame(
        {
            "int_col": [1, 2, 3],
            "float_col": [1.1, 2.2, 3.3],
            "bool_col": [True, False, True],
            "str_col": ["=danger", "safe", "+formula"],
        }
    )
    mock_record_mapper(df)
    monkeypatch.setattr(export_mod, "apply_quality_gate", lambda s, sk, rr, c: rr)
    sdk = MagicMock()
    path = tmp_path / "out.csv"

    export_mod.export_to_csv(sdk, "STUDY", str(path))

    content = path.read_text()

    # Check sanitization occurred
    assert "'=danger" in content
    assert "'+formula" in content

    # Check numeric values are preserved (as strings in CSV)
    assert "1" in content
    assert "1.1" in content
    assert "True" in content

    # Ensure they were not somehow quoted with single quote like sanitization does
    assert "'1" not in content
    assert "'1.1" not in content
    assert "'True" not in content


def test_mask_df_normalizes_sensitive_column_keys():
    """Test that _mask_df masks columns with uppercase acronyms and camelCase names."""
    df = pd.DataFrame(
        {
            "DOB": ["1990-01-01"],
            "SSN": ["123-45-6789"],
            "patientName": ["Jane Doe"],
            "DateOfBirth": ["1992-02-02"],
            "PatientInitials": ["JD"],
            "subject_key": ["SUBJ-001"],
            "RecordId": ["REC-100"],
            "visit_name": ["Baseline"],
            "nested_data": [{"DOB": "1980-01-01", "safe": "val"}],
        }
    )

    masked_df = export_mod._mask_df(df)

    assert masked_df["DOB"].iloc[0] == "***MASKED***"
    assert masked_df["SSN"].iloc[0] == "***MASKED***"
    assert masked_df["patientName"].iloc[0] == "***MASKED***"
    assert masked_df["DateOfBirth"].iloc[0] == "***MASKED***"
    assert masked_df["PatientInitials"].iloc[0] == "***MASKED***"
    # Exempt columns remain unmasked
    assert masked_df["subject_key"].iloc[0] == "SUBJ-001"
    assert masked_df["RecordId"].iloc[0] == "REC-100"
    assert masked_df["visit_name"].iloc[0] == "Baseline"
    # Nested dictionaries inside object columns are also masked
    assert masked_df["nested_data"].iloc[0]["DOB"] == "***MASKED***"
    assert masked_df["nested_data"].iloc[0]["safe"] == "val"


def test_export_to_csv_masks_mixed_case_phi(tmp_path, mock_record_mapper, monkeypatch):
    """Test that export_to_csv masks mixed-case PHI headers like DOB, SSN, and patientName."""
    df = pd.DataFrame(
        {
            "DOB": ["1990-01-01"],
            "SSN": ["123-45-6789"],
            "patientName": ["Jane Doe"],
            "subject_key": ["SUBJ-001"],
            "notes": ["Sample clinical notes"],
        }
    )
    mock_record_mapper(df)
    monkeypatch.setattr(export_mod, "apply_quality_gate", lambda s, sk, rr, c: rr)
    sdk = MagicMock()
    path = tmp_path / "phi_out.csv"

    export_mod.export_to_csv(sdk, "STUDY", str(path))

    content = path.read_text()
    assert "***MASKED***" in content
    assert "1990-01-01" not in content
    assert "123-45-6789" not in content
    assert "Jane Doe" not in content
    assert "SUBJ-001" in content
    assert "Sample clinical notes" in content


def test_export_to_sql_variables_masks_mixed_case(monkeypatch):
    """Test that export_to_sql_variables masks variables with uppercase and camelCase names."""
    from sqlalchemy import create_engine, text

    record = MagicMock()
    record.record_id = "REC01"
    record.form_id = "FORM01"
    record.date_modified = "2026-01-01T00:00:00Z"
    record.record_data = {
        "DOB": "1990-01-01",
        "patientName": "Jane Doe",
        "SSN": "123-45-6789",
        "vital_sign": "normal",
    }

    mock_mapper = MagicMock()
    mock_mapper._fetch_records.return_value = [record]
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: lambda sdk: mock_mapper)

    engine = create_engine("sqlite:///:memory:")
    # We pass the memory engine or connection string. Since each create_engine("sqlite:///:memory:")
    # creates a new in-memory db, let's use a temporary sqlite file or patch create_engine
    monkeypatch.setattr("sqlalchemy.create_engine", lambda conn_str: engine)

    sdk = MagicMock()
    export_mod.export_to_long_sql(sdk, "STUDY", "vars", "sqlite:///:memory:")

    with engine.connect() as conn:
        result = conn.execute(text("SELECT variable_name, value FROM vars")).fetchall()
        val_map = {row[0]: row[1] for row in result}

    assert val_map["DOB"] == "***MASKED***"
    assert val_map["patientName"] == "***MASKED***"
    assert val_map["SSN"] == "***MASKED***"
    assert val_map["vital_sign"] == "normal"
