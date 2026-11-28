"""Regression tests for wire-alias separation and static model override fixes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from pydantic import Field, ValidationError

from imednet.cli.subjects import _subject_view_model
from imednet.cli.utils.output import emit_json
from imednet.core.context import strict_mode_context
from imednet.models.base import ImednetBaseModel
from imednet.models.jobs import JobStatus
from imednet.models.queries import Query
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Keyword, Record
from imednet.models.studies import Study
from imednet.models.subjects import Subject, SubjectKeyword
from imednet.models.users import Role, User
from imednet.models.visits import Visit
from imednet.utils import validators
from imednet_workflows.uat.models import UATSpecification, UATVariableSpec


def test_strict_from_json_accepts_null_on_all_seven_optional_fields() -> None:
    """Under IMEDNET_STRICT_MODE=1, from_json accepts None on all 7 nullable fields."""
    with strict_mode_context(True):
        subj = Subject.from_json({"studyKey": "S1", "subjectKey": "SUBJ-1", "keywords": None})
        assert subj.keywords is None

        rec = Record.from_json(
            {"studyKey": "S1", "recordId": 10, "keywords": None, "recordData": None}
        )
        assert rec.keywords is None
        assert rec.record_data is None

        query = Query.from_json({"studyKey": "S1", "annotationId": 5, "queryComments": None})
        assert query.query_comments is None

        user = User.from_json({"userId": "U-1", "login": "ada", "roles": None})
        assert user.roles is None

        kw = Keyword.from_json(
            {"keywordName": "Safety", "keywordKey": "SAFE", "keywordId": None, "dateAdded": None}
        )
        assert kw.keyword_id is None
        assert kw.date_added is None

        subj_kw = SubjectKeyword.from_json(
            {"keywordName": "Enrolled", "keywordKey": "ENR", "keywordId": None, "dateAdded": None}
        )
        assert subj_kw.keyword_id is None
        assert subj_kw.date_added is None


def test_sentinel_datetime_retired_and_blank_dates_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permissive blank dates resolve to None, sentinel is gone, and strict rejects empty strings."""
    assert not hasattr(validators, "_SENTINEL_DATETIME")

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "0")
    assert validators.parse_datetime(float("inf")) is None
    assert validators.parse_datetime(1e30) is None
    for raw in ("", "   ", None):
        kw = Keyword.from_json({"keywordName": "K", "keywordKey": "KK", "dateAdded": raw})
        subj_kw = SubjectKeyword.from_json(
            {"keywordName": "K", "keywordKey": "KK", "dateAdded": raw}
        )
        assert kw.date_added is None
        assert subj_kw.date_added is None

    class _RequiredDatetimeModel(ImednetBaseModel):
        event_at: datetime = Field(..., alias="eventAt")

    with pytest.raises(ValidationError):
        _RequiredDatetimeModel.from_json({"eventAt": ""})

    with strict_mode_context(True):
        for raw_empty in ("", "   "):
            with pytest.raises(ValidationError):
                Keyword.from_json({"keywordName": "K", "keywordKey": "KK", "dateAdded": raw_empty})
            with pytest.raises(ValidationError):
                SubjectKeyword.from_json(
                    {"keywordName": "K", "keywordKey": "KK", "dateAdded": raw_empty}
                )
            with pytest.raises(ValidationError):
                Role.from_json({"roleId": "R1", "dateCreated": raw_empty})
            with pytest.raises(ValidationError):
                _RequiredDatetimeModel.from_json({"eventAt": raw_empty})


def test_user_name_property_omits_wire_key_and_rejects_wire_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """User.name works in Python, is omitted from JSON/CLI dumps, and rejected on strict wire input."""
    user = User(
        user_id="U-1",
        login="alovelace",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.org",
        user_active_in_study=True,
        roles=None,
    )
    assert user.name == "Ada Lovelace"

    dumped = user.model_dump(by_alias=True)
    expected_keys = {
        "userId",
        "login",
        "firstName",
        "lastName",
        "email",
        "userActiveInStudy",
        "roles",
    }
    assert set(dumped.keys()) == expected_keys
    assert "name" not in dumped

    emit_json([user], "users")
    cli_payload = json.loads(capsys.readouterr().out)
    assert len(cli_payload["items"]) == 1
    assert set(cli_payload["items"][0].keys()) == expected_keys
    assert "name" not in cli_payload["items"][0]

    with strict_mode_context(True):
        with pytest.raises(ValidationError, match="unexpected fields: name"):
            User.from_json({"userId": "U-1", "firstName": "Ada", "name": "Ada Lovelace"})


def test_cli_subject_view_model_guards_none_keywords() -> None:
    """CLI _subject_view_model and records_to_dataframe handle None collections safely."""
    from imednet.utils.pandas import records_to_dataframe

    subj = Subject.from_json({"studyKey": "S1", "subjectKey": "SUBJ-1", "keywords": None})
    row = _subject_view_model(subj)
    assert row["subject_key"] == "SUBJ-1"
    assert row["keywords"] == ""

    rec_null = Record.from_json({"studyKey": "S1", "recordId": 1, "recordData": None})
    rec_val = Record.from_json({"studyKey": "S1", "recordId": 2, "recordData": {"AGE": 42}})
    df = records_to_dataframe([rec_null, rec_val], flatten=True)
    assert len(df) == 2
    assert df.loc[1, "AGE"] == 42


