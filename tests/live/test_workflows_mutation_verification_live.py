"""Deep verification scenarios for workflow mutations.

This module strengthens live coverage by verifying post-action outcomes,
not just job submission success.
"""

import uuid

import pytest

from imednet.models.records import RegisterSubjectRequest
from imednet.sdk import ImednetSDK
from imednet_workflows import RegisterSubjectsWorkflow
from tests.live.helpers import require_mutation

pytestmark = pytest.mark.live


@pytest.fixture
def unique_tag() -> str:
    """Return a unique string for tagging test data."""
    return f"LT-{uuid.uuid4().hex[:8]}"


def test_verify_subject_registration_outcome(
    sdk: ImednetSDK, study_key: str, first_site_name: str, first_form_key: str, unique_tag: str
) -> None:
    """Validate that subject registration results in an observable subject."""
    require_mutation()

    # Find a variable to store our tag
    variables = list(sdk.variables.list(study_key=study_key, formKey=first_form_key))
    if not variables:
        pytest.skip(f"No variables available for form {first_form_key}")

    var_name = variables[0].variable_name
    data = {var_name: unique_tag}

    req = RegisterSubjectRequest(formKey=first_form_key, siteName=first_site_name, data=data)

    wf = RegisterSubjectsWorkflow(sdk)
    # Step 1: Submit mutation and wait for completion
    job = wf.register_subjects(study_key, [req], wait_for_completion=True)

    # Step 2: Observe job success
    assert job.is_successful, f"Registration job failed: {job.state}"

    # Step 3: Verify observable state
    # We list subjects for the site and look for our tag in the record data
    # Note: Finding by tag in record data might require fetching records for each candidate subject
    # if the subjects listing doesn't include form data.

    # Optimization: Usually job results for registration contain the new subject key.
    # If not, we fall back to searching.
    subject_key = None
    if isinstance(job.results, list) and len(job.results) > 0:
        res = job.results[0]
        if isinstance(res, dict):
            subject_key = res.get("subjectKey")

    if subject_key:
        matched_subjects = list(sdk.get_subjects(study_key, subjectKey=subject_key))
        assert matched_subjects, f"Subject {subject_key} not found"
        subj = matched_subjects[0]
        assert subj.subject_key == subject_key
        if subj.subject_id is not None:
            fetched = sdk.subjects.get(study_key, subj.subject_id)
            assert fetched.subject_id == subj.subject_id
        # Also check the record data
        records = list(sdk.get_records(study_key, subjectKey=subject_key, formKey=first_form_key))
        assert any(r.data.get(var_name) == unique_tag for r in records), "Record data mismatch"
    else:
        # Search all subjects for this site (might be slow but robust for testing)
        subjects = list(sdk.get_subjects(study_key, siteName=first_site_name))
        found = False
        for s in subjects:
            recs = list(
                sdk.get_records(study_key, subjectKey=s.subject_key, formKey=first_form_key)
            )
            if any(r.data.get(var_name) == unique_tag for r in recs):
                found = True
                break
        assert found, f"Subject with tag {unique_tag} not found"


def test_verify_create_new_record_outcome(
    sdk: ImednetSDK, study_key: str, first_subject_key: str, first_form_key: str, unique_tag: str
) -> None:
    """Validate that a newly created record is discoverable and correct."""
    require_mutation()

    variables = list(sdk.variables.list(study_key=study_key, formKey=first_form_key))
    if not variables:
        pytest.skip(f"No variables available for form {first_form_key}")

    var_name = variables[0].variable_name
    data = {var_name: unique_tag}

    # Step 1: Submit mutation
    job = sdk.workflows.record_update.create_new_record(
        study_key, first_form_key, first_subject_key, data, wait_for_completion=True
    )

    # Step 2: Observe job success
    assert job.is_successful, f"Record creation job failed: {job.state}"

    # Step 3: Verify observable state
    records = list(sdk.get_records(study_key, subjectKey=first_subject_key, formKey=first_form_key))
    found_record = None
    for r in records:
        if r.data.get(var_name) == unique_tag:
            found_record = r
            break

    assert found_record is not None, f"Record with tag {unique_tag} not found"
    assert found_record.subject_key == first_subject_key
    assert found_record.form_key == first_form_key


def test_verify_update_scheduled_record_outcome(
    sdk: ImednetSDK, study_key: str, first_subject_key: str, unique_tag: str
) -> None:
    """Validate that a scheduled record update is reflected in subsequent reads."""
    require_mutation()

    # Find a scheduled record to update
    all_records = list(sdk.get_records(study_key, subjectKey=first_subject_key))
    scheduled = [r for r in all_records if getattr(r, "interval_name", None)]

    if not scheduled:
        pytest.skip(f"No scheduled records found for subject {first_subject_key}")

    target = scheduled[0]
    variables = list(sdk.variables.list(study_key=study_key, formKey=target.form_key))
    if not variables:
        pytest.skip(f"No variables available for form {target.form_key}")

    var_name = variables[0].variable_name
    data = {var_name: unique_tag}

    # Step 1: Submit update
    job = sdk.workflows.record_update.update_scheduled_record(
        study_key,
        target.form_key,
        first_subject_key,
        target.interval_name,
        data,
        wait_for_completion=True,
    )

    # Step 2: Observe job success
    assert job.is_successful, f"Scheduled update job failed: {job.state}"

    # Step 3: Re-read and verify outcome
    filter_kwargs = {"subjectKey": first_subject_key, "formKey": target.form_key}
    if getattr(target, "interval_id", None) is not None:
        filter_kwargs["intervalId"] = target.interval_id
    updated_records = list(sdk.get_records(study_key, **filter_kwargs))

    assert any(r.data.get(var_name) == unique_tag for r in updated_records), (
        "Update not reflected in follow-up read"
    )


def test_verify_batch_update_outcome(
    sdk: ImednetSDK, study_key: str, first_subject_key: str, first_form_key: str, unique_tag: str
) -> None:
    """Validate that a batch mutation reaches terminal state and produces observable results."""
    require_mutation()

    variables = list(sdk.variables.list(study_key=study_key, formKey=first_form_key))
    if not variables:
        pytest.skip(f"No variables available for form {first_form_key}")

    var_name = variables[0].variable_name
    tag1 = f"{unique_tag}-B1"
    tag2 = f"{unique_tag}-B2"

    records_data = [
        {"formKey": first_form_key, "subjectKey": first_subject_key, "data": {var_name: tag1}},
        {"formKey": first_form_key, "subjectKey": first_subject_key, "data": {var_name: tag2}},
    ]

    # Step 1: Submit batch
    job = sdk.workflows.record_update.create_or_update_records(
        study_key, records_data, wait_for_completion=True
    )

    # Step 2: Verify terminal state and individual results
    assert job.is_successful
    assert job.successful_records >= 2

    # Step 3: Verify observable outcome
    records = list(sdk.get_records(study_key, subjectKey=first_subject_key, formKey=first_form_key))
    all_tags = [r.data.get(var_name) for r in records]

    assert tag1 in all_tags
    assert tag2 in all_tags
