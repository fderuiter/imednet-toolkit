"""Pydantic models describing a complete UAT specification."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from imednet.spi.models import ImednetBaseModel


class UATBaseModel(ImednetBaseModel):
    """UAT base model with camelCase alias generation."""

    model_config = ConfigDict(
        alias_generator=to_camel,
    )


class VariableTestStrategy(str, Enum):
    """How a variable's test value should be determined."""

    SYNTHETIC = "synthetic"
    FIXED = "fixed"
    SKIP = "skip"
    BOUNDARY = "boundary"
    CODED_ALL = "coded_all"


class RecordTestType(str, Enum):
    """What kind of record submission is being tested."""

    REGISTER_SUBJECT = "register_subject"
    UPDATE_SCHEDULED_RECORD = "update_scheduled_record"
    CREATE_NEW_RECORD = "create_new_record"


class UATVariableSpec(UATBaseModel):
    """Variable-level test specification."""

    variable_name: str
    variable_key: str
    variable_type: str
    form_key: str
    strategy: VariableTestStrategy = VariableTestStrategy.SYNTHETIC
    fixed_value: Any | None = None
    min_value: float | None = None
    max_value: float | None = None
    max_length: int | None = None
    coded_values: list[str] = Field(default_factory=list)
    required: bool = False
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_fixed_strategy(cls, data: Any) -> Any:
        """Require a fixed value when FIXED strategy is selected."""
        if not isinstance(data, dict):
            return data

        strategy = data.get("strategy", VariableTestStrategy.SYNTHETIC)
        fixed_value = data.get("fixed_value", data.get("fixedValue"))
        if strategy in (VariableTestStrategy.FIXED, VariableTestStrategy.FIXED.value) and (
            fixed_value is None
        ):
            raise ValueError("fixed_value must be provided when strategy is 'fixed'.")
        return data


class UATFormSpec(UATBaseModel):
    """Form-level UAT test specification."""

    form_key: str
    form_name: str
    form_type: str
    test_type: RecordTestType
    interval_name: str | None = None
    subject_count: int = 1
    variables: list[UATVariableSpec] = Field(default_factory=list)
    required_variable_names: list[str] = Field(default_factory=list)
    enabled: bool = True
    notes: str | None = None

    @field_validator("subject_count")
    @classmethod
    def validate_subject_count(cls, value: int) -> int:
        """Enforce safe subject count bounds."""
        if not 1 <= value <= 100:
            raise ValueError("subject_count must be between 1 and 100.")
        return value

    @field_validator("variables")
    @classmethod
    def validate_unique_variable_names(
        cls, variables: list[UATVariableSpec]
    ) -> list[UATVariableSpec]:
        """Ensure variables are uniquely identified by variable_name."""
        seen_names: set[str] = set()
        duplicates: set[str] = set()
        for variable in variables:
            if variable.variable_name in seen_names:
                duplicates.add(variable.variable_name)
            seen_names.add(variable.variable_name)
        if duplicates:
            duplicate_names = ", ".join(sorted(duplicates))
            raise ValueError(
                f"variables contains duplicate variable_name entries: {duplicate_names}."
            )
        return variables


class UATSubjectSpec(UATBaseModel):
    """Defines synthetic test subjects to register before form submission."""

    site_name: str
    subject_count: int = 1
    subject_key_prefix: str = "UAT-TEST-"
    keyword_tag: str = "UAT"

    @field_validator("subject_count")
    @classmethod
    def validate_subject_count(cls, value: int) -> int:
        """Enforce safe subject count bounds."""
        if not 1 <= value <= 100:
            raise ValueError("subject_count must be between 1 and 100.")
        return value


class UATSpecification(UATBaseModel):
    """Root model for a complete UAT test plan."""

    spec_version: str = "1.0"
    study_key: str
    study_name: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    generated_by: str = "imednet-sdk-uat"
    subject_specs: list[UATSubjectSpec] = Field(default_factory=list)
    form_specs: list[UATFormSpec] = Field(default_factory=list)
    global_date_value: date | None = None
    global_text_length: int = 10
    forms_snapshot_count: int = 0
    variables_snapshot_count: int = 0
    intervals_snapshot_count: int = 0
    sites_snapshot_count: int = 0

    @field_validator("spec_version")
    @classmethod
    def validate_spec_version(cls, value: str) -> str:
        """Pin this release to v1.0 schema."""
        if value != "1.0":
            raise ValueError("spec_version must be '1.0'.")
        return value

    def enabled_forms(self) -> list[UATFormSpec]:
        """Return only enabled forms."""
        return [form for form in self.form_specs if form.enabled]

    def forms_by_type(self, test_type: RecordTestType) -> list[UATFormSpec]:
        """Return enabled forms matching a specific test type."""
        return [form for form in self.enabled_forms() if form.test_type == test_type]

    @classmethod
    def from_json(cls, payload: str | bytes | Path) -> UATSpecification:
        """Load a UAT specification from JSON text, bytes, or file path."""
        if isinstance(payload, Path):
            content = payload.read_text(encoding="utf-8")
        elif isinstance(payload, bytes):
            content = payload.decode("utf-8")
        else:
            content = payload

        data = json.loads(content)
        return cls.model_validate(data)


