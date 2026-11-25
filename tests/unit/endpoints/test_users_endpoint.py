"""Unit tests for users endpoint."""

from unittest.mock import MagicMock

import pytest

from imednet.endpoints import users
from imednet.errors.validation import ConfigurationError
from imednet.models.users import User


def test_users_endpoint_has_no_get_method(dummy_client):
    """Test that UsersEndpoint and AsyncUsersEndpoint expose no get attribute."""
    sync_ep = users.UsersEndpoint(dummy_client)
    assert not hasattr(sync_ep, "get")
    assert not hasattr(users.UsersEndpoint, "get")

    async_ep = users.AsyncUsersEndpoint(dummy_client)
    assert not hasattr(async_ep, "get")
    assert not hasattr(users.AsyncUsersEndpoint, "get")

    assert not hasattr(users.UsersOperationDef, "_id_param")


def test_list_requires_study_key_and_include_inactive(dummy_client, paginator_factory):
    """Test that list requires study key and include inactive defaults to false."""
    ep = users.UsersEndpoint(dummy_client)
    capture = paginator_factory(users, [{"userId": 1}])

    with pytest.raises(ConfigurationError):
        ep.list()

    result = ep.list(study_key="S1", include_inactive=True)

    assert capture["path"] == "/api/v1/edc/studies/S1/users"
    assert capture["params"] == {"includeInactive": "true"}
    assert "filter" not in capture["params"]
    assert isinstance(result[0], User)


def test_list_default_include_inactive_is_false(dummy_client, paginator_factory):
    """Test that includeInactive defaults to false when not specified."""
    ep = users.UsersEndpoint(dummy_client)
    capture = paginator_factory(users, [{"userId": 1}])

    result = ep.list(study_key="S1")

    assert capture["path"] == "/api/v1/edc/studies/S1/users"
    assert capture["params"] == {"includeInactive": "false"}
    assert "filter" not in capture["params"]
    assert isinstance(result[0], User)


def test_list_accepts_include_inactive_camel_case(dummy_client, paginator_factory):
    """Test that includeInactive can be passed in camelCase."""
    ep = users.UsersEndpoint(dummy_client)
    capture = paginator_factory(users, [{"userId": 1}])

    result = ep.list(study_key="S1", includeInactive=True)

    assert capture["params"] == {"includeInactive": "true"}
    assert "filter" not in capture["params"]
    assert isinstance(result[0], User)


def test_list_accepts_pagination_and_sort_parameters(dummy_client, paginator_factory):
    """Test that pagination parameters and sort are preserved and filter is not generated."""
    ep = users.UsersEndpoint(dummy_client)
    capture = paginator_factory(users, [{"userId": 1}])

    result = ep.list(study_key="S1", page=0, size=25, sort="lastName,asc")

    assert capture["path"] == "/api/v1/edc/studies/S1/users"
    assert capture["params"] == {
        "includeInactive": "false",
        "page": 0,
        "size": 25,
        "sort": "lastName,asc",
    }
    assert "filter" not in capture["params"]
    assert isinstance(result[0], User)


@pytest.mark.parametrize(
    "invalid_kwargs",
    [
        {"email": "user@example.com"},
        {"userId": 123},
        {"first_name": "Alice"},
        {"filter": "login eq 'admin'"},
        {"role": "Coordinator", "status": "active"},
    ],
)
def test_list_rejects_unsupported_filters_sync(dummy_client, invalid_kwargs):
    """Test that passing unsupported filter arguments raises ConfigurationError."""
    ep = users.UsersEndpoint(dummy_client)

    with pytest.raises(ConfigurationError, match=r"does not support query filtering"):
        ep.list(study_key="S1", **invalid_kwargs)


@pytest.mark.asyncio
async def test_async_list_requires_study_key(dummy_client):
    """Test that async list requires a study key."""
    ep = users.AsyncUsersEndpoint(dummy_client)

    with pytest.raises(ConfigurationError):
        ep.list()


@pytest.mark.asyncio
async def test_async_list_success(dummy_client, async_paginator_factory):
    """Test async list returns valid User model instances without filter param."""
    ep = users.AsyncUsersEndpoint(dummy_client)
    captured = async_paginator_factory(users, [{"userId": 1}])

    result = [item async for item in ep.list(study_key="S1", include_inactive=True)]

    assert captured["path"] == "/api/v1/edc/studies/S1/users"
    assert captured["params"] == {"includeInactive": "true"}
    assert "filter" not in captured["params"]
    assert isinstance(result[0], User)


