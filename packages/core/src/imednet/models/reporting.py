"""Reporting and analysis models for iMedNet, including CDISC-aligned datasets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from imednet.models.base import ImednetBaseModel
from imednet.utils.validators import is_missing_value


def _coerce_numeric_or_str(v: Any, *, allow_none: bool = False) -> Any:
    """Coerce numeric values or numeric strings to float while preserving text."""
    if allow_none and is_missing_value(v):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        stripped = v.strip()
        try:
            return float(stripped)
        except ValueError:
            return stripped
    return v


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
        return _coerce_numeric_or_str(v)


VitalSign = VitalSigns


class ConcomitantMedication(ImednetBaseModel):
    """Canonical CDISC-aligned Concomitant Medication (CM) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    cm_trt: str = Field(
        ...,
        alias="cmTrt",
        description="Reported name of drug, medication, or therapy (CMTRT)",
    )
    cm_decod: str | None = Field(
        None, alias="cmDecod", description="Standardized medication name (CMDECOD)"
    )
    cm_dose: float | str | None = Field(
        None, alias="cmDose", description="Dose per administration (CMDOSE)"
    )
    cm_dosu: str | None = Field(None, alias="cmDosU", description="Dose units (CMDOSU)")
    cm_route: str | None = Field(
        None, alias="cmRoute", description="Route of administration (CMROUTE)"
    )
    cm_start_date: datetime | None = Field(
        None, alias="cmStartDate", description="Start date/time of medication (CMSTDTC)"
    )
    cm_end_date: datetime | None = Field(
        None, alias="cmEndDate", description="End date/time of medication (CMENDTC)"
    )
    cm_indication: str | None = Field(
        None, alias="cmIndication", description="Indication for medication use (CMINDC)"
    )

    @field_validator("cm_dose", mode="before")
    @classmethod
    def _coerce_optional_numeric(cls, v: Any) -> Any:
        return _coerce_numeric_or_str(v, allow_none=True)


class LaboratoryResult(ImednetBaseModel):
    """Canonical CDISC-aligned Laboratory Test Results (LB) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    lb_test: str = Field(
        ..., alias="lbTest", description="Laboratory test or examination name (LBTEST)"
    )
    lb_test_cd: str | None = Field(
        None, alias="lbTestCd", description="Laboratory test short name (LBTESTCD)"
    )
    lb_cat: str | None = Field(
        None, alias="lbCat", description="Category for laboratory test (LBCAT)"
    )
    lb_result: float | str = Field(
        ..., alias="lbResult", description="Result or finding in original units (LBORRES)"
    )
    lb_unit: str | None = Field(None, alias="lbUnit", description="Original units (LBORRESU)")
    lb_ornrlo: float | str | None = Field(
        None,
        alias="lbOrnrlo",
        description="Reference range lower limit in original unit (LBORNRLO)",
    )
    lb_ornrhi: float | str | None = Field(
        None,
        alias="lbOrnrhi",
        description="Reference range upper limit in original unit (LBORNRHI)",
    )
    lb_nrind: str | None = Field(
        None, alias="lbNrind", description="Reference range indicator (LBNRIND)"
    )
    lb_spec: str | None = Field(None, alias="lbSpec", description="Specimen type (LBSPEC)")
    lb_date: datetime | None = Field(
        None, alias="lbDate", description="Date/time of specimen collection (LBDTC)"
    )

    @field_validator("lb_result", mode="before")
    @classmethod
    def _coerce_lb_result(cls, v: Any) -> Any:
        return _coerce_numeric_or_str(v)

    @field_validator("lb_ornrlo", "lb_ornrhi", mode="before")
    @classmethod
    def _coerce_lb_bounds(cls, v: Any) -> Any:
        return _coerce_numeric_or_str(v, allow_none=True)


LabResult = LaboratoryResult


class Exposure(ImednetBaseModel):
    """Canonical CDISC-aligned Exposure (EX) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    ex_trt: str = Field(..., alias="exTrt", description="Name of treatment (EXTRT)")
    ex_dose: float | str | None = Field(
        None, alias="exDose", description="Dose per administration (EXDOSE)"
    )
    ex_dosu: str | None = Field(None, alias="exDosU", description="Dose units (EXDOSU)")
    ex_dosfrm: str | None = Field(None, alias="exDosFrm", description="Dose form (EXDOSFRM)")
    ex_dosfrq: str | None = Field(
        None, alias="exDosFrq", description="Dosing frequency per interval (EXDOSFRQ)"
    )
    ex_route: str | None = Field(
        None, alias="exRoute", description="Route of administration (EXROUTE)"
    )
    ex_start_date: datetime | None = Field(
        None, alias="exStartDate", description="Start date/time of treatment (EXSTDTC)"
    )
    ex_end_date: datetime | None = Field(
        None, alias="exEndDate", description="End date/time of treatment (EXENDTC)"
    )

    @field_validator("ex_dose", mode="before")
    @classmethod
    def _coerce_ex_dose(cls, v: Any) -> Any:
        return _coerce_numeric_or_str(v, allow_none=True)


