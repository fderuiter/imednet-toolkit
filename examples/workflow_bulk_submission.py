"""Demonstrate configuring BulkRecordSubmissionWorkflow and GeneratedRecordSet payloads."""

from imednet import ImednetSDK, load_config
from imednet_workflows.uat import BulkRecordSubmissionWorkflow, GeneratedRecordSet, RecordTestType


def main() -> None:
    """Configure bulk registration and study data submission sets."""
    try:
        cfg = load_config()
    except ValueError as e:
        print(f"Skipping execution: {e}")
        return

    # In a real environment, you'd execute against an active study.
    try:
        with ImednetSDK(
            api_key=cfg.api_key,
            security_key=cfg.security_key,
            base_url=cfg.base_url,
        ) as sdk:
            BulkRecordSubmissionWorkflow(
                sdk, batch_size=500, skip_existing_subjects=True, await_registration=True
            )

            # Example record sets (normally produced by a generator or mapped from source data)
            GeneratedRecordSet(
                form_key="REG",
                form_name="Registration Form",
                test_type=RecordTestType.REGISTER_SUBJECT,
                payloads=[{"subject_id": "SUBJ_1", "age": 45}],
                subject_keys=["SUBJ_1"],
            )

            GeneratedRecordSet(
                form_key="VITALS",
                form_name="Vitals Form",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                payloads=[{"subject_id": "SUBJ_1", "hr": 72, "bp": "120/80"}],
                subject_keys=["SUBJ_1"],
            )

            # Submit in two phases: Registration first, then Data
            # result = workflow.submit("MY_STUDY", [registration_set, data_set])
            # print(f"Submitted {result.total_records_submitted} records across {len(result.all_batches)} batches.")
    except Exception as e:
        print(f"Failed: {e}")


if __name__ == "__main__":
    main()
