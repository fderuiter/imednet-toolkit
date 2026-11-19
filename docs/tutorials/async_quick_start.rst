Async Quick Start
=================

This page shows a minimal asynchronous example using ``AsyncImednetSDK``.

.. pyodide-button:: Activate Interactive Runtime

.. important::

   ``AsyncImednetSDK`` and ``ImednetSDK`` have **strictly separate lifecycles**.
   Bridging synchronous code with async teardowns is **not supported**.

   * Always manage ``AsyncImednetSDK`` with ``async with`` or by explicitly
     calling ``await sdk.aclose()`` within an active event loop.
   * Calling the synchronous ``close()`` method on
     :class:`~imednet.sdk.AsyncImednetSDK` raises a :exc:`TypeError`.
   * Using ``with AsyncImednetSDK(...)`` (the synchronous context manager)
     raises a :exc:`TypeError`.

Install the package from PyPI:

.. code-block:: console

   pip install imednet

Set your credentials by copying the environment template or exporting them directly:

.. code-block:: bash

   # Option 1: Use a .env file (recommended)
   cp .env.example .env

   # Option 2: Export directly to your shell
   export IMEDNET_API_KEY="your_api_key"
   export IMEDNET_SECURITY_KEY="your_security_key"
   # Optional: Custom base URL for the API endpoint
   # export IMEDNET_BASE_URL="https://edc.prod.imednetapi.com"

List studies asynchronously and poll a job:

.. note::
   Ensure you've run ``cp .env.example .env`` or exported keys to your shell before running this script.

.. testcode::

   import asyncio
   from imednet import AsyncImednetSDK, load_config
   from imednet.utils import configure_json_logging

   async def main() -> None:
       configure_json_logging()
       cfg = load_config()
       async with AsyncImednetSDK(
           api_key=cfg.api_key,
           security_key=cfg.security_key,
           base_url=cfg.base_url,
       ) as sdk:
           studies = [s async for s in sdk.studies.list()]
           for study in studies:
               print(f"{study.study_name} ({study.study_key})")
           status = await sdk.async_poll_job("STUDY", "BATCH", interval=2, timeout=60)
           print(status)


   asyncio.run(main())

For synchronous usage, see :doc:`quick_start`.

The example script :doc:`examples/async_quick_start` provides a runnable version that
validates required environment variables and optionally polls a job when
``IMEDNET_JOB_STUDY_KEY`` and ``IMEDNET_BATCH_ID`` are set.
