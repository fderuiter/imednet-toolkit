"""Demonstrate running BulkRecordSubmissionWorkflow with GeneratedRecordSet payloads."""

import os

from imednet import ImednetSDK, load_config
from imednet_workflows.uat import BulkRecordSubmissionWorkflow, GeneratedRecordSet, RecordTestType


def main() -> None:
    """Configure and submit bulk Subject registration and Study Record sets."""
    try:
        cfg = load_config()
    except ValueError:
        cfg = load_config(simulation_mode=True)

    study_key = os.environ.get("IMEDNET_STUDY_KEY") or "STUDY"

    with ImednetSDK(
        api_key=cfg.api_key,
        security_key=cfg.security_key,
        base_url=cfg.base_url,
        simulation_mode=cfg.simulation_mode,
    ) as sdk:
        workflow = BulkRecordSubmissionWorkflow(
            sdk,
            batch_size=500,
            skip_existing_subjects=True,
            await_registration=True,
            registration_poll_interval=0.05,
        )

        registration_set = GeneratedRecordSet(
            form_key="F_DM",
            form_name="Demographics",
            test_type=RecordTestType.REGISTER_SUBJECT,
            payloads=[
                {
                    "formKey": "F_DM",
                    "siteName": "SITE",
                    "subjectKey": "SUBJ-BULK-001",
                    "data": {"V_AGE": 45, "V_GENDER": "M"},
                }
            ],
            subject_keys=["SUBJ-BULK-001"],
        )

        data_set = GeneratedRecordSet(
            form_key="F_VS",
            form_name="Vital Signs",
            test_type=RecordTestType.CREATE_NEW_RECORD,
            payloads=[
                {
                    "formKey": "F_VS",
                    "siteName": "SITE",
                    "subjectKey": "SUBJ-BULK-001",
                    "intervalName": "Baseline",
                    "data": {"V_WEIGHT": 72.5},
                }
            ],
            subject_keys=["SUBJ-BULK-001"],
        )

        # Submit in two phases: Subject registration first, then Study Form data
        result = workflow.submit(study_key, [registration_set, data_set])
        print(
            f"Submitted {result.total_records_submitted} Records across "
            f"{len(result.all_batches)} Job batches for Study {result.study_key}."
        )


if __name__ == "__main__":
    main()
