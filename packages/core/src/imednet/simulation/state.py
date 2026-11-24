"""In-memory fixture state for offline simulation mode."""

from __future__ import annotations

import copy
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from imednet.models.jobs import (
    ACTIVE_JOB_STATES,
    InvalidJobStateError,
    Job,
    JobState,
    normalize_job_state,
)
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record, normalize_record_status

_RECORD_ENVELOPE_KEYS = frozenset(
    {
        "data",
        "dateCreated",
        "dateModified",
        "date_created",
        "date_modified",
        "deleted",
        "formId",
        "formKey",
        "form_id",
        "form_key",
        "intervalId",
        "intervalName",
        "interval_id",
        "interval_name",
        "keywords",
        "lastUpdated",
        "last_updated",
        "reasonForChange",
        "reason_for_change",
        "recordData",
        "recordId",
        "recordOid",
        "recordStatus",
        "recordType",
        "record_data",
        "record_id",
        "record_oid",
        "record_status",
        "record_type",
        "role",
        "siteId",
        "siteName",
        "site_id",
        "site_name",
        "studyKey",
        "study_key",
        "subjectId",
        "subjectKey",
        "subjectOid",
        "subjectStatus",
        "subject_id",
        "subject_key",
        "subject_oid",
        "subject_status",
        "user",
        "visitId",
        "visit_id",
    }
)


