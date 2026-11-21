"""Reporting and analysis models for iMedNet, including CDISC-aligned datasets."""

from __future__ import annotations

from datetime import datetime
from typing import Any, overload

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
        None, alias="aeActionTaken", description="Action taken with study drug/device (AEACN)"
    )

    @overload
    def __init__(
        self,
        *,
        subject_key: str,
        ae_term: str,
        ae_decod: str | None = None,
        ae_severity: str,
        ae_serious: bool = False,
        ae_start_date: datetime | None = None,
        ae_end_date: datetime | None = None,
        ae_outcome: str | None = None,
        ae_relationship: str | None = None,
        ae_action_taken: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        subjectKey: str,
        aeTerm: str,
        aeDecod: str | None = None,
        aeSeverity: str,
        aeSerious: bool = False,
        aeStartDate: datetime | None = None,
        aeEndDate: datetime | None = None,
        aeOutcome: str | None = None,
        aeRelationship: str | None = None,
        aeActionTaken: str | None = None,
    ) -> None: ...

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
        ..., alias="dvSeverity", description="Deviation classification: MAJOR, MINOR, CRITICAL"
    )
    dv_date: datetime = Field(..., alias="dvDate", description="Date/time of deviation (DVSTDTC)")
    dv_status: str = Field(
        "Unreviewed", alias="dvStatus", description="Triage status: Unreviewed, Approved, Escalated"
    )

    @overload
    def __init__(
        self,
        *,
        subject_key: str,
        dv_term: str,
        dv_category: str,
        dv_severity: str,
        dv_date: datetime,
        dv_status: str = "Unreviewed",
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        subjectKey: str,
        dvTerm: str,
        dvCategory: str,
        dvSeverity: str,
        dvDate: datetime,
        dvStatus: str = "Unreviewed",
    ) -> None: ...

class DeviceDeficiency(ImednetBaseModel):
    """Canonical CDISC-aligned Device Deficiency (DD) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    dd_term: str = Field(..., alias="ddTerm", description="Deficiency term (DDTERM)")
    dd_category: str = Field(..., alias="ddCategory", description="Category of deficiency (DDCAT)")
    dd_serious: bool = Field(
        False, alias="ddSerious", description="Serious Adverse Event relation flag (DDSPID)"
    )
    dd_date: datetime = Field(
        ..., alias="ddDate", description="Date/time of deficiency occurrence (DDSTDTC)"
    )

    @overload
    def __init__(
        self,
        *,
        subject_key: str,
        dd_term: str,
        dd_category: str,
        dd_serious: bool = False,
        dd_date: datetime,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        subjectKey: str,
        ddTerm: str,
        ddCategory: str,
        ddSerious: bool = False,
        ddDate: datetime,
    ) -> None: ...

class SubjectLevelAnalysis(ImednetBaseModel):
    """Subject-Level Analysis Dataset (ADSL)."""

    subject_key: str = Field(..., alias="subjectKey")

    @overload
    def __init__(self, *, subject_key: str, **extra_data: Any) -> None: ...
    @overload
    def __init__(self, *, subjectKey: str, **extra_data: Any) -> None: ...

class AnalysisAdverseEvent(ImednetBaseModel):
    """Analysis Adverse Event Dataset (ADAE)."""

    subject_key: str = Field(..., alias="subjectKey")

    @overload
    def __init__(self, *, subject_key: str, **extra_data: Any) -> None: ...
    @overload
    def __init__(self, *, subjectKey: str, **extra_data: Any) -> None: ...

class AnalysisLabResult(ImednetBaseModel):
    """Analysis Lab Result Dataset (ADLB)."""

    subject_key: str = Field(..., alias="subjectKey")

    @overload
    def __init__(self, *, subject_key: str, **extra_data: Any) -> None: ...
    @overload
    def __init__(self, *, subjectKey: str, **extra_data: Any) -> None: ...

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

    @overload
    def __init__(
        self,
        *,
        subject_key: str,
        sex: str,
        birth_date: datetime | None = None,
        age: int | None = None,
        race: str | None = None,
        ethnicity: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        subjectKey: str,
        sex: str,
        birthDate: datetime | None = None,
        age: int | None = None,
        race: str | None = None,
        ethnicity: str | None = None,
    ) -> None: ...

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

    @overload
    def __init__(
        self,
        *,
        subject_key: str,
        vs_test: str,
        vs_test_cd: str | None = None,
        vs_result: float | str,
        vs_unit: str | None = None,
        vs_date: datetime,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        subjectKey: str,
        vsTest: str,
        vsTestCd: str | None = None,
        vsResult: float | str,
        vsUnit: str | None = None,
        vsDate: datetime,
    ) -> None: ...
    @field_validator("vs_result", mode="before")
    @classmethod
    def _coerce_result(cls, v: Any) -> Any: ...

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