@pytest.mark.asyncio
async def test_async_list_accepts_pagination_and_sort(dummy_client, async_paginator_factory):
    """Test async list preserves pagination and sort parameters without filter param."""
    ep = users.AsyncUsersEndpoint(dummy_client)
    captured = async_paginator_factory(users, [{"userId": 1}])

    result = [item async for item in ep.list(study_key="S1", page=1, size=50, sort="login,asc")]

    assert captured["path"] == "/api/v1/edc/studies/S1/users"
    assert captured["params"] == {
        "includeInactive": "false",
        "page": 1,
        "size": 50,
        "sort": "login,asc",
    }
    assert "filter" not in captured["params"]
    assert isinstance(result[0], User)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_kwargs",
    [
        {"email": "user@example.com"},
        {"userId": 123},
        {"filter": "login eq 'admin'"},
        {"role": "Coordinator"},
    ],
)
async def test_async_list_rejects_unsupported_filters(dummy_client, invalid_kwargs):
    """Test that passing unsupported filter arguments raises ConfigurationError immediately."""
    ep = users.AsyncUsersEndpoint(dummy_client)

    with pytest.raises(ConfigurationError, match=r"does not support query filtering"):
        ep.list(study_key="S1", **invalid_kwargs)


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried users to a pandas DataFrame."""
    ep = users.UsersEndpoint(dummy_client)
    paginator_factory(
        users,
        [
            {
                "userId": 1,
                "login": "jdoe",
                "firstName": "John",
                "lastName": "Doe",
                "email": "jdoe@example.com",
            }
        ],
    )

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "user_id"] == "1"
    assert df.loc[0, "login"] == "jdoe"
    assert df.loc[0, "first_name"] == "John"
    assert df.loc[0, "last_name"] == "Doe"
    assert df.loc[0, "name"] == "John Doe"


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = users.UsersEndpoint(dummy_client)
    captured = paginator_factory(users, [{"userId": 1}])

    df = ep.to_dataframe(
        study_key="S1",
        include_inactive=True,
        page=0,
        size=25,
        sort="lastName,asc",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/users"
    assert captured["params"] == {
        "includeInactive": "true",
        "page": 0,
        "size": 25,
        "sort": "lastName,asc",
    }
    assert "filter" not in captured["params"]
    assert len(df) == 1


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = users.UsersEndpoint(dummy_client)
    paginator_factory(users, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = users.UsersEndpoint(dummy_client)
    captured = paginator_factory(users, [{"userId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for users_to_dataframe"):
        ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, async_paginator_factory):
    """Test that async to_dataframe exports queried users to a pandas DataFrame."""
    ep = users.AsyncUsersEndpoint(dummy_client)
    async_paginator_factory(
        users,
        [
            {
                "userId": 1,
                "login": "jdoe",
                "firstName": "John",
                "lastName": "Doe",
                "email": "jdoe@example.com",
            }
        ],
    )

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "user_id"] == "1"
    assert df.loc[0, "login"] == "jdoe"
    assert df.loc[0, "first_name"] == "John"
    assert df.loc[0, "last_name"] == "Doe"
    assert df.loc[0, "name"] == "John Doe"


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(dummy_client, async_paginator_factory):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = users.AsyncUsersEndpoint(dummy_client)
    captured = async_paginator_factory(users, [{"userId": 1}])

    df = await ep.to_dataframe(
        study_key="S1",
        include_inactive=True,
        page=1,
        size=50,
        sort="login,asc",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/users"
    assert captured["params"] == {
        "includeInactive": "true",
        "page": 1,
        "size": 50,
        "sort": "login,asc",
    }
    assert "filter" not in captured["params"]
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = users.AsyncUsersEndpoint(dummy_client)
    async_paginator_factory(users, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = users.AsyncUsersEndpoint(dummy_client)
    captured = async_paginator_factory(users, [{"userId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for users_to_dataframe"):
        await ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


@pytest.mark.parametrize("bad_size", [0, -1, 501, 600, True, "abc"])
def test_users_endpoint_rejects_out_of_range_or_invalid_size(dummy_client, bad_size):
    """Test UsersEndpoint and AsyncUsersEndpoint raise ValueError on invalid size."""
    sync_ep = users.UsersEndpoint(dummy_client)
    with pytest.raises(ValueError, match="size"):
        sync_ep.list(study_key="S1", size=bad_size)

    async_ep = users.AsyncUsersEndpoint(dummy_client)
    with pytest.raises(ValueError, match="size"):
        async_ep.list(study_key="S1", size=bad_size)


@pytest.mark.asyncio
async def test_users_endpoint_limit_above_500_paginates_without_error():
    """Test UsersEndpoint and AsyncUsersEndpoint paginate limit=600 across 500-item pages."""
    pages = [
        {"data": [{"userId": i} for i in range(500)], "pagination": {"totalPages": 2}},
        {"data": [{"userId": i} for i in range(500, 650)], "pagination": {"totalPages": 2}},
    ]
    sync_calls: list[dict[str, object]] = []

    class SyncStub:
        def get(self, path, params=None):
            sync_calls.append({"path": path, "params": dict(params or {})})
            payload = pages[len(sync_calls) - 1]
            return type("Resp", (), {"json": lambda self: payload})()

    sync_ep = users.UsersEndpoint(SyncStub())  # type: ignore[arg-type]
    sync_items = list(sync_ep.list(study_key="S1", limit=600))
    assert len(sync_items) == 600
    assert sync_calls == [
        {
            "path": "/api/v1/edc/studies/S1/users",
            "params": {"includeInactive": "false", "page": 0, "size": 500},
        },
        {
            "path": "/api/v1/edc/studies/S1/users",
            "params": {"includeInactive": "false", "page": 1, "size": 500},
        },
    ]

    async_calls: list[dict[str, object]] = []

    class AsyncStub:
        async def get(self, path, params=None):
            async_calls.append({"path": path, "params": dict(params or {})})
            payload = pages[len(async_calls) - 1]
            return type("Resp", (), {"json": lambda self: payload})()

    async_ep = users.AsyncUsersEndpoint(AsyncStub())  # type: ignore[arg-type]
    async_items = [u async for u in async_ep.list(study_key="S1", limit=600)]
    assert len(async_items) == 600
    assert len(async_calls) == 2
