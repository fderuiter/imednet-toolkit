# pylint: disable=duplicate-code
"""Utility functions for generating fake API payloads."""

from __future__ import annotations

from typing import Any

from imednet.models.forms import Form
from imednet.models.variables import Variable
from imednet.validation.cache import SchemaCache

from .data_generator import generator as faker


def _timestamp() -> str:
    """Return a formatted datetime string."""
    return faker.date_time().strftime("%Y-%m-%d %H:%M:%S")


def fake_subject() -> dict[str, Any]:
    """Return a fake subject payload."""
    return {
        "studyKey": faker.bothify(text="??????"),
        "subjectId": faker.random_int(min=1, max=10000),
        "subjectOid": faker.uuid4(),
        "subjectKey": f"{faker.random_int(10, 99)}-{faker.random_int(100, 999)}",
        "subjectStatus": faker.random_element(["Enrolled", "Screened", "Completed"]),
        "siteId": faker.random_int(min=1, max=9999),
        "siteName": faker.company(),
        "deleted": False,
        "enrollmentStartDate": _timestamp(),
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
    }


def fake_site() -> dict[str, Any]:
    """Return a fake site payload."""
    return {
        "studyKey": faker.bothify(text="??????"),
        "siteId": faker.random_int(min=1, max=10000),
        "siteName": faker.company(),
        "siteEnrollmentStatus": faker.random_element(["Enrollment Open", "Enrollment Closed"]),
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
    }


def fake_interval() -> dict[str, Any]:
    """Return a fake interval payload."""
    return {
        "studyKey": faker.bothify(text="??????"),
        "intervalId": faker.random_int(min=1, max=10000),
        "intervalName": faker.word().title(),
        "intervalDescription": faker.sentence(nb_words=3),
        "intervalSequence": faker.random_int(min=1, max=500),
        "intervalGroupId": faker.random_int(min=1, max=100),
        "intervalGroupName": faker.word().title(),
        "disabled": faker.boolean(),
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
        "timeline": "Start Date End Date",
        "definedUsingInterval": faker.word().title(),
        "windowCalculationForm": faker.word().title(),
        "windowCalculationDate": faker.lexify(text="????"),
        "actualDateForm": faker.word().title(),
        "actualDate": faker.lexify(text="????"),
        "dueDateWillBeIn": faker.random_int(min=1, max=60),
        "negativeSlack": faker.random_int(min=1, max=14),
        "positiveSlack": faker.random_int(min=1, max=14),
        "eproGracePeriod": faker.random_int(min=1, max=7),
        "forms": [
            {
                "formId": faker.random_int(min=1, max=10000),
                "formKey": faker.lexify(text="????"),
                "formName": faker.word().title(),
            }
        ],
    }


def fake_query() -> dict[str, Any]:
    """Return a fake query payload."""
    return {
        "studyKey": faker.bothify(text="??????"),
        "subjectId": faker.random_int(min=1, max=10000),
        "subjectOid": faker.uuid4(),
        "annotationType": faker.random_element(["subject", "record", "question"]),
        "annotationId": faker.random_int(min=1, max=10000),
        "type": faker.random_element(["subject", "record", "question"]),
        "description": faker.sentence(nb_words=3),
        "recordId": faker.random_int(min=1, max=10000),
        "variable": faker.lexify(text="????"),
        "subjectKey": f"{faker.random_int(100, 999)}-{faker.random_int(100, 999)}",
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
        "queryComments": [
            {
                "sequence": 1,
                "annotationStatus": "Monitor Query Open",
                "user": faker.user_name(),
                "comment": faker.sentence(),
                "closed": False,
                "date": _timestamp(),
            }
        ],
    }


def _fake_value(var_type: str) -> Any:
    """Return a fake value matching ``var_type``."""
    var_type = (var_type or "").lower()
    if var_type in {"int", "integer"}:
        return faker.random_int(min=0, max=100)
    if var_type in {"number", "float", "decimal"}:
        return faker.pyfloat(left_digits=2, right_digits=2, positive=True)
    if var_type in {"bool", "boolean", "checkbox"}:
        return faker.boolean()
    if var_type == "date":
        return faker.date_time().strftime("%Y-%m-%d")
    return faker.word()


