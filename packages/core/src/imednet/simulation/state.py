"""In-memory fixture state for offline simulation mode."""

from __future__ import annotations

import copy
import threading
from typing import Any


class SimulationState:
    """Thread-safe in-memory store representing simulated EDC server state."""

    def __init__(self) -> None:
        """Initialize simulation state with deterministic fixtures."""
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Reset all in-memory fixtures to clean initial state."""
        with getattr(self, "_lock", threading.Lock()):
            self._next_record_id: int = 1000
            self._next_revision_id: int = 1202
            self._next_batch_num: int = 1

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
                    "recordData": {"V_AGE": 45, "V_GENDER": "Female"},
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
                    "recordData": {"V_WEIGHT": 68.5},
                    "deleted": False,
                    "dateCreated": "2026-01-10T09:35:00Z",
                    "dateModified": "2026-01-10T09:35:00Z",
                    "lastUpdated": "2026-01-10T09:35:00Z",
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
                    "status": "OPEN",
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
                    "status": "OPEN",
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
                    "results": {"status": "Completed"},
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
                    "results": {"status": "Running"},
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
        user: str = "sim_user",
        role: str = "Investigator",
        reason: str = "Record created",
        record_oid: str | None = None,
        subject_oid: str | None = None,
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
            "dataRevision": revision_number,
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
            "dateCreated": record_entry["dateCreated"],
            "lastUpdated": record_entry["lastUpdated"],
        }

    def create_records(self, study_key: str, records_data: list[dict[str, Any]]) -> dict[str, Any]:
        """Store new records in memory, sync enrolled subjects, append revisions, and return a Job."""
        with self._lock:
            study_sites = [s for s in self.sites if s.get("studyKey") == study_key]
            default_site = study_sites[0] if study_sites else {"siteId": 101, "siteName": "SITE"}
            job_results: list[dict[str, Any]] = []

            for item in records_data:
                requested_rec_id = item.get("recordId")
                existing_record = (
                    next(
                        (
                            r
                            for r in self.records
                            if r.get("studyKey") == study_key
                            and r.get("recordId") == requested_rec_id
                        ),
                        None,
                    )
                    if requested_rec_id is not None
                    else None
                )

                if "data" in item and isinstance(item["data"], dict):
                    rec_data: Any = item["data"]
                elif "recordData" in item and isinstance(item["recordData"], dict):
                    rec_data = item["recordData"]
                else:
                    rec_data = item
                modified_ts = str(item.get("dateModified", "2026-11-15T00:00:00Z"))
                updated_ts = str(item.get("lastUpdated", modified_ts))

                if existing_record is not None:
                    rec_id = int(existing_record["recordId"])
                    subj_id = existing_record.get("subjectId", 201)
                    subj_key = str(
                        item.get("subjectKey") or existing_record.get("subjectKey", "SUB1")
                    )
                    form_key = str(item.get("formKey") or existing_record.get("formKey", "F_DM"))
                    merged_data = copy.deepcopy(existing_record.get("recordData") or {})
                    if isinstance(rec_data, dict):
                        merged_data.update(copy.deepcopy(rec_data))
                    existing_record["recordData"] = merged_data
                    existing_record["formKey"] = form_key
                    if "recordStatus" in item:
                        existing_record["recordStatus"] = item["recordStatus"]
                    existing_record["dateModified"] = modified_ts
                    existing_record["lastUpdated"] = updated_ts
                    job_results.append(
                        {
                            "recordId": rec_id,
                            "subjectId": subj_id,
                            "subjectKey": subj_key,
                            "formKey": form_key,
                            "status": "SUCCESS",
                        }
                    )
                    self._next_revision_id += 1
                    rev_num = (
                        sum(1 for r in self.record_revisions if r.get("recordId") == rec_id) + 1
                    )
                    self.record_revisions.append(
                        self._build_record_revision(
                            revision_id=self._next_revision_id,
                            record_entry=existing_record,
                            revision_number=rev_num,
                            user=str(item.get("user", "sim_user")),
                            role=str(item.get("role", "Investigator")),
                            reason=str(item.get("reasonForChange", "Record updated")),
                            record_oid=item.get("recordOid"),
                            subject_oid=item.get("subjectOid"),
                        )
                    )
                    continue

                self._next_record_id += 1
                rec_id = self._next_record_id
                subj_key = item.get("subjectKey") or f"SUBJ-{rec_id}"

                requested_site_name = item.get("siteName")
                matched_site = (
                    next(
                        (s for s in study_sites if s.get("siteName") == requested_site_name),
                        None,
                    )
                    if requested_site_name
                    else None
                )
                site_id = item.get(
                    "siteId",
                    matched_site["siteId"] if matched_site else default_site["siteId"],
                )
                site_name = requested_site_name or (
                    matched_site["siteName"] if matched_site else default_site["siteName"]
                )

                existing_subject = next(
                    (
                        s
                        for s in self.subjects
                        if s.get("studyKey") == study_key and s.get("subjectKey") == subj_key
                    ),
                    None,
                )
                if existing_subject is not None:
                    subj_id = existing_subject.get("subjectId", 200 + (rec_id % 100))
                    existing_subject["dateModified"] = modified_ts
                    existing_subject["lastUpdated"] = updated_ts
                else:
                    subj_id = item.get("subjectId", 200 + len(self.subjects) + 1)
                    self.subjects.append(
                        {
                            "subjectId": subj_id,
                            "subjectKey": subj_key,
                            "studyKey": study_key,
                            "siteId": site_id,
                            "siteName": site_name,
                            "subjectStatus": "Enrolled",
                            "deleted": False,
                            "keywords": copy.deepcopy(item.get("keywords", [])),
                            "dateCreated": modified_ts,
                            "dateModified": modified_ts,
                            "lastUpdated": updated_ts,
                        }
                    )

                form_key = item.get("formKey", "F_DM")
                record_entry = {
                    "recordId": rec_id,
                    "studyKey": study_key,
                    "subjectKey": subj_key,
                    "subjectId": subj_id,
                    "formId": item.get("formId", 301),
                    "formKey": form_key,
                    "visitId": item.get("visitId", 401),
                    "intervalId": item.get("intervalId", 501),
                    "siteId": site_id,
                    "recordType": item.get("recordType", "Standard"),
                    "recordStatus": item.get("recordStatus", "Complete"),
                    "recordData": copy.deepcopy(rec_data),
                    "deleted": False,
                    "dateCreated": str(item.get("dateCreated", modified_ts)),
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

                self._next_revision_id += 1
                rev_num = sum(1 for r in self.record_revisions if r.get("recordId") == rec_id) + 1
                self.record_revisions.append(
                    self._build_record_revision(
                        revision_id=self._next_revision_id,
                        record_entry=record_entry,
                        revision_number=rev_num,
                        user=str(item.get("user", "sim_user")),
                        role=str(item.get("role", "Investigator")),
                        reason=str(item.get("reasonForChange", "Record created")),
                        record_oid=item.get("recordOid"),
                        subject_oid=item.get("subjectOid"),
                    )
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
                    "status": "OPEN",
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
                    "status": "OPEN",
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
        """Return list of all jobs."""
        with self._lock:
            return [copy.deepcopy(j) for j in self.jobs.values()]

    def get_job(self, study_key: str | None, batch_id: str) -> dict[str, Any]:
        """Return job status by batch ID, handling state transitions on polling."""
        with self._lock:
            if batch_id in self.jobs:
                job = self.jobs[batch_id]
                # If job was initialized in a non-terminal state, advance on polling
                poll_count = job.get("_poll_count", 0) + 1
                job["_poll_count"] = poll_count
                if (
                    job.get("state") in ("PROCESSING", "RUNNING", "QUEUED", "PENDING")
                    and poll_count >= 2
                ):
                    job["state"] = "COMPLETED"
                    job["progress"] = 100
                res = copy.deepcopy(job)
                res.pop("_poll_count", None)
                return res

            # Auto-provision terminal status for any unknown batch identifier
            job = {
                "jobId": f"JOB-{batch_id}",
                "batchId": batch_id,
                "state": "COMPLETED",
                "progress": 100,
                "totalRecords": 1,
                "successfulRecords": 1,
                "failedRecords": 0,
                "results": {"status": "Completed"},
                "dateCreated": "2026-01-01T00:00:00Z",
                "dateStarted": "2026-01-01T00:00:00Z",
                "dateFinished": "2026-01-01T00:00:00Z",
            }
            self.jobs[batch_id] = job
            return copy.deepcopy(job)

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
