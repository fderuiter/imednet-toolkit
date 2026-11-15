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


__all__ = [
    "RecordTestType",
    "UATFormSpec",
    "UATSpecification",
    "UATSubjectSpec",
    "UATVariableSpec",
    "VariableTestStrategy",
]
