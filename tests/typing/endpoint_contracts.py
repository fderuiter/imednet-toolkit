from collections.abc import AsyncIterator, Iterator
from typing import assert_type

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.models.jobs import JobStatus
from imednet.models.records import Record


def sync_contract(sdk: ImednetSDK) -> None:
    assert_type(sdk.records.list('S'), Iterator[Record])
    assert_type(sdk.jobs.poll('S', 'B'), JobStatus)


async def async_contract(sdk: AsyncImednetSDK) -> None:
    assert_type(sdk.records.list('S'), AsyncIterator[Record])
    assert_type(await sdk.jobs.poll('S', 'B'), JobStatus)
