"""Demonstrate configuring BulkRecordSubmissionWorkflow and GeneratedRecordSet payloads."""

import os

from imednet import ImednetSDK, load_config
from imednet_workflows.uat import BulkRecordSubmissionWorkflow, GeneratedRecordSet, RecordTestType


def main() -> None:
    """Configure bulk registration and study data submission sets."""
    try:
        cfg = load_config()
    except ValueError as e:
        print(f"Skipping execution: {e}")
        return

    study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"

    try:
        with ImednetSDK(
            api_key=cfg.api_key,
            security_key=cfg.security_key,
            base_url=cfg.base_url,
        ) as sdk:
            workflow = BulkRecordSubmissionWorkflow(
                sdk, batch_size=500, skip_existing_subjects=True, await_registration=True
            )

            # Example record sets (normally produced by a generator or mapped from source data)
            registration_set = GeneratedRecordSet(
                form_key="F_DM",
                form_name="Demographics",
                test_type=RecordTestType.REGISTER_SUBJECT,
                payloads=[{"subjectKey": "SUBJ_1", "V_AGE": 45}],
                subject_keys=["SUBJ_1"],
            )

            data_set = GeneratedRecordSet(
                form_key="F_VS",
                form_name="Vital Signs",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                payloads=[{"subjectKey": "SUBJ_1", "V_PULSE": 72, "V_SYSBP": 120}],
                subject_keys=["SUBJ_1"],
            )

            result = workflow.submit(study_key, [registration_set, data_set])
            print(
                f"Submitted {result.total_records_submitted} records across "
                f"{len(result.all_batches)} batches."
            )
    except Exception as e:
        print(f"Failed: {e}")


if __name__ == "__main__":
    main()
