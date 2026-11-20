"""Unit tests for study structure."""

import asyncio
from unittest.mock import MagicMock

import pytest

from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.variables import Variable
from imednet_workflows.study_structure import async_get_study_structure, get_study_structure


@pytest.mark.parametrize("async_mode", [False, True])
def test_get_study_structure_aggregates_related_data(async_mode: bool) -> None:
    """Test that get study structure aggregates related data."""
    sdk = MagicMock()
    interval = Interval(
        interval_id=1,
        interval_name="INT1",
        interval_sequence=1,
        interval_description="desc",
        interval_group_name="grp",
        date_created="2024-01-01T00:00:00Z",
        date_modified="2024-01-01T00:00:00Z",
        forms=[FormSummary(form_id=1, form_key="F1", form_name="Form1")],
    )
    form = Form(
        form_id=1,
        form_key="F1",
        form_name="Form1",
        date_created="2024-01-01T00:00:00Z",
        date_modified="2024-01-01T00:00:00Z",
    )
    variable = Variable(variable_id=1, variable_name="V1", label="Var 1", form_id=1)

    if async_mode:

        async def async_mock_return(items):
            """Helper function to async mock return."""
            return items

        sdk.async_get_intervals = MagicMock(return_value=async_mock_return([interval]))
        sdk.async_get_forms = MagicMock(return_value=async_mock_return([form]))
        sdk.async_get_variables = MagicMock(return_value=async_mock_return([variable]))
        structure = asyncio.run(async_get_study_structure(sdk, "STUDY"))
        sdk.async_get_intervals.assert_called_once_with("STUDY")
        sdk.async_get_forms.assert_called_once_with("STUDY")
        sdk.async_get_variables.assert_called_once_with("STUDY")
    else:
        sdk.get_intervals.return_value = [interval]
        sdk.get_forms.return_value = [form]
        sdk.get_variables.return_value = [variable]
        structure = get_study_structure(sdk, "STUDY")
        sdk.get_intervals.assert_called_once_with("STUDY")
        sdk.get_forms.assert_called_once_with("STUDY")
        sdk.get_variables.assert_called_once_with("STUDY")

    assert structure.study_key == "STUDY"
    assert structure.intervals[0].forms[0].variables[0].variable_name == "V1"
    assert [f.form_key for f in structure.forms] == ["F1"]


def test_build_study_structure_populates_standalone_forms_and_handles_none_form_id() -> None:
    """Verify standalone forms populate StudyStructure.forms and form_id=None summaries do not raise."""
    sdk = MagicMock()
    interval = Interval(
        interval_id=401,
        interval_name="Visit 1",
        interval_sequence=1,
        interval_description="Baseline",
        interval_group_name="Core",
        date_created="2024-01-01T00:00:00Z",
        date_modified="2024-01-01T00:00:00Z",
        forms=[
            FormSummary(form_id=None, form_key="UNLINKED_A", form_name="Unlinked A"),
            FormSummary(form_id=302, form_key="F_VS", form_name="Vital Signs"),
            FormSummary(form_id=None, form_key="UNLINKED_B", form_name="Unlinked B"),
        ],
    )
    forms = [
        Form(
            form_id=301,
            form_key="F_DM",
            form_name="Demographics",
            form_type="Enrollment",
            date_created="2024-01-01T00:00:00Z",
            date_modified="2024-01-01T00:00:00Z",
        ),
        Form(
            form_id=302,
            form_key="F_VS",
            form_name="Vital Signs",
            form_type="Standard",
            date_created="2024-01-01T00:00:00Z",
            date_modified="2024-01-01T00:00:00Z",
        ),
        Form(
            form_id=303,
            form_key="F_AE",
            form_name="Adverse Events",
            form_type="Standard",
            date_created="2024-01-01T00:00:00Z",
            date_modified="2024-01-01T00:00:00Z",
        ),
    ]
    variables = [
        Variable(variable_id=10, variable_name="AGE", label="Age", form_id=301),
        Variable(variable_id=20, variable_name="SYSBP", label="Systolic BP", form_id=302),
        Variable(variable_id=30, variable_name="AETERM", label="AE Term", form_id=303),
    ]

    sdk.get_intervals.return_value = [interval]
    sdk.get_forms.return_value = forms
    sdk.get_variables.return_value = variables

    structure = get_study_structure(sdk, "STUDY_DEMO")
    assert [f.form_key for f in structure.intervals[0].forms] == ["F_VS"]
    assert [f.form_key for f in structure.forms] == ["F_DM", "F_VS", "F_AE"]
    assert structure.forms[0].variables[0].variable_name == "AGE"
    assert structure.forms[2].variables[0].variable_name == "AETERM"
