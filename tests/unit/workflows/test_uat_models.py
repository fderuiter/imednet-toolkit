"""Unit tests for uat models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from faker import Faker
from pydantic import ValidationError

from imednet_workflows.uat import (
    RecordTestType,
    UATFormSpec,
    UATSpecification,
    UATSubjectSpec,
    UATVariableSpec,
    VariableTestStrategy,
)

fake = Faker()


def _build_valid_spec() -> UATSpecification:
    """Helper function to  build valid spec."""
    variable = UATVariableSpec(
        variable_name="SYSTOLIC",
        variable_key="VAR-1",
        variable_type="Number",
        form_key="VITALS",
    )
    form = UATFormSpec(
        form_key="VITALS",
        form_name="Vitals",
        form_type="CRF",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        variables=[variable],
        subject_count=1,
    )
    subject = UATSubjectSpec(site_name="Main Site", subject_count=1)
    return UATSpecification(
        study_key=fake.bothify(text="STUDY-####"),
        study_name=fake.company(),
        generated_at=datetime(2026, 1, 1, 0, 0, 0),
        subject_specs=[subject],
        form_specs=[form],
    )


def test_json_round_trip_serialization() -> None:
    """Test that json round trip serialization."""
    spec = _build_valid_spec()
    payload = spec.model_dump_json()
    parsed = UATSpecification.model_validate_json(payload)
    assert parsed == spec


def test_alias_dump_uses_camel_case() -> None:
    """Test that alias dump uses camel case."""
    spec = _build_valid_spec()
    payload = spec.model_dump(by_alias=True)
    assert "studyKey" in payload
    assert "formSpecs" in payload
    assert "subjectSpecs" in payload
    assert "generatedAt" in payload


def test_fixed_strategy_requires_fixed_value() -> None:
    """Test that fixed strategy requires fixed value."""
    with pytest.raises(ValidationError, match="fixed_value must be provided"):
        UATVariableSpec(
            variable_name="AGE",
            variable_key="AGE",
            variable_type="Number",
            form_key="DEMO",
            strategy=VariableTestStrategy.FIXED,
        )


@pytest.mark.parametrize("count", [0, 101])
def test_subject_count_bounds(count: int) -> None:
    """Test that subject count bounds."""
    with pytest.raises(ValidationError, match="subject_count must be between 1 and 100"):
        UATFormSpec(
            form_key="DEMO",
            form_name="Demographics",
            form_type="CRF",
            test_type=RecordTestType.CREATE_NEW_RECORD,
            subject_count=count,
        )
    with pytest.raises(ValidationError, match="subject_count must be between 1 and 100"):
        UATSubjectSpec(site_name="Main Site", subject_count=count)


def test_spec_version_is_pinned() -> None:
    """Test that spec version is pinned."""
    with pytest.raises(ValidationError, match="spec_version must be '1.0'"):
        UATSpecification(
            spec_version="2.0",
            study_key="S1",
            study_name="Study",
        )


def test_variables_must_have_unique_names() -> None:
    """Test that variables must have unique names."""
    with pytest.raises(ValidationError, match="duplicate variable_name"):
        UATFormSpec(
            form_key="LABS",
            form_name="Labs",
            form_type="CRF",
            test_type=RecordTestType.CREATE_NEW_RECORD,
            variables=[
                UATVariableSpec(
                    variable_name="HEMOGLOBIN",
                    variable_key="HGB-1",
                    variable_type="Number",
                    form_key="LABS",
                ),
                UATVariableSpec(
                    variable_name="HEMOGLOBIN",
                    variable_key="HGB-2",
                    variable_type="Number",
                    form_key="LABS",
                ),
            ],
        )


def test_enabled_forms_and_forms_by_type_filters_correctly() -> None:
    """Test that enabled forms and forms by type filters correctly."""
    enabled_register = UATFormSpec(
        form_key="SUBJECT",
        form_name="Subject Registration",
        form_type="CRF",
        test_type=RecordTestType.REGISTER_SUBJECT,
    )
    disabled_register = UATFormSpec(
        form_key="SUBJECT-2",
        form_name="Subject Registration Copy",
        form_type="CRF",
        test_type=RecordTestType.REGISTER_SUBJECT,
        enabled=False,
    )
    enabled_create = UATFormSpec(
        form_key="LABS",
        form_name="Labs",
        form_type="CRF",
        test_type=RecordTestType.CREATE_NEW_RECORD,
    )
    spec = UATSpecification(
        study_key="S1",
        study_name="Study",
        form_specs=[enabled_register, disabled_register, enabled_create],
    )
    assert spec.enabled_forms() == [enabled_register, enabled_create]
    assert spec.forms_by_type(RecordTestType.REGISTER_SUBJECT) == [enabled_register]


def test_from_json_loads_correctly(tmp_path: Path) -> None:
    """TODO: Add docstring."""
    json_file = tmp_path / "spec.json"
    json_file.write_text(
        '{"specVersion": "1.0", "studyKey": "S1", "studyName": "Study", "formSpecs": [], "subjectSpecs": []}',
        encoding="utf-8",
    )

    spec = UATSpecification.from_json(json_file)
    assert spec.study_key == "S1"


def test_uat_workflow_state_enum_and_terminals() -> None:
    """Verify all UATWorkflowState enum members and terminal state classifications."""
    from imednet_workflows.uat.models import (
        ACTIVE_UAT_STATES,
        TERMINAL_UAT_STATES,
        UATWorkflowState,
    )

    expected_states = {
        "PENDING",
        "INSPECTING",
        "BUILDING_SPEC",
        "GENERATING",
        "SUBMITTING",
        "MONITORING",
        "COMPLETED",
        "FAILED",
    }
    assert {s.value for s in UATWorkflowState} == expected_states

    assert TERMINAL_UAT_STATES == {UATWorkflowState.COMPLETED, UATWorkflowState.FAILED}
    assert ACTIVE_UAT_STATES == {
        UATWorkflowState.PENDING,
        UATWorkflowState.INSPECTING,
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.GENERATING,
        UATWorkflowState.SUBMITTING,
        UATWorkflowState.MONITORING,
    }

    assert UATWorkflowState.COMPLETED.is_terminal is True
    assert UATWorkflowState.FAILED.is_terminal is True
    assert UATWorkflowState.INSPECTING.is_terminal is False


def test_uat_workflow_state_valid_forward_transitions() -> None:
    """Test standard ordered forward progression across all states."""
    from imednet_workflows.uat.models import UATWorkflowState, validate_uat_transition

    path = [
        (UATWorkflowState.PENDING, UATWorkflowState.INSPECTING),
        (UATWorkflowState.INSPECTING, UATWorkflowState.BUILDING_SPEC),
        (UATWorkflowState.BUILDING_SPEC, UATWorkflowState.GENERATING),
        (UATWorkflowState.GENERATING, UATWorkflowState.SUBMITTING),
        (UATWorkflowState.SUBMITTING, UATWorkflowState.MONITORING),
        (UATWorkflowState.MONITORING, UATWorkflowState.COMPLETED),
    ]

    for from_s, to_s in path:
        assert from_s.can_transition_to(to_s) is True
        assert validate_uat_transition(from_s, to_s) is True


def test_uat_workflow_state_transitions_to_failed() -> None:
    """Verify every active state can transition to FAILED."""
    from imednet_workflows.uat.models import ACTIVE_UAT_STATES, UATWorkflowState

    for state in ACTIVE_UAT_STATES:
        assert state.can_transition_to(UATWorkflowState.FAILED) is True
        assert state.validate_transition_to(UATWorkflowState.FAILED) is True


def test_uat_workflow_state_invalid_transitions() -> None:
    """Verify backward, skipping, self, and terminal transitions raise InvalidUATStateTransitionError."""
    from imednet_workflows.uat.models import (
        InvalidUATStateTransitionError,
        UATWorkflowState,
        validate_uat_transition,
    )

    invalid_pairs = [
        # Backward transitions
        (UATWorkflowState.SUBMITTING, UATWorkflowState.INSPECTING),
        (UATWorkflowState.MONITORING, UATWorkflowState.SUBMITTING),
        (UATWorkflowState.BUILDING_SPEC, UATWorkflowState.INSPECTING),
        # Prerequisite skipping
        (UATWorkflowState.PENDING, UATWorkflowState.GENERATING),
        (UATWorkflowState.INSPECTING, UATWorkflowState.SUBMITTING),
        (UATWorkflowState.BUILDING_SPEC, UATWorkflowState.MONITORING),
        # Self-transitions
        (UATWorkflowState.INSPECTING, UATWorkflowState.INSPECTING),
        (UATWorkflowState.SUBMITTING, UATWorkflowState.SUBMITTING),
        # From terminal states
        (UATWorkflowState.COMPLETED, UATWorkflowState.PENDING),
        (UATWorkflowState.COMPLETED, UATWorkflowState.FAILED),
        (UATWorkflowState.FAILED, UATWorkflowState.PENDING),
        (UATWorkflowState.FAILED, UATWorkflowState.COMPLETED),
    ]

    for from_s, to_s in invalid_pairs:
        assert from_s.can_transition_to(to_s) is False
        with pytest.raises(InvalidUATStateTransitionError) as exc_info:
            validate_uat_transition(from_s, to_s)
        assert f"Cannot transition UAT workflow from {from_s.value} to {to_s.value}" in str(
            exc_info.value
        )


def test_uat_workflow_state_normalize() -> None:
    """Test state normalization from str and invalid type handling."""
    from imednet_workflows.uat.models import (
        InvalidUATStateTransitionError,
        UATWorkflowState,
        normalize_uat_state,
    )

    assert normalize_uat_state("pending") == UATWorkflowState.PENDING
    assert normalize_uat_state("SUBMITTING") == UATWorkflowState.SUBMITTING
    assert normalize_uat_state(UATWorkflowState.COMPLETED) == UATWorkflowState.COMPLETED

    with pytest.raises(InvalidUATStateTransitionError, match="Invalid UAT state: 'UNKNOWN'"):
        normalize_uat_state("UNKNOWN")

    with pytest.raises(InvalidUATStateTransitionError, match="Invalid UAT state type: int"):
        normalize_uat_state(123)  # type: ignore[arg-type]