@pytest.mark.parametrize(
    ("payload", "strict"),
    [
        ({"recordRevision": "0", "dataRevision": 0}, False),
        ({"recordRevision": "0.0", "dataRevision": 0}, False),
        ({"recordRevision": 0.4, "dataRevision": 0}, False),
        ({"recordRevision": "not-an-int", "dataRevision": 0}, False),
        ({"recordRevision": 1, "dataRevision": "2"}, False),
        ({"recordRevision": 1, "dataRevision": 2.9}, False),
        ({"recordRevision": 1, "dataRevision": "-1"}, False),
        ({"recordRevision": True, "dataRevision": 0}, False),
        ({"recordRevision": 2, "dataRevision": True}, False),
        ({"recordRevision": 2, "dataRevision": False}, False),
        ({"recordRevision": True, "dataRevision": 0}, True),
        ({"recordRevision": 2, "dataRevision": False}, True),
    ],
)
def test_record_revision_post_coercion_bounds_and_boolean_rejection(
    payload: dict[str, Any],
    strict: bool,
) -> None:
    """RecordRevision rejects invalid coerced revisions and boolean inputs across modes."""
    with strict_mode_context(strict):
        with pytest.raises(ValidationError):
            RecordRevision.from_json(payload)


def test_record_revision_direct_mutation_maintains_bounds() -> None:
    """Direct attribute mutation on RecordRevision continues enforcing Part 11 bounds."""
    rev = RecordRevision.from_json({"recordRevision": 2, "dataRevision": 1})
    assert rev.record_revision == 2
    assert rev.data_revision == 1

    with pytest.raises(ValueError, match="cannot exceed record_revision"):
        rev.data_revision = 3
    with pytest.raises(ValueError, match="must be >= 1"):
        rev.record_revision = 0
    with pytest.raises(ValueError, match="not bool"):
        rev.data_revision = True  # type: ignore[assignment]


def test_visit_from_json_preserves_caller_dict_with_empty_dates() -> None:
    """Visit.from_json and Visit(**payload) do not mutate caller dictionaries in place."""
    wire_payload = {
        "studyKey": "S1",
        "visitId": 101,
        "startDate": "",
        "endDate": "",
        "dueDate": "",
        "visitDate": "",
    }
    snapshot = dict(wire_payload)
    visit = Visit.from_json(wire_payload)
    assert wire_payload == snapshot
    assert visit.start_date is None
    assert visit.end_date is None

    py_payload = {"study_key": "S1", "visit_id": 102, "start_date": "", "endDate": ""}
    py_snapshot = dict(py_payload)
    py_visit = Visit.model_validate(py_payload)
    assert py_payload == py_snapshot
    assert py_visit.start_date is None
    assert py_visit.end_date is None


def test_strict_wire_from_json_rejects_snake_case_while_constructors_accept_it() -> None:
    """Strict from_json rejects snake_case wire keys while Python constructors preserve ergonomics."""
    with strict_mode_context(True):
        with pytest.raises(ValidationError, match="unexpected fields: study_key"):
            Study.from_json({"study_key": "S1"})

        with pytest.raises(ValidationError, match="unexpected fields: total_records"):
            JobStatus.from_json({"total_records": 1})

        # Nested models inside from_json also enforce wire-only keys
        with pytest.raises(ValidationError, match="unexpected fields: keyword_name"):
            Record.from_json({"recordId": 1, "keywords": [{"keyword_name": "K"}]})

        study = Study(study_key="S1")
        assert study.study_key == "S1"

        job = JobStatus(total_records=5, successful_records=4, failed_records=1)
        assert job.total_records == 5
        assert job.successful_records == 4
        assert job.failed_records == 1

        user = User(first_name="Ada", last_name="Lovelace")
        assert user.first_name == "Ada"
        assert user.name == "Ada Lovelace"

        uat_var = UATVariableSpec(
            variable_name="SUBJID",
            variable_key="SUBJID",
            variable_type="TEXT",
            form_key="DM",
        )
        assert uat_var.variable_name == "SUBJID"
        spec = UATSpecification(study_key="S1", study_name="Demo Study")
        assert spec.study_key == "S1"


def test_from_json_pre_pydantic_2_11_fallback_preserves_wire_only_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When model_validate lacks by_alias/by_name (e.g. Pyodide), from_json still enforces wire keys."""
    import imednet.models.base as base_mod

    monkeypatch.setattr(base_mod, "_HAS_VALIDATE_BY_ALIAS", False)

    # Permissive mode ignores snake_case keys on wire input without mutating caller dict
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "0")
    caller_payload = {"study_key": "SNAKE", "studyName": "Wire Study"}
    snapshot = dict(caller_payload)
    permissive_study = Study.from_json(caller_payload)
    assert caller_payload == snapshot
    assert permissive_study.study_key is None
    assert permissive_study.study_name == "Wire Study"

    nested_rec = Record.from_json(
        {"recordId": 7, "keywords": [{"keyword_name": "Ignored", "keywordKey": "WIRE_KEY"}]}
    )
    assert nested_rec.record_id == 7
    assert nested_rec.keywords is not None
    assert nested_rec.keywords[0].keyword_name == ""
    assert nested_rec.keywords[0].keyword_key == "WIRE_KEY"

    # Strict mode still raises ValidationError on snake_case wire keys
    with strict_mode_context(True):
        with pytest.raises(ValidationError, match="unexpected fields: study_key"):
            Study.from_json({"study_key": "S1"})
        valid_study = Study.from_json({"studyKey": "S1", "studyName": "Strict Study"})
        assert valid_study.study_key == "S1"
