Quick Start
===========

This page walks through a minimal example of using the SDK.

.. pyodide-button:: Activate Interactive Runtime

Installation
------------

.. code-block:: bash

   # PyPI release
   pip install imednet

.. include:: /_includes/install_extensions.rst

.. code-block:: bash

   # Workflow plugin package
   pip install imednet-workflows

   # Airflow provider package (core hook/operator/sensor support)
   pip install "apache-airflow>=3.2.0,<4.0.0" apache-airflow-providers-imednet


Basic Usage
-----------

Set your credentials by copying the environment template or exporting them directly:

.. code-block:: bash

   cp .env.example .env
   export IMEDNET_API_KEY="your_api_key"
   export IMEDNET_SECURITY_KEY="your_security_key"


Synchronous Example
-------------------

.. literalinclude:: /../examples/quick_start.py
   :language: python
   :start-after: # [start_quick_start]
   :end-before: # [end_quick_start]


Asynchronous Example
--------------------

.. literalinclude:: /../examples/async_quick_start.py
   :language: python
   :start-after: # [start_async_quick_start]
   :end-before: # [end_async_quick_start]


Custom retry logic can be provided via a ``RetryPolicy``:

.. testcode::

   from collections.abc import Iterator
   import httpx
   from imednet import ImednetSDK
   from imednet.core.retry import RetryConfig, RetryPolicy, RetryState
   from imednet.errors import RateLimitError, ServerError

   class ServerRetry(RetryPolicy):
       def should_retry(self, state: RetryState) -> bool:
           if isinstance(state.exception, (RateLimitError, ServerError, httpx.RequestError)):
               return True
           if isinstance(state.result, httpx.Response):
               status = state.result.status_code
               return status == 429 or 500 <= status < 600
           return False

   attempts: list[int] = []
   responses: Iterator[httpx.Response] = iter(
       [
           httpx.Response(429, json={"metadata": {"status": "TOO_MANY_REQUESTS"}}),
           httpx.Response(500, json={"metadata": {"status": "SERVER_ERROR"}}),
           httpx.Response(
               200,
               json={
                   "metadata": {"status": "OK"},
                   "pagination": {
                       "currentPage": 0,
                       "size": 25,
                       "totalPages": 1,
                       "totalElements": 1,
                   },
                   "data": [{"studyKey": "STUDY", "studyName": "Quick Start Study"}],
               },
           ),
       ]
   )

   def responder(_request: httpx.Request) -> httpx.Response:
       resp = next(responses)
       attempts.append(resp.status_code)
       return resp

   retry_sdk = ImednetSDK(
       api_key="mock",  # pragma: allowlist secret
       security_key="mock",  # pragma: allowlist secret
       retry_config=RetryConfig(retries=3, backoff_factor=0.01, retry_policy=ServerRetry()),
       transport=httpx.MockTransport(responder),
   )
   retried_studies = list(retry_sdk.studies.list())
   assert attempts == [429, 500, 200]
   assert len(retried_studies) == 1

   sdk = ImednetSDK(
       api_key="mock",  # pragma: allowlist secret
       security_key="mock",  # pragma: allowlist secret
       retry_config=RetryConfig(retries=3, backoff_factor=0.01, retry_policy=ServerRetry()),
   )

See :doc:`/how-to/retry_policy` for more guidance on error handling and exponential
backoff.
