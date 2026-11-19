"""TODO: Add docstring."""

from imednet import ImednetSDK
from imednet_workflows import JobPoller, RecordUpdateWorkflow

"""Example script for creating a new record and waiting for job completion.

This script demonstrates how to use :class:`RecordUpdateWorkflow` to create an
unscheduled record for an existing subject. After submitting the record the
returned batch ID is polled using :class:`JobPoller` until the job finishes.

Replace the placeholder values below with valid credentials and identifiers
before running the script.
"""

api_key = "XXXXXXXXXX"
security_key = "XXXXXXXXXX"
base_url = None  # Or set to your custom base URL if needed
study_key = "XXXXXXXXXX"
form_key = "XXXXXXXXXX"
subject_key = "XXXXXXXXXX"

sdk = ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url)
workflow = RecordUpdateWorkflow(sdk)

record_data = {"VARIABLE_NAME": "value"}

try:
    job = workflow.create_new_record(
        study_key=study_key,
        form_identifier=form_key,
        subject_identifier=subject_key,
        data=record_data,
        wait_for_completion=False,
    )

    if not job.batch_id:
        raise RuntimeError("Submission succeeded but no batch ID returned")

    status = JobPoller(sdk.jobs.get).run(study_key, job.batch_id)
    print(f"Job {status.batch_id} finished with state: {status.state}")
except Exception as e:
    print(f"Error creating record: {e}")
