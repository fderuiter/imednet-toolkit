"""Example usage of SyntheticRecordGenerator."""

from datetime import date
from unittest.mock import MagicMock

from imednet_workflows.uat import (
    RecordTestType,
    StudySnapshot,
    SyntheticRecordGenerator,
    UATFormSpec,
    UATSpecification,
    UATSubjectSpec,
    UATVariableSpec,
    VariableTestStrategy,
)


def main() -> None:
    """Execute the UAT record generation example."""
    # 1. Create a mocked StudySnapshot (normally from StudySchemaInspector)
    snapshot = MagicMock(spec=StudySnapshot)
    snapshot.active_sites.return_value = [MagicMock(site_name="North Site")]

    # 2. Define a UAT Specification
    spec = UATSpecification(
        study_key="DEMO-STUDY",
        study_name="Demo Study",
        global_date_value=date.today(),
        subject_specs=[
            UATSubjectSpec(site_name="North Site", subject_count=5, subject_key_prefix="UAT-")
        ],
        form_specs=[
            UATFormSpec(
                form_key="F_DM",
                form_name="Demographics",
                form_type="Enrollment",
                test_type=RecordTestType.REGISTER_SUBJECT,
                subject_count=2,
                variables=[
                    UATVariableSpec(
                        variable_name="AGE",
                        variable_key="V_AGE",
                        variable_type="Number",
                        form_key="F_DM",
                        min_value=18,
                        max_value=99,
                    ),
                    UATVariableSpec(
                        variable_name="GENDER",
                        variable_key="V_GENDER",
                        variable_type="Coded",
                        form_key="F_DM",
                        coded_values=["Male", "Female", "Other"],
                    ),
                ],
            ),
            UATFormSpec(
                form_key="F_VS",
                form_name="Vital Signs",
                form_type="Standard",
                test_type=RecordTestType.UPDATE_SCHEDULED_RECORD,
                interval_name="Visit 1",
                subject_count=3,
                variables=[
                    UATVariableSpec(
                        variable_name="WEIGHT",
                        variable_key="V_WEIGHT",
                        variable_type="Float",
                        form_key="F_VS",
                        strategy=VariableTestStrategy.SYNTHETIC,
                        min_value=50.0,
                        max_value=120.0,
                    ),
                    UATVariableSpec(
                        variable_name="COMMENT",
                        variable_key="V_COMMENT",
                        variable_type="TextArea",
                        form_key="F_VS",
                    ),
                ],
            ),
        ],
    )

    # 3. Initialize generator with a seed for reproducibility
    generator = SyntheticRecordGenerator(seed=12345)

    # 4. Generate payloads
    record_sets = generator.generate(spec, snapshot)

    # 5. Inspect output
    for record_set in record_sets:
        print(f"--- Form: {record_set.form_name} ({record_set.form_key}) ---")
        print(f"Test Type: {record_set.test_type}")
        print(f"Payloads Generated: {len(record_set.payloads)}")
        for i, payload in enumerate(record_set.payloads):
            print(f"  Payload {i + 1}: {payload}")
        if record_set.warnings:
            print(f"  Warnings: {record_set.warnings}")
        print()


if __name__ == "__main__":
    main()
