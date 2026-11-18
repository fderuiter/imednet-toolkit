"""Unit tests for core endpoint operations."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet.constants import HEADER_EMAIL_NOTIFY
from imednet.core.endpoint.operations.filter_get import FilterGetOperation
from imednet.core.endpoint.operations.get import PathGetOperation
from imednet.core.endpoint.operations.list import ListOperation
from imednet.core.endpoint.operations.record_create import RecordCreateOperation
from imednet.errors import ClientError, NotFoundError


def dummy_parse_func(data):
    """Helper function to dummy parse func."""
    return data


def test_path_get_operation_execute_sync():
    """Test that path get operation execute sync."""
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = {"id": 1, "name": "Test"}
    client.get.return_value = response

    not_found_func = MagicMock()

    operation = PathGetOperation(
        path="/test", parse_func=dummy_parse_func, not_found_func=not_found_func
    )

    result = operation.execute_sync(client)

    assert result == {"id": 1, "name": "Test"}
    client.get.assert_called_once_with("/test")
    not_found_func.assert_not_called()


def test_path_get_operation_execute_sync_not_found():
    """Test that path get operation execute sync not found."""
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = None
    client.get.return_value = response

    not_found_func = MagicMock(side_effect=NotFoundError("Not found"))

    operation = PathGetOperation(
        path="/test", parse_func=dummy_parse_func, not_found_func=not_found_func
    )

    with pytest.raises(NotFoundError, match="Not found"):
        operation.execute_sync(client)

    client.get.assert_called_once_with("/test")
    not_found_func.assert_called_once()


@pytest.mark.asyncio
async def test_path_get_operation_execute_async():
    """Test that path get operation execute async asynchronously."""
    client = AsyncMock()
    response = MagicMock()
    response.json.return_value = {"id": 1, "name": "Test"}
    client.get.return_value = response

    not_found_func = MagicMock()

    operation = PathGetOperation(
        path="/test", parse_func=dummy_parse_func, not_found_func=not_found_func
    )

    result = await operation.execute_async(client)

    assert result == {"id": 1, "name": "Test"}
    client.get.assert_called_once_with("/test")
    not_found_func.assert_not_called()


@pytest.mark.asyncio
async def test_path_get_operation_execute_async_not_found():
    """Test that path get operation execute async not found asynchronously."""
    client = AsyncMock()
    response = MagicMock()
    response.json.return_value = None
    client.get.return_value = response

    not_found_func = MagicMock(side_effect=NotFoundError("Not found"))

    operation = PathGetOperation(
        path="/test", parse_func=dummy_parse_func, not_found_func=not_found_func
    )

    with pytest.raises(NotFoundError, match="Not found"):
        await operation.execute_async(client)

    client.get.assert_called_once_with("/test")
    not_found_func.assert_called_once()


def test_list_operation_sync():
    """Test that list operation sync."""
    client = MagicMock()
    paginator_cls = MagicMock()
    paginator_instance = [{"id": 1}, {"id": 2}]
    paginator_cls.return_value = paginator_instance

    operation = ListOperation(
        path="/test", params={"q": "1"}, page_size=10, parse_func=dummy_parse_func
    )
    result = operation.execute_sync(client, paginator_cls)

    assert list(result) == [{"id": 1}, {"id": 2}]
    paginator_cls.assert_called_once_with(client, "/test", params={"q": "1"}, page_size=10)


@pytest.mark.asyncio
async def test_list_operation_async():
    """Test that list operation async asynchronously."""
    client = AsyncMock()
    paginator_cls = MagicMock()

    class AsyncIteratorMock:
        """Test suite for AsyncIteratorMock."""

        def __init__(self, items):
            """Initialize the test object."""
            self.items = items

        async def __aiter__(self):
            """Helper function to   aiter  ."""
            for item in self.items:
                yield item

    paginator_cls.return_value = AsyncIteratorMock([{"id": 1}, {"id": 2}])

    operation = ListOperation(
        path="/test", params={"q": "1"}, page_size=10, parse_func=dummy_parse_func
    )
    result = [item async for item in operation.execute_async(client, paginator_cls)]

    assert result == [{"id": 1}, {"id": 2}]
    paginator_cls.assert_called_once_with(client, "/test", params={"q": "1"}, page_size=10)


def test_filter_get_operation_sync():
    """Test that filter get operation sync."""
    list_sync_func = MagicMock(return_value=[{"id": 1}])
    validate_func = MagicMock(return_value={"id": 1})
    client = MagicMock()
    paginator_cls = MagicMock()

    operation = FilterGetOperation(
        study_key="STUDY1",
        item_id=1,
        filters={"name": "test"},
        validate_func=validate_func,
        list_sync_func=list_sync_func,
    )

    result = operation.execute_sync(client, paginator_cls)

    assert result == {"id": 1}
    list_sync_func.assert_called_once_with(client, paginator_cls, study_key="STUDY1", name="test")
    validate_func.assert_called_once_with([{"id": 1}], "STUDY1", 1)


def test_filter_get_operation_sync_missing_list_func():
    """Test that filter get operation sync missing list func."""
    client = MagicMock()
    paginator_cls = MagicMock()

    operation = FilterGetOperation(
        study_key="STUDY1",
        item_id=1,
        filters={},
        validate_func=MagicMock(),
    )

    with pytest.raises(NotImplementedError, match="list_sync_func not provided"):
        operation.execute_sync(client, paginator_cls)


@pytest.mark.asyncio
async def test_filter_get_operation_async():
    """Test that filter get operation async asynchronously."""
    list_async_func = AsyncMock(return_value=[{"id": 1}])
    validate_func = MagicMock(return_value={"id": 1})
    client = AsyncMock()
    paginator_cls = MagicMock()

    operation = FilterGetOperation(
        study_key="STUDY1",
        item_id=1,
        filters={"name": "test"},
        validate_func=validate_func,
        list_async_func=list_async_func,
    )

    result = await operation.execute_async(client, paginator_cls)

    assert result == {"id": 1}
    list_async_func.assert_called_once_with(client, paginator_cls, study_key="STUDY1", name="test")
    validate_func.assert_called_once_with([{"id": 1}], "STUDY1", 1)


@pytest.mark.asyncio
async def test_filter_get_operation_async_missing_list_func():
    """Test that filter get operation async missing list func asynchronously."""
    client = AsyncMock()
    paginator_cls = MagicMock()

    operation = FilterGetOperation(
        study_key="STUDY1",
        item_id=1,
        filters={},
        validate_func=MagicMock(),
    )

    with pytest.raises(NotImplementedError, match="list_async_func not provided"):
        await operation.execute_async(client, paginator_cls)


def test_record_create_operation_sync():
    """Test that record create operation sync."""
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = {"status": "created"}
    client.post.return_value = response

    operation = RecordCreateOperation(
        path="/create",
        records_data=[{"formKey": "F1", "data": {"field1": "val1"}}],
        email_notify="notify@example.com",
    )

    result = operation.execute_sync(client, dummy_parse_func)

    assert result == {"status": "created"}
    client.post.assert_called_once_with(
        "/create",
        json=[{"formKey": "F1", "data": {"field1": "val1"}}],
        headers={HEADER_EMAIL_NOTIFY: "notify@example.com"},
    )


def test_record_create_operation_header_validation_failure():
    """Test that record create operation header validation failure."""
    with pytest.raises(ClientError, match="Header value must not contain newlines"):
        RecordCreateOperation(
            path="/create",
            records_data=[{"formKey": "F1", "data": {"field1": "val1"}}],
            email_notify="test@example.com\n",
        )
    with pytest.raises(ClientError, match="control characters"):
        RecordCreateOperation(
            path="/create",
            records_data=[{"formKey": "F1", "data": {"field1": "val1"}}],
            email_notify="test\x00@example.com",
        )


def test_record_create_operation_schema_validation_failure():
    """Test that record create operation schema validation failure."""
    schema_mock = MagicMock()

    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            "imednet.core.endpoint.operations.record_create.validate_record_entry",
            MagicMock(side_effect=ClientError("Invalid record data")),
        )

        with pytest.raises(ClientError, match="Invalid record data"):
            RecordCreateOperation(
                path="/create",
                records_data=[{"formKey": "F1", "data": {"invalid": "data"}}],
                schema=schema_mock,
            )


@pytest.mark.asyncio
async def test_record_create_operation_async():
    """Test that record create operation async asynchronously."""
    client = AsyncMock()
    response = MagicMock()
    response.json.return_value = {"status": "created"}
    client.post.return_value = response

    operation = RecordCreateOperation(
        path="/create",
        records_data=[{"formId": 12, "data": {"field1": "val1"}}],
        email_notify="user@example.com",
    )

    result = await operation.execute_async(client, dummy_parse_func)

    assert result == {"status": "created"}
    client.post.assert_called_once_with(
        "/create",
        json=[{"formId": 12, "data": {"field1": "val1"}}],
        headers={HEADER_EMAIL_NOTIFY: "user@example.com"},
    )


def test_record_create_operation_all_identifier_pairs_and_strict_rejections():
    """Verify BaseRecordRequest subclasses, identifier serialization, PHI masking, and email checks."""
    from imednet.errors import ValidationError
    from imednet.models.records import (
        CreateNewRecordRequest,
        RecordData,
        RegisterSubjectRequest,
        UpdateScheduledRecordRequest,
    )

    reg_by_id = RegisterSubjectRequest(formId=10, siteId=20, data=RecordData({"v": 1}))
    sched_by_oid = UpdateScheduledRecordRequest(
        formKey="VISIT_FORM",
        siteName="Site A",
        subjectId=30,
        subjectOid="SUBJ-OID-1",
        subjectKey="SUBJ-1",
        intervalId=40,
        intervalName="Baseline",
        recordId=50,
        recordOid="REC-OID-1",
        data=RecordData({"bp": 120}),
    )
    unsched = CreateNewRecordRequest(
        form_id=11,
        subject_oid="SUBJ-OID-2",
        record_id=55,
        data=RecordData({"note": "ok"}),
    )

    op = RecordCreateOperation(
        path="/create",
        records_data=[
            reg_by_id,  # type: ignore[list-item]
            sched_by_oid,  # type: ignore[list-item]
            unsched.model_dump(by_alias=True, exclude_none=True),
            {"form_key": "F2", "site_id": 9, "interval_id": 3, "data": {"x": "y"}},
        ],
    )
    assert op.records_data == [
        {"formId": 10, "siteId": 20, "data": {"v": 1}},
        {
            "formKey": "VISIT_FORM",
            "siteName": "Site A",
            "subjectKey": "SUBJ-1",
            "subjectId": 30,
            "subjectOid": "SUBJ-OID-1",
            "intervalName": "Baseline",
            "intervalId": 40,
            "recordId": 50,
            "recordOid": "REC-OID-1",
            "data": {"bp": 120},
        },
        {"formId": 11, "subjectOid": "SUBJ-OID-2", "recordId": 55, "data": {"note": "ok"}},
        {"formKey": "F2", "siteId": 9, "intervalId": 3, "data": {"x": "y"}},
    ]

    # Reject subjectKey on RegisterSubjectRequest
    with pytest.raises(Exception):
        RegisterSubjectRequest(formKey="F1", siteName="S1", subjectKey="SUBJ-1")  # type: ignore[call-arg]

    # Reject non-list records_data and non-dict items
    with pytest.raises(ValidationError, match="records_data must be a list"):
        RecordCreateOperation(path="/create", records_data={"formKey": "F1"})  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="expected dictionary"):
        RecordCreateOperation(path="/create", records_data=["bad"])  # type: ignore[list-item]

    # Reject missing formKey & formId, string/bool IDs, non-positive IDs, and recordData without leaking PHI
    for invalid_item in [
        {"data": {"patient_name": "Jane Doe"}},
        {"formKey": "", "data": {"patient_name": "Jane Doe"}},
        {"formId": 0, "data": {"patient_name": "Jane Doe"}},
        {"formId": True, "data": {"patient_name": "Jane Doe"}},
        {"formId": "10", "data": {"patient_name": "Jane Doe"}},
        {"formKey": 123, "data": {"patient_name": "Jane Doe"}},
        {"formKey": "F1", "recordData": {"patient_name": "Jane Doe", "ssn": "123-45-6789"}},
        {"formKey": "F1", "unknownKey": "Jane Doe", "data": {}},
        {"formKey": "F1", "data": "Jane Doe"},
    ]:
        with pytest.raises(ValidationError) as exc_info:
            RecordCreateOperation(path="/create", records_data=[invalid_item])  # type: ignore[list-item]
        assert "Jane Doe" not in str(exc_info.value)
        assert "123-45-6789" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None

    # Reject boolean, non-string, empty, and malformed email_notify
    for bad_notify in [
        True,
        False,
        123,
        "",
        "   ",
        "user..a@domain.com",
        "user@domain..com",
        "user@domain.1",
    ]:
        with pytest.raises(ValidationError):
            RecordCreateOperation(
                path="/create",
                records_data=[{"formKey": "F1", "data": {}}],
                email_notify=bad_notify,  # type: ignore[arg-type]
            )
