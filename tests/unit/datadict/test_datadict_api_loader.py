"""Tests for live API data dictionary loader."""

from __future__ import annotations

from unittest.mock import Mock

from imednet.datadict.loaders.api import load_from_api


def test_load_from_api_mocked_sdk() -> None:
    """Test extracting forms and variables from live SDK with documented partial structure."""
    mock_sdk = Mock()
    mock_sdk.forms.list.return_value = [
        {"form_key": "DM", "form_name": "Demographics", "form_id": 1001, "form_type": "Patient"},
        {"form_key": "AE", "form_name": "Adverse Events", "form_id": 1002, "form_type": "Patient"},
    ]
    mock_sdk.variables.list.return_value = [
        {
            "form_key": "DM",
            "variable_name": "AGE",
            "label": "Subject Age",
            "variable_type": "Number",
        },
        {
            "form_key": "AE",
            "variable_name": "AESTDAT",
            "label": "Start Date",
            "variable_type": "Date",
        },
    ]

    dd = load_from_api(mock_sdk, study_key="STUDY_ABC")

    assert dd.metadata.studyKey == "STUDY_ABC"
    assert dd.metadata.source == "api"
    assert any("Business logic rules and choices" in w for w in dd.metadata.validationWarnings)

    assert len(dd.forms) == 2
    dm_form = next(f for f in dd.forms if f.formKey == "DM")
    assert dm_form.formName == "Demographics"
    assert len(dm_form.variables) == 1
    assert dm_form.variables[0].variableName == "AGE"
    assert dm_form.variables[0].choices == []
    assert dm_form.businessLogic == []
    assert dd.unassignedBusinessLogic == []
