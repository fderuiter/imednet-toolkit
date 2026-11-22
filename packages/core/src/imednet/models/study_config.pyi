"""Study configuration models and validation.

This module provides Pydantic models for parsing and validating study configurations,
including reporting profiles, widgets, and mapping rules.
"""

from __future__ import annotations

from typing import Self, overload

from pydantic import Field, field_validator, model_validator

from imednet.models.base import ImednetBaseModel

class MappingRule(ImednetBaseModel):
    """Mapping from raw source variable to canonical reporting field."""

    domain: str = Field(..., alias="domain")
    target_field: str = Field(..., alias="targetField")
    source_form_key: str = Field(..., alias="sourceFormKey")
    source_variable_name: str = Field(..., alias="sourceVariableName")
    fallback_value: str | None = Field(default=None, alias="fallbackValue")
    business_logic: str | None = Field(default=None, alias="businessLogic")
    is_baseline: bool = Field(default=False, alias="isBaseline")
    strict: bool = Field(default=False, alias="strict")
    strict_mode: bool = Field(default=False, alias="strictMode")
    strict_cdash: bool = Field(default=False, alias="strictCdash")

    @overload
    def __init__(
        self,
        *,
        domain: str,
        target_field: str,
        source_form_key: str,
        source_variable_name: str,
        fallback_value: str | None = None,
        business_logic: str | None = None,
        is_baseline: bool = False,
        strict: bool = False,
        strict_mode: bool = False,
        strict_cdash: bool = False,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        domain: str,
        targetField: str,
        sourceFormKey: str,
        sourceVariableName: str,
        fallbackValue: str | None = None,
        businessLogic: str | None = None,
        isBaseline: bool = False,
        strict: bool = False,
        strictMode: bool = False,
        strictCdash: bool = False,
    ) -> None: ...
    @model_validator(mode="after")
    def _validate_cdash_target(self) -> Self: ...

class WidgetConfig(ImednetBaseModel):
    """Declarative dashboard widget configuration."""

    widget_id: str = Field(..., alias="widgetId")
    type: str = Field(..., alias="type")
    title: str = Field(..., alias="title")
    domain: str = Field(..., alias="domain")
    x_axis: str | None = Field(default=None, alias="xAxis")
    y_axis: str | None = Field(default=None, alias="yAxis")
    layout_cols: int = Field(default=12, alias="layoutCols")

    @overload
    def __init__(
        self,
        *,
        widget_id: str,
        type: str,
        title: str,
        domain: str,
        x_axis: str | None = None,
        y_axis: str | None = None,
        layout_cols: int = 12,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        widgetId: str,
        type: str,
        title: str,
        domain: str,
        xAxis: str | None = None,
        yAxis: str | None = None,
        layoutCols: int = 12,
    ) -> None: ...

class StudyConfiguration(ImednetBaseModel):
    """Serialized study reporting dashboard configuration."""

    version: str = Field(default="1.0.0", alias="version")
    study_key: str = Field(..., alias="studyKey")
    reporting_profile: str = Field(default="general", alias="reportingProfile")
    mappings: list[MappingRule] = Field(default_factory=list, alias="mappings")
    terminology_lookups: dict[str, dict[str, str]] = Field(
        default_factory=dict, alias="terminologyLookups"
    )
    widgets: list[WidgetConfig] = Field(default_factory=list, alias="widgets")
    phi_fields: list[str] = Field(default_factory=list, alias="phiFields")
    strict: bool = Field(default=False, alias="strict")
    strict_mode: bool = Field(default=False, alias="strictMode")
    strict_cdash: bool = Field(default=False, alias="strictCdash")

    @overload
    def __init__(
        self,
        *,
        version: str = "1.0.0",
        study_key: str,
        reporting_profile: str = "general",
        mappings: list[MappingRule] = ...,
        terminology_lookups: dict[str, dict[str, str]] = ...,
        widgets: list[WidgetConfig] = ...,
        phi_fields: list[str] = ...,
        strict: bool = False,
        strict_mode: bool = False,
        strict_cdash: bool = False,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        version: str = "1.0.0",
        studyKey: str,
        reportingProfile: str = "general",
        mappings: list[MappingRule] = ...,
        terminologyLookups: dict[str, dict[str, str]] = ...,
        widgets: list[WidgetConfig] = ...,
        phiFields: list[str] = ...,
        strict: bool = False,
        strictMode: bool = False,
        strictCdash: bool = False,
    ) -> None: ...
    @field_validator("reporting_profile", check_fields=False, mode="before")
    @classmethod
    def _validate_reporting_profile(cls, value: object) -> object: ...
    @model_validator(mode="after")
    def _validate_mappings_strict(self) -> Self: ...
