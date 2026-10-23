"""Structured results consumed by the CI verification report aggregator."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VerificationViolation(BaseModel):
    """A verification failure with its category and explanation."""

    model_config = ConfigDict(extra="forbid")

    violation_type: str
    message: str


class VerificationPerformanceMetric(BaseModel):
    """A named numeric measurement with an optional unit."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str
    value: float
    unit: str | None = None


class VerificationReport(BaseModel):
    """Versioned report shared by verification tracks."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["1.0"]
    track: Literal["fuzzing", "performance", "validation", "live_tests", "drift"]
    status: Literal["passed", "failed"]
    violations: list[VerificationViolation]
    metrics: list[VerificationPerformanceMetric] = Field(default_factory=list)
    summary: str