class MedicalHistory(ImednetBaseModel):
    """Canonical CDISC-aligned Medical History (MH) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    mh_term: str = Field(
        ..., alias="mhTerm", description="Reported term for the medical history (MHTERM)"
    )
    mh_decod: str | None = Field(
        None, alias="mhDecod", description="Dictionary-derived text description (MHDECOD)"
    )
    mh_cat: str | None = Field(
        None, alias="mhCat", description="Category for medical history (MHCAT)"
    )
    mh_bodsys: str | None = Field(
        None, alias="mhBodSys", description="Body system or organ class (MHBODSYS)"
    )
    mh_presp: bool | None = Field(None, alias="mhPresp", description="Pre-specified flag (MHPRESP)")
    mh_occur: bool | None = Field(
        None, alias="mhOccur", description="Medical history occurrence (MHOCCUR)"
    )
    mh_start_date: datetime | None = Field(
        None, alias="mhStartDate", description="Start date/time of medical history event (MHSTDTC)"
    )
    mh_end_date: datetime | None = Field(
        None, alias="mhEndDate", description="End date/time of medical history event (MHENDTC)"
    )
    mh_enrf: str | None = Field(
        None, alias="mhEnrf", description="End relative to reference period (MHENRF)"
    )


class Disposition(ImednetBaseModel):
    """Canonical CDISC-aligned Disposition (DS) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    ds_term: str = Field(
        ..., alias="dsTerm", description="Reported term for the disposition event (DSTERM)"
    )
    ds_decod: str | None = Field(
        None, alias="dsDecod", description="Standardized disposition term (DSDECOD)"
    )
    ds_cat: str | None = Field(
        None, alias="dsCat", description="Category for disposition event (DSCAT)"
    )
    ds_scat: str | None = Field(
        None, alias="dsScat", description="Subcategory for disposition event (DSSCAT)"
    )
    ds_start_date: datetime | None = Field(
        None, alias="dsStartDate", description="Start date/time of disposition event (DSSTDTC)"
    )
    ds_date: datetime | None = Field(
        None, alias="dsDate", description="Date/time of disposition collection (DSDTC)"
    )


class EcgResult(ImednetBaseModel):
    """Canonical CDISC-aligned ECG Test Results (EG) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    eg_test: str = Field(..., alias="egTest", description="ECG test or examination name (EGTEST)")
    eg_test_cd: str | None = Field(
        None, alias="egTestCd", description="ECG test short name (EGTESTCD)"
    )
    eg_cat: str | None = Field(None, alias="egCat", description="Category for ECG (EGCAT)")
    eg_result: float | str = Field(
        ..., alias="egResult", description="ECG result or finding in original units (EGORRES)"
    )
    eg_unit: str | None = Field(None, alias="egUnit", description="ECG original units (EGORRESU)")
    eg_pos: str | None = Field(None, alias="egPos", description="ECG position of subject (EGPOS)")
    eg_date: datetime | None = Field(
        None, alias="egDate", description="Date/time of ECG measurement (EGDTC)"
    )

    @field_validator("eg_result", mode="before")
    @classmethod
    def _coerce_eg_result(cls, v: Any) -> Any:
        return _coerce_numeric_or_str(v)


Electrocardiogram = EcgResult


class PhysicalExam(ImednetBaseModel):
    """Canonical CDISC-aligned Physical Examination (PE) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    pe_test: str = Field(..., alias="peTest", description="Body system examined (PETEST)")
    pe_test_cd: str | None = Field(
        None, alias="peTestCd", description="Body system examined short name (PETESTCD)"
    )
    pe_cat: str | None = Field(None, alias="peCat", description="Category for examination (PECAT)")
    pe_bodsys: str | None = Field(
        None, alias="peBodSys", description="Body system or organ class (PEBODSYS)"
    )
    pe_result: str = Field(
        ..., alias="peResult", description="Verbatim examination finding (PEORRES)"
    )
    pe_loc: str | None = Field(
        None, alias="peLoc", description="Location of physical exam finding (PELOC)"
    )
    pe_date: datetime | None = Field(
        None, alias="peDate", description="Date/time of examination (PEDTC)"
    )


PhysicalExamination = PhysicalExam


class InclusionExclusion(ImednetBaseModel):
    """Canonical CDISC-aligned Inclusion/Exclusion Criteria Not Met (IE) reporting model."""

    subject_key: str = Field(..., alias="subjectKey", description="Unique subject identifier")
    ie_test: str = Field(
        ..., alias="ieTest", description="Inclusion/exclusion criterion description (IETEST)"
    )
    ie_test_cd: str = Field(
        ..., alias="ieTestCd", description="Inclusion/exclusion criterion short name (IETESTCD)"
    )
    ie_cat: str = Field(
        ...,
        alias="ieCat",
        description="Inclusion/exclusion category: INCLUSION or EXCLUSION (IECAT)",
    )
    ie_result: str = Field(
        ..., alias="ieResult", description="Inclusion/exclusion criterion met value (IEORRES)"
    )
    ie_date: datetime | None = Field(
        None, alias="ieDate", description="Date/time of criterion assessment (IEDTC)"
    )


__all__ = [
    "AdverseEvent",
    "AnalysisAdverseEvent",
    "AnalysisLabResult",
    "ConcomitantMedication",
    "Demographics",
    "DeviceDeficiency",
    "Disposition",
    "EcgResult",
    "Electrocardiogram",
    "Exposure",
    "InclusionExclusion",
    "LabResult",
    "LaboratoryResult",
    "MedicalHistory",
    "PhysicalExam",
    "PhysicalExamination",
    "ProtocolDeviation",
    "SubjectLevelAnalysis",
    "VitalSign",
    "VitalSigns",
]
