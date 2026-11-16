"""Reporting and analysis models for iMedNet, including CDISC-aligned datasets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from imednet.models.base import ImednetBaseModel


class AdverseEvent(ImednetBaseModel):
    """Canonical CDISC-aligned Adverse Event (AE) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    ae_term: str = Field(
        ..., alias="aeTerm", description="Reported term for the adverse event (AETERM)"
    )
    ae_decod: str | None = Field(
        None, alias="aeDecod", description="Dictionary-derived preferred term (AEDECOD)"
    )
    ae_severity: str = Field(
        ..., alias="aeSeverity", description="Severity/intensity: MILD, MODERATE, SEVERE (AESEV)"
    )
    ae_serious: bool = Field(
        False, alias="aeSerious", description="Serious Adverse Event flag (AESER)"
    )
    ae_start_date: datetime | None = Field(
        None, alias="aeStartDate", description="Start date/time of event (AESTDTC)"
    )
    ae_end_date: datetime | None = Field(
        None, alias="aeEndDate", description="End date/time of event (AEENDTC)"
    )
    ae_outcome: str | None = Field(
        None, alias="aeOutcome", description="Outcome of adverse event (AEOUT)"
    )
    ae_relationship: str | None = Field(
        None,
        alias="aeRelationship",
        description="Causality / relationship to study drug/device (AEREL)",
    )
    ae_action_taken: str | None = Field(
        None,
        alias="aeActionTaken",
        description="Action taken with study drug/device (AEACN)",
    )


class ProtocolDeviation(ImednetBaseModel):
    """Canonical CDISC-aligned Protocol Deviation (PD) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    dv_term: str = Field(..., alias="dvTerm", description="Description of the deviation (DVTERM)")
    dv_category: str = Field(
        ...,
        alias="dvCategory",
        description="Deviation category, e.g., ELIGIBILITY, CONSENT, PROCEDURE (DVCAT)",
    )
    dv_severity: str = Field(
        ...,
        alias="dvSeverity",
        description="Deviation classification: MAJOR, MINOR, CRITICAL",
    )
    dv_date: datetime = Field(..., alias="dvDate", description="Date/time of deviation (DVSTDTC)")
    dv_status: str = Field(
        "Unreviewed",
        alias="dvStatus",
        description="Triage status: Unreviewed, Approved, Escalated",
    )


class DeviceDeficiency(ImednetBaseModel):
    """Canonical CDISC-aligned Device Deficiency (DD) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    dd_term: str = Field(..., alias="ddTerm", description="Deficiency term (DDTERM)")
    dd_category: str = Field(..., alias="ddCategory", description="Category of deficiency (DDCAT)")
    dd_serious: bool = Field(
        False,
        alias="ddSerious",
        description="Serious Adverse Event relation flag (DDSPID)",
    )
    dd_date: datetime = Field(
        ..., alias="ddDate", description="Date/time of deficiency occurrence (DDSTDTC)"
    )


class SubjectLevelAnalysis(ImednetBaseModel):
    """Subject-Level Analysis Dataset (ADSL)."""

    model_config = {"extra": "allow"}  # noqa: RUF012
    subject_key: str = Field(..., alias="subjectKey")


class AnalysisAdverseEvent(ImednetBaseModel):
    """Analysis Adverse Event Dataset (ADAE)."""

    model_config = {"extra": "allow"}  # noqa: RUF012
    subject_key: str = Field(..., alias="subjectKey")


class AnalysisLabResult(ImednetBaseModel):
    """Analysis Lab Result Dataset (ADLB)."""

    model_config = {"extra": "allow"}  # noqa: RUF012
    subject_key: str = Field(..., alias="subjectKey")


class Demographics(ImednetBaseModel):
    """Canonical CDISC-aligned Demographics (DM) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    sex: str = Field(..., alias="sex", description="Biological sex of the subject (SEX)")
    birth_date: datetime | None = Field(
        None, alias="birthDate", description="Date of birth of the subject (BRTHDTC)"
    )
    age: int | None = Field(None, alias="age", description="Age in years of the subject (AGE)")
    race: str | None = Field(None, alias="race", description="Reported race of the subject (RACE)")
    ethnicity: str | None = Field(
        None, alias="ethnicity", description="Ethnicity of the subject (ETHNIC)"
    )


class VitalSigns(ImednetBaseModel):
    """Canonical CDISC-aligned Vital Signs (VS) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    vs_test: str = Field(..., alias="vsTest", description="Vital signs test name (VSTEST)")
    vs_test_cd: str | None = Field(
        None, alias="vsTestCd", description="Vital signs test short code (VSTESTCD)"
    )
    vs_result: float | str = Field(
        ..., alias="vsResult", description="Vital signs measurement result (VSORRES/VSSTRESN)"
    )
    vs_unit: str | None = Field(
        None, alias="vsUnit", description="Vital signs measurement unit (VSORRESU/VSSTRESU)"
    )
    vs_date: datetime = Field(
        ..., alias="vsDate", description="Date/time of vital signs measurement (VSDTC)"
    )

    @field_validator("vs_result", mode="before")
    @classmethod
    def _coerce_result(cls, v: Any) -> Any:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.strip())
            except ValueError:
                return v.strip()
        return v


VitalSign = VitalSigns

__all__ = [
    "AdverseEvent",
    "AnalysisAdverseEvent",
    "AnalysisLabResult",
    "Demographics",
    "DeviceDeficiency",
    "ProtocolDeviation",
    "SubjectLevelAnalysis",
    "VitalSign",
    "VitalSigns",
]