def fake_record(schema: SchemaCache | None = None) -> dict[str, Any]:
    """Return a fake record payload.

    When ``schema`` is provided variable metadata is used to generate typed
    ``recordData`` values.
    """
    if schema and schema._form_variables:
        form_key = faker.random_element(list(schema._form_variables))
        variables = schema.variables_for_form(form_key)
        first_var = next(iter(variables.values()))
        form_id = first_var.form_id
        record_data = {
            name: _fake_value(var.variable_type or "") for name, var in variables.items()
        }
        return {
            "studyKey": faker.bothify(text="??????"),
            "intervalId": faker.random_int(min=1, max=10000),
            "formId": form_id,
            "formKey": form_key,
            "siteId": faker.random_int(min=1, max=10000),
            "recordId": faker.random_int(min=1, max=10000),
            "recordOid": faker.uuid4(),
            "recordType": "SUBJECT",
            "recordStatus": faker.random_element(["Record Incomplete", "Record Complete"]),
            "deleted": False,
            "dateCreated": _timestamp(),
            "dateModified": _timestamp(),
            "subjectId": faker.random_int(min=1, max=10000),
            "subjectOid": faker.uuid4(),
            "subjectKey": f"{faker.random_int(100, 999)}-{faker.random_int(100, 999)}",
            "visitId": faker.random_int(min=1, max=10000),
            "parentRecordId": faker.random_int(min=1, max=10000),
            "recordData": record_data,
        }

    return {
        "studyKey": faker.bothify(text="??????"),
        "intervalId": faker.random_int(min=1, max=10000),
        "formId": faker.random_int(min=1, max=10000),
        "formKey": faker.lexify(text="????"),
        "siteId": faker.random_int(min=1, max=10000),
        "recordId": faker.random_int(min=1, max=10000),
        "recordOid": faker.uuid4(),
        "recordType": "SUBJECT",
        "recordStatus": faker.random_element(["Record Incomplete", "Record Complete"]),
        "deleted": False,
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
        "subjectId": faker.random_int(min=1, max=10000),
        "subjectOid": faker.uuid4(),
        "subjectKey": f"{faker.random_int(100, 999)}-{faker.random_int(100, 999)}",
        "visitId": faker.random_int(min=1, max=10000),
        "parentRecordId": faker.random_int(min=1, max=10000),
        "recordData": {
            "dateCreated": _timestamp(),
            "unvnum": str(faker.random_int(min=1, max=5)),
            "dateModified": _timestamp(),
            "aeser": "",
            "aeterm": faker.word().title(),
        },
    }


def fake_form() -> dict[str, Any]:
    """Return a fake form payload."""
    return {
        "studyKey": faker.bothify(text="??????"),
        "formId": faker.random_int(min=1, max=10000),
        "formKey": faker.lexify(text="????"),
        "formName": faker.word().title(),
        "formType": faker.random_element(["CRF", "Diary"]),
        "revision": faker.random_int(min=1, max=10),
        "embeddedLog": faker.boolean(),
        "enforceOwnership": faker.boolean(),
        "userAgreement": faker.boolean(),
        "subjectRecordReport": faker.boolean(),
        "unscheduledVisit": faker.boolean(),
        "otherForms": faker.boolean(),
        "eproForm": faker.boolean(),
        "allowCopy": faker.boolean(),
        "disabled": faker.boolean(),
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
    }


def fake_variable() -> dict[str, Any]:
    """Return a fake variable payload."""
    return {
        "studyKey": faker.bothify(text="??????"),
        "variableId": faker.random_int(min=1, max=10000),
        "variableType": faker.random_element(["text", "integer", "date"]),
        "variableName": faker.lexify(text="????"),
        "sequence": faker.random_int(min=1, max=100),
        "revision": faker.random_int(min=1, max=10),
        "disabled": faker.boolean(),
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
        "formId": faker.random_int(min=1, max=10000),
        "variableOid": faker.uuid4(),
        "deleted": faker.boolean(),
        "formKey": faker.lexify(text="????"),
        "formName": faker.word().title(),
        "label": faker.word().title(),
        "blinded": faker.boolean(),
    }


def fake_forms_for_cache(num_forms: int = 1, study_key: str | None = None) -> list[Form]:
    """Return a list of :class:`~imednet.models.forms.Form` objects for caching."""
    forms: list[Form] = []
    for _ in range(num_forms):
        data = fake_form()
        if study_key is not None:
            data["studyKey"] = study_key
        forms.append(Form.from_json(data))
    return forms


def fake_variables_for_cache(
    forms: list[Form],
    vars_per_form: int = 1,
    study_key: str | None = None,
) -> list[Variable]:
    """Return a list of :class:`~imednet.models.variables.Variable` objects."""
    variables: list[Variable] = []
    for form in forms:
        for _ in range(vars_per_form):
            data = fake_variable()
            data["formId"] = form.form_id
            data["formKey"] = form.form_key
            data["formName"] = form.form_name
            if study_key is not None:
                data["studyKey"] = study_key
            variables.append(Variable.from_json(data))
    return variables