def _strip_private_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow dictionary copy excluding private ``_``-prefixed keys."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _advance_iso_timestamp(raw_iso: str, *, seconds: int = 5) -> str:
    """Advance an ISO-8601 timestamp by *seconds*, returning a UTC ``Z`` string."""
    try:
        parsed = datetime.fromisoformat(raw_iso)
    except ValueError:
        return "2026-01-02T00:00:05Z"
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return (parsed + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class SimulationState:
    """Thread-safe in-memory store representing simulated EDC server state."""

    def __init__(self) -> None:
        """Initialize simulation state with deterministic fixtures."""
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Reset all in-memory fixtures to clean initial state."""
        with getattr(self, "_lock", threading.Lock()):
            self._next_subject_id: int = 202
            self._next_record_id: int = 1000
            self._next_revision_id: int = 1203
            self._next_batch_num: int = 1
            self._job_poll_counts: dict[str, int] = {}

            self.studies: list[dict[str, Any]] = [
                {
                    "studyKey": "DEMO-STUDY",
                    "studyName": "Demo Oncology Trial",
                    "studyDescription": "A simulated Phase II oncology investigation",
                    "studyType": "Interventional",
                    "sponsorKey": "ACME-SPONSOR",
                    "studyId": 1001,
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
                {
                    "studyKey": "MY_STUDY",
                    "studyName": "Primary Clinical Trial",
                    "studyDescription": "Primary simulated clinical investigation",
                    "studyType": "Interventional",
                    "sponsorKey": "ACME-SPONSOR",
                    "studyId": 1002,
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
                {
                    "studyKey": "STUDY",
                    "studyName": "Benchmark Validation Study",
                    "studyDescription": "Benchmark simulated study",
                    "studyType": "Observational",
                    "sponsorKey": "ACME-SPONSOR",
                    "studyId": 1003,
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
            ]

            self.sites: list[dict[str, Any]] = [
                {
                    "siteId": 101,
                    "siteName": "SITE",
                    "studyKey": "STUDY",
                    "siteEnrollmentStatus": "Active",
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
                {
                    "siteId": 102,
                    "siteName": "North Site",
                    "studyKey": "DEMO-STUDY",
                    "siteEnrollmentStatus": "Active",
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
                {
                    "siteId": 103,
                    "siteName": "Main Site",
                    "studyKey": "MY_STUDY",
                    "siteEnrollmentStatus": "Active",
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
            ]

            self.subjects: list[dict[str, Any]] = [
                {
                    "subjectId": 201,
                    "subjectKey": "SUB1",
                    "studyKey": "STUDY",
                    "siteId": 101,
                    "siteName": "SITE",
                    "subjectStatus": "Enrolled",
                    "deleted": False,
                    "keywords": [],
                    "dateCreated": "2026-01-05T08:00:00Z",
                    "dateModified": "2026-01-05T08:00:00Z",
                    "lastUpdated": "2026-01-05T08:00:00Z",
                },
                {
                    "subjectId": 202,
                    "subjectKey": "SUBJ-001",
                    "studyKey": "DEMO-STUDY",
                    "siteId": 102,
                    "siteName": "North Site",
                    "subjectStatus": "Enrolled",
                    "deleted": False,
                    "keywords": [],
                    "dateCreated": "2026-01-05T08:00:00Z",
                    "dateModified": "2026-01-05T08:00:00Z",
                    "lastUpdated": "2026-01-05T08:00:00Z",
                },
            ]

            self.forms: list[dict[str, Any]] = [
                {
                    "formId": 301,
                    "formKey": "F_DM",
                    "formName": "Demographics",
                    "formType": "Enrollment",
                    "studyKey": "STUDY",
                    "revision": 1,
                    "disabled": False,
                    "eproForm": False,
                    "allowCopy": False,
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
                {
                    "formId": 302,
                    "formKey": "F_VS",
                    "formName": "Vital Signs",
                    "formType": "Standard",
                    "studyKey": "STUDY",
                    "revision": 1,
                    "disabled": False,
                    "eproForm": False,
                    "allowCopy": False,
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
                {
                    "formId": 303,
                    "formKey": "F_AE",
                    "formName": "Adverse Events",
                    "formType": "Standard",
                    "studyKey": "STUDY",
                    "revision": 1,
                    "disabled": False,
                    "eproForm": False,
                    "allowCopy": False,
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateModified": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                },
            ]

            self.visits: list[dict[str, Any]] = [
                {
                    "visitId": 401,
                    "studyKey": "STUDY",
                    "intervalId": 501,
                    "intervalName": "Baseline",
                    "subjectId": 201,
                    "subjectKey": "SUB1",
                    "startDate": "2026-01-10T09:00:00Z",
                    "visitDate": "2026-01-10T09:00:00Z",
                    "deleted": False,
                    "dateCreated": "2026-01-10T09:00:00Z",
                    "dateModified": "2026-01-10T09:00:00Z",
                    "lastUpdated": "2026-01-10T09:00:00Z",
                },
                {
                    "visitId": 402,
                    "studyKey": "STUDY",
                    "intervalId": 502,
                    "intervalName": "Follow-Up Month 1",
                    "subjectId": 201,
                    "subjectKey": "SUB1",
                    "startDate": "2026-02-10T09:00:00Z",
                    "visitDate": "2026-02-10T09:00:00Z",
                    "deleted": False,
                    "dateCreated": "2026-02-10T09:00:00Z",
                    "dateModified": "2026-02-10T09:00:00Z",
                    "lastUpdated": "2026-02-10T09:00:00Z",
                },
            ]

            self.records: list[dict[str, Any]] = [
                {
                    "recordId": 601,
                    "studyKey": "STUDY",
                    "subjectKey": "SUB1",
                    "subjectId": 201,
                    "formId": 301,
                    "formKey": "F_DM",
                    "visitId": 401,
                    "intervalId": 501,
                    "siteId": 101,
                    "recordType": "Standard",
                    "recordStatus": "Complete",
                    "recordData": {
                        "V_AGE": 45,
                        "V_GENDER": "Female",
                        "sex": "F",
                        "birthDate": "1981-05-14",
                        "age": 45,
                        "race": "WHITE",
                        "ethnicity": "NOT HISPANIC OR LATINO",
                    },
                    "deleted": False,
                    "dateCreated": "2026-01-10T09:30:00Z",
                    "dateModified": "2026-01-10T09:30:00Z",
                    "lastUpdated": "2026-01-10T09:30:00Z",
                },
                {
                    "recordId": 602,
                    "studyKey": "STUDY",
                    "subjectKey": "SUB1",
                    "subjectId": 201,
                    "formId": 302,
                    "formKey": "F_VS",
                    "visitId": 401,
                    "intervalId": 501,
                    "siteId": 101,
                    "recordType": "Standard",
                    "recordStatus": "Complete",
                    "recordData": {
                        "V_WEIGHT": 68.5,
                        "vsTest": "Weight",
                        "vsTestCd": "WEIGHT",
                        "vsResult": 68.5,
                        "vsUnit": "kg",
                        "vsDate": "2026-01-10T09:35:00Z",
                    },
                    "deleted": False,
                    "dateCreated": "2026-01-10T09:35:00Z",
                    "dateModified": "2026-01-10T09:35:00Z",
                    "lastUpdated": "2026-01-10T09:35:00Z",
                },
                {
                    "recordId": 600,
                    "studyKey": "STUDY",
                    "subjectKey": "SUB1",
                    "subjectId": 201,
                    "formId": 303,
                    "formKey": "F_AE",
                    "visitId": 401,
                    "intervalId": 501,
                    "siteId": 101,
                    "recordType": "Standard",
                    "recordStatus": "Complete",
                    "recordData": {
                        "aeTerm": "Headache",
                        "aeDecod": "HEADACHE",
                        "aeSeverity": "2",
                        "aeRelationship": "POSSIBLE",
                        "aeActionTaken": "NONE",
                        "aeOutcome": "RECOVERED",
                        "aeSerious": "N",
                        "aeStartDate": "2026-01-10T09:25:00Z",
                    },
                    "deleted": False,
                    "dateCreated": "2026-01-10T09:25:00Z",
                    "dateModified": "2026-01-10T09:25:00Z",
                    "lastUpdated": "2026-01-10T09:25:00Z",
                },
            ]

            self.queries: list[dict[str, Any]] = [
                {
                    "annotationId": 701,
                    "studyKey": "STUDY",
                    "subjectKey": "SUB1",
                    "subjectId": 201,
                    "recordId": 601,
                    "description": "Please verify demographic details.",
                    "variable": "V_AGE",
                    "queryComments": [
                        {
                            "sequence": 1,
                            "closed": False,
                            "comment": "Initial check required",
                            "user": "verifier",
                            "date": "2026-01-12T10:00:00Z",
                        }
                    ],
                },
                {
                    "annotationId": 702,
                    "studyKey": "STUDY",
                    "subjectKey": "SUB1",
                    "subjectId": 201,
                    "recordId": 601,
                    "description": "Check vital sign measurement.",
                    "variable": "V_WEIGHT",
                    "queryComments": [
                        {
                            "sequence": 1,
                            "closed": False,
                            "comment": "Measurement outlier",
                            "user": "verifier",
                            "date": "2026-01-12T10:00:00Z",
                        }
                    ],
                },
            ]

            self.intervals: list[dict[str, Any]] = [
                {
                    "intervalId": 501,
                    "intervalName": "Baseline",
                    "studyKey": "STUDY",
                    "forms": [
                        {
                            "formId": 302,
                            "formKey": "F_VS",
                            "formName": "Vital Signs",
                        }
                    ],
                }
            ]

            self.variables: list[dict[str, Any]] = [
                {
                    "variableId": 801,
                    "variableName": "V_AGE",
                    "variableKey": "V_AGE",
                    "variableType": "Number",
                    "formId": 301,
                    "formKey": "F_DM",
                    "studyKey": "STUDY",
                    "label": "Age",
                },
                {
                    "variableId": 802,
                    "variableName": "V_GENDER",
                    "variableKey": "V_GENDER",
                    "variableType": "Coded",
                    "formId": 301,
                    "formKey": "F_DM",
                    "studyKey": "STUDY",
                    "label": "Gender",
                },
                {
                    "variableId": 803,
                    "variableName": "V_WEIGHT",
                    "variableKey": "V_WEIGHT",
                    "variableType": "Float",
                    "formId": 302,
                    "formKey": "F_VS",
                    "studyKey": "STUDY",
                    "label": "Weight",
                },
                {
                    "variableId": 804,
                    "variableName": "sex",
                    "variableKey": "sex",
                    "variableType": "Text",
                    "formId": 301,
                    "formKey": "F_DM",
                    "studyKey": "STUDY",
                    "label": "Sex",
                },
                {
                    "variableId": 805,
                    "variableName": "birthDate",
                    "variableKey": "birthDate",
                    "variableType": "Date",
                    "formId": 301,
                    "formKey": "F_DM",
                    "studyKey": "STUDY",
                    "label": "Birth Date",
                },
                {
                    "variableId": 806,
                    "variableName": "age",
                    "variableKey": "age",
                    "variableType": "Number",
                    "formId": 301,
                    "formKey": "F_DM",
                    "studyKey": "STUDY",
                    "label": "Age (Years)",
                },
                {
                    "variableId": 807,
                    "variableName": "race",
                    "variableKey": "race",
                    "variableType": "Text",
                    "formId": 301,
                    "formKey": "F_DM",
                    "studyKey": "STUDY",
                    "label": "Race",
                },
                {
                    "variableId": 808,
                    "variableName": "ethnicity",
                    "variableKey": "ethnicity",
                    "variableType": "Text",
                    "formId": 301,
                    "formKey": "F_DM",
                    "studyKey": "STUDY",
                    "label": "Ethnicity",
                },
                {
                    "variableId": 809,
                    "variableName": "vsTest",
                    "variableKey": "vsTest",
                    "variableType": "Text",
                    "formId": 302,
                    "formKey": "F_VS",
                    "studyKey": "STUDY",
                    "label": "Vital Signs Test Name",
                },
                {
                    "variableId": 810,
                    "variableName": "vsTestCd",
                    "variableKey": "vsTestCd",
                    "variableType": "Text",
                    "formId": 302,
                    "formKey": "F_VS",
                    "studyKey": "STUDY",
                    "label": "Vital Signs Test Short Name",
                },
                {
                    "variableId": 811,
                    "variableName": "vsResult",
                    "variableKey": "vsResult",
                    "variableType": "Float",
                    "formId": 302,
                    "formKey": "F_VS",
                    "studyKey": "STUDY",
                    "label": "Vital Signs Result",
                },
                {
                    "variableId": 812,
                    "variableName": "vsUnit",
                    "variableKey": "vsUnit",
                    "variableType": "Text",
                    "formId": 302,
                    "formKey": "F_VS",
                    "studyKey": "STUDY",
                    "label": "Vital Signs Units",
                },
                {
                    "variableId": 813,
                    "variableName": "vsDate",
                    "variableKey": "vsDate",
                    "variableType": "Date",
                    "formId": 302,
                    "formKey": "F_VS",
                    "studyKey": "STUDY",
                    "label": "Vital Signs Date",
                },
                {
                    "variableId": 814,
                    "variableName": "aeTerm",
                    "variableKey": "aeTerm",
                    "variableType": "Text",
                    "formId": 303,
                    "formKey": "F_AE",
                    "studyKey": "STUDY",
                    "label": "Reported Term for the Adverse Event",
                },
                {
                    "variableId": 815,
                    "variableName": "aeDecod",
                    "variableKey": "aeDecod",
                    "variableType": "Text",
                    "formId": 303,
                    "formKey": "F_AE",
                    "studyKey": "STUDY",
                    "label": "Dictionary-Derived Term",
                },
                {
                    "variableId": 816,
                    "variableName": "aeSeverity",
                    "variableKey": "aeSeverity",
                    "variableType": "Text",
                    "formId": 303,
                    "formKey": "F_AE",
                    "studyKey": "STUDY",
                    "label": "Severity/Intensity",
                },
                {
                    "variableId": 817,
                    "variableName": "aeRelationship",
                    "variableKey": "aeRelationship",
                    "variableType": "Text",
                    "formId": 303,
                    "formKey": "F_AE",
                    "studyKey": "STUDY",
                    "label": "Causality",
                },
                {
                    "variableId": 818,
                    "variableName": "aeActionTaken",
                    "variableKey": "aeActionTaken",
                    "variableType": "Text",
                    "formId": 303,
                    "formKey": "F_AE",
                    "studyKey": "STUDY",
                    "label": "Action Taken with Study Treatment",
                },
                {
                    "variableId": 819,
                    "variableName": "aeOutcome",
                    "variableKey": "aeOutcome",
                    "variableType": "Text",
                    "formId": 303,
                    "formKey": "F_AE",
                    "studyKey": "STUDY",
                    "label": "Outcome of Adverse Event",
                },
                {
                    "variableId": 820,
                    "variableName": "aeSerious",
                    "variableKey": "aeSerious",
                    "variableType": "Text",
                    "formId": 303,
                    "formKey": "F_AE",
                    "studyKey": "STUDY",
                    "label": "Serious Event",
                },
                {
                    "variableId": 821,
                    "variableName": "aeStartDate",
                    "variableKey": "aeStartDate",
                    "variableType": "Date",
                    "formId": 303,
                    "formKey": "F_AE",
                    "studyKey": "STUDY",
                    "label": "Start Date/Time of Adverse Event",
                },
            ]

            self.users: list[dict[str, Any]] = [
                {
                    "userId": "901",
                    "login": "sim_user",
                    "firstName": "Simulated",
                    "lastName": "User",
                    "email": "sim@example.com",
                    "userActiveInStudy": True,
                    "roles": [
                        {
                            "roleId": "ROLE-INV",
                            "name": "Investigator",
                            "description": "Principal Investigator",
                            "level": 1,
                            "type": "Study",
                            "inactive": False,
                            "communityId": 1,
                            "dateCreated": "2026-01-01T00:00:00Z",
                            "dateModified": "2026-01-01T00:00:00Z",
                        }
                    ],
                },
                {
                    "userId": "902",
                    "login": "sim_admin",
                    "firstName": "Study",
                    "lastName": "Admin",
                    "email": "admin@example.com",
                    "userActiveInStudy": True,
                    "roles": [
                        {
                            "roleId": "ROLE-DM",
                            "name": "Data Manager",
                            "description": "Clinical Data Manager",
                            "level": 2,
                            "type": "Study",
                            "inactive": False,
                            "communityId": 1,
                            "dateCreated": "2026-01-01T00:00:00Z",
                            "dateModified": "2026-01-01T00:00:00Z",
                        }
                    ],
                },
                {
                    "userId": "903",
                    "login": "sim_inactive",
                    "firstName": "Former",
                    "lastName": "Coordinator",
                    "email": "inactive@example.com",
                    "userActiveInStudy": False,
                    "roles": [],
                },
            ]

            self.codings: list[dict[str, Any]] = [
                {
                    "codingId": 1101,
                    "studyKey": "STUDY",
                    "siteId": 101,
                    "siteName": "SITE",
                    "subjectId": 201,
                    "subjectKey": "SUB1",
                    "formId": 301,
                    "formName": "Demographics",
                    "formKey": "F_DM",
                    "recordId": 601,
                    "variable": "V_GENDER",
                    "value": "Female",
                    "code": "C16576",
                    "codedBy": "sim_user",
                    "dictionaryName": "MedDRA",
                    "dictionaryVersion": "26.1",
                    "dateCoded": "2026-01-11T10:00:00Z",
                    "reason": "Initial coding",
                    "revision": 1,
                    "lastUpdated": "2026-01-11T10:00:00Z",
                },
                {
                    "codingId": 1102,
                    "studyKey": "STUDY",
                    "siteId": 101,
                    "siteName": "SITE",
                    "subjectId": 201,
                    "subjectKey": "SUB1",
                    "formId": 302,
                    "formName": "Vital Signs",
                    "formKey": "F_VS",
                    "recordId": 602,
                    "variable": "V_WEIGHT",
                    "value": "68.5",
                    "code": "W001",
                    "codedBy": "sim_admin",
                    "dictionaryName": "WHODrug",
                    "dictionaryVersion": "2026-03",
                    "dateCoded": "2026-01-11T10:05:00Z",
                    "reason": "Automated coding",
                    "revision": 1,
                    "lastUpdated": "2026-01-11T10:05:00Z",
                },
            ]

            self.record_revisions: list[dict[str, Any]] = [
                self._build_record_revision(
                    revision_id=1201,
                    record_entry=self.records[0],
                    revision_number=1,
                    user="sim_user",
                    role="Investigator",
                    reason="Initial entry",
                ),
                self._build_record_revision(
                    revision_id=1202,
                    record_entry=self.records[1],
                    revision_number=1,
                    user="sim_user",
                    role="Investigator",
                    reason="Initial entry",
                ),
                self._build_record_revision(
                    revision_id=1203,
                    record_entry=self.records[2],
                    revision_number=1,
                    user="sim_user",
                    role="Investigator",
                    reason="Initial entry",
                ),
            ]

            self.jobs: dict[str, dict[str, Any]] = {
                "BATCH": {
                    "jobId": "JOB-BATCH",
                    "batchId": "BATCH",
                    "state": "COMPLETED",
                    "progress": 100,
                    "totalRecords": 1,
                    "successfulRecords": 1,
                    "failedRecords": 0,
                    "results": {"status": "Completed", "total": 1, "success": 1, "failed": 0},
                    "dateCreated": "2026-01-01T00:00:00Z",
                    "dateStarted": "2026-01-01T00:00:00Z",
                    "dateFinished": "2026-01-01T00:00:00Z",
                },
                "JOB-1002": {
                    "jobId": "JOB-1002",
                    "batchId": "JOB-1002",
                    "state": "RUNNING",
                    "progress": 50,
                    "totalRecords": 10,
                    "successfulRecords": 5,
                    "failedRecords": 0,
                    "results": {"status": "Running", "total": 10, "success": 5, "failed": 0},
                    "dateCreated": "2026-01-02T00:00:00Z",
                    "dateStarted": "2026-01-02T00:00:00Z",
                    "dateFinished": None,
                },
            }

    def get_studies(self) -> list[dict[str, Any]]:
        """Return list of simulated studies."""
        with self._lock:
            return copy.deepcopy(self.studies)

    def get_sites(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of sites, ensuring sites exist for the requested study."""
        with self._lock:
            sites = copy.deepcopy(self.sites)
            if not study_key:
                return sites
            matched = [s for s in sites if s.get("studyKey") == study_key]
            if matched:
                return matched
            # Provide a deterministic fallback site for the requested study
            fallback = {
                "siteId": 101,
                "siteName": "SITE",
                "studyKey": study_key,
                "siteEnrollmentStatus": "Active",
                "dateCreated": "2026-01-01T00:00:00Z",
                "dateModified": "2026-01-01T00:00:00Z",
                "lastUpdated": "2026-01-01T00:00:00Z",
            }
            return [fallback]

    def get_subjects(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of subjects for a study."""
        with self._lock:
            subjects = copy.deepcopy(self.subjects)
            if not study_key:
                return subjects
            matched = [s for s in subjects if s.get("studyKey") == study_key]
            if matched:
                return matched
            fallback = {
                "subjectId": 201,
                "subjectKey": "SUB1",
                "studyKey": study_key,
                "siteId": 101,
                "siteName": "SITE",
                "subjectStatus": "Enrolled",
                "deleted": False,
                "keywords": [],
                "dateCreated": "2026-01-05T08:00:00Z",
                "dateModified": "2026-01-05T08:00:00Z",
                "lastUpdated": "2026-01-05T08:00:00Z",
            }
            return [fallback]

    def get_forms(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of forms for a study."""
        with self._lock:
            forms = copy.deepcopy(self.forms)
            if not study_key:
                return forms
            matched = [f for f in forms if f.get("studyKey") == study_key]
            if matched:
                return matched
            return [dict(f, studyKey=study_key) for f in forms]

    def get_visits(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of visits for a study."""
        with self._lock:
            visits = copy.deepcopy(self.visits)
            if not study_key:
                return visits
            matched = [v for v in visits if v.get("studyKey") == study_key]
            if matched:
                return matched
            return [dict(v, studyKey=study_key) for v in visits]

    def get_records(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of records for a study."""
        with self._lock:
            records = copy.deepcopy(self.records)
            if not study_key:
                return records
            return [r for r in records if r.get("studyKey") == study_key]

    @staticmethod
    def _build_record_revision(
        *,
        revision_id: int,
        record_entry: dict[str, Any],
        revision_number: int,
        data_revision: int | None = None,
        user: str = "sim_user",
        role: str = "Investigator",
        reason: str = "Record created",
        record_oid: str | None = None,
        subject_oid: str | None = None,
        date_created: str | None = None,
    ) -> dict[str, Any]:
        """Construct a contract-compliant RecordRevision entry for a record."""
        rec_id = record_entry["recordId"]
        subj_id = record_entry["subjectId"]
        return {
            "studyKey": record_entry["studyKey"],
            "recordRevisionId": revision_id,
            "recordId": rec_id,
            "recordOid": record_oid if record_oid is not None else f"REC-{rec_id}",
            "recordRevision": revision_number,
            "dataRevision": data_revision if data_revision is not None else revision_number,
            "recordStatus": record_entry["recordStatus"],
            "subjectId": subj_id,
            "subjectOid": subject_oid if subject_oid is not None else f"SUBJ-{subj_id}",
            "subjectKey": record_entry["subjectKey"],
            "siteId": record_entry["siteId"],
            "formKey": record_entry["formKey"],
            "intervalId": record_entry["intervalId"],
            "role": role,
            "user": user,
            "reasonForChange": reason,
            "deleted": bool(record_entry.get("deleted", False)),
            "dateCreated": (
                date_created if date_created is not None else record_entry["dateCreated"]
            ),
            "lastUpdated": record_entry["lastUpdated"],
        }

    def _resolve_form_identifiers(self, study_key: str, item: dict[str, Any]) -> tuple[int, str]:
        """Resolve ``(formId, formKey)`` from study forms with deterministic fallbacks."""
        raw_form_key = str(item.get("formKey") or item.get("form_key") or "").strip()
        raw_form_id = item.get("formId") if item.get("formId") is not None else item.get("form_id")
        study_forms = [f for f in self.forms if f.get("studyKey") == study_key]
        candidates = study_forms + [f for f in self.forms if f.get("studyKey") != study_key]

        if raw_form_key and raw_form_id is not None:
            return int(raw_form_id), raw_form_key
        if raw_form_key:
            matched = next((f for f in candidates if f.get("formKey") == raw_form_key), None)
            resolved_id = (
                int(matched["formId"])
                if matched is not None and matched.get("formId") is not None
                else 301
            )
            return resolved_id, raw_form_key
        if raw_form_id is not None:
            target_id = int(raw_form_id)
            matched = next((f for f in candidates if f.get("formId") == target_id), None)
            resolved_key = (
                str(matched["formKey"])
                if matched is not None and matched.get("formKey")
                else "F_DM"
            )
            return target_id, resolved_key
        if study_forms:
            first = study_forms[0]
            return int(first.get("formId", 301)), str(first.get("formKey", "F_DM"))
        return 301, "F_DM"

    def _resolve_site_identifiers(
        self,
        study_key: str,
        item: dict[str, Any],
        existing_subject: dict[str, Any] | None,
    ) -> tuple[int, str]:
        """Resolve ``(siteId, siteName)`` from ``self.sites`` or enrolled subject."""
        study_sites = [s for s in self.sites if s.get("studyKey") == study_key]
        other_sites = [s for s in self.sites if s.get("studyKey") != study_key]
        all_sites = study_sites + other_sites
        default_site: dict[str, Any] = (
            study_sites[0] if study_sites else {"siteId": 101, "siteName": "SITE"}
        )

        requested_name = str(item.get("siteName") or item.get("site_name") or "").strip()
        requested_id = item.get("siteId") if item.get("siteId") is not None else item.get("site_id")

        if requested_name:
            matched = next((s for s in all_sites if s.get("siteName") == requested_name), None)
            site_id = (
                int(requested_id)
                if requested_id is not None
                else int(matched["siteId"])
                if matched is not None and matched.get("siteId") is not None
                else int(default_site["siteId"])
            )
            return site_id, requested_name

        if requested_id is not None:
            target_id = int(requested_id)
            matched = next((s for s in all_sites if s.get("siteId") == target_id), None)
            site_name = (
                str(matched["siteName"])
                if matched is not None and matched.get("siteName")
                else str(default_site["siteName"])
            )
            return target_id, site_name

        if existing_subject is not None:
            subj_site_id = int(existing_subject.get("siteId", default_site["siteId"]))
            subj_site_name = str(existing_subject.get("siteName") or "")
            if not subj_site_name:
                matched = next((s for s in all_sites if s.get("siteId") == subj_site_id), None)
                subj_site_name = (
                    str(matched["siteName"])
                    if matched is not None and matched.get("siteName")
                    else str(default_site["siteName"])
                )
            return subj_site_id, subj_site_name

        return int(default_site["siteId"]), str(default_site["siteName"])

    def _resolve_interval_and_visit(
        self,
        study_key: str,
        subj_key: str,
        item: dict[str, Any],
    ) -> tuple[int | None, int | None]:
        """Resolve ``(intervalId, visitId)`` from ``self.intervals`` and ``self.visits``."""
        raw_interval_id = (
            item.get("intervalId")
            if item.get("intervalId") is not None
            else item.get("interval_id")
        )
        raw_interval_name = str(item.get("intervalName") or item.get("interval_name") or "").strip()
        raw_visit_id = (
            item.get("visitId") if item.get("visitId") is not None else item.get("visit_id")
        )

        resolved_interval_id: int | None = None
        if raw_interval_id is not None:
            resolved_interval_id = int(raw_interval_id)
        elif raw_interval_name:
            sources = [
                *[i for i in self.intervals if i.get("studyKey") == study_key],
                *[v for v in self.visits if v.get("studyKey") == study_key],
                *[i for i in self.intervals if i.get("studyKey") != study_key],
                *[v for v in self.visits if v.get("studyKey") != study_key],
            ]
            matched_interval = next(
                (
                    entry
                    for entry in sources
                    if entry.get("intervalName") == raw_interval_name
                    and entry.get("intervalId") is not None
                ),
                None,
            )
            if matched_interval is None:
                lowered = raw_interval_name.lower()
                matched_interval = next(
                    (
                        entry
                        for entry in sources
                        if str(entry.get("intervalName") or "").strip().lower() == lowered
                        and entry.get("intervalId") is not None
                    ),
                    None,
                )
            if matched_interval is not None:
                resolved_interval_id = int(matched_interval["intervalId"])

        if raw_visit_id is not None:
            return resolved_interval_id, int(raw_visit_id)

        if resolved_interval_id is None:
            return None, None

        visit_candidates = [
            *[
                v
                for v in self.visits
                if v.get("studyKey") == study_key
                and v.get("subjectKey") == subj_key
                and v.get("intervalId") == resolved_interval_id
            ],
            *[
                v
                for v in self.visits
                if v.get("studyKey") == study_key and v.get("intervalId") == resolved_interval_id
            ],
            *[v for v in self.visits if v.get("intervalId") == resolved_interval_id],
        ]
        matched_visit = next(
            (v for v in visit_candidates if v.get("visitId") is not None),
            None,
        )
        resolved_visit_id = int(matched_visit["visitId"]) if matched_visit is not None else None
        return resolved_interval_id, resolved_visit_id

    @staticmethod
    def _extract_record_data(item: dict[str, Any]) -> dict[str, Any]:
        """Extract record variable dictionary from a request payload."""
        for key in ("data", "recordData", "record_data"):
            if key in item:
                candidate = item[key]
                if hasattr(candidate, "model_dump"):
                    dumped = candidate.model_dump()
                    if isinstance(dumped, dict):
                        return copy.deepcopy(dumped)
                if isinstance(candidate, dict):
                    return copy.deepcopy(candidate)
        return {k: copy.deepcopy(v) for k, v in item.items() if k not in _RECORD_ENVELOPE_KEYS}

    def _append_validated_revision(
        self,
        *,
        study_key: str,
        record_entry: dict[str, Any],
        data_changed: bool,
        user: str,
        role: str,
        reason: str,
        record_oid: str | None,
        subject_oid: str | None,
        date_created: str,
    ) -> None:
        """Append a monotonic ``RecordRevision`` entry for *record_entry*."""
        rec_id = int(record_entry["recordId"])
        prev_revisions = [
            r
            for r in self.record_revisions
            if r.get("studyKey") == study_key and r.get("recordId") == rec_id
        ]
        prev_rec_rev = max((int(r.get("recordRevision") or 0) for r in prev_revisions), default=0)
        prev_data_rev = max((int(r.get("dataRevision") or 0) for r in prev_revisions), default=0)
        rev_num = prev_rec_rev + 1
        data_rev = (prev_data_rev + 1) if (data_changed or prev_data_rev == 0) else prev_data_rev

        self._next_revision_id += 1
        new_revision = self._build_record_revision(
            revision_id=self._next_revision_id,
            record_entry=record_entry,
            revision_number=rev_num,
            data_revision=data_rev,
            user=user,
            role=role,
            reason=reason,
            record_oid=record_oid,
            subject_oid=subject_oid,
            date_created=date_created,
        )
        validated_curr = RecordRevision.model_validate(new_revision)
        if prev_revisions:
            validated_prev = RecordRevision.model_validate(prev_revisions[-1])
            validated_curr.validate_follows(validated_prev)
        self.record_revisions.append(new_revision)

    def create_records(
        self, study_key: str, records_data: list[dict[str, Any] | Any]
    ) -> dict[str, Any]:
        """Store or update records in memory, enroll subjects, append revisions, and return a Job."""
        with self._lock:
            job_results: list[dict[str, Any]] = []

            for raw_item in records_data:
                item: dict[str, Any] = (
                    raw_item.model_dump(by_alias=True)
                    if hasattr(raw_item, "model_dump")
                    else dict(raw_item)
                )
                rec_data = self._extract_record_data(item)
                form_id, form_key = self._resolve_form_identifiers(study_key, item)
                raw_subj_key = str(item.get("subjectKey") or item.get("subject_key") or "").strip()
                requested_rec_id = (
                    item.get("recordId")
                    if item.get("recordId") is not None
                    else item.get("record_id")
                )
                resolved_interval_id, resolved_visit_id = self._resolve_interval_and_visit(
                    study_key, raw_subj_key, item
                )

                existing_record: dict[str, Any] | None = None
                if requested_rec_id is not None:
                    target_rec_id = int(requested_rec_id)
                    existing_record = next(
                        (
                            r
                            for r in self.records
                            if r.get("studyKey") == study_key and r.get("recordId") == target_rec_id
                        ),
                        None,
                    )
                elif raw_subj_key and resolved_interval_id is not None:
                    existing_record = next(
                        (
                            r
                            for r in self.records
                            if r.get("studyKey") == study_key
                            and r.get("subjectKey") == raw_subj_key
                            and (r.get("formKey") == form_key or r.get("formId") == form_id)
                            and r.get("intervalId") == resolved_interval_id
                            and not r.get("deleted", False)
                        ),
                        None,
                    )

                modified_ts = str(
                    item.get("dateModified") or item.get("date_modified") or "2026-11-15T00:00:00Z"
                )
                updated_ts = str(item.get("lastUpdated") or item.get("last_updated") or modified_ts)
                user_str = str(item.get("user") or "sim_user")
                role_str = str(item.get("role") or "Investigator")
                record_oid = item.get("recordOid") or item.get("record_oid")
                subject_oid = item.get("subjectOid") or item.get("subject_oid")

                if existing_record is not None:
                    rec_id = int(existing_record["recordId"])
                    subj_id = int(existing_record.get("subjectId", 201))
                    subj_key = raw_subj_key or str(existing_record.get("subjectKey", "SUB1"))
                    prev_data = copy.deepcopy(existing_record.get("recordData") or {})
                    merged_data = copy.deepcopy(prev_data)
                    merged_data.update(rec_data)
                    status_changed = False

                    if "recordStatus" in item or "record_status" in item:
                        target_status = (
                            item["recordStatus"]
                            if "recordStatus" in item
                            else item["record_status"]
                        )
                        if target_status is not None:
                            old_status = existing_record.get("recordStatus")
                            old_deleted = bool(existing_record.get("deleted", False))
                            norm_old = normalize_record_status(old_status)
                            norm_new = normalize_record_status(target_status)
                            if old_deleted or norm_old != norm_new:
                                rec_model = Record.model_validate(
                                    {"recordStatus": old_status, "deleted": old_deleted}
                                )
                                transitioned = rec_model.transition_to(target_status)
                                existing_record["recordStatus"] = str(transitioned)
                                if rec_model.is_deleted:
                                    existing_record["deleted"] = True
                                status_changed = True
                            else:
                                existing_record["recordStatus"] = str(norm_new)

                    data_changed = merged_data != prev_data
                    existing_record["recordData"] = merged_data
                    existing_record["formKey"] = form_key
                    if existing_record.get("formId") is None:
                        existing_record["formId"] = form_id
                    existing_record["dateModified"] = modified_ts
                    existing_record["lastUpdated"] = updated_ts

                    matched_subj = next(
                        (
                            s
                            for s in self.subjects
                            if s.get("studyKey") == study_key and s.get("subjectKey") == subj_key
                        ),
                        None,
                    )
                    if matched_subj is not None:
                        matched_subj["dateModified"] = modified_ts
                        matched_subj["lastUpdated"] = updated_ts

                    job_results.append(
                        {
                            "recordId": rec_id,
                            "subjectId": subj_id,
                            "subjectKey": subj_key,
                            "formKey": form_key,
                            "status": "SUCCESS",
                        }
                    )
                    self._append_validated_revision(
                        study_key=study_key,
                        record_entry=existing_record,
                        data_changed=data_changed or (not status_changed and bool(rec_data)),
                        user=user_str,
                        role=role_str,
                        reason=str(
                            item.get("reasonForChange")
                            or item.get("reason_for_change")
                            or "Record updated"
                        ),
                        record_oid=record_oid,
                        subject_oid=subject_oid,
                        date_created=updated_ts,
                    )
                    continue

                self._next_record_id += 1
                rec_id = self._next_record_id
                subj_key = raw_subj_key or f"SUBJ-{rec_id}"

                existing_subject = next(
                    (
                        s
                        for s in self.subjects
                        if s.get("studyKey") == study_key and s.get("subjectKey") == subj_key
                    ),
                    None,
                )
                site_id, site_name = self._resolve_site_identifiers(
                    study_key, item, existing_subject
                )
                created_ts = str(item.get("dateCreated") or item.get("date_created") or modified_ts)

                if existing_subject is not None:
                    subj_id = int(existing_subject.get("subjectId", 201))
                    existing_subject["dateModified"] = modified_ts
                    existing_subject["lastUpdated"] = updated_ts
                else:
                    raw_subj_id = (
                        item.get("subjectId")
                        if item.get("subjectId") is not None
                        else item.get("subject_id")
                    )
                    if raw_subj_id is not None:
                        subj_id = int(raw_subj_id)
                        self._next_subject_id = max(self._next_subject_id, subj_id)
                    else:
                        max_subj_id = max(
                            (
                                int(s["subjectId"])
                                for s in self.subjects
                                if s.get("subjectId") is not None
                            ),
                            default=0,
                        )
                        self._next_subject_id = max(self._next_subject_id, max_subj_id) + 1
                        subj_id = self._next_subject_id
                    self.subjects.append(
                        {
                            "subjectId": subj_id,
                            "subjectKey": subj_key,
                            "studyKey": study_key,
                            "siteId": site_id,
                            "siteName": site_name,
                            "subjectStatus": str(
                                item.get("subjectStatus")
                                or item.get("subject_status")
                                or "Enrolled"
                            ),
                            "deleted": False,
                            "keywords": copy.deepcopy(item.get("keywords", [])),
                            "dateCreated": created_ts,
                            "dateModified": modified_ts,
                            "lastUpdated": updated_ts,
                        }
                    )

                if resolved_interval_id is not None and resolved_visit_id is None:
                    _, resolved_visit_id = self._resolve_interval_and_visit(
                        study_key, subj_key, {"intervalId": resolved_interval_id}
                    )

                raw_status = (
                    item.get("recordStatus")
                    if item.get("recordStatus") is not None
                    else item.get("record_status")
                )
                norm_status = normalize_record_status(
                    raw_status if raw_status is not None else "Complete"
                )
                record_status_str = str(norm_status) if norm_status is not None else "Complete"

                record_entry = {
                    "recordId": rec_id,
                    "studyKey": study_key,
                    "subjectKey": subj_key,
                    "subjectId": subj_id,
                    "formId": form_id,
                    "formKey": form_key,
                    "visitId": resolved_visit_id,
                    "intervalId": resolved_interval_id,
                    "siteId": site_id,
                    "recordType": str(
                        item.get("recordType") or item.get("record_type") or "Standard"
                    ),
                    "recordStatus": record_status_str,
                    "recordData": rec_data,
                    "deleted": record_status_str == "DELETED",
                    "dateCreated": created_ts,
                    "dateModified": modified_ts,
                    "lastUpdated": updated_ts,
                }
                self.records.append(record_entry)
                job_results.append(
                    {
                        "recordId": rec_id,
                        "subjectId": subj_id,
                        "subjectKey": subj_key,
                        "formKey": form_key,
                        "status": "SUCCESS",
                    }
                )
                self._append_validated_revision(
                    study_key=study_key,
                    record_entry=record_entry,
                    data_changed=True,
                    user=user_str,
                    role=role_str,
                    reason=str(
                        item.get("reasonForChange")
                        or item.get("reason_for_change")
                        or "Record created"
                    ),
                    record_oid=record_oid,
                    subject_oid=subject_oid,
                    date_created=created_ts,
                )

            batch_id = f"BATCH-{self._next_batch_num}"
            job_id = f"JOB-{self._next_batch_num}"
            self._next_batch_num += 1

            job = {
                "jobId": job_id,
                "batchId": batch_id,
                "state": "COMPLETED",
                "progress": 100,
                "totalRecords": len(records_data),
                "successfulRecords": len(records_data),
                "failedRecords": 0,
                "resultUrl": f"/api/v1/edc/studies/{study_key}/jobs/{batch_id}/results",
                "results": job_results,
                "dateCreated": "2026-11-15T00:00:00Z",
                "dateStarted": "2026-11-15T00:00:00Z",
                "dateFinished": "2026-11-15T00:00:00Z",
            }
            self.jobs[batch_id] = job
            return {
                "jobId": job_id,
                "batchId": batch_id,
                "state": "COMPLETED",
                "dateCreated": "2026-11-15T00:00:00Z",
                "dateStarted": "2026-11-15T00:00:00Z",
                "dateFinished": "2026-11-15T00:00:00Z",
            }

    def get_queries(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of queries for a study."""
        with self._lock:
            queries = copy.deepcopy(self.queries)
            if not study_key:
                return queries
            matched = [q for q in queries if q.get("studyKey") == study_key]
            if matched:
                return matched
            return [
                {
                    "annotationId": 701,
                    "studyKey": study_key,
                    "subjectKey": "SUB1",
                    "subjectId": 201,
                    "recordId": 601,
                    "description": "Please verify demographic details.",
                    "variable": "V_AGE",
                    "queryComments": [
                        {
                            "sequence": 1,
                            "closed": False,
                            "comment": "Initial check required",
                            "user": "verifier",
                            "date": "2026-01-12T10:00:00Z",
                        }
                    ],
                },
                {
                    "annotationId": 702,
                    "studyKey": study_key,
                    "subjectKey": "SUB1",
                    "subjectId": 201,
                    "recordId": 601,
                    "description": "Check vital sign measurement.",
                    "variable": "V_WEIGHT",
                    "queryComments": [
                        {
                            "sequence": 1,
                            "closed": False,
                            "comment": "Measurement outlier",
                            "user": "verifier",
                            "date": "2026-01-12T10:00:00Z",
                        }
                    ],
                },
            ]

    def get_jobs(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of all jobs with private tracking keys stripped."""
        with self._lock:
            return [_strip_private_keys(copy.deepcopy(j)) for j in self.jobs.values()]

    def get_job_results(self, study_key: str | None, batch_id: str) -> Any | None:
        """Return job results by batch ID without advancing poll count, or None if missing."""
        with self._lock:
            if batch_id not in self.jobs:
                return None
            return copy.deepcopy(self.jobs[batch_id].get("results", []))

    def get_job(self, study_key: str | None, batch_id: str) -> dict[str, Any] | None:
        """Return job status by batch ID, governing active state transitions on polling."""
        with self._lock:
            if batch_id not in self.jobs:
                return None

            job = self.jobs[batch_id]
            legacy_polls = job.pop("_poll_count", None)
            if batch_id not in self._job_poll_counts and isinstance(legacy_polls, int):
                self._job_poll_counts[batch_id] = legacy_polls

            raw_state = job.get("state")
            current_state: JobState | None = None
            if isinstance(raw_state, (str, JobState)) and str(raw_state).strip():
                try:
                    current_state = normalize_job_state(raw_state)
                except InvalidJobStateError:
                    current_state = None

            if current_state in ACTIVE_JOB_STATES:
                poll_count = self._job_poll_counts.get(batch_id, 0) + 1
                self._job_poll_counts[batch_id] = poll_count
                job["state"] = str(current_state)

                if current_state in {JobState.RUNNING, JobState.PROCESSING} and not job.get(
                    "dateStarted"
                ):
                    job["dateStarted"] = str(job.get("dateCreated") or "2026-01-02T00:00:00Z")

                if poll_count >= 2:
                    transitioned = Job.model_validate({"state": current_state}).transition_to(
                        JobState.COMPLETED
                    )
                    assert transitioned.state is not None
                    job["state"] = str(transitioned.state)
                    job["progress"] = 100

                    created_ts = str(job.get("dateCreated") or "2026-01-02T00:00:00Z")
                    job["dateCreated"] = created_ts
                    started_ts = str(job.get("dateStarted") or created_ts)
                    job["dateStarted"] = started_ts
                    if not job.get("dateFinished"):
                        job["dateFinished"] = _advance_iso_timestamp(started_ts, seconds=5)

                    raw_results = job.get("results")
                    if isinstance(raw_results, list):
                        total_rec = len(raw_results)
                        failed_rec = sum(
                            1
                            for entry in raw_results
                            if isinstance(entry, dict)
                            and (
                                str(entry.get("status") or "").upper()
                                in {"FAIL", "FAILED", "ERROR"}
                                or (
                                    not entry.get("status")
                                    and bool(entry.get("error") or entry.get("errors"))
                                )
                            )
                        )
                        success_rec = max(0, total_rec - failed_rec)
                        if "totalRecords" in job or job.get("totalRecords") is not None:
                            job["totalRecords"] = total_rec
                        if "successfulRecords" in job or job.get("successfulRecords") is not None:
                            job["successfulRecords"] = success_rec
                        if "failedRecords" in job or job.get("failedRecords") is not None:
                            job["failedRecords"] = failed_rec
                    else:
                        existing_dict = raw_results if isinstance(raw_results, dict) else {}
                        raw_total = job.get("totalRecords")
                        total_rec = (
                            int(raw_total)
                            if raw_total is not None
                            else int(existing_dict.get("total", 1))
                        )
                        raw_failed = job.get("failedRecords")
                        failed_rec = (
                            int(raw_failed)
                            if raw_failed is not None
                            else int(existing_dict.get("failed", 0))
                        )
                        success_rec = max(0, total_rec - failed_rec)
                        job["totalRecords"] = total_rec
                        job["successfulRecords"] = success_rec
                        job["failedRecords"] = failed_rec
                        updated_results = dict(existing_dict)
                        updated_results.update(
                            {
                                "status": "Completed",
                                "total": total_rec,
                                "success": success_rec,
                                "failed": failed_rec,
                            }
                        )
                        job["results"] = updated_results

            return _strip_private_keys(copy.deepcopy(job))

    def get_intervals(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of intervals for a study."""
        with self._lock:
            intervals = copy.deepcopy(self.intervals)
            if not study_key:
                return intervals
            return [dict(i, studyKey=study_key) for i in intervals]

    def get_variables(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of variables for a study."""
        with self._lock:
            variables = copy.deepcopy(self.variables)
            if not study_key:
                return variables
            return [dict(v, studyKey=study_key) for v in variables]

    def get_codings(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of medical codings for a study."""
        with self._lock:
            codings = copy.deepcopy(self.codings)
            if not study_key:
                return codings
            matched = [c for c in codings if c.get("studyKey") == study_key]
            if matched:
                return matched
            return [dict(c, studyKey=study_key) for c in codings]

    def get_record_revisions(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of record audit revisions for a study."""
        with self._lock:
            revisions = copy.deepcopy(self.record_revisions)
            if not study_key:
                return revisions
            return [r for r in revisions if r.get("studyKey") == study_key]

    def get_users(self, study_key: str | None = None) -> list[dict[str, Any]]:
        """Return list of users for a study."""
        with self._lock:
            users = copy.deepcopy(self.users)
            if not study_key:
                return users
            return [u for u in users if "studyKey" not in u or u.get("studyKey") == study_key]


_default_state: SimulationState | None = None
_state_lock = threading.Lock()


def get_default_simulation_state() -> SimulationState:
    """Return the global default simulation state instance."""
    global _default_state
    with _state_lock:
        if _default_state is None:
            _default_state = SimulationState()
        return _default_state


def reset_default_simulation_state() -> None:
    """Reset the global default simulation state."""
    global _default_state
    with _state_lock:
        if _default_state is not None:
            _default_state.reset()
        else:
            _default_state = SimulationState()
