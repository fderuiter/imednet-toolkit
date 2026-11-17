"""Unit tests for endpoints sync live."""

from typing import Any

import pytest

from imednet.errors import ServerError
from imednet.models.codings import Coding
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.queries import Query
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.sdk import ImednetSDK
from tests.live.helpers import require_mutation


def test_list_studies(sdk: ImednetSDK) -> None:
    """Test that list studies."""
    studies = list(sdk.studies.list())
    assert isinstance(studies, list)
    assert studies, "No studies returned from server"
    assert isinstance(studies[0], Study)
    study = sdk.studies.get(None, studies[0].study_key)
    assert study.study_key == studies[0].study_key


def test_list_sites(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list sites."""
    sites = list(sdk.sites.list(study_key=study_key))
    assert isinstance(sites, list)
    if sites:
        assert isinstance(sites[0], Site)
        site = sdk.sites.get(study_key, sites[0].site_id)
        assert site.site_id == sites[0].site_id


def test_get_study(sdk: ImednetSDK, study_key: str) -> None:
    """Test that get study."""
    try:
        study = sdk.studies.get(None, study_key)
    except ServerError as exc:
        pytest.fail(f"Server error retrieving study {study_key}: {exc.response}")
    else:
        assert study.study_key == study_key


def test_list_forms(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list forms."""
    forms = list(sdk.forms.list(study_key=study_key))
    assert isinstance(forms, list)
    if forms:
        assert isinstance(forms[0], Form)
        form = sdk.forms.get(study_key, forms[0].form_id)
        assert form.form_id == forms[0].form_id


def test_list_subjects(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list subjects."""
    subjects = list(sdk.subjects.list(study_key=study_key))
    assert isinstance(subjects, list)
    if subjects:
        assert isinstance(subjects[0], Subject)
        subject = sdk.subjects.get(study_key, subjects[0].subject_key)
        assert subject.subject_key == subjects[0].subject_key


def test_list_records(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list records."""
    records = list(sdk.records.list(study_key=study_key))
    assert isinstance(records, list)
    if records:
        assert isinstance(records[0], Record)
        record = sdk.records.get(study_key, records[0].record_id)
        assert record.record_id == records[0].record_id


def test_list_intervals(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list intervals."""
    intervals = list(sdk.intervals.list(study_key=study_key))
    assert isinstance(intervals, list)
    if intervals:
        assert isinstance(intervals[0], Interval)
        interval = sdk.intervals.get(study_key, intervals[0].interval_id)
        assert interval.interval_id == intervals[0].interval_id


def test_list_visits(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list visits."""
    visits = list(sdk.visits.list(study_key=study_key))
    assert isinstance(visits, list)
    if visits:
        assert isinstance(visits[0], Visit)
        visit = sdk.visits.get(study_key, visits[0].visit_id)
        assert visit.visit_id == visits[0].visit_id


def test_list_variables(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list variables."""
    variables = list(sdk.variables.list(study_key=study_key))
    assert isinstance(variables, list)
    if variables:
        assert isinstance(variables[0], Variable)
        variable = sdk.variables.get(study_key, variables[0].variable_id)
        assert variable.variable_id == variables[0].variable_id


def test_list_users(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list users."""
    assert not hasattr(sdk.users, "get")
    users = list(sdk.users.list(study_key=study_key))
    assert isinstance(users, list)
    if users:
        assert isinstance(users[0], User)


def test_list_queries(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list queries."""
    queries = list(sdk.queries.list(study_key=study_key))
    assert isinstance(queries, list)
    if queries:
        assert isinstance(queries[0], Query)
        query = sdk.queries.get(study_key, queries[0].annotation_id)
        assert query.annotation_id == queries[0].annotation_id


def test_list_record_revisions(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list record revisions."""
    revisions = list(sdk.record_revisions.list(study_key=study_key))
    assert isinstance(revisions, list)
    if revisions:
        assert isinstance(revisions[0], RecordRevision)
        revision = sdk.record_revisions.get(study_key, revisions[0].record_revision_id)
        assert revision.record_revision_id == revisions[0].record_revision_id


def test_job_get_known_batch(sdk: ImednetSDK, study_key: str, generated_batch_id: str) -> None:
    """Test that job get known batch."""
    job = sdk.jobs.get(study_key, generated_batch_id)
    assert job.batch_id == generated_batch_id


@pytest.mark.parametrize(
    "record_payload",
    ["register", "scheduled", "new"],
    indirect=True,
)
def test_create_record_and_poll_job(
    sdk: ImednetSDK,
    study_key: str,
    record_payload: dict[str, Any],
) -> None:
    """Test that create record and poll job."""
    require_mutation()
    job = sdk.records.create(study_key, [record_payload])
    if not job.batch_id:
        pytest.skip("Job completed synchronously without a batch ID")
    polled = sdk.jobs.get(study_key, job.batch_id)
    assert polled.batch_id == job.batch_id


def test_list_codings(sdk: ImednetSDK, study_key: str) -> None:
    """Test that list codings."""
    codings = list(sdk.codings.list(study_key=study_key))
    assert isinstance(codings, list)
    if codings:
        assert isinstance(codings[0], Coding)
        coding = sdk.codings.get(study_key, str(codings[0].coding_id))
        assert coding.coding_id == codings[0].coding_id