def fake_visit() -> dict[str, Any]:
    """Return a fake visit payload."""
    return {
        "visitId": faker.random_int(min=1, max=10000),
        "studyKey": faker.bothify(text="??????"),
        "intervalId": faker.random_int(min=1, max=10000),
        "intervalName": faker.word().title(),
        "subjectId": faker.random_int(min=1, max=10000),
        "subjectKey": f"{faker.random_int(100, 999)}-{faker.random_int(100, 999)}",
        "startDate": _timestamp(),
        "endDate": _timestamp(),
        "dueDate": _timestamp(),
        "visitDate": _timestamp(),
        "visitDateForm": faker.word().title(),
        "visitDateQuestion": faker.word(),
        "deleted": False,
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
    }


def fake_coding() -> dict[str, Any]:
    """Return a fake coding payload."""
    return {
        "studyKey": faker.bothify(text="??????"),
        "siteName": faker.company(),
        "siteId": faker.random_int(min=1, max=10000),
        "subjectId": faker.random_int(min=1, max=10000),
        "subjectKey": f"{faker.random_int(100, 999)}-{faker.random_int(100, 999)}",
        "formId": faker.random_int(min=1, max=10000),
        "formName": faker.word().title(),
        "formKey": faker.lexify(text="????"),
        "revision": faker.random_int(min=1, max=5),
        "recordId": faker.random_int(min=1, max=10000),
        "variable": faker.lexify(text="????"),
        "value": faker.word(),
        "codingId": faker.random_int(min=1, max=10000),
        "code": faker.lexify(text="???"),
        "codedBy": faker.user_name(),
        "reason": faker.sentence(nb_words=3),
        "dictionaryName": faker.word().title(),
        "dictionaryVersion": str(faker.random_int(min=1, max=5)),
        "dateCoded": _timestamp(),
    }


def fake_record_revision() -> dict[str, Any]:
    """Return a fake record revision payload."""
    return {
        "studyKey": faker.bothify(text="??????"),
        "recordRevisionId": faker.random_int(min=1, max=10000),
        "recordId": faker.random_int(min=1, max=10000),
        "recordOid": faker.uuid4(),
        "recordRevision": faker.random_int(min=1, max=10),
        "dataRevision": faker.random_int(min=1, max=10),
        "recordStatus": faker.random_element(["Record Incomplete", "Record Complete"]),
        "subjectId": faker.random_int(min=1, max=10000),
        "subjectOid": faker.uuid4(),
        "subjectKey": f"{faker.random_int(100, 999)}-{faker.random_int(100, 999)}",
        "siteId": faker.random_int(min=1, max=10000),
        "formKey": faker.lexify(text="????"),
        "intervalId": faker.random_int(min=1, max=10000),
        "role": faker.word(),
        "user": faker.user_name(),
        "reasonForChange": faker.sentence(nb_words=3),
        "deleted": False,
        "dateCreated": _timestamp(),
    }


def fake_study() -> dict[str, Any]:
    """Return a fake study payload."""
    return {
        "sponsorKey": faker.lexify(text="????????"),
        "studyKey": faker.bothify(text="??????"),
        "studyId": faker.random_int(min=1, max=10000),
        "studyName": faker.word().title(),
        "studyDescription": faker.sentence(nb_words=3),
        "studyType": faker.random_element(["Clinical", "Observational", "Registry"]),
        "dateCreated": _timestamp(),
        "dateModified": _timestamp(),
    }


def fake_job() -> dict[str, Any]:
    """Return a fake job payload."""
    return {
        "jobId": faker.uuid4(),
        "batchId": faker.lexify(text="????????"),
        "state": faker.random_element(["OPEN", "RUNNING", "COMPLETE"]),
        "dateCreated": _timestamp(),
        "dateStarted": _timestamp(),
        "dateFinished": _timestamp(),
    }


def _timestamp_array() -> list[int]:
    """Return a 7-element numeric timestamp array [Y, M, D, h, m, s, ns]."""
    dt = faker.date_time()
    return [dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond * 1000]


def fake_user() -> dict[str, Any]:
    """Return a fake user payload."""
    return {
        "userId": faker.uuid4(),
        "login": faker.user_name(),
        "firstName": faker.first_name(),
        "lastName": faker.last_name(),
        "email": faker.email(),
        "userActiveInStudy": faker.boolean(),
        "roles": [
            {
                "dateCreated": _timestamp_array(),
                "dateModified": _timestamp_array(),
                "roleId": faker.lexify(text="????"),
                "communityId": faker.random_int(min=1, max=10000),
                "name": faker.word().title(),
                "description": faker.sentence(nb_words=3),
                "level": faker.random_int(min=1, max=5),
                "type": faker.word(),
                "inactive": faker.boolean(),
            }
        ],
    }


__all__ = [
    "fake_coding",
    "fake_form",
    "fake_forms_for_cache",
    "fake_interval",
    "fake_job",
    "fake_query",
    "fake_record",
    "fake_record_revision",
    "fake_site",
    "fake_study",
    "fake_subject",
    "fake_user",
    "fake_variable",
    "fake_variables_for_cache",
    "fake_visit",
]