class UATWorkflowState(str, Enum):
    """Lifecycle states for a UAT workflow execution."""

    PENDING = "PENDING"
    INSPECTING = "INSPECTING"
    BUILDING_SPEC = "BUILDING_SPEC"
    GENERATING = "GENERATING"
    SUBMITTING = "SUBMITTING"
    MONITORING = "MONITORING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    def can_transition_to(self, target: UATWorkflowState | str) -> bool:
        """Check if this state can transition to target state."""
        return is_valid_uat_transition(self, target)

    def validate_transition_to(self, target: UATWorkflowState | str) -> bool:
        """Validate transition from this state to target state."""
        return validate_uat_transition(self, target)

    @property
    def allowed_transitions(self) -> set[UATWorkflowState]:
        """Return the set of allowed target states from this state."""
        return ALLOWED_UAT_TRANSITIONS.get(self, set())

    @property
    def is_terminal(self) -> bool:
        """Return True if this is a terminal state."""
        return self in TERMINAL_UAT_STATES


TERMINAL_UAT_STATES: set[UATWorkflowState] = {
    UATWorkflowState.COMPLETED,
    UATWorkflowState.FAILED,
}

ACTIVE_UAT_STATES: set[UATWorkflowState] = {
    UATWorkflowState.PENDING,
    UATWorkflowState.INSPECTING,
    UATWorkflowState.BUILDING_SPEC,
    UATWorkflowState.GENERATING,
    UATWorkflowState.SUBMITTING,
    UATWorkflowState.MONITORING,
}

ALLOWED_UAT_TRANSITIONS: dict[UATWorkflowState, set[UATWorkflowState]] = {
    UATWorkflowState.PENDING: {
        UATWorkflowState.INSPECTING,
        UATWorkflowState.FAILED,
    },
    UATWorkflowState.INSPECTING: {
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.FAILED,
    },
    UATWorkflowState.BUILDING_SPEC: {
        UATWorkflowState.GENERATING,
        UATWorkflowState.FAILED,
    },
    UATWorkflowState.GENERATING: {
        UATWorkflowState.SUBMITTING,
        UATWorkflowState.FAILED,
    },
    UATWorkflowState.SUBMITTING: {
        UATWorkflowState.MONITORING,
        UATWorkflowState.FAILED,
    },
    UATWorkflowState.MONITORING: {
        UATWorkflowState.COMPLETED,
        UATWorkflowState.FAILED,
    },
    UATWorkflowState.COMPLETED: set(),
    UATWorkflowState.FAILED: set(),
}


class InvalidUATStateTransitionError(ValueError):
    """Raised when an invalid UAT workflow state transition is attempted."""


def normalize_uat_state(state: UATWorkflowState | str) -> UATWorkflowState:
    """Normalize and validate a state into a UATWorkflowState enum member.

    Args:
        state: State to normalize.

    Returns:
        The normalized UATWorkflowState enum member.

    Raises:
        InvalidUATStateTransitionError: If the state is not recognized.
    """
    if isinstance(state, UATWorkflowState):
        return state
    if isinstance(state, str):
        cleaned = state.strip().upper()
        try:
            return UATWorkflowState(cleaned)
        except ValueError as err:
            allowed = sorted(s.value for s in UATWorkflowState)
            raise InvalidUATStateTransitionError(
                f"Invalid UAT state: '{state}'. Must be one of: {allowed}"
            ) from err
    raise InvalidUATStateTransitionError(f"Invalid UAT state type: {type(state).__name__}")


def is_valid_uat_transition(
    from_state: UATWorkflowState | str,
    to_state: UATWorkflowState | str,
) -> bool:
    """Check if transitioning from from_state to to_state is permitted.

    Args:
        from_state: The source state.
        to_state: The target state.

    Returns:
        True if permitted, False otherwise.
    """
    try:
        from_enum = normalize_uat_state(from_state)
        to_enum = normalize_uat_state(to_state)
    except (InvalidUATStateTransitionError, ValueError):
        return False
    return to_enum in ALLOWED_UAT_TRANSITIONS.get(from_enum, set())


def validate_uat_transition(
    from_state: UATWorkflowState | str,
    to_state: UATWorkflowState | str,
) -> bool:
    """Validate that transitioning from from_state to to_state is permitted.

    Args:
        from_state: The current or originating state.
        to_state: The target state.

    Returns:
        True if the transition is permitted.

    Raises:
        InvalidUATStateTransitionError: If the transition is not allowed.
    """
    from_enum = normalize_uat_state(from_state)
    to_enum = normalize_uat_state(to_state)
    allowed = ALLOWED_UAT_TRANSITIONS.get(from_enum, set())
    if to_enum not in allowed:
        allowed_names = sorted(s.value for s in allowed)
        raise InvalidUATStateTransitionError(
            f"Cannot transition UAT workflow from {from_enum.value} to {to_enum.value}. "
            f"Allowed transitions from {from_enum.value}: {allowed_names}"
        )
    return True


__all__ = [
    "ACTIVE_UAT_STATES",
    "ALLOWED_UAT_TRANSITIONS",
    "TERMINAL_UAT_STATES",
    "InvalidUATStateTransitionError",
    "RecordTestType",
    "UATFormSpec",
    "UATSpecification",
    "UATSubjectSpec",
    "UATVariableSpec",
    "UATWorkflowState",
    "VariableTestStrategy",
    "is_valid_uat_transition",
    "normalize_uat_state",
    "validate_uat_transition",
]
