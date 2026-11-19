"""Create a new record and wait for job completion using :class:`RecordUpdateWorkflow`.

Demonstrates creating an unscheduled record for an existing subject and polling
the returned batch ID with :class:`JobPoller` until the job finishes.
"""

import os

from imednet import ImednetSDK
from imednet_workflows import JobPoller, RecordUpdateWorkflow

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"
form_key = os.getenv("IMEDNET_FORM_KEY") or "F_VS"
subject_key = os.getenv("IMEDNET_SUBJECT_KEY") or "SUB1"

record_data = {"V_WEIGHT": 70.0}

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    workflow = RecordUpdateWorkflow(sdk)
    job = workflow.create_new_record(
        study_key=study_key,
        form_identifier=form_key,
        subject_identifier=subject_key,
        data=record_data,
        wait_for_completion=False,
    )

    if not job.batch_id:
        raise RuntimeError("Submission succeeded but no batch ID returned")

    status = JobPoller(sdk.jobs.get, False).run(study_key, job.batch_id)
    print(f"Job {status.batch_id} finished with state: {status.state}")
