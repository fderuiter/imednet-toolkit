"""Unit tests for schema profiler."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet_workflows.schema_profiler import SchemaProfiler


def test_schema_profiler_builds_form_and_field_profiles() -> None:
    """Test that schema profiler builds form and field profiles."""
    sdk = MagicMock()
    sdk.get_forms.return_value = [Form(form_key="AE", form_name="Adverse Events", form_id=10)]
    sdk.get_variables.return_value = [
        Variable(
            form_id=10,
            form_key="AE",
            variable_name="AGE",
            label="Age",
            variable_type="integer",
        ),
        Variable(
            form_id=10,
            form_key="AE",
            variable_name="CONSENT_DATE",
            label="Consent Date",
            variable_type="date",
        ),
        Variable(
            form_id=10,
            form_key="AE",
            variable_name="SMOKER",
            label="Smoker",
            variable_type="boolean",
        ),
        Variable(
            form_id=10,
            form_key="AE",
            variable_name="COMMENT",
            label="Comment",
            variable_type="string",
        ),
    ]
    records = [
        Record(
            study_key="STUDY",
            form_id=10,
            form_key="AE",
            record_id=1,
            subject_key="S1",
            date_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
            record_data={
                "AGE": 34,
                "CONSENT_DATE": "2024-01-10",
                "SMOKER": True,
                "COMMENT": "baseline",
            },
        ),
        Record(
            study_key="STUDY",
            form_id=10,
            form_key="AE",
            record_id=2,
            subject_key="S2",
            date_modified=datetime(2024, 1, 2, tzinfo=timezone.utc),
            record_data={
                "AGE": None,
                "CONSENT_DATE": "2024-01-12",
                "SMOKER": False,
                "COMMENT": "",
                "EXTRA_FIELD": "investigator note",
            },
        ),
    ]

    profiler = SchemaProfiler(sdk)
    profiles = profiler.profile_records("STUDY", records=records)

    sdk.get_forms.assert_called_once_with(study_key="STUDY")
    sdk.get_variables.assert_called_once_with(study_key="STUDY")

    form_profile = profiles["AE"]
    assert form_profile.form_name == "Adverse Events"
    assert form_profile.record_count == 2
    assert form_profile.fields["AGE"].population_rate == 50.0
    assert form_profile.fields["AGE"].inferred_type == "numeric"
    assert form_profile.fields["AGE"].unique_values == ["34"]
    assert form_profile.fields["CONSENT_DATE"].inferred_type == "date"
    assert form_profile.fields["SMOKER"].inferred_type == "boolean"
    assert form_profile.fields["COMMENT"].population_rate == 50.0
    assert form_profile.fields["EXTRA_FIELD"].label == "EXTRA_FIELD"
    assert form_profile.fields["EXTRA_FIELD"].unique_count == 1


def test_schema_profiler_uses_loader_when_records_are_not_supplied() -> None:
    """Test that schema profiler uses loader when records are not supplied."""
    sdk = MagicMock()
    loader = MagicMock()
    loader.load_records.return_value = [
        Record(
            study_key="STUDY",
            form_id=10,
            form_key="LAB",
            record_id=1,
            subject_key="S1",
            date_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
            record_data={"RESULT": "7.2"},
        )
    ]
    sdk.get_forms.return_value = [Form(form_key="LAB", form_name="Labs", form_id=10)]
    sdk.get_variables.return_value = [
        Variable(
            form_id=10,
            form_key="LAB",
            variable_name="RESULT",
            label="Result",
            variable_type="float",
        )
    ]

    profiler = SchemaProfiler(sdk, loader=loader)
    profiles = profiler.profile_records("STUDY")

    loader.load_records.assert_called_once_with("STUDY")
    assert profiles["LAB"].fields["RESULT"].inferred_type == "numeric"


def test_schema_profiler_streams_chunked_loader_records() -> None:
    """Test that schema profiler streams chunked loader records."""
    sdk = MagicMock()

    class _ChunkedLoader:
        """Test suite for  ChunkedLoader."""

        def __init__(self) -> None:
            """Initialize the test object."""
            self.load_records = MagicMock()
            self.sync_records = MagicMock()
            self._iter_mock = MagicMock()

        def iter_cached_records(self, study_key: str):
            """Helper function to iter cached records."""
            self._iter_mock(study_key)
            return iter(
                [
                    Record(
                        study_key="STUDY",
                        form_id=10,
                        form_key="LAB",
                        record_id=1,
                        subject_key="S1",
                        date_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
                        record_data={"RESULT": "7.2"},
                    ),
                    Record(
                        study_key="STUDY",
                        form_id=10,
                        form_key="LAB",
                        record_id=2,
                        subject_key="S2",
                        date_modified=datetime(2024, 1, 2, tzinfo=timezone.utc),
                        record_data={"RESULT": "8.1"},
                    ),
                ]
            )

    loader = _ChunkedLoader()
    sdk.get_forms.return_value = [Form(form_key="LAB", form_name="Labs", form_id=10)]
    sdk.get_variables.return_value = [
        Variable(
            form_id=10,
            form_key="LAB",
            variable_name="RESULT",
            label="Result",
            variable_type="float",
        )
    ]

    profiler = SchemaProfiler(sdk, loader=loader)
    profile = profiler.profile_records("STUDY")["LAB"]

    loader.sync_records.assert_called_once_with("STUDY")
    loader.load_records.assert_not_called()
    loader._iter_mock.assert_called_once_with("STUDY")
    assert profile.record_count == 2
    assert profile.fields["RESULT"].population_rate == 100.0


def test_schema_profiler_falls_back_form_name_to_form_key_and_guards_null_form_id() -> None:
    """Verify SchemaProfiler populates FormProfile.form_name with form_key when Form.form_name is None."""
    sdk = MagicMock()
    sdk.get_forms.return_value = [
        Form(form_key="DM_FORM", form_name=None, form_id=10),
        Form(form_key=None, form_name="Ignored Missing Key", form_id=20),
    ]
    sdk.get_variables.return_value = [
        Variable(
            form_id=10,
            form_key="DM_FORM",
            variable_name="BRTHDAT",
            label=None,
            variable_type="date",
        )
    ]
    records = [
        Record(
            study_key="STUDY-NULL-FORM",
            form_id=10,
            form_key="DM_FORM",
            record_id=1,
            subject_key="SUBJ-01",
            record_data={"BRTHDAT": "1985-06-15"},
        ),
        Record(
            study_key="STUDY-NULL-FORM",
            form_id=None,
            form_key=None,
            record_id=2,
            subject_key="SUBJ-02",
            record_data={"BRTHDAT": "1990-01-01"},
        ),
    ]

    profiler = SchemaProfiler(sdk)
    profiles = profiler.profile_records("STUDY-NULL-FORM", records=records)

    assert list(profiles.keys()) == ["DM_FORM"]
    dm_profile = profiles["DM_FORM"]
    assert dm_profile.form_key == "DM_FORM"
    assert dm_profile.form_name == "DM_FORM"
    assert dm_profile.record_count == 1
    assert dm_profile.fields["BRTHDAT"].label == "BRTHDAT"
