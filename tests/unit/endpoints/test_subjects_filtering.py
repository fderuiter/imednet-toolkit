"""Unit tests for subjects filtering."""

from unittest.mock import Mock

import pytest

from imednet.endpoints.subjects import AsyncSubjectsEndpoint, SubjectsEndpoint


def test_list_by_site_filtering():
    """Test that list by site filtering."""
    mock_client = Mock()

    endpoint = SubjectsEndpoint(mock_client)
    endpoint._list_sync = Mock(return_value=[])

    endpoint.list_by_site("sk", 101)
    endpoint._list_sync.assert_called_with(
        mock_client, endpoint.PAGINATOR_CLS, study_key="sk", site_id=101
    )

    endpoint.list_by_site("sk", "101")
    endpoint._list_sync.assert_called_with(
        mock_client, endpoint.PAGINATOR_CLS, study_key="sk", site_id="101"
    )


@pytest.mark.asyncio
async def test_async_list_by_site_filtering():
    """Test that async list by site filtering asynchronously."""
    mock_client = Mock()

    endpoint = AsyncSubjectsEndpoint(mock_client)

    async def fake_async_list(*args, **kwargs):
        """Helper function to fake async list."""
        return []

    endpoint._list_async = Mock(side_effect=fake_async_list)

    await endpoint.list_by_site("sk", 101)
    endpoint._list_async.assert_called_with(
        mock_client, endpoint.ASYNC_PAGINATOR_CLS, study_key="sk", site_id=101
    )

    await endpoint.list_by_site("sk", "101")
    endpoint._list_async.assert_called_with(
        mock_client, endpoint.ASYNC_PAGINATOR_CLS, study_key="sk", site_id="101"
    )
