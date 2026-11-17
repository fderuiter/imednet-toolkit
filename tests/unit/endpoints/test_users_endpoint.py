"""Unit tests for users endpoint."""

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
