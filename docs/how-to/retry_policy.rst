Retry Policy and Error Handling
===============================

:class:`~imednet.core.RetryPolicy` decouples retry decisions from the underlying tenacity logic
configured via :class:`~imednet.core.RetryConfig`.
The :class:`~imednet.core.DefaultRetryPolicy` implementation retries idempotent network and 5xx errors and HTTP 429 rate limits.
You can implement your own strategy by inspecting :class:`imednet.core.retry.RetryState`.

Handling exceptions
-------------------

Use typed exceptions to respond to different failure modes:

.. testcode::

   from imednet import ImednetSDK
   from imednet.errors import NotFoundError, RateLimitError, ServerError

   sdk = ImednetSDK()
   try:
       for study in sdk.studies.list():
           print(study.study_key)
   except RateLimitError:
       print("Too many requests, try again later")
   except NotFoundError as exc:
       print(f"Missing resource: {exc}")
   except ServerError as exc:
       print(f"Server error: {exc}")

Custom strategies
-----------------

Retry policies decide if a request should be retried before the executor converts
non-2xx responses into SDK exceptions. Inspect ``state.result`` for HTTP 429 and
5xx responses (alongside network-level ``state.exception`` failures):

.. testcode::

   from collections.abc import Iterator
   import httpx
   from imednet import ImednetSDK
   from imednet.core.retry import RetryConfig, RetryPolicy, RetryState
   from imednet.errors import RateLimitError, ServerError

   class RateLimitServerRetry(RetryPolicy):
       def should_retry(self, state: RetryState) -> bool:
           if isinstance(state.exception, (RateLimitError, ServerError, httpx.RequestError)):
               return True
           if isinstance(state.result, httpx.Response):
               status = state.result.status_code
               return status == 429 or 500 <= status < 600
           return False

   attempts: list[int] = []
   canned_responses: Iterator[httpx.Response] = iter(
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
                   "data": [{"studyKey": "STUDY", "studyName": "Retry Demo Study"}],
               },
           ),
       ]
   )

   def responder(_request: httpx.Request) -> httpx.Response:
       response = next(canned_responses)
       attempts.append(response.status_code)
       return response

   retry_sdk = ImednetSDK(
       api_key="A",
       security_key="B",
       retry_config=RetryConfig(
           retries=5,
           backoff_factor=0.01,
           retry_policy=RateLimitServerRetry(),
       ),
       transport=httpx.MockTransport(responder),
   )
   recovered_studies = list(retry_sdk.studies.list())
   assert attempts == [429, 500, 200]
   assert [s.study_key for s in recovered_studies] == ["STUDY"]

Logging and exponential backoff
-------------------------------

Each attempt is logged with JSON formatting. Configure logging globally via
:func:`imednet.utils.configure_json_logging`. Retries use exponential backoff with delays controlled
by ``retries`` and ``backoff_factor``. With ``retries=5`` and ``backoff_factor=0.5``
the wait times are 0.5 s, 1 s, 2 s, 4 s, and 8 s.

Example script
--------------

The :doc:`/tutorials/examples/custom_retry` script demonstrates these concepts using a mock
transport:

.. code-block:: console

   export IMEDNET_API_KEY=dummy
   export IMEDNET_SECURITY_KEY=dummy
   hatch run python examples/custom_retry.py
