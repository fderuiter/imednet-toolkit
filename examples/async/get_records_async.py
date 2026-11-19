"""Example script demonstrating basic usage of the AsyncImednetSDK.

This script initializes the asynchronous client with API credentials loaded
from environment variables, retrieves a list of studies, selects the first study,
and then fetches and prints a few records associated with that study.

Ensure your environment variables (``IMEDNET_API_KEY``, ``IMEDNET_SECURITY_KEY``) are set
or enable ``IMEDNET_SIMULATION_MODE=true``.
"""

import asyncio

from imednet import AsyncImednetSDK, load_config
from imednet.utils import configure_json_logging


async def main() -> None:
    """Asynchronously fetch and print up to five records for the first study."""
    configure_json_logging()

    cfg = load_config()
    async with AsyncImednetSDK(
        api_key=cfg.api_key,
        security_key=cfg.security_key,
        base_url=cfg.base_url,
    ) as sdk:
        studies = [s async for s in sdk.studies.list()]
        if not studies:
            print("No studies returned from API.")
            return

        for study in studies[:1]:
            study_key = study.study_key
            records = [r async for r in sdk.records.list(study_key=study_key)]
            print(f"Records for study '{study_key}': {len(records)}")
            for record in records[:5]:
                print(f"- Record ID: {record.record_id}, Subject Key: {record.subject_key}")


if __name__ == "__main__":
    asyncio.run(main())
