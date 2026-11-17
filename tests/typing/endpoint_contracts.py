from collections.abc import AsyncIterator, Iterator
from typing import assert_type

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.core.endpoint.dispatch import AsyncEndpointContext, SyncEndpointContext
from imednet.endpoints.studies import AsyncStudiesEndpoint, StudiesEndpoint, StudiesOperationDef
from imednet.models.jobs import JobStatus
from imednet.models.records import Record
from imednet.models.studies import Study


def sync_contract(sdk: ImednetSDK) -> None:
    assert_type(sdk.records.list('S'), Iterator[Record])
    assert_type(sdk.jobs.get('S', 'B'), JobStatus)
    assert_type(sdk.jobs.poll('S', 'B'), JobStatus)
    assert_type(sdk.studies.get('S'), Study)
    assert_type(sdk.studies.get('S').study_key, str | None)
    assert_type(sdk.studies.get(study_key='S'), Study)
    assert_type(sdk.studies.get(item_id='S'), Study)
    assert_type(sdk.studies.get(None, 'S'), Study)
    assert_type(sdk.studies.get('S', 'S'), Study)
    assert_type(sdk.studies.get(study_key=None, item_id='S'), Study)
    assert_type(sdk.studies.get(study_key='S', item_id='S'), Study)
    assert_type(sdk.studies.get('S', item_id='S'), Study)


async def async_contract(sdk: AsyncImednetSDK) -> None:
    assert_type(sdk.records.list('S'), AsyncIterator[Record])
    assert_type(await sdk.jobs.get('S', 'B'), JobStatus)
    assert_type(await sdk.jobs.poll('S', 'B'), JobStatus)
    assert_type(await sdk.studies.get('S'), Study)
    assert_type((await sdk.studies.get('S')).study_key, str | None)
    assert_type(await sdk.studies.get(study_key='S'), Study)
    assert_type(await sdk.studies.get(item_id='S'), Study)
    assert_type(await sdk.studies.get(None, 'S'), Study)
    assert_type(await sdk.studies.get('S', 'S'), Study)
    assert_type(await sdk.studies.get(study_key=None, item_id='S'), Study)
    assert_type(await sdk.studies.get(study_key='S', item_id='S'), Study)
    assert_type(await sdk.studies.get('S', item_id='S'), Study)


def sync_endpoint_contract(ep: StudiesEndpoint) -> None:
    assert_type(ep.get('S'), Study)
    assert_type(ep.get('S').study_key, str | None)
    assert_type(ep.get(study_key='S'), Study)
    assert_type(ep.get(item_id='S'), Study)
    assert_type(ep.get(None, 'S'), Study)
    assert_type(ep.get(study_key=None, item_id='S'), Study)


async def async_endpoint_contract(ep: AsyncStudiesEndpoint) -> None:
    assert_type(await ep.get('S'), Study)
    assert_type((await ep.get('S')).study_key, str | None)
    assert_type(await ep.get(study_key='S'), Study)
    assert_type(await ep.get(item_id='S'), Study)
    assert_type(await ep.get(None, 'S'), Study)
    assert_type(await ep.get(study_key=None, item_id='S'), Study)


class _SyncStub(StudiesOperationDef, SyncEndpointContext):
    pass


class _AsyncStub(StudiesOperationDef, AsyncEndpointContext):
    pass


def sync_stub_contract(ep: _SyncStub) -> None:
    assert_type(ep.get('S'), Study)
    assert_type(ep.get('S').study_key, str | None)
    assert_type(ep.get(item_id='S'), Study)
    assert_type(ep.get(None, 'S'), Study)


async def async_stub_contract(ep: _AsyncStub) -> None:
    assert_type(await ep.get('S'), Study)
    assert_type((await ep.get('S')).study_key, str | None)
    assert_type(await ep.get(item_id='S'), Study)
    assert_type(await ep.get(None, 'S'), Study)
